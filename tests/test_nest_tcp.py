"""Regression tests. Run with ``python -m unittest discover -s tests``."""
import asyncio
import gc
import json
import math
import socket
import threading
import time
import unittest

from nest_tcp import RPCException, TCPClient, TCPServer, event_pattern, message_pattern
from nest_tcp.framing import FrameDecoder, encode_frame

EVENTS = []
SECRET = "postgres://app:hunter2@10.0.0.5/prod"


@message_pattern({"cmd": "echo"})
async def echo(data):
    return data


@message_pattern({"cmd": "add"})
def add(data):
    return data["a"] + data["b"]


@message_pattern({"cmd": "sleep"})
async def sleep(data):
    await asyncio.sleep(data["seconds"])
    return data["seconds"]


@message_pattern({"cmd": "fail"})
async def fail(data):
    raise RPCException({"code": 404, "message": "User not found", "data": {"field": "id"}})


@message_pattern({"cmd": "boom"})
async def boom(data):
    raise RuntimeError(SECRET)


@message_pattern({"cmd": "nan"})
async def nan(data):
    return math.nan


@event_pattern({"event": "user_viewed"})
async def on_viewed(data):
    EVENTS.append(data)


def frame(message) -> bytes:
    return encode_frame(json.dumps(message))


def read_frames(sock, count, timeout=5):
    """Read ``count`` reply frames off a raw socket."""
    sock.settimeout(timeout)
    decoder = FrameDecoder()
    replies = []
    while len(replies) < count:
        payload = decoder.next_frame()
        if payload is not None:
            replies.append(json.loads(payload))
            continue
        chunk = sock.recv(65536)
        if not chunk:
            break
        decoder.feed(chunk)
    return replies


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


class ServerThread:
    """Runs a TCPServer on an ephemeral port in a background event loop."""

    def __init__(self, **kwargs):
        self.server = TCPServer("127.0.0.1", 0, **kwargs)
        self._ready = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.task = self.server.start()
        while self.server.port == 0 and not self.task.done():
            await asyncio.sleep(0.01)
        self._ready.set()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(5)
        return self

    def __exit__(self, *exc):
        self.stop()

    def stop(self, timeout=5) -> bool:
        """Cancel the server; True if it shut down within ``timeout``."""
        self.loop.call_soon_threadsafe(self.task.cancel)
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def port(self):
        return self.server.port

    def client(self, **kwargs):
        return TCPClient("127.0.0.1", self.port, **kwargs)

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)


class FakeServer:
    """One-shot TCP peer that answers the first request with canned bytes."""

    def __init__(self, reply: bytes = b"", delay_between_bytes: float = 0, hold: float = 0):
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.request = b""
        self._reply, self._delay, self._hold = reply, delay_between_bytes, hold
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        conn, _ = self._listener.accept()
        with conn, self._listener:
            self.request = conn.recv(65536)
            try:
                if self._delay:
                    for i in range(len(self._reply)):
                        conn.sendall(self._reply[i:i + 1])
                        time.sleep(self._delay)
                else:
                    conn.sendall(self._reply)
                time.sleep(self._hold)
            except OSError:
                pass

    def client(self, **kwargs):
        return TCPClient("127.0.0.1", self.port, **kwargs)

    def join(self):
        self._thread.join(5)


