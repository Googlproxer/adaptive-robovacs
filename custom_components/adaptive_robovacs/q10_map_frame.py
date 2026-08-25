"""Bounded parser and preview renderer for Q10/B01 map frames.

The Q10 map stream is not a public Home Assistant API.  This module deliberately
keeps its decoder small: it understands the observed ``01 01`` grid packet
enough to validate and preview it, while retaining the original bytes for any
future decoder.  It does not attempt to mutate or reconstruct a robot map.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct
import zlib


MAX_PACKET_BYTES = 512 * 1024
MAX_GRID_CELLS = 1_000_000
MAX_ROOM_NAME_BYTES = 128


class Q10MapFrameError(ValueError):
    """Raised when an untrusted Q10 map frame is malformed or oversized."""


@dataclass(frozen=True, slots=True)
class Q10MapRoom:
    """The safe, observed portion of a Q10 room metadata record."""

    room_id: int
    name: str
    order_hint: int
    pixel_count: int


@dataclass(frozen=True, slots=True)
class Q10MapFrame:
    """Decoded metadata and the original immutable Q10 full-map packet."""

    map_id: str
    width: int
    height: int
    grid: bytes
    rooms: tuple[Q10MapRoom, ...]
    packet: bytes
    sha256: str


def _u16be(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise Q10MapFrameError("truncated unsigned 16-bit value")
    return int.from_bytes(data[offset : offset + 2], "big")


def _u16le(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise Q10MapFrameError("truncated little-endian unsigned 16-bit value")
    return int.from_bytes(data[offset : offset + 2], "little")


def _read_lz4_block(data: bytes, expected_size: int | None = None) -> bytes:
    """Decode a raw LZ4 block with strict bounds and output limits.

    Q10 packets carry raw LZ4 blocks rather than a frame container.  The
    implementation follows the public LZ4 block format and is intentionally
    original so this integration has no additional runtime dependency.
    """

    maximum_size = expected_size if expected_size is not None else MAX_GRID_CELLS + 64 * 1024
    if maximum_size < 0 or maximum_size > MAX_GRID_CELLS + 64 * 1024:
        raise Q10MapFrameError("declared map block is too large")
    output = bytearray()
    cursor = 0
    while cursor < len(data):
        token = data[cursor]
        cursor += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                if cursor >= len(data):
                    raise Q10MapFrameError("truncated LZ4 literal length")
                extension = data[cursor]
                cursor += 1
                literal_length += extension
                if extension != 255:
                    break
        if cursor + literal_length > len(data):
            raise Q10MapFrameError("truncated LZ4 literals")
        output.extend(data[cursor : cursor + literal_length])
        cursor += literal_length
        if len(output) > maximum_size:
            raise Q10MapFrameError("LZ4 output exceeds the safe map limit")
        if cursor == len(data):
            break
        if cursor + 2 > len(data):
            raise Q10MapFrameError("truncated LZ4 match offset")
        offset = int.from_bytes(data[cursor : cursor + 2], "little")
        cursor += 2
        if offset == 0 or offset > len(output):
            raise Q10MapFrameError("invalid LZ4 match offset")
        match_length = token & 0x0F
        if match_length == 15:
            while True:
                if cursor >= len(data):
                    raise Q10MapFrameError("truncated LZ4 match length")
                extension = data[cursor]
                cursor += 1
                match_length += extension
                if extension != 255:
                    break
        match_length += 4
        if len(output) + match_length > maximum_size:
            raise Q10MapFrameError("LZ4 match exceeds the safe map limit")
        start = len(output) - offset
        for _ in range(match_length):
            output.append(output[start])
            start += 1
    if expected_size is not None and len(output) != expected_size:
        raise Q10MapFrameError("LZ4 output does not match declared map size")
    return bytes(output)


def _parse_rooms(room_data: bytes, grid: bytes) -> tuple[Q10MapRoom, ...]:
    """Parse the bounded room table after the known-size occupancy grid."""

    if not room_data:
        return ()
    if len(room_data) < 2 or room_data[0] != 1:
        return ()
    count = room_data[1]
    required = 2 + count * 47
    if required > len(room_data):
        raise Q10MapFrameError("truncated Q10 room metadata")
    rooms: list[Q10MapRoom] = []
    for index in range(count):
        offset = 2 + index * 47
        record = room_data[offset : offset + 47]
        room_id = _u16be(record, 0)
        order_hint = _u16le(record, 2)
        name_length = min(record[26], MAX_ROOM_NAME_BYTES, len(record) - 27)
        name = record[27 : 27 + name_length].decode("ascii", "replace").strip("\x00 ")
        pixel_value = (room_id * 4) & 0xFF
        rooms.append(
            Q10MapRoom(
                room_id=room_id,
                name=name or f"Room {room_id}",
                order_hint=order_hint,
                pixel_count=grid.count(pixel_value),
            )
        )
    return tuple(rooms)


def parse_q10_map_frame(packet: bytes) -> Q10MapFrame:
    """Validate and decode one complete ``01 01`` Q10 map packet."""

    if not isinstance(packet, bytes):
        raise Q10MapFrameError("map packet is not bytes")
    if len(packet) > MAX_PACKET_BYTES:
        raise Q10MapFrameError("map packet is too large")
    if len(packet) < 29 or packet[:2] != b"\x01\x01":
        raise Q10MapFrameError("not a Q10 full-map packet")
    map_id = str(int.from_bytes(packet[2:6], "big"))
    # The Q10 wire header stores two consecutive big-endian dimensions at
    # offsets 7 (width) and 9 (height). Deriving height by scanning for a room
    # marker can misread an ordinary grid cell as metadata.
    width = _u16be(packet, 7)
    height = _u16be(packet, 9)
    compressed_layout = _u16be(packet, 27)
    grid_size = width * height
    if (
        width == 0
        or height == 0
        or grid_size > MAX_GRID_CELLS
    ):
        raise Q10MapFrameError("invalid Q10 map dimensions")
    layout_start = 29
    layout_end = layout_start + compressed_layout
    if layout_end > len(packet):
        raise Q10MapFrameError("truncated Q10 compressed layout")
    # Bytes 25-26 are not a documented uncompressed-length field on the
    # current Q10 transport. Treat them as opaque header data: the bounded LZ4
    # decoder and the dimensions below provide the actual safety proof.
    layout = _read_lz4_block(packet[layout_start:layout_end])
    grid = layout[:grid_size]
    if len(grid) != grid_size:
        raise Q10MapFrameError("truncated Q10 map grid")
    rooms = _parse_rooms(layout[grid_size:], grid)
    return Q10MapFrame(
        map_id=map_id,
        width=width,
        height=height,
        grid=grid,
        rooms=rooms,
        packet=packet,
        sha256=hashlib.sha256(packet).hexdigest(),
    )


def _png_chunk(kind: bytes, value: bytes) -> bytes:
    return (
        struct.pack(">I", len(value))
        + kind
        + value
        + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF)
    )


def render_q10_map_preview(frame: Q10MapFrame) -> bytes:
    """Render a deterministic, dependency-free RGB PNG for one map frame."""

    # Room IDs are encoded as room_id * 4.  Use a repeatable muted palette;
    # non-room pixels remain dark so room outlines survive small previews.
    rows = bytearray()
    for row in range(frame.height):
        rows.append(0)  # PNG filter type: None.
        for pixel in frame.grid[row * frame.width : (row + 1) * frame.width]:
            if pixel in {0, 243, 249}:
                rows.extend((28, 34, 42))
            elif pixel % 4 == 0:
                seed = pixel // 4
                rows.extend(((53 * seed + 75) % 176 + 48, (97 * seed + 45) % 160 + 48, (149 * seed + 15) % 144 + 64))
            else:
                rows.extend((92, 103, 117))
    header = struct.pack(">IIBBBBB", frame.width, frame.height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(
        b"IDAT", zlib.compress(bytes(rows), level=9)
    ) + _png_chunk(b"IEND", b"")
