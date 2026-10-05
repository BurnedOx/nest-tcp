import asyncio
import inspect
import json
import logging

from nest_tcp.decorators import EVENT_HANDLERS, MESSAGE_HANDLERS, pattern_keys
from nest_tcp.errors import RPCException
from nest_tcp.framing import FrameDecoder, encode_frame


logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 10 * 1024 * 1024  # 10 MiB cap on a frame's declared length and its size in bytes
CLIENT_READ_TIMEOUT = 30  # seconds a client may take to finish sending a frame it has started
CLIENT_IDLE_TIMEOUT = 300  # seconds a connection may stay open with no frame or handler in progress
CLIENT_WRITE_TIMEOUT = 30  # seconds a client may take to accept a reply
MAX_PENDING_PER_CONNECTION = 100  # messages one connection may have in flight at once
READ_CHUNK_SIZE = 64 * 1024

INTERNAL_ERROR = {"message": "Internal server error"}


def _reject_constant(name: str):
    # json.loads accepts NaN/Infinity by default; they are not JSON and could
    # not be echoed back to a NestJS peer.
    raise ValueError(f"{name} is not valid JSON")


class TCPServer:
    def __init__(self, host: str | None, port: int | None, max_connections: int | None = None):
        self.host = host or "127.0.0.1"
        self.port = 5000 if port is None else port
        self.max_connections = max_connections
        self._task: asyncio.Task | None = None
        self._connections: set[asyncio.Task] = set()
        self._closing = False

    def start(self) -> asyncio.Task:
        """Start the TCP server as a background task on the running event loop.

        The task is retained on ``self._task`` so it is not garbage-collected
        mid-run (a bare ``create_task`` may be), and is returned for callers who
        want to ``await`` or cancel it. Must be called from within a running
        event loop; prefer :meth:`serve` to run the server directly.
        """
        self._task = asyncio.create_task(self.__start_server())
        self._task.add_done_callback(self.__log_if_failed)
        return self._task

    async def serve(self):
        """Run the server until cancelled (use this when you can await)."""
        await self.__start_server()

    def __log_if_failed(self, task: asyncio.Task):
        # Nobody may be awaiting the task, so without this a failure (e.g. the
        # port is already in use) would pass unnoticed.
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "TCP server on %s:%s stopped: %s", self.host, self.port,
                task.exception(), exc_info=task.exception(),
            )

    async def __start_server(self):
        server = await asyncio.start_server(self.__handle_client, self.host, self.port)
        if self.port == 0:
            # An ephemeral port was requested; expose the one we were given.
            self.port = server.sockets[0].getsockname()[1]
        self._closing = False

        serving = asyncio.ensure_future(server.serve_forever())
        try:
            # wait() leaves ``serving`` running when we are cancelled, which
            # lets us hang up on clients before the listener shuts down:
            # keep-alive connections would otherwise hold wait_closed() open.
            await asyncio.wait({serving})
        finally:
            self._closing = True
            server.close()
            for task in tuple(self._connections):
                task.cancel()
            serving.cancel()
            await asyncio.wait({serving})
            await server.wait_closed()
        serving.result()

    async def __handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Serve one connection until the peer hangs up.

        A NestJS ``ClientTCP`` keeps a single socket open and multiplexes every
        request over it, so frames are read in a loop and each message's handler
        runs as its own task; replies are matched to requests by ``id``.
        """
        if self._closing:
            writer.close()
            return
        if self.max_connections is not None and len(self._connections) >= self.max_connections:
            logger.warning("Rejecting connection: %d already open", len(self._connections))
            writer.close()
            return

        connection = asyncio.current_task()
        self._connections.add(connection)
        pending: set[asyncio.Task] = set()
        try:
            await self.__read_messages(reader, writer, pending)
            # The peer has stopped sending (e.g. a fire-and-forget emit); let
            # the handlers already running finish and reply before hanging up.
            if pending:
                await asyncio.wait(pending)
        finally:
            self._connections.discard(connection)
            for task in pending:  # only left over when we are being cancelled
                task.cancel()
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def __read_messages(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        pending: set[asyncio.Task],
    ):
        """Read frames off a connection, starting a handler task for each."""
        decoder = FrameDecoder(MAX_MESSAGE_LENGTH)
        write_lock = asyncio.Lock()
        slots = asyncio.Semaphore(MAX_PENDING_PER_CONNECTION)

        def done(task: asyncio.Task):
            pending.discard(task)
            slots.release()

        while True:
            try:
                payload = await self.__read_frame(reader, decoder, pending)
            except ValueError as e:
                # The stream is no longer frame-aligned, so nothing after this
                # point can be trusted: report the problem and hang up.
                error = self.__build_response(None, {"message": str(e)}, None)
                await self.__send(writer, write_lock, error)
                return
            except (ConnectionError, OSError):
                return
            if payload is None:
                return

            # Stop reading while this connection has too many messages in
            # flight; TCP backpressure then slows the sender down.
            await slots.acquire()
            task = asyncio.create_task(self.__handle_message(payload, writer, write_lock))
            pending.add(task)
            task.add_done_callback(done)

    async def __read_frame(
        self,
        reader: asyncio.StreamReader,
        decoder: FrameDecoder,
        pending: set[asyncio.Task],
    ) -> str | None:
        """Read a single ``length#payload`` frame and return its payload.

        Returns ``None`` if the peer closed the connection, or left it idle for
        too long, between frames. Raises ``ValueError`` on a malformed,
        oversized, truncated or stalled frame.
        """
        loop = asyncio.get_running_loop()
        deadline = None
        while True:
            payload = decoder.next_frame()
            if payload is not None:
                return payload

            if decoder.mid_frame:
                # Bound how long a slow peer may take to finish a frame it has
                # started (slowloris protection), however it paces the bytes.
                if deadline is None:
                    deadline = loop.time() + CLIENT_READ_TIMEOUT
                timeout = deadline - loop.time()
            else:
                timeout = CLIENT_IDLE_TIMEOUT

            try:
                chunk = await asyncio.wait_for(reader.read(READ_CHUNK_SIZE), timeout)
            except asyncio.TimeoutError:
                if decoder.mid_frame:
                    raise ValueError("timed out waiting for a complete message") from None
                if pending:
                    continue  # not idle: handlers for this connection are still running
                return None

            if not chunk:
                if decoder.mid_frame:
                    raise ValueError("connection closed before full payload received")
                return None
            decoder.feed(chunk)

    async def __handle_message(self, payload: str, writer: asyncio.StreamWriter, write_lock: asyncio.Lock):
        try:
            response = await self.__process_message(payload)
        except Exception:
            # Should be unreachable, but a request must never go unanswered.
            logger.exception("Failed to process message")
            response = self.__build_response(None, INTERNAL_ERROR, None)
        if response is not None:
            await self.__send(writer, write_lock, response)

    async def __process_message(self, payload: str) -> bytes | None:
        """Run the handler for one message and return the framed reply.

        Returns ``None`` when no reply is due (an event).
        """
        try:
            message = json.loads(payload, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return self.__build_response(None, {"message": "Invalid message: malformed JSON"}, None)
        if not isinstance(message, dict) or "pattern" not in message:
            message_id = message.get("id") if isinstance(message, dict) else None
            return self.__build_response(message_id, {"message": "Invalid message: no pattern"}, None)

        message_id = message.get("id")
        pattern = message["pattern"]
        handler, is_event = self.__find_handler(pattern)
        if handler is None:
            return self.__build_response(message_id, {"message": "Pattern not found"}, None)

        # Events are fire-and-forget: NestJS marks them by leaving out the id,
        # and nobody is waiting for a reply.
        expects_reply = not (is_event and message_id is None)
        response_data = None
        error_data = None
        try:
            response_data = await self.__call_handler(handler, message.get("data"))
        except RPCException as e:
            error_data = e.to_dict()
            if not expects_reply:
                logger.warning("Event handler for pattern %r raised %s", pattern, e)
        except Exception:
            # The details stay in our log; a remote caller only learns that the
            # handler failed (raise RPCException to return a specific error).
            logger.exception("Unhandled error in handler for pattern %r", pattern)
            error_data = INTERNAL_ERROR

        if not expects_reply:
            return None
        try:
            return self.__build_response(message_id, error_data, response_data)
        except (TypeError, ValueError):
            logger.exception("Reply for pattern %r is not JSON serializable", pattern)
            return self.__build_response(message_id, INTERNAL_ERROR, None)

    def __find_handler(self, pattern):
        """Return ``(handler, is_event)`` for a wire pattern, or ``(None, False)``."""
        keys = pattern_keys(pattern)
        for handlers, is_event in ((MESSAGE_HANDLERS, False), (EVENT_HANDLERS, True)):
            for key in keys:
                if key in handlers:
                    return handlers[key], is_event
        return None, False

    async def __send(self, writer: asyncio.StreamWriter, write_lock: asyncio.Lock, frame: bytes):
        """Write one frame, dropping a peer that has gone away or stopped reading."""
        # Handlers for one connection finish in any order; the lock keeps their
        # replies from waiting on the same drain().
        async with write_lock:
            if writer.is_closing():
                return
            try:
                writer.write(frame)
                await asyncio.wait_for(writer.drain(), CLIENT_WRITE_TIMEOUT)
            except asyncio.TimeoutError:
                # The peer is not reading its replies; drop it rather than
                # buffer them indefinitely.
                writer.transport.abort()
            except (ConnectionError, OSError):
                # Peer went away before we could reply (e.g. fire-and-forget emit).
                pass

    async def __call_handler(self, handler, data):
        """Invoke a handler, supporting both sync and async functions."""
        result = handler(data)
        if inspect.isawaitable(result):
            result = await result
        return result

    def __build_response(
        self,
        message_id: str,
        error_data: dict | None,
        response_data: dict | None
    ):
        """Build a NestJS-framed response (UTF-16-unit length + UTF-8 body)."""
        payload = json.dumps({
            "id": message_id,
            "err": error_data,
            "response": response_data,
            # Tells a NestJS client this reply is final, so its observable
            # completes instead of waiting for further values.
            "isDisposed": True,
        }, allow_nan=False)
        return encode_frame(payload)
