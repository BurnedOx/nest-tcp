"""Wire framing compatible with NestJS's ``JsonSocket``.

NestJS frames each message as ``<length>#<json>`` where:

* ``<length>`` is ``JSON.stringify(message).length`` — the number of **UTF-16
  code units** in the (raw, unescaped) JSON string, NOT the byte count and NOT
  the Unicode code-point count. A character outside the BMP (e.g. an emoji)
  counts as 2 units (a surrogate pair).
* the body is written/read as **UTF-8 bytes**.
* on read, NestJS decodes the bytes to a string and slices the message with
  ``buffer.substring(0, length)`` — i.e. the boundary is measured in UTF-16
  code units.

To interoperate we must therefore measure length in UTF-16 units while moving
UTF-8 bytes on the wire. For pure-ASCII payloads (e.g. ``json.dumps`` with its
default ``ensure_ascii=True``) all three measures coincide, so this is wire-
identical to a naive byte-length implementation; the difference only matters
for non-ASCII content sent by a real NestJS peer.

Reference: nestjs/nest packages/microservices/helpers/json-socket.ts
"""

DELIMITER = b"#"
MAX_PREFIX_DIGITS = 20  # longest length prefix accepted before the delimiter


def utf16_length(s: str) -> int:
    """Number of UTF-16 code units in ``s`` (what JS ``String.length`` returns)."""
    # encode to UTF-16LE without BOM: 2 bytes per code unit.
    return len(s.encode("utf-16-le")) // 2


def encode_frame(payload: str) -> bytes:
    """Frame a JSON string the way NestJS expects: ``<utf16len>#<utf8 bytes>``."""
    body = payload.encode("utf-8")
    return f"{utf16_length(payload)}{DELIMITER.decode()}".encode("ascii") + body


def _char_size(lead: int) -> int:
    """Byte length of the UTF-8 sequence that starts with ``lead``."""
    if lead < 0x80:
        return 1
    if 0xC0 <= lead < 0xE0:
        return 2
    if 0xE0 <= lead < 0xF0:
        return 3
    if 0xF0 <= lead < 0xF8:
        return 4
    # 0x80-0xBF is a stray continuation byte; 0xF8+ never appears in UTF-8.
    raise ValueError("invalid UTF-8 in frame")


class FrameDecoder:
    """Incremental decoder for a stream of ``<length>#<json>`` frames.

    Feed it bytes as they arrive and pull complete payloads out with
    :meth:`next_frame`. Each byte is examined once however the stream is
    chunked, and bytes past the end of a frame are kept for the next one, so
    several frames may share a connection.

    ``max_length`` caps both a frame's declared length and its size in bytes.
    """

    def __init__(self, max_length: int | None = None):
        self._max_length = max_length
        self._buf = bytearray()
        self._target: int | None = None  # declared UTF-16 units of the frame being read
        self._pos = 0    # body bytes of that frame scanned so far
        self._units = 0  # UTF-16 units those bytes decode to

    @property
    def mid_frame(self) -> bool:
        """True while part of a frame has arrived but not all of it."""
        return self._target is not None or bool(self._buf)

    def feed(self, data: bytes) -> None:
        self._buf += data

    def next_frame(self) -> str | None:
        """Return the next complete payload, or ``None`` if more bytes are needed.

        Raises ``ValueError`` on a malformed or oversized frame; the stream is
        then no longer frame-aligned and the decoder should be discarded.
        """
        if self._target is None and not self._read_prefix():
            return None
        if not self._scan_body():
            return None
        payload = self._buf[:self._pos].decode("utf-8")
        del self._buf[:self._pos]
        self._target = None
        self._pos = self._units = 0
        return payload

    def _read_prefix(self) -> bool:
        """Consume ``<length>#`` from the buffer; False if it isn't all there yet."""
        buf = self._buf
        end = buf.find(DELIMITER, 0, MAX_PREFIX_DIGITS + 1)
        if end < 0:
            head = bytes(buf[:MAX_PREFIX_DIGITS + 1])
            if head and not head.isdigit():
                raise ValueError(f"invalid length prefix: {head!r}")
            if len(head) > MAX_PREFIX_DIGITS:
                raise ValueError("length prefix too long")
            return False

        prefix = bytes(buf[:end])
        if not prefix.isdigit():
            raise ValueError(f"invalid length prefix: {prefix!r}")
        length = int(prefix)
        if self._max_length is not None and length > self._max_length:
            raise ValueError(f"declared message length out of range: {length}")
        del buf[:end + 1]
        self._target = length
        return True

    def _scan_body(self) -> bool:
        """Advance over buffered body bytes; True once the whole body is in."""
        buf = self._buf
        while self._units < self._target:
            need = self._target - self._units
            start = self._pos
            # ``need`` code units occupy at least ``need`` bytes, so this slice
            # cannot run past the end of the frame.
            end = min(start + need, len(buf))
            if end == start:
                return False

            # The slice may cut a multi-byte character in half. Back up to the
            # lead byte of its last character and leave that character for the
            # next round if it is incomplete.
            last = end - 1
            while last > start and end - last < 4 and (buf[last] & 0xC0) == 0x80:
                last -= 1
            size = _char_size(buf[last])
            if last + size > end:
                end = last
                if end == start:
                    # Nothing but that one character is left in the slice:
                    # take it whole once all of its bytes have arrived.
                    end = start + size
                    if end > len(buf):
                        return False

            chunk = buf[start:end]
            if chunk.isascii():
                units = len(chunk)
            else:
                try:
                    units = utf16_length(chunk.decode("utf-8"))
                except UnicodeDecodeError as e:
                    raise ValueError("invalid UTF-8 in frame") from e
            if units > need:
                # The boundary would fall inside an astral char's surrogate
                # pair. A well-formed sender never frames mid-code-point, so
                # treat this as a corrupt length rather than splitting it.
                raise ValueError("frame length splits a surrogate pair")
            self._pos = end
            self._units += units
            if self._max_length is not None and self._pos > self._max_length:
                raise ValueError("message too large")
        return True
