import struct
import zlib

import pytest

from visual_regression import assert_png_matches


def _png(width, rows):
    raw = b"".join(b"\0" + row for row in rows)

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, len(rows), 8,
                                         6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_png_mismatch_writes_a_readable_diff(tmp_path):
    baseline = _png(2, [bytes((255, 255, 255, 255)) * 2])
    actual = _png(2, [bytes((255, 0, 0, 255)) + bytes((255, 255, 255, 255))])
    diff = tmp_path / "metrics.diff.png"

    with pytest.raises(AssertionError, match=r"1 changed pixel"):
        assert_png_matches(actual, baseline, diff)

    assert diff.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
