"""Message chunking protocol for LoRa transmission.

Header format (8 bytes):
  Byte 0-1: MSG_ID  (uint16) - unique message identifier
  Byte 2:   SEQ     (uint8)  - chunk sequence number (0-indexed)
  Byte 3:   TOTAL   (uint8)  - total chunks in message
  Byte 4:   FLAGS   (uint8)  - bit flags
  Byte 5-7: RESERVED

FLAGS:
  Bit 0: COMPRESSED - payload is zlib-compressed
  Bit 1: FINAL      - last chunk
  Bit 2: ERROR      - error message
"""

import struct
import zlib
import threading
from typing import Dict, List

MAX_LORA_PAYLOAD = 228
HEADER_SIZE = 8
EFFECTIVE_PAYLOAD = MAX_LORA_PAYLOAD - HEADER_SIZE

FLAG_COMPRESSED = 0x01
FLAG_FINAL = 0x02
FLAG_ERROR = 0x04

_msg_id_counter = 0
_msg_id_lock = threading.Lock()


def _next_msg_id() -> int:
    """Get the next message ID (wraps at 65535)."""
    global _msg_id_counter
    with _msg_id_lock:
        _msg_id_counter = (_msg_id_counter + 1) % 65536
        return _msg_id_counter


def pack_header(msg_id: int, seq: int, total: int, flags: int) -> bytes:
    """Pack an 8-byte protocol header."""
    return struct.pack("!HBBB3x", msg_id, seq, total, flags)


def unpack_header(data: bytes) -> dict:
    """Unpack an 8-byte protocol header."""
    msg_id, seq, total, flags = struct.unpack("!HBBB3x", data[:HEADER_SIZE])
    return {
        "msg_id": msg_id,
        "seq": seq,
        "total": total,
        "flags": flags,
        "compressed": bool(flags & FLAG_COMPRESSED),
        "final": bool(flags & FLAG_FINAL),
        "error": bool(flags & FLAG_ERROR),
    }


def chunk_message(text: str, compression_enabled: bool = True, is_error: bool = False) -> List[bytes]:
    """Split a text message into LoRa-sized chunks with protocol headers.

    Returns a list of byte strings, each ready to send over Meshtastic.
    """
    raw_bytes = text.encode("utf-8")
    flags = 0

    if is_error:
        flags |= FLAG_ERROR

    # Try compression if enabled
    compressed = None
    if compression_enabled and len(raw_bytes) > EFFECTIVE_PAYLOAD:
        compressed = zlib.compress(raw_bytes, level=6)
        # Only use compression if it reduces chunk count
        raw_chunks = _count_chunks(raw_bytes)
        compressed_chunks = _count_chunks(compressed)
        if compressed_chunks < raw_chunks:
            flags |= FLAG_COMPRESSED
            payload = compressed
        else:
            payload = raw_bytes
    else:
        payload = raw_bytes

    # Split into chunks
    msg_id = _next_msg_id()
    chunks = []
    total = _count_chunks(payload)

    for i in range(total):
        start = i * EFFECTIVE_PAYLOAD
        end = start + EFFECTIVE_PAYLOAD
        chunk_data = payload[start:end]

        chunk_flags = flags
        if i == total - 1:
            chunk_flags |= FLAG_FINAL

        header = pack_header(msg_id, i, total, chunk_flags)
        chunks.append(header + chunk_data)

    return chunks


def reassemble_message(chunks: List[bytes]) -> str:
    """Reassemble a message from received chunks.

    Chunks should be sorted by sequence number.
    """
    if not chunks:
        return ""

    # Parse first header to get metadata
    header = unpack_header(chunks[0])
    compressed = header["compressed"]

    # Extract payloads
    payloads = []
    for chunk in sorted(chunks, key=lambda c: unpack_header(c)["seq"]):
        payloads.append(chunk[HEADER_SIZE:])

    combined = b"".join(payloads)

    if compressed:
        combined = zlib.decompress(combined)

    return combined.decode("utf-8")


def _count_chunks(data: bytes) -> int:
    """Calculate number of chunks needed for data."""
    return max(1, (len(data) + EFFECTIVE_PAYLOAD - 1) // EFFECTIVE_PAYLOAD)
