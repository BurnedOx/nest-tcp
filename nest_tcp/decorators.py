import json
from functools import wraps

MESSAGE_HANDLERS = {}
EVENT_HANDLERS = {}


def normalize_pattern(pattern) -> str:
    """Canonical string key for a pattern.

    Uses sorted keys and compact separators so that two equal patterns with
    different dict key order (e.g. ``{"cmd":"x","v":1}`` vs ``{"v":1,"cmd":"x"}``)
    map to the same handler. Both registration and lookup must use this.
    """
    return json.dumps(pattern, sort_keys=True, separators=(",", ":"))


def message_pattern(pattern):
    """Decorator for handling RPC messages"""
    def decorator(func):
        MESSAGE_HANDLERS[normalize_pattern(pattern)] = func

        @wraps(func)
        async def wrapper(data):
            return await func(data)

        return wrapper
    return decorator


def event_pattern(pattern):
    """Decorator for handling events"""
    def decorator(func):
        EVENT_HANDLERS[normalize_pattern(pattern)] = func

        @wraps(func)
        async def wrapper(data):
            return await func(data)

        return wrapper
    return decorator
