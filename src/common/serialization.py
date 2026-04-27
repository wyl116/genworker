"""
Common serialization utility module.

Provides unified data serialization supporting:
- Pydantic models
- dataclass
- Enum
- datetime types (datetime, date, time, timedelta)
- Nested structures (list, dict)
- Custom to_dict() methods
"""
from typing import Any
from datetime import datetime, date, time, timedelta
from pydantic import BaseModel
from src.common.logger import get_logger

logger = get_logger()


def serialize_data(data: Any) -> Any:
    """
    Recursively serialize data, supporting various complex types.

    Args:
        data: Data to serialize.

    Returns:
        JSON-serializable data.
    """
    if data is None:
        return None

    if isinstance(data, BaseModel):
        dict_data = data.model_dump(exclude_none=True)
        return serialize_data(dict_data)

    if isinstance(data, datetime):
        return data.isoformat()
    if isinstance(data, date):
        return data.isoformat()
    if isinstance(data, time):
        return data.isoformat()
    if isinstance(data, timedelta):
        return data.total_seconds()

    if isinstance(data, (list, tuple)):
        return [serialize_data(item) for item in data]

    if isinstance(data, dict):
        return {k: serialize_data(v) for k, v in data.items()}

    if hasattr(data, 'to_dict') and callable(getattr(data, 'to_dict')):
        try:
            dict_data = data.to_dict()
            return serialize_data(dict_data)
        except Exception as e:
            logger.warning(f"to_dict() call failed: {e}")

    from dataclasses import is_dataclass, asdict
    if is_dataclass(data) and not isinstance(data, type):
        try:
            dict_data = asdict(data)
            return serialize_data(dict_data)
        except Exception as e:
            logger.warning(f"Dataclass serialization failed: {e}")

    from enum import Enum
    if isinstance(data, Enum):
        return data.value

    if isinstance(data, (str, int, float, bool)):
        return data

    try:
        import json
        json.dumps(data)
        return data
    except (TypeError, ValueError):
        logger.warning(
            f"Cannot serialize type {type(data).__name__}, converting to string"
        )
        return str(data)
