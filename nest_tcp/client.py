import socket
import json
import uuid

from nest_tcp.errors import RPCException


DEFAULT_TIMEOUT = 30  # seconds for connect and each socket read


class TCPClient:
    def __init__(self, host, port, timeout: float | None = DEFAULT_TIMEOUT):
        self.host = host
        self.port = int(port) if isinstance(port, str) else port
        self.timeout = timeout

    def send(self, pattern: dict, data):
        """Send a message and expect a response (RPC-style)."""
        return self.__communicate(pattern, data, expect_response=True)

    def emit(self, pattern: str, data):
        """Send an event without expecting a response."""
        self.__communicate(pattern, data, expect_response=False)

    def __communicate(self, pattern, data, expect_response: bool):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            try:
                sock.connect((self.host, self.port))
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

            json_data = self.__pack_outgoing_message_to_nest(pattern, data)
            try:
                sock.sendall(json_data)
                if not expect_response:
                    return
                payload = self.__receive_response(sock)
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

    def __pack_outgoing_message_to_nest(self, pattern, data):
        _id = uuid.uuid4()
        dict_merged = {'pattern': pattern, 'data': data, 'id': str(_id)}
        s_json = json.dumps(dict_merged)
        # Frame length is the byte count of the (ASCII, since json.dumps defaults
        # to ensure_ascii) payload, matching the server's byte-based framing.
        body = s_json.encode()
        return f'{len(body)}#'.encode() + body

    def __receive_response(self, sock: socket) -> bytes:
        """Read one ``length#payload`` frame and return the raw payload bytes.

        Reads the prefix up to '#' (tolerant of it spanning multiple packets),
        then reads exactly ``length`` payload bytes. Raises RPCException if the
        peer closes before a complete frame arrives.
        """
        buf = b''
        # Accumulate until the '#' delimiter so we can read the length prefix,
        # even if the prefix itself is split across recv() boundaries.
        while b'#' not in buf:
            chunk = sock.recv(1024)
            if not chunk:
                raise RPCException({
                    "message": "Connection closed before response",
                    "code": 502,
                })
            buf += chunk
            if len(buf) > 64 and b'#' not in buf:
                raise RPCException({
                    "message": "Malformed response: length prefix too long",
                    "code": 502,
                })

        prefix, _, body = buf.partition(b'#')
        try:
            length = int(prefix)
        except ValueError:
            raise RPCException({
                "message": f"Malformed response: invalid length prefix {prefix!r}",
                "code": 502,
            })

        # Keep reading until we have the full payload.
        while len(body) < length:
            chunk = sock.recv(1024)
            if not chunk:
                raise RPCException({
                    "message": "Connection closed mid-response",
                    "code": 502,
                })
            body += chunk

        return body[:length]

    def __unpack_incoming_response_from_nest(self, payload: bytes):
        message: dict = json.loads(payload.decode())
        return message.get('err'), message.get('response')
