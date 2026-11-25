# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import unittest

from fontTools.pens.qu2cuPen import Qu2CuPen
from fontTools.pens.recordingPen import RecordingPen
from textwrap import dedent
import pytest

try:
    from .utils import CUBIC_GLYPHS, QUAD_GLYPHS
    from .utils import DummyGlyph
    from .utils import DummyPen
except ImportError as e:
    pytest.skip(str(e), allow_module_level=True)

MAX_ERR = 1.0


def _midpoint(a, b):
    return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)


def _eval_quadratic(p0, p1, p2, t):
    mt = 1 - t
    return (
        mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
        mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1],
    )


def _eval_cubic(p0, p1, p2, p3, t):
    mt = 1 - t
    return (
        mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0],
        mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1],
    )


def _quad_blob_reference(offcurves, samples):
    """Dense points tracing the TrueType geometry of a contour made entirely
    of off-curve points, using the implicit on-curve midpoint rule."""
    n = len(offcurves)
    start = _midpoint(offcurves[-1], offcurves[0])
    points = []
    previous = start
    for i in range(n):
        on = _midpoint(offcurves[i], offcurves[(i + 1) % n])
        for j in range(samples):
            points.append(_eval_quadratic(previous, offcurves[i], on, j / samples))
        previous = on
    points.append(start)
    return start, points


def _recorded_points(recording, samples):
    """Dense geometric points of a recorded cubic path, chaining each
    segment from the end point of the previous one."""
    points = []
    current = None
    for op, args in recording:
        if op == "moveTo":
            current = args[0]
            points.append(current)
        elif op == "lineTo":
            for j in range(1, samples + 1):
                t = j / samples
                points.append(
                    (
                        current[0] + t * (args[0][0] - current[0]),
                        current[1] + t * (args[0][1] - current[1]),
                    )
                )
            current = args[0]
        elif op == "curveTo":
            p1, p2, p3 = args
            for j in range(1, samples + 1):
                points.append(_eval_cubic(current, p1, p2, p3, j / samples))
            current = p3
        elif op == "qCurveTo":
            raise AssertionError("all_cubic output must not contain qCurveTo")
    return points


def _hausdorff(a, b):
    def one_way(x, y):
        return max(
            min((px[0] - qx[0]) ** 2 + (px[1] - qx[1]) ** 2 for qx in y) ** 0.5
            for px in x
        )

    return max(one_way(a, b), one_way(b, a))


def _convert(contour, max_err=MAX_ERR, **kwargs):
    outPen = RecordingPen()
    pen = Qu2CuPen(outPen, max_err, all_cubic=True, **kwargs)
    for op, args in contour:
        getattr(pen, op)(*args)
    return outPen


class _TestPenMixin(object):
    """Collection of tests that are shared by both the SegmentPen and the
    PointPen test cases, plus some helper methods.
    Note: We currently don't have a PointPen.
    """

    maxDiff = None

    def diff(self, expected, actual):
        import difflib

        expected = str(self.Glyph(expected)).splitlines(True)
        actual = str(self.Glyph(actual)).splitlines(True)
        diff = difflib.unified_diff(
            expected, actual, fromfile="expected", tofile="actual"
        )
        return "".join(diff)

    def convert_glyph(self, glyph, **kwargs):
        # draw source glyph onto a new glyph using a Cu2Qu pen and return it
        converted = self.Glyph()
        pen = getattr(converted, self.pen_getter_name)()
        cubicpen = self.Qu2CuPen(pen, MAX_ERR, all_cubic=True, **kwargs)
        getattr(glyph, self.draw_method_name)(cubicpen)
        return converted

    def expect_glyph(self, source, expected):
        converted = self.convert_glyph(source)
        self.assertNotEqual(converted, source)
        if not converted.approx(expected):
            print(self.diff(expected, converted))
            self.fail("converted glyph is different from expected")

    def test_convert_simple_glyph(self):
        self.expect_glyph(QUAD_GLYPHS["a"], CUBIC_GLYPHS["a"])
        self.expect_glyph(QUAD_GLYPHS["A"], CUBIC_GLYPHS["A"])

    def test_convert_composite_glyph(self):
        source = CUBIC_GLYPHS["Aacute"]
        converted = self.convert_glyph(source)
        # components don't change after quadratic conversion
        self.assertEqual(converted, source)

    def test_reverse_direction(self):
        for name in ("a", "A", "Eacute"):
            source = QUAD_GLYPHS[name]
            normal_glyph = self.convert_glyph(source)
            reversed_glyph = self.convert_glyph(source, reverse_direction=True)

            # the number of commands is the same, just their order is iverted
            self.assertTrue(len(normal_glyph.outline), len(reversed_glyph.outline))
            self.assertNotEqual(normal_glyph, reversed_glyph)

    def test_stats(self):
        stats = {}
        for name in QUAD_GLYPHS.keys():
            source = QUAD_GLYPHS[name]
            self.convert_glyph(source, stats=stats)

        self.assertTrue(stats)
        self.assertTrue("2" in stats)
        self.assertEqual(type(stats["2"]), int)

    def test_addComponent(self):
        pen = self.Pen()
        cubicpen = self.Qu2CuPen(pen, MAX_ERR)
        cubicpen.addComponent("a", (1, 2, 3, 4, 5.0, 6.0))

        # components are passed through without changes
        self.assertEqual(
            str(pen).splitlines(),
            [
                "pen.addComponent('a', (1, 2, 3, 4, 5.0, 6.0))",
            ],
        )


