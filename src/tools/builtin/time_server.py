"""
Time tool - Fuzzy time expression parsing.

Registers a parse_time tool to the MCP server for converting
natural language time expressions to standard datetime formats.

Supported expressions:
- Relative: yesterday, 3 days ago, next week
- Chinese relative: 昨天, 三天前, 下周
- Ranges: 昨天到今天, 上午9点到10点
- Holidays: 国庆节期间, 春节
- Specific: 2024年1月1日, 三月十五日
"""
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from src.common.logger import get_logger

from .registry import builtin_tool
from ..mcp.server import MCPServer
from ..mcp.tool import Tool
from ..mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType

logger = get_logger()


# --- Parser ---

class FuzzyTimeParser:
    """Parser for fuzzy time expressions in Chinese."""

    CN_NUMBERS = {
        '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
        '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
        '十一': 11, '十二': 12,
    }

    WEEKDAYS = {
        '一': 0, '二': 1, '三': 2, '四': 3,
        '五': 4, '六': 5, '日': 6, '天': 6,
    }

    SOLAR_HOLIDAYS: dict[str, tuple[int, int, int]] = {
        '元旦': (1, 1, 1), '劳动节': (5, 1, 5), '五一': (5, 1, 5),
        '国庆': (10, 1, 7), '国庆节': (10, 1, 7),
        '圣诞': (12, 25, 1), '圣诞节': (12, 25, 1),
    }

    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.tz = ZoneInfo(timezone)
        self._now: Optional[datetime] = None

    @property
    def now(self) -> datetime:
        if self._now is None:
            self._now = datetime.now(self.tz)
        return self._now

    def reset_now(self) -> None:
        self._now = None

    def parse(self, expression: str) -> dict:
        """Parse fuzzy time expression, return structured dict."""
        self.reset_now()
        expr = expression.strip()

        parsers = [
            self._parse_range,
            self._parse_holiday,
            self._parse_recent_period,
            self._parse_relative_day,
            self._parse_relative_week,
            self._parse_relative_month,
            self._parse_time_of_day,
            self._parse_specific_date,
            self._parse_weekday,
        ]

        for parser in parsers:
            result = parser(expr)
            if result:
                return result

        # Fallback
        return {
            "value": self.now.strftime("%Y-%m-%d"),
            "is_range": False,
            "is_date_only": True,
            "original_expression": expression,
            "confidence": 0.3,
        }

    def _cn_to_num(self, cn: str) -> int:
        if cn.isdigit():
            return int(cn)
        if cn in self.CN_NUMBERS:
            return self.CN_NUMBERS[cn]
        if cn.startswith('十') and len(cn) == 2:
            return 10 + self.CN_NUMBERS.get(cn[1], 0)
        if len(cn) == 2 and cn.endswith('十'):
            return self.CN_NUMBERS.get(cn[0], 0) * 10
        if '十' in cn and len(cn) == 3:
            parts = cn.split('十')
            return self.CN_NUMBERS.get(parts[0], 0) * 10 + self.CN_NUMBERS.get(parts[1], 0)
        return 1

    def _fmt(self, dt: datetime, date_only: bool = False) -> str:
        return dt.strftime("%Y-%m-%d") if date_only else dt.strftime("%Y-%m-%d %H:%M:%S")

    def _parse_range(self, expr: str) -> Optional[dict]:
        for pattern in (r'(.+?)到(.+)', r'(.+?)至(.+)', r'从(.+?)到(.+)'):
            match = re.match(pattern, expr)
            if match:
                start_r = self._parse_single(match.group(1).strip())
                end_r = self._parse_single(match.group(2).strip())
                if start_r and end_r:
                    return {
                        "value": [start_r[0], end_r[0]],
                        "is_range": True,
                        "is_date_only": start_r[1] and end_r[1],
                        "original_expression": expr,
                        "confidence": min(start_r[2], end_r[2]),
                    }
        return None

    def _parse_single(self, expr: str) -> Optional[tuple[str, bool, float]]:
        parsers = [
            self._parse_holiday, self._parse_recent_period,
            self._parse_relative_day, self._parse_relative_week,
            self._parse_relative_month, self._parse_time_of_day,
            self._parse_specific_date, self._parse_weekday,
        ]
        for parser in parsers:
            result = parser(expr)
            if result:
                val = result["value"]
                if isinstance(val, list):
                    val = val[0]
                return (val, result["is_date_only"], result["confidence"])
        return None

    def _parse_holiday(self, expr: str) -> Optional[dict]:
        year = self.now.year
        for name, (m, d, dur) in self.SOLAR_HOLIDAYS.items():
            if name in expr:
                hd = datetime(year, m, d, tzinfo=self.tz)
                if dur > 1 or '期间' in expr:
                    end = hd + timedelta(days=dur - 1)
                    return {
                        "value": [self._fmt(hd, True), self._fmt(end, True)],
                        "is_range": True, "is_date_only": True,
                        "original_expression": expr, "confidence": 0.95,
                    }
                return {
                    "value": self._fmt(hd, True), "is_range": False,
                    "is_date_only": True, "original_expression": expr,
                    "confidence": 0.95,
                }
        return None

    def _parse_recent_period(self, expr: str) -> Optional[dict]:
        patterns = [
            (r'最?近(\d+|[一二两三四五六七八九十]+)天', 'day'),
            (r'最?近(\d+|[一二两三四五六七八九十]+)个?(?:周|星期)', 'week'),
            (r'最?近(\d+|[一二两三四五六七八九十]+)个?月', 'month'),
        ]
        for pattern, unit in patterns:
            match = re.match(pattern, expr)
            if match:
                num = self._cn_to_num(match.group(1))
                today = self.now.date()
                if unit == 'day':
                    start = today - timedelta(days=num)
                elif unit == 'week':
                    start = today - timedelta(weeks=num)
                else:
                    y, m = today.year, today.month - num
                    while m < 1:
                        m += 12
                        y -= 1
                    _, last = monthrange(y, m)
                    start = date(y, m, min(today.day, last))
                return {
                    "value": [start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")],
                    "is_range": True, "is_date_only": True,
                    "original_expression": expr, "confidence": 0.95,
                }
        return None

    def _parse_relative_day(self, expr: str) -> Optional[dict]:
        day_map = {
            '今天': 0, '今日': 0, '昨天': -1, '昨日': -1,
            '前天': -2, '大前天': -3, '明天': 1, '明日': 1,
            '后天': 2, '大后天': 3,
        }
        for key, offset in day_map.items():
            if expr == key:
                target = self.now + timedelta(days=offset)
                return {
                    "value": self._fmt(target, True), "is_range": False,
                    "is_date_only": True, "original_expression": expr,
                    "confidence": 1.0,
                }

        for pattern, direction in [
            (r'(\d+|[一二三四五六七八九十]+)天前', -1),
            (r'(\d+|[一二三四五六七八九十]+)天后', 1),
        ]:
            match = re.match(pattern, expr)
            if match:
                num = self._cn_to_num(match.group(1))
                target = self.now + timedelta(days=num * direction)
                return {
                    "value": self._fmt(target, True), "is_range": False,
                    "is_date_only": True, "original_expression": expr,
                    "confidence": 0.95,
                }
        return None

    def _parse_relative_week(self, expr: str) -> Optional[dict]:
        week_map = {
            '本周': 0, '这周': 0, '上周': -1, '下周': 1,
        }
        for key, offset in week_map.items():
            if expr == key or expr.startswith(key):
                today = self.now.date()
                start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
                end = start + timedelta(days=6)
                return {
                    "value": [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")],
                    "is_range": True, "is_date_only": True,
                    "original_expression": expr, "confidence": 0.95,
                }
        return None

    def _parse_relative_month(self, expr: str) -> Optional[dict]:
        month_map = {'本月': 0, '这个月': 0, '上个月': -1, '上月': -1, '下个月': 1, '下月': 1}
        for key, offset in month_map.items():
            if expr == key:
                y, m = self.now.year, self.now.month + offset
                while m < 1:
                    m += 12
                    y -= 1
                while m > 12:
                    m -= 12
                    y += 1
                _, last = monthrange(y, m)
                return {
                    "value": [f"{y}-{m:02d}-01", f"{y}-{m:02d}-{last:02d}"],
                    "is_range": True, "is_date_only": True,
                    "original_expression": expr, "confidence": 0.95,
                }
        return None

    def _parse_time_of_day(self, expr: str) -> Optional[dict]:
        pattern = r'(凌晨|早上|上午|中午|下午|晚上)?(\d+|[一二三四五六七八九十]+)点(?:(\d+|[一二三四五六七八九十]+)分?)?'
        match = re.match(pattern, expr)
        if match:
            period = match.group(1)
            hour = self._cn_to_num(match.group(2))
            minute = self._cn_to_num(match.group(3)) if match.group(3) else 0
            if period in ('下午', '晚上') and hour < 12:
                hour += 12
            elif period == '凌晨' and hour == 12:
                hour = 0
            today = self.now.date()
            target = datetime(today.year, today.month, today.day, hour, minute, tzinfo=self.tz)
            return {
                "value": self._fmt(target, False), "is_range": False,
                "is_date_only": False, "original_expression": expr,
                "confidence": 0.9,
            }
        return None

    def _parse_specific_date(self, expr: str) -> Optional[dict]:
        cn = self._cn_to_num
        num = r'(\d{1,2}|[一二三四五六七八九十]+)'
        patterns = [
            (r'(\d{4})年' + num + r'月' + num + r'[日号]?',
                lambda m: (int(m.group(1)), cn(m.group(2)), cn(m.group(3)))),
            (num + r'月' + num + r'[日号]?',
                lambda m: (self.now.year, cn(m.group(1)), cn(m.group(2)))),
            (num + r'[日号]',
                lambda m: (self.now.year, self.now.month, cn(m.group(1)))),
        ]
        for pattern, extractor in patterns:
            match = re.match(pattern, expr)
            if match:
                try:
                    y, m, d = extractor(match)
                    target = datetime(y, m, d, tzinfo=self.tz)
                    return {
                        "value": self._fmt(target, True), "is_range": False,
                        "is_date_only": True, "original_expression": expr,
                        "confidence": 1.0,
                    }
                except ValueError:
                    continue
        return None

    def _parse_weekday(self, expr: str) -> Optional[dict]:
        pattern = r'(上上?|下下?|这)?(?:周|星期)([一二三四五六日天])'
        match = re.match(pattern, expr)
        if match:
            prefix = match.group(1) or '这'
            weekday = self.WEEKDAYS.get(match.group(2), 0)
            today = self.now.date()
            week_offset = {'上上': -2, '上': -1, '这': 0, '下': 1, '下下': 2}.get(prefix, 0)
            days_diff = weekday - today.weekday() + (week_offset * 7)
            target = today + timedelta(days=days_diff)
            return {
                "value": target.strftime("%Y-%m-%d"), "is_range": False,
                "is_date_only": True, "original_expression": expr,
                "confidence": 0.95,
            }
        return None


# --- Tool Handler ---

async def parse_time(expression: str, timezone: str = "Asia/Shanghai") -> dict:
    """Parse fuzzy time expression to standard datetime format."""
    logger.info(f"parse_time: expression='{expression}', timezone='{timezone}'")
    try:
        parser = FuzzyTimeParser(timezone=timezone)
        result = parser.parse(expression)
        return {"success": True, "parsed": result}
    except Exception as e:
        logger.error(f"Failed to parse time '{expression}': {e}")
        return {"success": False, "error": str(e)}


# --- Registration ---

def create_parse_time_tool() -> Tool:
    return Tool(
        name="parse_time",
        description=(
            "Parse fuzzy time expressions to standard datetime format. "
            "Supports relative time, ranges, holidays, specific dates in Chinese."
        ),
        handler=parse_time,
        parameters={
            "expression": {
                "type": "string",
                "description": "Fuzzy time expression (e.g., 昨天, 三周前, 国庆节期间)",
            },
            "timezone": {
                "type": "string",
                "description": "Timezone (default: Asia/Shanghai)",
            },
        },
        required_params=("expression",),
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        concurrency=ConcurrencyLevel.SAFE,
        tags=frozenset({"time", "datetime", "parsing"}),
    )


@builtin_tool(multi=True)
def create_time_tools() -> list[Tool]:
    return [create_parse_time_tool()]

def register_time_tools(mcp_server: Optional[MCPServer] = None) -> int:
    """Register time parsing tools. Returns number registered."""
    if mcp_server is None:
        from ..mcp.server import get_mcp_server
        mcp_server = get_mcp_server()
    if mcp_server is None:
        logger.warning("MCP server not available, skipping time tool registration")
        return 0

    tools = create_time_tools()
    for tool in tools:
        mcp_server.register_tool(tool)
        logger.info("Registered MCP tool: %s", tool.name)
    return len(tools)