class FrameDecoderTests(unittest.TestCase):
    PAYLOADS = ['{"a":1}', '{"t":"héllo wörld"}', '{"t":"日本語のテキスト"}', '{"t":"a😀b😀😀"}', ""]

    def test_roundtrip_at_every_chunk_size(self):
        stream = b"".join(encode_frame(p) for p in self.PAYLOADS)
        for size in range(1, 12):
            decoder = FrameDecoder()
            out = []
            for i in range(0, len(stream), size):
                decoder.feed(stream[i:i + size])
                while (payload := decoder.next_frame()) is not None:
                    out.append(payload)
            self.assertEqual(out, self.PAYLOADS, f"chunk size {size}")
            self.assertFalse(decoder.mid_frame)

    def test_length_counts_utf16_units(self):
        self.assertTrue(encode_frame('"😀"').startswith(b"4#"))  # 2 quotes + surrogate pair

    def test_keeps_bytes_of_next_frame(self):
        decoder = FrameDecoder()
        decoder.feed(b"2#{}3#[1")
        self.assertEqual(decoder.next_frame(), "{}")
        self.assertIsNone(decoder.next_frame())
        self.assertTrue(decoder.mid_frame)
        decoder.feed(b"]")
        self.assertEqual(decoder.next_frame(), "[1]")

    def test_rejects_malformed_frames(self):
        cases = {
            "garbage prefix": b"GET / HTTP/1.1\r\n",
            "negative length": b"-5#{}",
            "empty prefix": b"#{}",
            "signed length": b"+2#{}",
            "prefix too long": b"1" * 30,
            "over the cap": b"101#",
            "stray continuation byte": b"3#\x80ab",
            "truncated sequence": b"2#\xe6a",
            "splits a surrogate pair": b"1#" + "😀".encode(),
        }
        for name, data in cases.items():
            decoder = FrameDecoder(max_length=100)
            decoder.feed(data)
            with self.assertRaises(ValueError, msg=name):
                decoder.next_frame()

    def test_byte_cap_applies_to_multibyte_bodies(self):
        decoder = FrameDecoder(max_length=10)
        decoder.feed(b"8#" + ("日" * 8).encode())  # 8 units but 24 bytes
        with self.assertRaises(ValueError):
            decoder.next_frame()

    def test_scan_is_linear(self):
        # Rescanning the buffer on every chunk made this take minutes.
        for body in ("a" * 2_000_000, "日" * 700_000):
            stream = encode_frame(body)
            decoder = FrameDecoder()
            started = time.perf_counter()
            for i in range(0, len(stream), 1024):
                decoder.feed(stream[i:i + 1024])
                payload = decoder.next_frame()
            self.assertEqual(payload, body)
            self.assertLess(time.perf_counter() - started, 5)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ServerThread().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_send_roundtrip(self):
        data = {"text": "héllo 日本 😀", "n": [1, 2.5, None, True]}
        self.assertEqual(self.srv.client().send({"cmd": "echo"}, data), data)
        self.assertEqual(self.srv.client().send({"cmd": "add"}, {"a": 1, "b": 2}), 3)

    def test_large_payload_is_fast(self):
        blob = "a" * 3_000_000
        started = time.perf_counter()
        self.assertEqual(self.srv.client().send({"cmd": "echo"}, blob), blob)
        self.assertLess(time.perf_counter() - started, 10)

    def test_sync_handler_still_callable_directly(self):
        self.assertEqual(add({"a": 1, "b": 2}), 3)

    def test_errors(self):
        with self.assertRaises(RPCException) as ctx:
            self.srv.client().send({"cmd": "fail"}, {})
        self.assertEqual((ctx.exception.code, ctx.exception.message), (404, "User not found"))
        self.assertEqual(ctx.exception.data, {"field": "id"})

        with self.assertRaises(RPCException) as ctx:
            self.srv.client().send({"cmd": "nope"}, {})
        self.assertEqual(ctx.exception.message, "Pattern not found")

    def test_unhandled_exception_text_is_not_sent_to_caller(self):
        with self.assertLogs("nest_tcp.server", "ERROR") as logs:
            with self.assertRaises(RPCException) as ctx:
                self.srv.client().send({"cmd": "boom"}, {})
        self.assertEqual(ctx.exception.message, "Internal server error")
        self.assertNotIn(SECRET, json.dumps(ctx.exception.to_dict()))
        self.assertIn(SECRET, "\n".join(logs.output))

    def test_nan_result_is_not_put_on_the_wire(self):
        with self.assertLogs("nest_tcp.server", "ERROR"):
            with self.srv.connect() as sock:
                sock.sendall(frame({"pattern": {"cmd": "nan"}, "data": {}, "id": "n"}))
                sock.settimeout(5)
                raw = sock.recv(65536)
        self.assertNotIn(b"NaN", raw)
        self.assertIn(b"Internal server error", raw)

    def test_emit_runs_event_handler(self):
        EVENTS.clear()
        self.srv.client().emit({"event": "user_viewed"}, {"id": 1})
        self.assertTrue(wait_for(lambda: EVENTS == [{"id": 1}]))

    def test_many_requests_on_one_connection(self):
        # NestJS's ClientTCP multiplexes every request over a single socket.
        with self.srv.connect() as sock:
            sock.sendall(
                frame({"pattern": {"cmd": "sleep"}, "data": {"seconds": 0.3}, "id": "slow"})
                + frame({"pattern": {"cmd": "add"}, "data": {"a": 1, "b": 2}, "id": "fast"})
            )
            first, second = read_frames(sock, 2)
            self.assertEqual((first["id"], first["response"]), ("fast", 3))
            self.assertEqual((second["id"], second["response"]), ("slow", 0.3))
            # ...and the connection stays usable afterwards.
            sock.sendall(frame({"pattern": {"cmd": "add"}, "data": {"a": 5, "b": 5}, "id": "again"}))
            self.assertEqual(read_frames(sock, 1)[0]["response"], 10)

    def test_replies_are_marked_disposed(self):
        with self.srv.connect() as sock:
            sock.sendall(frame({"pattern": {"cmd": "add"}, "data": {"a": 1, "b": 2}, "id": "x"}))
            self.assertEqual(
                read_frames(sock, 1), [{"id": "x", "err": None, "response": 3, "isDisposed": True}])

    def test_nestjs_event_with_stringified_pattern(self):
        # NestJS emit() sends a dict pattern as its JSON string, with no id.
        EVENTS.clear()
        with self.srv.connect() as sock:
            sock.sendall(frame({"pattern": '{"event":"user_viewed"}', "data": {"id": 2}}))
            self.assertTrue(wait_for(lambda: EVENTS == [{"id": 2}]))
            # No reply is sent for an event; the next request is answered first.
            sock.sendall(frame({"pattern": {"cmd": "add"}, "data": {"a": 1, "b": 1}, "id": "next"}))
            self.assertEqual(read_frames(sock, 1)[0]["id"], "next")

    def test_bad_message_does_not_kill_the_connection(self):
        with self.srv.connect() as sock:
            sock.sendall(b"8#not json" + frame([1, 2]) + frame({"pattern": {"cmd": "add"}, "data": {"a": 1, "b": 1}, "id": "ok"}))
            replies = read_frames(sock, 3)
        self.assertEqual([r["id"] for r in replies], [None, None, "ok"])
        self.assertIn("Invalid message", replies[0]["err"]["message"])

    def test_malformed_frame_is_reported_and_connection_closed(self):
        with self.srv.connect() as sock:
            sock.sendall(b"GET / HTTP/1.1\r\n\r\n")
            replies = read_frames(sock, 2)  # asks for two, gets one and then EOF
        self.assertEqual(len(replies), 1)
        self.assertIn("invalid length prefix", replies[0]["err"]["message"])

    def test_event_loop_stays_responsive_during_large_upload(self):
        blob = "a" * 4_000_000
        threading.Thread(
            target=lambda: self.srv.client().send({"cmd": "echo"}, blob), daemon=True).start()
        worst = 0.0
        for _ in range(20):
            started = time.perf_counter()
            self.srv.client().send({"cmd": "add"}, {"a": 1, "b": 1})
            worst = max(worst, time.perf_counter() - started)
        self.assertLess(worst, 1)


