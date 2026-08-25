import base64
import struct
import zlib

import pytest

from test_screenshots import (
    _apply_css_shift_probe,
    _assert_browser_version,
    _disable_transient_rendering,
    _wait_for_stable_screenshot,
)
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


class FakeCdp:
    def __init__(self, product):
        self.product = product
        self.expressions = []

    def call(self, method, params=None, session=False):
        assert method == "Browser.getVersion"
        return {"product": self.product}

    def eval(self, expression):
        self.expressions.append(expression)
        return True


class FakeScreenshotCdp(FakeCdp):
    def __init__(self, frames):
        super().__init__("Chrome/151.0.7922.170")
        self.frames = iter(frames)
        self.captures = 0

    def call(self, method, params=None, session=False):
        if method == "Page.captureScreenshot":
            self.captures += 1
            return {"data": base64.b64encode(next(self.frames)).decode("ascii")}
        return super().call(method, params, session)


def test_png_mismatch_writes_a_readable_diff(tmp_path):
    baseline = _png(2, [bytes((255, 255, 255, 255)) * 2])
    actual = _png(2, [bytes((255, 0, 0, 255)) + bytes((255, 255, 255, 255))])
    diff = tmp_path / "metrics.diff.png"

    with pytest.raises(AssertionError, match=r"1 changed pixel"):
        assert_png_matches(actual, baseline, diff)

    assert diff.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_browser_version_mismatch_is_explicit():
    with pytest.raises(RuntimeError, match=r"Chrome version mismatch.*151\.0\.7922\.170"):
        _assert_browser_version(FakeCdp("Chrome/150.0.0.0"), "151.0.7922.170")


def test_transient_rendering_is_disabled_only_in_test_page():
    cdp = FakeCdp("Chrome/151.0.7922.170")

    _disable_transient_rendering(cdp)

    assert len(cdp.expressions) == 1
    assert "animation: none !important" in cdp.expressions[0]
    assert "transition: none !important" in cdp.expressions[0]
    assert "caret-color: transparent !important" in cdp.expressions[0]
    assert "color-scheme: light !important" in cdp.expressions[0]
    assert "outline: none" not in cdp.expressions[0]


def test_css_probe_is_a_test_only_one_pixel_shift():
    cdp = FakeCdp("Chrome/151.0.7922.170")

    _apply_css_shift_probe(cdp)

    assert "translateX(1px)" in cdp.expressions[0]
    assert "!important" in cdp.expressions[0]


def test_screenshot_stability_observes_a_late_changed_frame():
    warmup = _png(1, [bytes((1, 1, 1, 255))])
    early = _png(1, [bytes((2, 2, 2, 255))])
    settled = _png(1, [bytes((3, 3, 3, 255))])
    cdp = FakeScreenshotCdp([warmup, early, early, early] + [settled] * 9)

    assert _wait_for_stable_screenshot(cdp) == settled
    assert cdp.captures == 13
    assert "setTimeout(resolve, 500)" in cdp.expressions[0]
