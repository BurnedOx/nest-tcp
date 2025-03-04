import asyncio
import json

from nest_tcp.decorators import EVENT_HANDLERS, MESSAGE_HANDLERS
from nest_tcp.errors import RPCException


class TCPServer:
    def __init__(self, host: str | None, port: int | None):
        self.host = host or "127.0.0.1"
        self.port = port or 5000

    def start(self):
        """Start the TCP server"""
        asyncio.create_task(self.__start_server())

    async def __start_server(self):
        server = await asyncio.start_server(self.__handle_client, self.host, self.port)
        async with server:
            await server.serve_forever()

    async def __handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handles incoming TCP requests"""
        response: bytes
        try:
            # Read data from socket
            data = await reader.read(1024)
            if not data:
                return

            # Unpack incoming JSON message
            msg_length, msg_data = data.split(b"#", 1)
            msg_length = int(msg_length.decode())

            if len(msg_data) < msg_length:
                remaining_data = await reader.read(msg_length - len(msg_data))
                msg_data += remaining_data

            message = json.loads(msg_data.decode())

            pattern = json.dumps(message["pattern"])
            response_data = None
            error_data = None

            if pattern in MESSAGE_HANDLERS:
                response_data = await MESSAGE_HANDLERS[pattern](message["data"])
            elif pattern in EVENT_HANDLERS:
                await EVENT_HANDLERS[pattern](message["data"])
            else:
                error_data = {"message": "Pattern not found"}

            response = self.__build_response(message["id"], error_data, response_data)

        except RPCException as e:
            response = self.__build_response(message["id"], e.to_dict(), None)
        except Exception as e:
            print(f"TCP Error: {e}")
            error_data = {"message": str(e)}
            response = self.__build_response(message["id"], error_data, None)

        finally:
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    def __build_response(
        self,
        message_id: str,
        error_data: dict | None,
        response_data: dict | None
    ):
        """Write response to the socket"""
        response = json.dumps({
            "id": message_id,
            "err": error_data,
            "response": response_data,
        }).encode()
        response = f"{len(response)}#{response.decode()}".encode()
        return response
