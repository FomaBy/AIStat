import struct
import zlib
from pathlib import Path


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(left, above, upper_left):
    estimate = left + above - upper_left
    distances = (abs(estimate - left), abs(estimate - above),
                 abs(estimate - upper_left))
    return (left, above, upper_left)[distances.index(min(distances))]


def _unfilter(raw, width, height, channels):
    stride = width * channels
    rows = []
    offset = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        encoded = raw[offset:offset + stride]
        offset += stride
        row = bytearray(stride)
        for index, value in enumerate(encoded):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                row[index] = value
            elif filter_type == 1:
                row[index] = (value + left) & 0xff
            elif filter_type == 2:
                row[index] = (value + above) & 0xff
            elif filter_type == 3:
                row[index] = (value + (left + above) // 2) & 0xff
            elif filter_type == 4:
                row[index] = (value + _paeth(left, above, upper_left)) & 0xff
            else:
                raise ValueError("unsupported PNG filter %d" % filter_type)
        rows.append(row)
        previous = row
    return rows


def _decode_png(data):
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG image")
    offset = len(_PNG_SIGNATURE)
    header = None
    payload = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        chunk = data[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            payload.extend(chunk)
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError("PNG has no IHDR")
    width, height, bit_depth, color_type, compression, filtering, interlace = header
    if bit_depth != 8 or compression != 0 or filtering != 0 or interlace != 0:
        raise ValueError("unsupported PNG encoding")
    if color_type not in (2, 6):
        raise ValueError("unsupported PNG color type %d" % color_type)
    channels = 3 if color_type == 2 else 4
    rows = _unfilter(zlib.decompress(payload), width, height, channels)
    rgba = bytearray()
    for row in rows:
        if channels == 4:
            rgba.extend(row)
        else:
            for index in range(0, len(row), 3):
                rgba.extend(row[index:index + 3])
                rgba.append(255)
    return width, height, rgba


def _chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload +
            struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))


def _encode_png(width, height, rgba):
    rows = []
    stride = width * 4
    for offset in range(0, len(rgba), stride):
        rows.append(b"\0" + rgba[offset:offset + stride])
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (_PNG_SIGNATURE + _chunk(b"IHDR", header) +
            _chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) +
            _chunk(b"IEND", b""))


def assert_png_matches(actual, baseline, diff_path):
    """Compare two Chrome PNG screenshots and emit a red-pixel diff on failure."""
    actual_image = _decode_png(actual)
    baseline_image = _decode_png(baseline)
    if actual_image[:2] != baseline_image[:2]:
        width, height, _ = actual_image
        diff_path = Path(diff_path)
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_bytes(_encode_png(
            width, height, b"\xff\x00\x00\xff" * (width * height)))
        raise AssertionError(
            "screenshot dimensions differ: actual=%sx%s, baseline=%sx%s; "
            "diff artifact: %s" %
            (actual_image[0], actual_image[1], baseline_image[0],
             baseline_image[1], diff_path))

    width, height, actual_pixels = actual_image
    _, _, baseline_pixels = baseline_image
    changed = sum(
        actual_pixels[offset:offset + 4] != baseline_pixels[offset:offset + 4]
        for offset in range(0, len(actual_pixels), 4)
    )
    if not changed:
        return

    diff = bytearray(b"\xff\xff\xff\xff" * (width * height))
    for offset in range(0, len(actual_pixels), 4):
        if actual_pixels[offset:offset + 4] != baseline_pixels[offset:offset + 4]:
            diff[offset:offset + 4] = b"\xff\x00\x00\xff"
    diff_path = Path(diff_path)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_bytes(_encode_png(width, height, diff))
    raise AssertionError(
        "%d changed pixel%s; diff artifact: %s" %
        (changed, "" if changed == 1 else "s", diff_path))