class ServerLifecycleTests(unittest.TestCase):
    def test_shutdown_is_not_held_open_by_idle_connections(self):
        srv = ServerThread().__enter__()
        with srv.connect() as sock:
            sock.sendall(frame({"pattern": {"cmd": "add"}, "data": {"a": 1, "b": 1}, "id": "1"}))
            read_frames(sock, 1)
            self.assertTrue(srv.stop(timeout=5))
            sock.settimeout(5)
            self.assertEqual(sock.recv(1), b"")  # we were hung up on

    def test_max_connections(self):
        with ServerThread(max_connections=1) as srv, self.assertLogs("nest_tcp.server", "WARNING"):
            with srv.connect() as first:
                first.sendall(frame({"pattern": {"cmd": "add"}, "data": {"a": 1, "b": 1}, "id": "1"}))
                read_frames(first, 1)
                with self.assertRaises(RPCException):
                    srv.client().send({"cmd": "add"}, {"a": 1, "b": 1})
            self.assertTrue(wait_for(lambda: not srv.server._connections))
            self.assertEqual(srv.client().send({"cmd": "add"}, {"a": 1, "b": 1}), 2)

    def test_port_defaults(self):
        self.assertEqual(TCPServer(None, None).port, 5000)
        self.assertEqual(TCPServer(None, 0).port, 0)

    def test_start_logs_bind_failure(self):
        async def main(port):
            task = TCPServer("127.0.0.1", port).start()
            await asyncio.wait({task})

        with socket.create_server(("127.0.0.1", 0)) as taken:
            with self.assertLogs("nest_tcp.server", "ERROR") as logs:
                asyncio.run(main(taken.getsockname()[1]))
        self.assertIn("stopped", logs.output[0])

    def test_unreferenced_server_survives_garbage_collection(self):
        # ``TCPServer(...).start()`` with the instance and task both dropped.
        async def main():
            server = TCPServer("127.0.0.1", 0)
            server.start()
            while server.port == 0:
                await asyncio.sleep(0.01)
            port = server.port
            del server
            for _ in range(3):
                gc.collect()
                await asyncio.sleep(0.05)
            client = TCPClient("127.0.0.1", port, timeout=5)
            return await asyncio.to_thread(client.send, {"cmd": "add"}, {"a": 2, "b": 2})

        self.assertEqual(asyncio.run(main()), 4)


