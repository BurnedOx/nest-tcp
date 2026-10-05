import json

MESSAGE_HANDLERS = {}
EVENT_HANDLERS = {}


def normalize_pattern(pattern) -> str:
    """Canonical string key for a pattern.

    Uses sorted keys and compact separators so that two equal patterns with
    different dict key order (e.g. ``{"cmd":"x","v":1}`` vs ``{"v":1,"cmd":"x"}``)
    map to the same handler. Both registration and lookup must use this.
    """
    return json.dumps(pattern, sort_keys=True, separators=(",", ":"))


def pattern_keys(pattern) -> list[str]:
    """Registry keys an incoming wire pattern may match, most specific first.

    A NestJS client puts a dict pattern on the wire as an object for ``send()``
    but as its JSON *string* for ``emit()``. Like NestJS, also look a string
    pattern up by the value it parses to, so both forms reach the same handler.
    """
    keys = [normalize_pattern(pattern)]
    if isinstance(pattern, str):
        try:
            parsed = json.loads(pattern)
        except (ValueError, RecursionError):
            return keys
        if not isinstance(parsed, str):
            keys.append(normalize_pattern(parsed))
    return keys


def message_pattern(pattern):
    """Decorator for handling RPC messages"""
    def decorator(func):
        MESSAGE_HANDLERS[normalize_pattern(pattern)] = func
        return func
    return decorator


def event_pattern(pattern):
    """Decorator for handling events"""
    def decorator(func):
        EVENT_HANDLERS[normalize_pattern(pattern)] = func
        return func
    return decorator
