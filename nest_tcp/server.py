import asyncio
import json

from nest_tcp.decorators import EVENT_HANDLERS, MESSAGE_HANDLERS
from nest_tcp.errors import RPCException


MAX_MESSAGE_LENGTH = 10 * 1024 * 1024  # 10 MiB cap on a declared frame length


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
        response: bytes | None = None
        message_id = None
        try:
            message = await self.__read_message(reader)
            if message is None:
                # Peer closed before sending a (complete) frame; nothing to reply.
                return
            message_id = message.get("id")

            pattern = json.dumps(message["pattern"])
            response_data = None
            error_data = None

            if pattern in MESSAGE_HANDLERS:
                response_data = await MESSAGE_HANDLERS[pattern](message["data"])
            elif pattern in EVENT_HANDLERS:
                await EVENT_HANDLERS[pattern](message["data"])
            else:
                error_data = {"message": "Pattern not found"}

            response = self.__build_response(message_id, error_data, response_data)

        except RPCException as e:
            response = self.__build_response(message_id, e.to_dict(), None)
        except Exception as e:
            print(f"TCP Error: {e}")
            error_data = {"message": str(e)}
            response = self.__build_response(message_id, error_data, None)

        finally:
            try:
                if response is not None:
                    writer.write(response)
                    await writer.drain()
            except (ConnectionError, OSError):
                # Peer went away before we could reply (e.g. fire-and-forget emit).
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def __read_message(self, reader: asyncio.StreamReader) -> dict | None:
        """Read and decode a single ``length#payload`` frame.

        Returns the parsed message dict, or ``None`` if the peer closed the
        connection before a complete frame arrived. Raises ``ValueError`` on a
        malformed or oversized frame.
        """
        # Read the length prefix up to (and including) the '#' delimiter. This
        # tolerates the prefix being split across packets and never over-reads
        # into the payload. A missing '#' trips StreamReader's buffer limit
        # (64 KiB) and is rejected, capping a hostile/garbage prefix.
        try:
            prefix = await reader.readuntil(b"#")
        except asyncio.IncompleteReadError as e:
            # Clean EOF with no buffered bytes -> peer closed, nothing to handle.
            if not e.partial:
                return None
            raise ValueError("connection closed while reading length prefix") from e
        except asyncio.LimitOverrunError as e:
            raise ValueError("length prefix too long") from e

        prefix = prefix[:-1]  # strip trailing '#'
        try:
            msg_length = int(prefix.decode())
        except (ValueError, UnicodeDecodeError) as e:
            raise ValueError(f"invalid length prefix: {prefix!r}") from e

        if msg_length < 0 or msg_length > MAX_MESSAGE_LENGTH:
            raise ValueError(f"declared message length out of range: {msg_length}")

        # readexactly guarantees the full payload (or raises on short EOF),
        # unlike read(n) which may return fewer bytes.
        try:
            msg_data = await reader.readexactly(msg_length)
        except asyncio.IncompleteReadError as e:
            raise ValueError("connection closed before full payload received") from e

        return json.loads(msg_data.decode())

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
