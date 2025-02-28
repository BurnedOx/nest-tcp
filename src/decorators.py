import json
from functools import wraps

MESSAGE_HANDLERS = {}
EVENT_HANDLERS = {}


def message_pattern(pattern):
    """Decorator for handling RPC messages"""
    def decorator(func):
        MESSAGE_HANDLERS[json.dumps(pattern)] = func

        @wraps(func)
        async def wrapper(data):
            return await func(data)

        return wrapper
    return decorator


def event_pattern(pattern):
    """Decorator for handling events"""
    def decorator(func):
        EVENT_HANDLERS[json.dumps(pattern)] = func

        @wraps(func)
        async def wrapper(data):
            return await func(data)

        return wrapper
    return decorator