class ClientTests(unittest.TestCase):
    def send(self, reply: bytes, **kwargs):
        fake = FakeServer(reply)
        return fake.client(timeout=5, **kwargs).send({"cmd": "x"}, {})

    def test_string_error_from_nestjs(self):
        # What NestJS replies when no handler matches the pattern.
        text = "There is no matching message handler defined in the remote service."
        with self.assertRaises(RPCException) as ctx:
            self.send(frame({"id": "1", "status": "error", "err": text}))
        self.assertEqual(ctx.exception.message, text)
        self.assertIn(text, str(ctx.exception))

    def test_malformed_responses_raise_rpc_exception(self):
        cases = {
            "invalid JSON": b"9#not json!",
            "not an object": b"4#null",
            "negative length": b"-5#{}",
            "bad UTF-8": b"3#\xff\xff\xff",
            "closed before response": b"",
            "closed mid-response": b"50#{",
        }
        for name, reply in cases.items():
            with self.assertRaises(RPCException, msg=name) as ctx:
                self.send(reply)
            self.assertEqual(ctx.exception.code, 502, name)

    def test_response_size_cap(self):
        reply = frame({"id": "1", "err": None, "response": "a" * 1000})
        self.assertEqual(self.send(reply), "a" * 1000)
        with self.assertRaises(RPCException) as ctx:
            self.send(reply, max_response_length=100)
        self.assertEqual(ctx.exception.code, 502)

    def test_timeout_bounds_whole_response(self):
        # A byte every 0.2s never trips a per-recv timeout of 1s.
        fake = FakeServer(frame({"id": "1", "err": None, "response": "a" * 100}), delay_between_bytes=0.2)
        started = time.perf_counter()
        with self.assertRaises(RPCException) as ctx:
            fake.client(timeout=1).send({"cmd": "x"}, {})
        self.assertEqual(ctx.exception.code, 408)
        self.assertLess(time.perf_counter() - started, 3)

    def test_connection_refused(self):
        with socket.create_server(("127.0.0.1", 0)) as s:
            port = s.getsockname()[1]
        with self.assertRaises(RPCException) as ctx:
            TCPClient("127.0.0.1", port, timeout=5).send({"cmd": "x"}, {})
        self.assertEqual(ctx.exception.code, 503)

    def test_emit_has_no_id_and_send_does(self):
        fake = FakeServer(hold=0.2)
        fake.client(timeout=5).emit({"event": "e"}, {"k": "v"})
        fake.join()
        self.assertEqual(json.loads(fake.request.partition(b"#")[2]), {"pattern": {"event": "e"}, "data": {"k": "v"}})

        fake = FakeServer(frame({"id": "1", "err": None, "response": 1}))
        fake.client(timeout=5).send({"cmd": "x"}, {})
        fake.join()
        self.assertIn("id", json.loads(fake.request.partition(b"#")[2]))

    def test_nan_is_rejected_before_sending(self):
        fake = FakeServer(hold=0.2)
        with self.assertRaises(ValueError):
            fake.client(timeout=5).send({"cmd": "x"}, {"value": math.nan})

    def test_hostname_resolution(self):
        with ServerThread() as srv:
            client = TCPClient("localhost", str(srv.port), timeout=5)
            self.assertEqual(client.send({"cmd": "add"}, {"a": 1, "b": 2}), 3)


class RPCExceptionTests(unittest.TestCase):
    def test_accepts_non_dict_errors(self):
        self.assertEqual(RPCException("nope").message, "nope")
        self.assertEqual(RPCException(["a", 1]).message, "['a', 1]")

    def test_str_tolerates_non_dict_data(self):
        self.assertIn("Unknown error", str(RPCException({"data": "plain string"})))
        self.assertIn("inner", str(RPCException({"data": {"message": "inner"}})))


if __name__ == "__main__":
    unittest.main()
