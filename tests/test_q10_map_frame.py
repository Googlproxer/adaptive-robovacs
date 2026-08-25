"""Tests for the bounded, dependency-free Q10 map frame parser."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "q10_map_frame.py"
SPEC = importlib.util.spec_from_file_location("adaptive_robovacs_q10_map_frame", MODULE_PATH)
assert SPEC and SPEC.loader
frame = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = frame
SPEC.loader.exec_module(frame)


def _literal_lz4(value: bytes) -> bytes:
    """Encode a test-only raw LZ4 literal block."""

    if len(value) < 15:
        return bytes([len(value) << 4]) + value
    remainder = len(value) - 15
    extensions = bytearray()
    while remainder >= 255:
        extensions.append(255)
        remainder -= 255
    extensions.append(remainder)
    return b"\xf0" + bytes(extensions) + value


def _packet(*, width: int = 2, grid: bytes = b"\x04\x04\x08\x08") -> bytes:
    record = bytearray(47)
    record[0:2] = (1).to_bytes(2, "big")
    record[2:4] = (3).to_bytes(2, "little")
    record[26] = 7
    record[27:34] = b"Kitchen"
    layout = grid + b"\x01\x01" + bytes(record)
    compressed = _literal_lz4(layout)
    header = bytearray(29)
    header[0:2] = b"\x01\x01"
    header[2:6] = (1234).to_bytes(4, "big")
    header[7:9] = width.to_bytes(2, "big")
    header[9:11] = (2).to_bytes(2, "big")
    header[25:27] = len(layout).to_bytes(2, "big")
    header[27:29] = len(compressed).to_bytes(2, "big")
    return bytes(header) + compressed


class Q10MapFrameTests(unittest.TestCase):
    def test_parses_a_valid_bounded_map_and_renders_png(self) -> None:
        packet = _packet()
        result = frame.parse_q10_map_frame(packet)
        duplicate = frame.parse_q10_map_frame(packet)
        self.assertEqual(result.map_id, "1234")
        self.assertEqual((result.width, result.height), (2, 2))
        self.assertEqual(result.rooms[0].name, "Kitchen")
        self.assertEqual(result.rooms[0].pixel_count, 2)
        self.assertEqual(result.sha256, duplicate.sha256)
        self.assertTrue(frame.render_q10_map_preview(result).startswith(b"\x89PNG"))

    def test_rejects_non_frame_and_inconsistent_grid(self) -> None:
        with self.assertRaises(frame.Q10MapFrameError):
            frame.parse_q10_map_frame(b"not-a-map")
        with self.assertRaises(frame.Q10MapFrameError):
            frame.parse_q10_map_frame(_packet(width=100))

    def test_lz4_decoder_rejects_invalid_match_offset(self) -> None:
        with self.assertRaises(frame.Q10MapFrameError):
            frame._read_lz4_block(b"\x00\x01\x00", 4)

    def test_lz4_decoder_accepts_a_overlapping_match(self) -> None:
        self.assertEqual(
            frame._read_lz4_block(b"\x10A\x01\x00", 5), b"AAAAA"
        )

    def test_packet_limit_is_enforced_before_decoding(self) -> None:
        oversized = _packet() + b"\x00" * frame.MAX_PACKET_BYTES
        with self.assertRaises(frame.Q10MapFrameError):
            frame.parse_q10_map_frame(oversized)


if __name__ == "__main__":
    unittest.main()
