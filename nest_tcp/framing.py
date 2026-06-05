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


def utf16_length(s: str) -> int:
    """Number of UTF-16 code units in ``s`` (what JS ``String.length`` returns)."""
    # encode to UTF-16LE without BOM: 2 bytes per code unit.
    return len(s.encode("utf-16-le")) // 2


def encode_frame(payload: str) -> bytes:
    """Frame a JSON string the way NestJS expects: ``<utf16len>#<utf8 bytes>``."""
    body = payload.encode("utf-8")
    return f"{utf16_length(payload)}{DELIMITER.decode()}".encode("ascii") + body


def utf16_byte_boundary(buf: bytes, target_units: int):
    """Find where ``target_units`` UTF-16 code units end inside a UTF-8 buffer.

    Returns the byte length of the prefix of ``buf`` that decodes to exactly
    ``target_units`` UTF-16 code units, or ``None`` if ``buf`` does not yet
    contain that many complete units (caller should read more bytes).

    Walks UTF-8 lead bytes rather than fully decoding, so it is cheap and
    correctly handles a multi-byte sequence split across the buffer's tail.
    """
    if target_units <= 0:
        return 0
    units = 0
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b < 0x80:        # 1-byte ASCII -> 1 UTF-16 unit
            step, u = 1, 1
        elif b < 0xC0:      # 0x80-0xBF: stray continuation byte -> invalid
            raise ValueError("invalid UTF-8 lead byte in frame")
        elif b < 0xE0:      # 2-byte sequence -> 1 unit
            step, u = 2, 1
        elif b < 0xF0:      # 3-byte sequence -> 1 unit
            step, u = 3, 1
        elif b < 0xF8:      # 4-byte sequence -> 2 units (surrogate pair)
            step, u = 4, 2
        else:
            raise ValueError("invalid UTF-8 lead byte in frame")

        if i + step > n:
            # Incomplete trailing multi-byte sequence; need more bytes.
            return None
        if units + u > target_units:
            # The boundary would fall inside an astral char's surrogate pair.
            # A well-formed sender never frames mid-code-point, so treat this
            # as a corrupt length rather than splitting the character.
            raise ValueError("frame length splits a surrogate pair")
        units += u
        i += step
        if units == target_units:
            return i
    return None  # ran out of bytes before reaching target_units
