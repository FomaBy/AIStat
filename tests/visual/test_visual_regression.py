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


class FakeCausalPaintCdp(FakeCdp):
    def __init__(self, early, settled, *, completes=True):
        super().__init__("Chrome/151.0.7922.170")
        self.early = early
        self.settled = settled
        self.completes = completes
        self.paint_completed = False
        self.captures = 0
        self.deadlines = []
        self._events = []

    def _read_message(self, deadline):
        return self._events.pop(0)

    def call(self, method, params=None, session=False, deadline=None):
        self.deadlines.append(deadline)
        if method in ("LayerTree.enable", "LayerTree.disable"):
            return {}
        if method == "Runtime.evaluate":
            expression = params["expression"]
            self.expressions.append(expression)
            if "willChange =" in expression and "removeProperty" not in expression:
                paint_count = 1
            elif "removeProperty" in expression:
                paint_count = 2 if self.completes else 1
                self.paint_completed = self.completes
            else:
                raise TimeoutError("paint completion signal did not arrive")
            self._events.append({
                "method": "LayerTree.layerTreeDidChange",
                "params": {"layers": [{
                    "layerId": "document",
                    "drawsContent": True,
                    "paintCount": paint_count,
                }]},
            })
            self._read_message(deadline)
            return {"result": {"value": True}}
        if method == "Page.captureScreenshot":
            self.captures += 1
            frame = self.settled if self.paint_completed else self.early
            return {"data": base64.b64encode(frame).decode("ascii")}
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


def test_screenshot_stability_waits_for_causal_repaint_after_equal_early_frames():
    early = _png(1, [bytes((2, 2, 2, 255))])
    settled = _png(1, [bytes((3, 3, 3, 255))])
    cdp = FakeCausalPaintCdp(early, settled)

    assert _wait_for_stable_screenshot(cdp) == settled
    assert cdp.captures == 1
    assert cdp.paint_completed
    assert all(deadline is not None for deadline in cdp.deadlines)
    assert not any("setTimeout" in expression for expression in cdp.expressions)


def test_screenshot_stability_fails_closed_without_causal_repaint():
    early = _png(1, [bytes((2, 2, 2, 255))])
    settled = _png(1, [bytes((3, 3, 3, 255))])
    cdp = FakeCausalPaintCdp(early, settled, completes=False)

    with pytest.raises(AssertionError, match="paint completion"):
        _wait_for_stable_screenshot(cdp)

    assert cdp.captures == 0
