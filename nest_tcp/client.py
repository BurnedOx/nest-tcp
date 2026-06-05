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
                message = self.__receive_all_messages(sock)
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

            error, response = self.__unpack_incoming_response_from_nest(message)
            if error:
                raise RPCException(error)
            return response

    def __pack_outgoing_message_to_nest(self, pattern, data):
        _id = uuid.uuid4()
        dict_merged = {'pattern': pattern, 'data': data, 'id': str(_id)}
        s_json = json.dumps(dict_merged)
        return f'{len(s_json)}#{s_json}'.encode()

    def __receive_all_messages(self, sock: socket):
        message = b''
        final_length = 0
        while True:
            _d = sock.recv(1024)
            if _d:
                message, final_length, done = self.__get_response_message(
                    message, _d, final_length)
                if done:
                    break
            else:
                break
        return message

    def __get_response_message(self, message, data, final_length):
        if message == b'':
            _s = data.split(b'#')
            final_length = int(_s[0].decode())

        message += data
        try:
            if len(message.decode()) == final_length + len(str(final_length)) + 1:
                return message, final_length, True
        except UnicodeDecodeError:
            pass
        return message, final_length, False

    def __unpack_incoming_response_from_nest(self, message: str):
        _s = message.split(b'#')
        final_length = int(_s[0])
        message: dict = json.loads(
            message[len(str(final_length)) + 1:].decode())
        return message.get('err'), message.get('response')
