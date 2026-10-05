import socket
import json
import time
import uuid

from nest_tcp.errors import RPCException
from nest_tcp.framing import FrameDecoder, encode_frame


DEFAULT_TIMEOUT = 30  # seconds to connect, and again for the whole request/response
MAX_RESPONSE_LENGTH = 10 * 1024 * 1024  # 10 MiB cap on a response frame
RECV_CHUNK_SIZE = 64 * 1024


class TCPClient:
    def __init__(
        self,
        host,
        port,
        timeout: float | None = DEFAULT_TIMEOUT,
        max_response_length: int | None = MAX_RESPONSE_LENGTH,
    ):
        self.host = host
        self.port = int(port) if isinstance(port, str) else port
        self.timeout = timeout
        self.max_response_length = max_response_length

    def send(self, pattern: dict, data):
        """Send a message and expect a response (RPC-style)."""
        return self.__communicate(pattern, data, expect_response=True)

    def emit(self, pattern: str, data):
        """Send an event without expecting a response."""
        self.__communicate(pattern, data, expect_response=False)

    def __communicate(self, pattern, data, expect_response: bool):
        try:
            # create_connection resolves host names and tries every address
            # (IPv6 as well as IPv4) rather than assuming AF_INET.
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except socket.timeout as e:
            raise RPCException({
                "message": "Connection Timeout",
                "data": {"message": str(e)},
                "code": 408,
            })
        except OSError as e:
            raise RPCException({
                "message": "Connection Error",
                "data": {"message": str(e)},
                "code": 503,
            })

        with sock:
            json_data = self.__pack_outgoing_message_to_nest(pattern, data, with_id=expect_response)
            # One deadline for the whole exchange, so a peer that trickles
            # bytes cannot keep the call alive past the timeout.
            deadline = None if self.timeout is None else time.monotonic() + self.timeout
            try:
                sock.sendall(json_data)
                if not expect_response:
                    return
                payload = self.__receive_response(sock, deadline)
            except socket.timeout as e:
                raise RPCException({
                    "message": "Request Timeout",
                    "data": {"message": str(e)},
                    "code": 408,
                })
            except OSError as e:
                raise RPCException({
                    "message": "Connection Error",
                    "data": {"message": str(e)},
                    "code": 503,
                })

            error, response = self.__unpack_incoming_response_from_nest(payload)
            if error:
                raise RPCException(error)
            return response

    def __pack_outgoing_message_to_nest(self, pattern, data, with_id: bool):
        dict_merged = {'pattern': pattern, 'data': data}
        if with_id:
            # NestJS treats a packet as a request only if it carries an id;
            # without one it is an event and no reply is sent.
            dict_merged['id'] = str(uuid.uuid4())
        # NestJS-compatible framing: UTF-16-unit length prefix + UTF-8 body.
        # json.dumps defaults to ensure_ascii=True, so the body is ASCII and the
        # prefix equals the byte count; non-ASCII still frames correctly.
        # allow_nan=False because NaN/Infinity are not JSON and NestJS's
        # JSON.parse would reject the whole frame.
        return encode_frame(json.dumps(dict_merged, allow_nan=False))

    def __receive_response(self, sock: socket.socket, deadline: float | None) -> str:
        """Read one ``length#payload`` frame and return the decoded payload.

        Raises RPCException if the frame is malformed or the peer closes before
        a complete frame arrives, and ``socket.timeout`` once ``deadline`` has
        passed.
        """
        decoder = FrameDecoder(self.max_response_length)
        while True:
            try:
                payload = decoder.next_frame()
            except ValueError as e:
                raise RPCException({
                    "message": f"Malformed response: {e}",
                    "code": 502,
                })
            if payload is not None:
                return payload

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("timed out")
                sock.settimeout(remaining)
            chunk = sock.recv(RECV_CHUNK_SIZE)
            if not chunk:
                closed = "mid-response" if decoder.mid_frame else "before response"
                raise RPCException({
                    "message": f"Connection closed {closed}",
                    "code": 502,
                })
            decoder.feed(chunk)

    def __unpack_incoming_response_from_nest(self, payload: str):
        try:
            message = json.loads(payload)
        except ValueError as e:
            raise RPCException({
                "message": f"Malformed response: invalid JSON ({e})",
                "code": 502,
            })
        if not isinstance(message, dict):
            raise RPCException({
                "message": "Malformed response: expected a JSON object",
                "code": 502,
            })
        return message.get('err'), message.get('response')