class TestQu2CuPen(unittest.TestCase, _TestPenMixin):
    def __init__(self, *args, **kwargs):
        super(TestQu2CuPen, self).__init__(*args, **kwargs)
        self.Glyph = DummyGlyph
        self.Pen = DummyPen
        self.Qu2CuPen = Qu2CuPen
        self.pen_getter_name = "getPen"
        self.draw_method_name = "draw"

    def test_qCurveTo_1_point(self):
        pen = DummyPen()
        cubicpen = Qu2CuPen(pen, MAX_ERR)
        cubicpen.moveTo((0, 0))
        cubicpen.qCurveTo((1, 1))
        cubicpen.closePath()

        self.assertEqual(
            str(pen).splitlines(),
            [
                "pen.moveTo((0, 0))",
                "pen.qCurveTo((1, 1))",
                "pen.closePath()",
            ],
        )

    def test_qCurveTo_2_points(self):
        pen = DummyPen()
        cubicpen = Qu2CuPen(pen, MAX_ERR)
        cubicpen.moveTo((0, 0))
        cubicpen.qCurveTo((1, 1), (2, 2))
        cubicpen.closePath()

        self.assertEqual(
            str(pen).splitlines(),
            [
                "pen.moveTo((0, 0))",
                "pen.qCurveTo((1, 1), (2, 2))",
                "pen.closePath()",
            ],
        )

    def test_qCurveTo_3_points_no_conversion(self):
        pen = DummyPen()
        cubicpen = Qu2CuPen(pen, MAX_ERR)
        cubicpen.moveTo((0, 0))
        cubicpen.qCurveTo((0, 3), (1, 3), (1, 0))
        cubicpen.closePath()

        self.assertEqual(
            str(pen).splitlines(),
            [
                "pen.moveTo((0, 0))",
                "pen.qCurveTo((0, 3), (1, 3), (1, 0))",
                "pen.closePath()",
            ],
        )

    def test_qCurveTo_no_oncurve_points(self):
        pen = DummyPen()
        cubicpen = Qu2CuPen(pen, MAX_ERR)
        cubicpen.qCurveTo((0, 0), (1, 0), (1, 1), (0, 1), None)
        cubicpen.closePath()

        self.assertEqual(
            str(pen).splitlines(),
            ["pen.qCurveTo((0, 0), (1, 0), (1, 1), (0, 1), None)", "pen.closePath()"],
        )

    def test_curveTo_1_point(self):
        pen = DummyPen()
        cubicpen = Qu2CuPen(pen, MAX_ERR)
        cubicpen.moveTo((0, 0))
        cubicpen.curveTo((1, 1))
        cubicpen.closePath()

        self.assertEqual(
            str(pen).splitlines(),
            [
                "pen.moveTo((0, 0))",
                "pen.curveTo((1, 1))",
                "pen.closePath()",
            ],
        )

    def test_curveTo_2_points(self):
        pen = DummyPen()
        cubicpen = Qu2CuPen(pen, MAX_ERR)
        cubicpen.moveTo((0, 0))
        cubicpen.curveTo((1, 1), (2, 2))
        cubicpen.closePath()

        self.assertEqual(
            str(pen).splitlines(),
            [
                "pen.moveTo((0, 0))",
                "pen.curveTo((1, 1), (2, 2))",
                "pen.closePath()",
            ],
        )

    def test_curveTo_3_points(self):
        pen = DummyPen()
        cubicpen = Qu2CuPen(pen, MAX_ERR)
        cubicpen.moveTo((0, 0))
        cubicpen.curveTo((1, 1), (2, 2), (3, 3))
        cubicpen.closePath()

        self.assertEqual(
            str(pen).splitlines(),
            [
                "pen.moveTo((0, 0))",
                "pen.curveTo((1, 1), (2, 2), (3, 3))",
                "pen.closePath()",
            ],
        )

    def test_all_cubic(self):
        inPen = RecordingPen()
        inPen.value = [
            ("moveTo", ((1204, 347),)),
            ("qCurveTo", ((1255, 347), (1323, 433), (1323, 467))),
            ("qCurveTo", ((1323, 478), (1310, 492), (1302, 492))),
            ("qCurveTo", ((1295, 492), (1289, 484))),
            ("lineTo", ((1272, 461),)),
            ("qCurveTo", ((1256, 439), (1221, 416), (1200, 416))),
            ("qCurveTo", ((1181, 416), (1141, 440), (1141, 462))),
            ("qCurveTo", ((1141, 484), (1190, 565), (1190, 594))),
            ("qCurveTo", ((1190, 607), (1181, 634), (1168, 634))),
            ("qCurveTo", ((1149, 634), (1146, 583), (1081, 496), (1081, 463))),
            ("qCurveTo", ((1081, 417), (1164, 347), (1204, 347))),
            ("closePath", ()),
        ]

        outPen = RecordingPen()
        q2cPen = Qu2CuPen(outPen, 1.0, all_cubic=True)
        inPen.replay(q2cPen)

        print(outPen.value)

        assert not any(typ == "qCurveTo" for typ, _ in outPen.value)

    # A fixed contour made entirely of off-curve points (a TrueType "quad
    # blob"), used by the tests below.
    OFFCURVES = [(0, 100), (100, 200), (300, 200), (400, 100), (300, 0), (100, 0)]

    def test_all_cubic_offcurve_only(self):
        contour = [
            ("qCurveTo", tuple(self.OFFCURVES) + (None,)),
            ("closePath", ()),
        ]
        outPen = _convert(contour)

        # The output uses only cubic curves and starts at the implied
        # on-curve point between the last and first off-curve points.
        implied_start = _midpoint(self.OFFCURVES[-1], self.OFFCURVES[0])
        assert outPen.value[0] == ("moveTo", (implied_start,))
        assert not any(op == "qCurveTo" for op, _ in outPen.value)
        assert outPen.value[-1] == ("closePath", ())

        # One cubic per original quadratic segment: no off-curve point lost.
        curves = [args for op, args in outPen.value if op == "curveTo"]
        assert len(curves) == len(self.OFFCURVES)
        # The last cubic closes exactly back on the implied start point.
        assert curves[-1][-1] == implied_start

        # The cubic geometry matches the original quadratic path within the
        # requested error (here the elevation is exact, up to rounding).
        _, reference = _quad_blob_reference(self.OFFCURVES, 120)
        sampled = _recorded_points(outPen.value, 120)
        assert _hausdorff(reference, sampled) < 1e-9

    def test_all_cubic_offcurve_only_within_tolerance(self):
        contour = [
            ("qCurveTo", tuple(self.OFFCURVES) + (None,)),
            ("closePath", ()),
        ]
        for max_err in (0.1, 0.5, 1.0, 2.0):
            outPen = _convert(contour, max_err=max_err)
            _, reference = _quad_blob_reference(self.OFFCURVES, 200)
            sampled = _recorded_points(outPen.value, 200)
            # Dense sampling introduces a small discretization floor on top of
            # the true approximation error, hence the modest extra slack.
            assert _hausdorff(reference, sampled) <= max_err + 0.25

    def test_all_cubic_offcurve_only_minimum_points(self):
        for n in (1, 2, 3, 4):
            offcurves = [(100 * i, 50 * (i % 2)) for i in range(n)]
            contour = [
                ("qCurveTo", tuple(offcurves) + (None,)),
                ("closePath", ()),
            ]
            outPen = _convert(contour)

            implied_start = _midpoint(offcurves[-1], offcurves[0])
            assert outPen.value[0] == ("moveTo", (implied_start,))
            curves = [args for op, args in outPen.value if op == "curveTo"]
            assert len(curves) == n
            assert curves[-1][-1] == implied_start

            _, reference = _quad_blob_reference(offcurves, 120)
            assert _hausdorff(reference, _recorded_points(outPen.value, 120)) < 1e-9

    def test_all_cubic_offcurve_only_duplicate_points(self):
        # Consecutive duplicate off-curve points, including a fully
        # degenerate contour, must not crash and must stay within tolerance.
        offcurves = [
            (0, 0),
            (0, 0),
            (100, 0),
            (100, 100),
            (100, 100),
            (0, 100),
        ]
        contour = [("qCurveTo", tuple(offcurves) + (None,)), ("closePath", ())]
        outPen = _convert(contour)
        curves = [args for op, args in outPen.value if op == "curveTo"]
        assert len(curves) == len(offcurves)  # every point preserved
        _, reference = _quad_blob_reference(offcurves, 120)
        assert _hausdorff(reference, _recorded_points(outPen.value, 120)) < 1e-9

        # All points identical: every elevated cubic is a single point.
        same = [(5, 5)] * 4
        outPen = _convert([("qCurveTo", tuple(same) + (None,)), ("closePath", ())])
        assert not any(op == "qCurveTo" for op, _ in outPen.value)
        assert all(
            pt == (5, 5) for op, args in outPen.value if op == "curveTo" for pt in args
        )

    def test_all_cubic_offcurve_only_reverse_direction(self):
        contour = [
            ("qCurveTo", tuple(self.OFFCURVES) + (None,)),
            ("closePath", ()),
        ]
        forward = _convert(contour)
        reversed_pen = _convert(contour, reverse_direction=True)

        implied_start = _midpoint(self.OFFCURVES[-1], self.OFFCURVES[0])
        # Reversing keeps the same starting point ...
        assert reversed_pen.value[0] == ("moveTo", (implied_start,))
        # ... but traverses the contour in the opposite order.
        forward_points = _recorded_points(forward.value, 120)
        reversed_points = _recorded_points(reversed_pen.value, 120)
        length = min(len(forward_points), len(reversed_points))
        order_diff = max(
            (
                (forward_points[i][0] - reversed_points[i][0]) ** 2
                + (forward_points[i][1] - reversed_points[i][1]) ** 2
            )
            ** 0.5
            for i in range(1, length - 1)
        )
        assert order_diff > 10 * MAX_ERR
        # ... while describing the exact same geometry.
        _, reference = _quad_blob_reference(self.OFFCURVES, 120)
        assert _hausdorff(reference, reversed_points) < 1e-9

    def test_all_cubic_offcurve_only_multiple_contours(self):
        contours = [
            [("qCurveTo", tuple(self.OFFCURVES) + (None,)), ("closePath", ())],
            [
                ("moveTo", ((10, 10),)),
                ("qCurveTo", ((20, 20), (30, 10))),
                ("lineTo", ((40, 40),)),
                ("closePath", ()),
            ],
            [
                ("qCurveTo", ((0, 0), (10, 0), (10, 10), None)),
                ("closePath", ()),
            ],
        ]
        outPen = RecordingPen()
        pen = Qu2CuPen(outPen, MAX_ERR, all_cubic=True)
        for contour in contours:
            for op, args in contour:
                getattr(pen, op)(*args)

        # Three separate contours, none containing quadratic output.
        assert sum(1 for op, _ in outPen.value if op == "moveTo") == 3
        assert not any(op == "qCurveTo" for op, _ in outPen.value)

        # Each quad blob keeps its own implied starting point.
        assert outPen.value[0] == (
            "moveTo",
            (_midpoint(self.OFFCURVES[-1], self.OFFCURVES[0]),),
        )
        assert ("moveTo", ((5.0, 5.0),)) in outPen.value

    def test_offcurve_only_passthrough_when_not_all_cubic(self):
        # Without all_cubic, an oncurve-less contour is passed through
        # unchanged, preserving the previous behavior.
        contour = [
            ("qCurveTo", tuple(self.OFFCURVES) + (None,)),
            ("closePath", ()),
        ]
        outPen = RecordingPen()
        pen = Qu2CuPen(outPen, MAX_ERR)
        for op, args in contour:
            getattr(pen, op)(*args)
        assert outPen.value == contour

    def test_all_cubic_offcurve_only_stats(self):
        stats = {}
        outPen = RecordingPen()
        pen = Qu2CuPen(outPen, MAX_ERR, all_cubic=True, stats=stats)
        pen.qCurveTo(*(self.OFFCURVES + [None]))
        pen.closePath()
        # Each emitted cubic has two off-curve controls -> key "2".
        assert stats == {"2": len(self.OFFCURVES)}


if __name__ == "__main__":
    unittest.main()
