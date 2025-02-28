import socket
import json
import uuid

from src.errors import RPCException


class TcpClient:
    def __init__(self, host, port):
        self.host = host
        if isinstance(port, str):
            self.port = int(port)
        else:
            self.port = port

    def send(self, pattern: dict, data):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((self.host, self.port))
            json_data = self.__pack_outgoing_message_to_nest(pattern, data)
            sock.sendall(json_data)
            message = self.__receive_all_messages(sock)
            sock.close()

        error, response = self.__unpack_incoming_response_from_nest(message)

        if error:
            raise RPCException(error)
        return response

    def __pack_outgoing_message_to_nest(self, pattern, data):
        _id = uuid.uuid4()
        dict_merged = {'pattern': pattern, 'data': data, 'id': str(_id)}
        s_json = json.dumps(obj=dict_merged)
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
        message: dict = json.loads(message[len(str(final_length))+1:].decode())
        return message.get('err'), message.get('response')
