# Copyright 2016 Google Inc. All Rights Reserved.
# Copyright 2023 Behdad Esfahbod. All Rights Reserved.
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

from fontTools.qu2cu import quadratic_to_curves
from fontTools.pens.filterPen import ContourFilterPen
from fontTools.pens.reverseContourPen import ReverseContourPen
import math


class Qu2CuPen(ContourFilterPen):
    """A filter pen to convert quadratic bezier splines to cubic curves
    using the FontTools SegmentPen protocol.

    Args:

        other_pen: another SegmentPen used to draw the transformed outline.
        max_err: maximum approximation error in font units. For optimal results,
            if you know the UPEM of the font, we recommend setting this to a
            value equal, or close to UPEM / 1000.
        reverse_direction: flip the contours' direction but keep starting point.
        stats: a dictionary counting the point numbers of cubic segments.
    """

    def __init__(
        self,
        other_pen,
        max_err,
        all_cubic=False,
        reverse_direction=False,
        stats=None,
    ):
        if reverse_direction:
            other_pen = ReverseContourPen(other_pen)
        super().__init__(other_pen)
        self.all_cubic = all_cubic
        self.max_err = max_err
        self.stats = stats

    def _quadratics_to_curve(self, q):
        curves = quadratic_to_curves(q, self.max_err, all_cubic=self.all_cubic)
        if self.stats is not None:
            for curve in curves:
                n = str(len(curve) - 2)
                self.stats[n] = self.stats.get(n, 0) + 1
        for curve in curves:
            if len(curve) == 4:
                yield ("curveTo", curve[1:])
            else:
                yield ("qCurveTo", curve[1:])

    def _offcurve_only_contour(self, args):
        """Convert a closed contour made entirely of off-curve points.

        Such a contour is drawn with a single ``qCurveTo`` whose final
        argument is ``None``.  The on-curve points are only implied: each is
        the midpoint of two consecutive off-curve points, and the contour
        starts at the midpoint between the last and the first off-curve
        points.  We make the implied start point explicit with a ``moveTo``
        and feed one quadratic spline per implied on-curve segment to
        :func:`quadratic_to_curves`, so no point is dropped and the contour
        direction is preserved.
        """
        offcurves = args[:-1]
        n = len(offcurves)

        # Implied on-curve start point, halfway between the last and the
        # first off-curve points.
        first = offcurves[0]
        last = offcurves[-1]
        start = ((last[0] + first[0]) * 0.5, (last[1] + first[1]) * 0.5)
        yield ("moveTo", (start,))

        # Each quadratic spline runs from one implied on-curve point to the
        # next, with the off-curve point in between as its control point.
        quadratics = []
        previous = start
        for i in range(n):
            off = offcurves[i]
            nxt = offcurves[(i + 1) % n]
            on = ((off[0] + nxt[0]) * 0.5, (off[1] + nxt[1]) * 0.5)
            quadratics.append((previous, off, on))
            previous = on

        yield from self._quadratics_to_curve(quadratics)

    def filterContour(self, contour):
        quadratics = []
        currentPt = None
        newContour = []
        for op, args in contour:
            if op == "qCurveTo" and args[-1] is None:
                # A closed contour consisting entirely of off-curve points.
                # Flush any pending quadratic runs first.
                if quadratics:
                    newContour.extend(self._quadratics_to_curve(quadratics))
                    quadratics = []
                if self.all_cubic:
                    newContour.extend(self._offcurve_only_contour(args))
                else:
                    # No cubic conversion requested; pass the quad blob
                    # through unchanged.
                    newContour.append((op, args))
            elif op == "qCurveTo" and (
                self.all_cubic or (len(args) > 2 and args[-1] is not None)
            ):
                quadratics.append((currentPt,) + args)
            else:
                if quadratics:
                    newContour.extend(self._quadratics_to_curve(quadratics))
                    quadratics = []
                newContour.append((op, args))
            currentPt = args[-1] if args else None
        if quadratics:
            newContour.extend(self._quadratics_to_curve(quadratics))

        if not self.all_cubic:
            # Add back implicit oncurve points
            contour = newContour
            newContour = []
            for op, args in contour:
                if op == "qCurveTo" and newContour and newContour[-1][0] == "qCurveTo":
                    pt0 = newContour[-1][1][-2]
                    pt1 = newContour[-1][1][-1]
                    pt2 = args[0]
                    if (
                        pt1 is not None
                        and math.isclose(pt2[0] - pt1[0], pt1[0] - pt0[0])
                        and math.isclose(pt2[1] - pt1[1], pt1[1] - pt0[1])
                    ):
                        newArgs = newContour[-1][1][:-1] + args
                        newContour[-1] = (op, newArgs)
                        continue

                newContour.append((op, args))

        return newContour
