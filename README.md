# nest_tcp

A small Python client/server for the **NestJS TCP microservice** transport.

It speaks NestJS's `JsonSocket` wire protocol (`<length>#<json>` frames, with the
length measured in UTF-16 code units and the body sent as UTF-8), so a Python
service can call into NestJS microservices — and expose handlers that NestJS
clients can call — without a message broker. Useful for wiring a FastAPI app to
an existing NestJS backend.

## Installation

```bash
pip install git+https://github.com/BurnedOx/nest-tcp.git
```

Requires Python 3.10+ (it uses `X | None` type syntax).

## Client

Use `TCPClient` to call NestJS message/event patterns.

```python
from nest_tcp import TCPClient, RPCException

client = TCPClient("127.0.0.1", 3001, timeout=30)  # timeout is seconds (default 30)

# Request/response (NestJS @MessagePattern). Returns the handler's response.
try:
    user = client.send({"cmd": "get_user"}, {"id": 42})
    print(user)
except RPCException as e:
    print(e.code, e.message, e.data)

# Fire-and-forget event (NestJS @EventPattern). Returns immediately, no reply.
client.emit({"event": "user_viewed"}, {"id": 42})
```

- `send(pattern, data)` — sends an RPC message and blocks for the response.
  Raises `RPCException` on a transport error (connect/timeout) or when the remote
  handler returns an error.
- `emit(pattern, data)` — sends an event and returns without waiting.
- `timeout` bounds both connection and each read; on expiry `send` raises
  `RPCException` with code `408`. A refused/failed connection raises code `503`.

## Server

Register handlers with the pattern decorators, then start a `TCPServer` inside an
asyncio event loop.

```python
import asyncio
from nest_tcp import TCPServer, message_pattern, event_pattern, RPCException

@message_pattern({"cmd": "get_user"})
async def get_user(data):
    user = await db.find(data["id"])
    if user is None:
        raise RPCException({"code": 404, "message": "User not found"})
    return user            # becomes the RPC response

@message_pattern({"cmd": "add"})
def add(data):             # sync handlers work too
    return data["a"] + data["b"]

@event_pattern({"event": "user_viewed"})
async def on_viewed(data):
    await analytics.track(data)   # no response is sent for events

async def main():
    server = TCPServer("127.0.0.1", 5000)
    await server.serve()          # runs until cancelled

asyncio.run(main())
```

Notes:
- Patterns may be strings or dicts. Dict patterns are matched regardless of key
  order (`{"cmd": "x", "v": 1}` == `{"v": 1, "cmd": "x"}`).
- Handlers may be `async def` or plain `def`; both receive the message `data`.
- Raise `RPCException` from a handler to return a structured error to the caller.
- Events (`@event_pattern`) are fire-and-forget — the server runs the handler and
  sends no reply.

### Starting in an already-running loop

`serve()` is the simplest entry point. If you need to launch the server as a
background task on an existing loop (e.g. from a FastAPI startup hook), use
`start()`, which retains and returns the task so it isn't garbage-collected:

```python
@app.on_event("startup")
async def startup():
    app.state.tcp = TCPServer("127.0.0.1", 5000)
    app.state.tcp.start()     # returns the asyncio.Task
```

## Errors

`RPCException(error_dict)` carries `code`, `message`, and `data` and is used both
to raise errors from server handlers and to surface remote/transport errors on
the client.

```python
raise RPCException({"code": 400, "message": "Bad request", "data": {"field": "id"}})
```

## Protocol & interoperability

Frames follow NestJS's `JsonSocket`: a decimal length, a `#` delimiter, then the
JSON payload. The length counts **UTF-16 code units** (matching JavaScript's
`String.length`) while the body travels as **UTF-8 bytes**, so non-ASCII content
(accents, emoji, etc.) is framed correctly in both directions. For pure-ASCII
payloads this is identical to a plain byte-length framing.

The transport bounds slow/idle peers with read timeouts and caps the declared
message length to guard against malformed or hostile input.

## License

MIT
