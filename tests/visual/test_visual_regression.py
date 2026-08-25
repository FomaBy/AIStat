import base64
import copy
import struct
import zlib

import pytest

import test_screenshots
from test_screenshots import (
    _apply_css_shift_probe,
    _assert_browser_version,
    _disable_transient_rendering,
    _set_viewport,
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
        self.screenshot_params = None
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
            self.screenshot_params = params
            frame = self.settled if self.paint_completed else self.early
            return {"data": base64.b64encode(frame).decode("ascii")}
        return super().call(method, params, session)


class FakeCaptureSurfaceCdp(FakeCdp):
    def __init__(self, geometries):
        super().__init__("Chrome/151.0.7922.170")
        self.geometries = iter(geometries)
        self.metrics = []

    def call(self, method, params=None, session=False):
        assert method == "Emulation.setDeviceMetricsOverride"
        self.metrics.append(params)
        return {}

    def eval(self, expression):
        self.expressions.append(expression)
        if "window.innerWidth" in expression:
            return next(self.geometries)
        return True

    def wait_for(self, expression):
        self.expressions.append(expression)
        return True


class FakeNativeControlCdp(FakeCdp):
    def __init__(self, controls, multiple_ids=None):
        super().__init__("Chrome/151.0.7922.170")
        self.controls = controls
        self.multiple_ids = (multiple_ids if multiple_ids is not None else
                             [control["id"] for control in controls])

    def eval(self, expression):
        self.expressions.append(expression)
        return {"controls": copy.deepcopy(self.controls),
                "multiple_ids": list(self.multiple_ids)}


def _native_controls():
    return [
        {
            "id": "filter-project", "matches": 1, "id_matches": 1,
            "rect": [45, 107.234375, 291, 172.234375], "value": "",
            "multiple": True, "disabled": False, "hidden": False,
            "display": "block", "visibility": "visible",
            "options": [["", "All projects", True, False],
                        ["P1", "Alpha", False, False],
                        ["P2", "Beta", False, False]],
        },
        {
            "id": "filter-agent", "matches": 1, "id_matches": 1,
            "rect": [45, 207.078125, 291, 289.078125], "value": "",
            "multiple": True, "disabled": False, "hidden": False,
            "display": "block", "visibility": "visible",
            "options": [["", "All agents", True, False],
                        ["A2", "Dev Shared", False, False],
                        ["A3", "QA Shared", False, False],
                        ["A1", "Solo Claude", False, False]],
        },
        {
            "id": "filter-model", "matches": 1, "id_matches": 1,
            "rect": [45, 323.921875, 291, 405.921875], "value": "",
            "multiple": True, "disabled": False, "hidden": False,
            "display": "block", "visibility": "visible",
            "options": [["", "All models", True, False],
                        ["m-claude", "m-claude", False, False],
                        ["m-mystery", "m-mystery", False, False],
                        ["m-shared", "m-shared", False, False]],
        },
    ]


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
    assert cdp.screenshot_params == {
        "clip": {"x": 0, "y": 0, "width": 1440, "height": 1000,
                 "scale": 1},
        "captureBeyondViewport": False,
    }


def test_screenshot_stability_fails_closed_without_causal_repaint():
    early = _png(1, [bytes((2, 2, 2, 255))])
    settled = _png(1, [bytes((3, 3, 3, 255))])
    cdp = FakeCausalPaintCdp(early, settled, completes=False)

    with pytest.raises(AssertionError, match="paint completion"):
        _wait_for_stable_screenshot(cdp)

    assert cdp.captures == 0


@pytest.mark.parametrize(("initial", "final", "metrics_width"), [
    (
        {"viewportWidth": 1440, "viewportHeight": 1000,
         "clientWidth": 1440, "clientHeight": 1000,
         "rootScrollWidth": 1440, "bodyScrollWidth": 1440,
         "scrollbarWidth": 0},
        {"viewportWidth": 1440, "viewportHeight": 1000,
         "clientWidth": 1440, "clientHeight": 1000,
         "rootScrollWidth": 1440, "bodyScrollWidth": 1440,
         "scrollbarWidth": 0},
        1440,
    ),
    (
        {"viewportWidth": 1440, "viewportHeight": 1000,
         "clientWidth": 1425, "clientHeight": 1000,
         "rootScrollWidth": 1425, "bodyScrollWidth": 1425,
         "scrollbarWidth": 15},
        {"viewportWidth": 1455, "viewportHeight": 1000,
         "clientWidth": 1440, "clientHeight": 1000,
         "rootScrollWidth": 1440, "bodyScrollWidth": 1440,
         "scrollbarWidth": 15},
        1455,
    ),
])
def test_capture_surface_keeps_1440_pixel_client_area(
        initial, final, metrics_width):
    cdp = FakeCaptureSurfaceCdp([initial, final])

    _set_viewport(cdp)
    assert test_screenshots._normalize_capture_surface(cdp, "ready") == final

    assert cdp.metrics == [
        {"width": 1440, "height": 1000, "deviceScaleFactor": 1,
         "mobile": False},
        {"width": metrics_width, "height": 1000, "deviceScaleFactor": 1,
         "mobile": False},
    ]


def test_capture_surface_fails_if_remeasurement_loses_application_pixel():
    cdp = FakeCaptureSurfaceCdp([
        {"viewportWidth": 1440, "viewportHeight": 1000,
         "clientWidth": 1425, "clientHeight": 1000,
         "rootScrollWidth": 1425, "bodyScrollWidth": 1425,
         "scrollbarWidth": 15},
        {"viewportWidth": 1455, "viewportHeight": 1000,
         "clientWidth": 1439, "clientHeight": 1000,
         "rootScrollWidth": 1439, "bodyScrollWidth": 1439,
         "scrollbarWidth": 16},
    ])

    _set_viewport(cdp)

    with pytest.raises(AssertionError, match="capture surface geometry"):
        test_screenshots._normalize_capture_surface(cdp, "ready")

    assert cdp.metrics[-1]["width"] == 1455


def test_native_control_contract_returns_only_documented_interiors():
    contract = test_screenshots._native_control_contract(
        FakeNativeControlCdp(_native_controls()), "metrics")

    assert contract["masks"] == [
        (46, 108, 290, 171),
        (46, 208, 290, 288),
        (46, 325, 290, 405),
    ]
    assert contract["excluded_pixels"] == 54412
    assert len(contract["semantic_digest"]) == 64


@pytest.mark.parametrize("mutation", [
    pytest.param(lambda controls: controls.pop(), id="missing"),
    pytest.param(lambda controls: controls[0].__setitem__("matches", 2),
                 id="duplicate"),
    pytest.param(lambda controls: controls[0].__setitem__("hidden", True),
                 id="hidden"),
    pytest.param(lambda controls: controls[0].__setitem__("options", []),
                 id="empty"),
    pytest.param(lambda controls: controls[0]["rect"].__setitem__(0, 46),
                 id="moved"),
    pytest.param(lambda controls: controls[0]["rect"].__setitem__(2, 292),
                 id="resized"),
])
def test_native_control_contract_fails_closed_for_invalid_control(mutation):
    controls = _native_controls()
    mutation(controls)

    with pytest.raises(AssertionError, match="native control"):
        test_screenshots._native_control_contract(
            FakeNativeControlCdp(controls), "metrics")


def test_native_control_contract_rejects_extra_multiple_select():
    controls = _native_controls()

    with pytest.raises(AssertionError, match="native control"):
        test_screenshots._native_control_contract(
            FakeNativeControlCdp(controls, [
                "filter-project", "filter-agent", "filter-model", "extra",
            ]), "metrics")


def test_native_control_contract_rejects_duplicate_id():
    controls = _native_controls()
    controls[0]["id_matches"] = 2

    with pytest.raises(AssertionError, match="native control"):
        test_screenshots._native_control_contract(
            FakeNativeControlCdp(controls), "metrics")


@pytest.mark.parametrize("index", range(3))
def test_native_control_contract_rejects_semantic_mutation(index):
    controls = _native_controls()
    controls[index]["options"][0][1] = "changed"

    with pytest.raises(AssertionError, match="native control"):
        test_screenshots._native_control_contract(
            FakeNativeControlCdp(controls), "metrics")


def test_native_control_masks_reject_overlap():
    controls = _native_controls()
    controls[1]["rect"] = [45, 120, 291, 201]

    with pytest.raises(AssertionError, match="overlap"):
        test_screenshots._native_control_masks(controls)


@pytest.mark.parametrize("changed", [(3, 4), (4, 3), (6, 4), (4, 6)])
def test_native_control_comparison_rejects_pixel_adjacent_to_mask(
        tmp_path, changed):
    baseline = _png(10, [bytes((255, 255, 255, 255)) * 10] * 10)
    rows = [bytearray(bytes((255, 255, 255, 255)) * 10) for _ in range(10)]
    x, y = changed
    rows[y][x * 4:x * 4 + 4] = bytes((255, 0, 0, 255))
    actual = _png(10, [bytes(row) for row in rows])

    with pytest.raises(AssertionError, match="non-native"):
        test_screenshots._assert_native_control_pixels_match(
            actual, baseline, [(4, 4, 6, 6)], tmp_path / "diff.png")


def test_native_control_comparison_ignores_only_the_verified_interior(tmp_path):
    baseline = _png(10, [bytes((255, 255, 255, 255)) * 10] * 10)
    row = (bytes((255, 255, 255, 255)) * 4 + bytes((255, 0, 0, 255)) +
           bytes((255, 255, 255, 255)) * 5)
    actual = _png(10, [row if index == 4 else
                       bytes((255, 255, 255, 255)) * 10
                       for index in range(10)])

    assert test_screenshots._assert_native_control_pixels_match(
        actual, baseline, [(4, 4, 6, 6)], tmp_path / "diff.png") == 0
