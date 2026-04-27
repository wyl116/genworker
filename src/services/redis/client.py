"""
Redis基础工具类
提供Redis连接管理和基础操作封装
"""
import asyncio
import json
from typing import Any, Dict, List, Optional

from src.common.logger import get_logger
from src.services.client_registry import (
    close_keyed_clients,
    get_or_create_keyed_client,
    init_keyed_client,
)
from .config import RedisConfig

logger = get_logger()


class RedisClient:
    """
    Redis客户端封装
    提供异步操作和连接池管理
    """

    def __init__(self, config: Optional[RedisConfig] = None):
        """
        初始化Redis客户端

        Args:
            config: Redis配置，如果不提供则从全局设置加载
        """
        self._config = config
        self._pool = None
        self._client = None
        self._initialized = False

    async def _ensure_initialized(self):
        """确保客户端已初始化"""
        if self._initialized:
            return

        try:
            import redis.asyncio as redis

            if self._config is None:
                from src.common.settings import get_settings
                self._config = RedisConfig.from_settings(get_settings())

            # 创建连接池
            self._pool = redis.ConnectionPool(
                host=self._config.host,
                port=self._config.port,
                password=self._config.password,
                db=self._config.database,
                max_connections=self._config.pool_max_connections,
                socket_timeout=self._config.timeout / 1000,  # 转换为秒
                socket_connect_timeout=self._config.connect_timeout / 1000,
                decode_responses=True,  # 自动解码为字符串
            )

            self._client = redis.Redis(connection_pool=self._pool)
            self._initialized = True

            logger.info(
                f"Redis客户端初始化成功 | "
                f"host={self._config.host}:{self._config.port} | "
                f"db={self._config.database}"
            )

        except ImportError:
            logger.error("Redis库未安装，请执行: pip install redis")
            raise
        except Exception as e:
            logger.error(f"Redis客户端初始化失败: {e}")
            raise

    # ==================== String 操作 ====================

    async def get(self, key: str) -> Optional[str]:
        """获取字符串值"""
        await self._ensure_initialized()
        try:
            return await self._client.get(key)
        except Exception as e:
            logger.error(f"Redis GET失败 | key={key} | error={e}")
            return None

    async def set(
        self,
        key: str,
        value: str,
        ttl: Optional[int] = None,
        nx: bool = False,
        xx: bool = False
    ) -> bool:
        """
        设置字符串值

        Args:
            key: 键
            value: 值
            ttl: 过期时间(秒)
            nx: 只在key不存在时设置
            xx: 只在key存在时设置
        """
        await self._ensure_initialized()
        try:
            return await self._client.set(
                key,
                value,
                ex=ttl,
                nx=nx,
                xx=xx
            )
        except Exception as e:
            logger.error(f"Redis SET失败 | key={key} | error={e}")
            return False

    async def delete(self, *keys: str) -> int:
        """删除Key"""
        await self._ensure_initialized()
        try:
            return await self._client.delete(*keys)
        except Exception as e:
            logger.error(f"Redis DELETE失败 | keys={keys} | error={e}")
            return 0

    async def exists(self, *keys: str) -> int:
        """检查Key是否存在"""
        await self._ensure_initialized()
        try:
            return await self._client.exists(*keys)
        except Exception as e:
            logger.error(f"Redis EXISTS失败 | keys={keys} | error={e}")
            return 0

    async def expire(self, key: str, ttl: int) -> bool:
        """设置Key过期时间"""
        await self._ensure_initialized()
        try:
            return await self._client.expire(key, ttl)
        except Exception as e:
            logger.error(f"Redis EXPIRE失败 | key={key} | error={e}")
            return False

    async def ttl(self, key: str) -> int:
        """获取Key剩余过期时间"""
        await self._ensure_initialized()
        try:
            return await self._client.ttl(key)
        except Exception as e:
            logger.error(f"Redis TTL失败 | key={key} | error={e}")
            return -2

    # ==================== Hash 操作 ====================

    async def hget(self, key: str, field: str) -> Optional[str]:
        """获取Hash字段值"""
        await self._ensure_initialized()
        try:
            return await self._client.hget(key, field)
        except Exception as e:
            logger.error(f"Redis HGET失败 | key={key} | field={field} | error={e}")
            return None

    async def hset(
        self,
        key: str,
        field: Optional[str] = None,
        value: Optional[str] = None,
        mapping: Optional[Dict[str, str]] = None
    ) -> int:
        """
        设置Hash字段值

        Args:
            key: Hash键
            field: 字段名
            value: 字段值
            mapping: 批量设置的字段映射
        """
        await self._ensure_initialized()
        try:
            if mapping:
                return await self._client.hset(key, mapping=mapping)
            elif field and value is not None:
                return await self._client.hset(key, field, value)
            return 0
        except Exception as e:
            logger.error(f"Redis HSET失败 | key={key} | error={e}")
            return 0

    async def hgetall(self, key: str) -> Dict[str, str]:
        """获取Hash所有字段"""
        await self._ensure_initialized()
        try:
            return await self._client.hgetall(key)
        except Exception as e:
            logger.error(f"Redis HGETALL失败 | key={key} | error={e}")
            return {}

    async def hdel(self, key: str, *fields: str) -> int:
        """删除Hash字段"""
        await self._ensure_initialized()
        try:
            return await self._client.hdel(key, *fields)
        except Exception as e:
            logger.error(f"Redis HDEL失败 | key={key} | error={e}")
            return 0

    async def hexists(self, key: str, field: str) -> bool:
        """检查Hash字段是否存在"""
        await self._ensure_initialized()
        try:
            return await self._client.hexists(key, field)
        except Exception as e:
            logger.error(f"Redis HEXISTS失败 | key={key} | error={e}")
            return False

    # ==================== List 操作 ====================

    async def lpush(self, key: str, *values: str) -> int:
        """左侧插入列表"""
        await self._ensure_initialized()
        try:
            return await self._client.lpush(key, *values)
        except Exception as e:
            logger.error(f"Redis LPUSH失败 | key={key} | error={e}")
            return 0

    async def rpush(self, key: str, *values: str) -> int:
        """右侧插入列表"""
        await self._ensure_initialized()
        try:
            return await self._client.rpush(key, *values)
        except Exception as e:
            logger.error(f"Redis RPUSH失败 | key={key} | error={e}")
            return 0

    async def lrange(self, key: str, start: int, end: int) -> List[str]:
        """获取列表范围"""
        await self._ensure_initialized()
        try:
            return await self._client.lrange(key, start, end)
        except Exception as e:
            logger.error(f"Redis LRANGE失败 | key={key} | error={e}")
            return []

    async def llen(self, key: str) -> int:
        """获取列表长度"""
        await self._ensure_initialized()
        try:
            return await self._client.llen(key)
        except Exception as e:
            logger.error(f"Redis LLEN失败 | key={key} | error={e}")
            return 0

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        """裁剪列表"""
        await self._ensure_initialized()
        try:
            return await self._client.ltrim(key, start, end)
        except Exception as e:
            logger.error(f"Redis LTRIM失败 | key={key} | error={e}")
            return False

    async def lindex(self, key: str, index: int) -> Optional[str]:
        """获取列表指定位置元素"""
        await self._ensure_initialized()
        try:
            return await self._client.lindex(key, index)
        except Exception as e:
            logger.error(f"Redis LINDEX失败 | key={key} | error={e}")
            return None

    # ==================== Set 操作 ====================

    async def sadd(self, key: str, *values: str) -> int:
        """添加集合成员"""
        await self._ensure_initialized()
        try:
            return await self._client.sadd(key, *values)
        except Exception as e:
            logger.error(f"Redis SADD失败 | key={key} | error={e}")
            return 0

    async def smembers(self, key: str) -> set:
        """获取集合所有成员"""
        await self._ensure_initialized()
        try:
            return await self._client.smembers(key)
        except Exception as e:
            logger.error(f"Redis SMEMBERS失败 | key={key} | error={e}")
            return set()

    async def srem(self, key: str, *values: str) -> int:
        """移除集合成员"""
        await self._ensure_initialized()
        try:
            return await self._client.srem(key, *values)
        except Exception as e:
            logger.error(f"Redis SREM失败 | key={key} | error={e}")
            return 0

    async def sismember(self, key: str, value: str) -> bool:
        """检查是否为集合成员"""
        await self._ensure_initialized()
        try:
            return await self._client.sismember(key, value)
        except Exception as e:
            logger.error(f"Redis SISMEMBER失败 | key={key} | error={e}")
            return False

    # ==================== JSON 便捷操作 ====================

    async def get_json(self, key: str) -> Optional[Any]:
        """获取JSON值"""
        value = await self.get(key)
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析失败 | key={key} | error={e}")
        return None

    async def set_json(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None
    ) -> bool:
        """设置JSON值"""
        try:
            json_str = json.dumps(value, ensure_ascii=False)
            return await self.set(key, json_str, ttl=ttl)
        except Exception as e:
            logger.error(f"JSON序列化失败 | key={key} | error={e}")
            return False

    async def lpush_json(self, key: str, value: Any) -> int:
        """左侧插入JSON"""
        try:
            json_str = json.dumps(value, ensure_ascii=False)
            return await self.lpush(key, json_str)
        except Exception as e:
            logger.error(f"JSON序列化失败 | key={key} | error={e}")
            return 0

    async def rpush_json(self, key: str, value: Any) -> int:
        """右侧插入JSON"""
        try:
            json_str = json.dumps(value, ensure_ascii=False)
            return await self.rpush(key, json_str)
        except Exception as e:
            logger.error(f"JSON序列化失败 | key={key} | error={e}")
            return 0

    async def lrange_json(self, key: str, start: int, end: int) -> List[Any]:
        """获取列表范围并解析JSON"""
        items = await self.lrange(key, start, end)
        result = []
        for item in items:
            try:
                result.append(json.loads(item))
            except json.JSONDecodeError:
                result.append(item)
        return result

    # ==================== 批量操作 ====================

    async def mget(self, *keys: str) -> List[Optional[str]]:
        """批量获取"""
        await self._ensure_initialized()
        try:
            return await self._client.mget(keys)
        except Exception as e:
            logger.error(f"Redis MGET失败 | keys={keys} | error={e}")
            return [None] * len(keys)

    async def mset(self, mapping: Dict[str, str]) -> bool:
        """批量设置"""
        await self._ensure_initialized()
        try:
            return await self._client.mset(mapping)
        except Exception as e:
            logger.error(f"Redis MSET失败 | error={e}")
            return False

    # ==================== 管理操作 ====================

    async def keys(self, pattern: str = "*") -> List[str]:
        """获取匹配的Key列表"""
        await self._ensure_initialized()
        try:
            return await self._client.keys(pattern)
        except Exception as e:
            logger.error(f"Redis KEYS失败 | pattern={pattern} | error={e}")
            return []

    async def ping(self) -> bool:
        """测试连接"""
        await self._ensure_initialized()
        try:
            return await self._client.ping()
        except Exception as e:
            logger.error(f"Redis PING失败 | error={e}")
            return False

    async def close(self):
        """关闭连接"""
        if self._pool:
            await self._pool.disconnect()
            self._initialized = False
            logger.info("Redis连接已关闭")

    async def flush_db(self):
        """清空当前数据库（慎用）"""
        await self._ensure_initialized()
        try:
            await self._client.flushdb()
            logger.warning("Redis数据库已清空")
        except Exception as e:
            logger.error(f"Redis FLUSHDB失败 | error={e}")


# 全局Redis客户端实例
_redis_clients: dict[str, RedisClient] = {}
_client_lock = asyncio.Lock()


def _build_redis_client(config: Optional[RedisConfig] = None) -> RedisClient:
    return RedisClient(config)


def get_redis_client() -> RedisClient:
    """获取Redis客户端单例"""
    return get_or_create_keyed_client(
        _redis_clients,
        key="default",
        factory=_build_redis_client,
    )


async def init_redis_client(config: Optional[RedisConfig] = None) -> RedisClient:
    """初始化Redis客户端"""
    return await init_keyed_client(
        _redis_clients,
        key="default",
        lock=_client_lock,
        factory=lambda: _build_redis_client(config),
        initializer=lambda client: client._ensure_initialized(),
    )


async def close_redis_client() -> None:
    """关闭Redis客户端单例。"""
    await close_keyed_clients(
        _redis_clients,
        database="default",
        close_client=lambda client: client.close(),
    )
