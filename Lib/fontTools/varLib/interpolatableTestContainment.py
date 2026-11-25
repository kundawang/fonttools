"""Opt-in contour containment and crossing checks at explicit samples.

This implements the repeatable, sampled part of the request in
`fonttools issue #3562 <https://github.com/fonttools/fonttools/issues/3562>`_:
a contour that is safely hidden inside another contour in every compared
master can protrude through it at intermediate interpolation positions.

For each compared master pair the outlines are linearly interpolated at a
small, explicit set of interior fractions (the midpoint by default) and the
pairwise contour relations -- contained, disjoint, crossing or shared
boundary -- are compared with the relations at the two master endpoints.

Only the listed sample positions are examined.  A clean report does **not**
prove that the whole continuous interpolation space is free of crossings.

Curves are flattened to polygons with a bounded subdivision depth, and all
geometric comparisons use a coordinate-unit epsilon derived from UPEM so
that tessellation error and coincident ("shared") edges do not produce
spurious crossings.  Empty, open or component-only contours are skipped
instead of raising.
"""

from math import hypot

from fontTools.pens.basePen import BasePen
from fontTools.pens.recordingPen import lerpRecordings

# Relation labels.  "a" and "b" are the two contours in the order supplied
# (always lower index first by the check driver).
CONTAINMENT = "contains"  # a contains b
CONTAINED = "contained_by"  # b contains a
DISJOINT = "disjoint"
INTERSECTS = "intersects"
COINCIDENT = "coincident"

# Interior interpolation fractions examined unless the caller asks for more.
# A single midpoint is enough for the common "crosses half way" failure and
# keeps the added cost at one tessellation per master pair.
DEFAULT_CONTAINMENT_SAMPLES = (0.5,)

# Hard upper bound on caller-requested interior samples.
MAX_CONTAINMENT_SAMPLES = 9

# Curve tessellation.  Flatness/epsilon are expressed in font units and scale
# with UPEM; at UPEM 1000 the chord error target is 0.2 units and the relation
# dead-zone is 0.4 units.
DEFAULT_FLATNESS_FACTOR = 0.0002
DEFAULT_GEOMETRY_EPS_FACTOR = 0.0004
MAX_CURVE_DEPTH = 6  # at most 2**6 = 64 segments per source curve
MAX_CONTOUR_POINTS = 1024

# Skip an edge pair beyond this many edge-vs-edge tests.  Nested contours
# normally have a few dozen edges each, so this only affects pathologically
# complex contours; bounding the work keeps whole-glyph runs predictable.
MAX_EDGE_TESTS_PER_PAIR = 400_000


def normalized_fractions(samples=None):
    """Coerce requested samples to a sorted, de-duplicated tuple in (0, 1).

    ``None`` or a false value selects :data:`DEFAULT_CONTAINMENT_SAMPLES`.
    An integer ``n`` means ``n`` evenly spaced interior fractions
    (``n=1`` is the midpoint).  The result never contains more than
    :data:`MAX_CONTAINMENT_SAMPLES` positions.
    """
    if samples is None:
        return DEFAULT_CONTAINMENT_SAMPLES
    if isinstance(samples, bool):
        # bool is an int subclass; treat True as one midpoint, False as default
        return DEFAULT_CONTAINMENT_SAMPLES if samples else ()
    if isinstance(samples, int):
        count = max(0, min(samples, MAX_CONTAINMENT_SAMPLES))
        if count == 0:
            return ()
        return tuple(i / (count + 1) for i in range(1, count + 1))

    fractions = []
    for value in samples:
        t = float(value)
        if 0.0 < t < 1.0:
            fractions.append(t)
    fractions = sorted({round(t, 12) for t in fractions})
    if len(fractions) > MAX_CONTAINMENT_SAMPLES:
        # Keep an evenly spread selection rather than only the smallest ts.
        step = len(fractions) / MAX_CONTAINMENT_SAMPLES
        fractions = [
            fractions[int(i * step + step / 2)] for i in range(MAX_CONTAINMENT_SAMPLES)
        ]
    return tuple(fractions)


def sample_location(location0, location1, t):
    """Linearly interpolate two normalized location dicts at fraction ``t``."""
    location0 = location0 or {}
    location1 = location1 or {}
    result = {}
    for key in sorted(location0.keys() | location1.keys()):
        v0 = float(location0.get(key, 0.0))
        v1 = float(location1.get(key, 0.0))
        value = v0 + (v1 - v0) * t
        if abs(value) < 5e-12:
            value = 0.0
        else:
            value = round(value, 10)
        result[key] = value
    return result


def _point_distance_to_segment(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0.0:
        return hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length2
    if t < 0.0:
        qx, qy = ax, ay
    elif t > 1.0:
        qx, qy = bx, by
    else:
        qx = ax + t * dx
        qy = ay + t * dy
    return hypot(px - qx, py - qy)


def _line_distance(p, a, b):
    """Perpendicular distance of p to the infinite line through a, b."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    length = hypot(dx, dy)
    if length == 0.0:
        return hypot(p[0] - a[0], p[1] - a[1])
    cross = dx * (a[1] - p[1]) - dy * (a[0] - p[0])
    return abs(cross) / length


def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


class _FlattenPen(BasePen):
    """Collect one closed contour as a polygon of line-only points."""

    def __init__(self, flatness, max_points=MAX_CONTOUR_POINTS):
        BasePen.__init__(self, glyphSet=None)
        self._flatness = flatness
        self._points_left = max(0, max_points)
        self.polygon = []
        self.closed = False
        self.ended_open = False

    def _moveTo(self, p):
        self.polygon = [tuple(p)]

    def _lineTo(self, p):
        self.polygon.append(tuple(p))

    def _closePath(self):
        self.closed = True

    def _endPath(self):
        self.ended_open = True

    def _append(self, p):
        if self._points_left > 0:
            self._points_left -= 1
            self.polygon.append(tuple(p))
        else:
            # Budget exhausted: stop subdividing but keep the endpoint so the
            # polygon stays topologically connected.
            self.polygon.append(tuple(p))

    def _flat_enough(self, p0, controls, p3):
        return all(_line_distance(c, p0, p3) <= self._flatness for c in controls)

    def _curveToOne(self, p1, p2, p3, depth=MAX_CURVE_DEPTH):
        self._cubic(self._getCurrentPoint(), p1, p2, p3, depth)

    def _cubic(self, p0, p1, p2, p3, depth):
        if depth <= 0 or self._points_left <= 0 or self._flat_enough(p0, (p1, p2), p3):
            self._append(p3)
            return

        p01 = _mid(p0, p1)
        p12 = _mid(p1, p2)
        p23 = _mid(p2, p3)
        p012 = _mid(p01, p12)
        p123 = _mid(p12, p23)
        p0123 = _mid(p012, p123)

        self._cubic(p0, p01, p012, p0123, depth - 1)
        self._cubic(p0123, p123, p23, p3, depth - 1)

    def _qCurveToOne(self, p1, p2, depth=MAX_CURVE_DEPTH):
        self._quad(self._getCurrentPoint(), p1, p2, depth)

    def _quad(self, p0, p1, p2, depth):
        if depth <= 0 or self._points_left <= 0 or self._flat_enough(p0, (p1,), p2):
            self._append(p2)
            return

        p01 = _mid(p0, p1)
        p12 = _mid(p1, p2)
        p012 = _mid(p01, p12)

        self._quad(p0, p01, p012, depth - 1)
        self._quad(p012, p12, p2, depth - 1)


def _normalize_polygon(points):
    if not points:
        return None
    cleaned = []
    for p in points:
        p = (float(p[0]), float(p[1]))
        if not cleaned or hypot(p[0] - cleaned[-1][0], p[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(p)
    if (
        len(cleaned) >= 2
        and hypot(cleaned[0][0] - cleaned[-1][0], cleaned[0][1] - cleaned[-1][1])
        <= 1e-9
    ):
        cleaned.pop()
    if len(cleaned) < 3:
        return None
    area2 = 0.0
    for i, p in enumerate(cleaned):
        q = cleaned[(i + 1) % len(cleaned)]
        area2 += p[0] * q[1] - q[0] * p[1]
    if abs(area2) <= 1e-9:
        return None
    return cleaned


def flatten_recording(recording, flatness=0.2, max_points=MAX_CONTOUR_POINTS):
    """Flatten one per-contour recording into a closed polygon.

    Returns a list of ``(x, y)`` points, or ``None`` for empty, open,
    degenerate or component-only contours.  ``recording`` may be a
    :class:`RecordingPen`-like object with a ``.value`` list or a raw ops
    list/generator as produced by :func:`lerpRecordings`.
    """
    if recording is None:
        return None
    ops = getattr(recording, "value", recording)
    if ops is None:
        return None
    if not isinstance(ops, list):
        ops = list(ops)
    if not ops:
        return None
    if any(op == "addComponent" for op, _ in ops):
        # Component interpolation needs decomposition and glyphset context;
        # the containment check simply does not cover these contours.
        return None

    pen = _FlattenPen(flatness, max_points=max_points)
    try:
        for op, operands in ops:
            getattr(pen, op)(*operands)
    except (TypeError, ValueError, NotImplementedError, AttributeError):
        return None

    if pen.ended_open and not pen.closed:
        return None
    return _normalize_polygon(pen.polygon)


def _side(a, b, p):
    """Signed perpendicular distance of p from directed line a -> b."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    length = hypot(dx, dy)
    if length == 0.0:
        return 0.0
    cross = dx * (p[1] - a[1]) - dy * (p[0] - a[0])
    return cross / length


def _proper_segment_crossing(a, b, c, d, eps):
    s1 = _side(a, b, c)
    s2 = _side(a, b, d)
    if not ((s1 > eps and s2 < -eps) or (s1 < -eps and s2 > eps)):
        return False
    t1 = _side(c, d, a)
    t2 = _side(c, d, b)
    return (t1 > eps and t2 < -eps) or (t1 < -eps and t2 > eps)


def _bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _bboxes_overlap(ba, bb, eps):
    return not (
        ba[2] + eps < bb[0]
        or bb[2] + eps < ba[0]
        or ba[3] + eps < bb[1]
        or bb[3] + eps < ba[1]
    )


def _polygons_cross(a, b, eps):
    """Return True for transverse edge crossings only.

    Endpoint touches, tangencies within ``eps`` and collinear/shared edges
    are deliberately not counted: coincident outlines are a normal font
    construction and must not be reported as intersections.
    """
    ba = _bbox(a)
    bb = _bbox(b)
    if not _bboxes_overlap(ba, bb, eps):
        return False

    tests = 0
    na = len(a)
    nb = len(b)
    if na * nb > MAX_EDGE_TESTS_PER_PAIR:
        # Pathologically detailed pair: do not guess.  The caller treats a
        # None relation as "skip this pair".
        return None

    for i in range(na):
        a1 = a[i]
        a2 = a[(i + 1) % na]
        aminx = min(a1[0], a2[0]) - eps
        amaxx = max(a1[0], a2[0]) + eps
        aminy = min(a1[1], a2[1]) - eps
        amaxy = max(a1[1], a2[1]) + eps
        for j in range(nb):
            b1 = b[j]
            b2 = b[(j + 1) % nb]
            if max(b1[0], b2[0]) < aminx or min(b1[0], b2[0]) > amaxx:
                continue
            if max(b1[1], b2[1]) < aminy or min(b1[1], b2[1]) > amaxy:
                continue
            tests += 1
            if tests > MAX_EDGE_TESTS_PER_PAIR:
                return None
            if _proper_segment_crossing(a1, a2, b1, b2, eps):
                return True
    return False


def _point_inside_polygon(p, poly):
    """Even-odd ray cast.  Callers pass points known to be off the boundary."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > x:
                inside = not inside
    return inside


def _representative_inside(points, boundary, eps):
    """Find a candidate point of one contour strictly off another boundary.

    Vertices and edge midpoints are probed.  Return ``True``/``False`` for
    whether such a representative lies inside ``boundary``, or ``None`` when
    every candidate sits on the boundary (shared/coincident geometry).
    """
    n = len(points)
    for i, p in enumerate(points):
        q = points[(i + 1) % n]
        for candidate in (p, _mid(p, q)):
            on_boundary = False
            m = len(boundary)
            for j in range(m):
                if (
                    _point_distance_to_segment(
                        candidate, boundary[j], boundary[(j + 1) % m]
                    )
                    <= eps
                ):
                    on_boundary = True
                    break
            if not on_boundary:
                return _point_inside_polygon(candidate, boundary)
    return None


def contour_relation(a, b, eps=0.4):
    """Classify the geometric relation between two closed polygons.

    Returns one of :data:`CONTAINMENT`, :data:`CONTAINED`,
    :data:`DISJOINT`, :data:`INTERSECTS`, :data:`COINCIDENT`, or ``None``
    when either input is not a usable polygon or the computation budget was
    exceeded.
    """
    if a is None or b is None or len(a) < 3 or len(b) < 3:
        return None

    crossed = _polygons_cross(a, b, eps)
    if crossed is None:
        return None
    if crossed:
        return INTERSECTS

    b_in_a = _representative_inside(b, a, eps)
    a_in_b = _representative_inside(a, b, eps)

    if b_in_a is True:
        return CONTAINMENT
    if a_in_b is True:
        return CONTAINED
    if b_in_a is None and a_in_b is None:
        return COINCIDENT
    return DISJOINT


def _polygons_for_recordings(recordings, flatness):
    polygons = []
    for recording in recordings:
        ops = getattr(recording, "value", recording)
        polygons.append(flatten_recording(ops, flatness=flatness))
    return polygons


def relations_for_pair(
    recordings0,
    recordings1=None,
    *,
    matching=None,
    fractions=(0.0, 0.5, 1.0),
    flatness=0.2,
    eps=0.4,
    location0=None,
    location1=None,
):
    """Compute pairwise contour relations at explicit sample positions.

    ``recordings0`` and ``recordings1`` are per-glyph lists of per-contour
    recordings (the :attr:`Glyph.recordings` shape).  When ``recordings1`` is
    omitted, ``recordings0`` is used for every fraction (static outline).

    Returns a list of ``{"sample", "location", "relations"}`` dictionaries,
    where ``relations`` maps ``(i, j)`` (``i < j``) to a relation label.
    Fraction ``0`` and ``1`` use the exact master outlines.
    """
    if recordings1 is None:
        recordings1 = recordings0

    n = min(len(recordings0), len(recordings1))
    if matching is not None:
        aligned1 = [recordings1[matching[i]] for i in range(n)]
    else:
        aligned1 = list(recordings1[:n])
    recordings0 = list(recordings0[:n])

    polys0 = _polygons_for_recordings(recordings0, flatness)
    polys1 = _polygons_for_recordings(aligned1, flatness)

    reports = []
    for t in fractions:
        t = float(t)
        if t <= 0.0:
            polys = polys0
            location = dict(location0 or {})
        elif t >= 1.0:
            polys = polys1
            location = dict(location1 or {})
        else:
            polys = _interpolated_polygons(recordings0, aligned1, t, flatness)
            location = sample_location(location0, location1, t)

        relations = {}
        for i in range(n):
            for j in range(i + 1, n):
                relation = contour_relation(polys[i], polys[j], eps)
                if relation is not None:
                    relations[(i, j)] = relation
        reports.append({"sample": t, "location": location, "relations": relations})
    return reports


def _interpolated_polygons(recordings0, recordings1, t, flatness):
    polys = []
    for rec0, rec1 in zip(recordings0, recordings1):
        ops0 = getattr(rec0, "value", rec0)
        ops1 = getattr(rec1, "value", rec1)
        try:
            ops = list(lerpRecordings(ops0, ops1, factor=t))
        except (ValueError, TypeError, ZeroDivisionError):
            polys.append(None)
            continue
        polys.append(flatten_recording(ops, flatness=flatness))
    return polys


def containment_findings_for_pair(
    recordings0,
    recordings1,
    *,
    master_0_idx=0,
    master_1_idx=1,
    master_0_name=None,
    master_1_name=None,
    location0=None,
    location1=None,
    matching=None,
    fractions=DEFAULT_CONTAINMENT_SAMPLES,
    upem=1000,
    geometry_tolerance=None,
    flatness=None,
):
    """Yield containment/intersection problem dicts for one master pair.

    Endpoint relations form the baseline.  An interior sample is reported
    when

    * a pair that is contained in the same direction at both masters stops
      being so (it crosses or becomes disjoint) -- the "hidden contour pops
      out" case; or
    * a pair crosses at the interior sample while crossing at neither
      explicit master (a novel mid-interpolation intersection).

    Only the requested fractions are examined; this does not certify the
    continuous interpolation.
    """
    if geometry_tolerance is None:
        eps = upem * DEFAULT_GEOMETRY_EPS_FACTOR
    else:
        eps = float(geometry_tolerance)
    if flatness is None:
        flatness = upem * DEFAULT_FLATNESS_FACTOR

    interior = normalized_fractions(fractions)

    endpoint_reports = relations_for_pair(
        recordings0,
        recordings1,
        matching=matching,
        fractions=(0.0, 1.0),
        flatness=flatness,
        eps=eps,
        location0=location0,
        location1=location1,
    )
    rel0 = endpoint_reports[0]["relations"]
    rel1 = endpoint_reports[1]["relations"]

    interior_reports = relations_for_pair(
        recordings0,
        recordings1,
        matching=matching,
        fractions=interior,
        flatness=flatness,
        eps=eps,
        location0=location0,
        location1=location1,
    )

    common_keys = set(rel0) & set(rel1)

    def base_problem(i, j, t, location, relation):
        primary = _primary_contour(i, j, rel0.get((i, j)), relation)
        return {
            "contour": primary,
            # Older/report callers sometimes call a contour index "path".
            "path": primary,
            "contour_1": i,
            "contour_2": j,
            "sample": t,
            "t": t,
            "location": location,
            "axis_location": location,
            "master_1": master_0_name,
            "master_2": master_1_name,
            "master_1_idx": master_0_idx,
            "master_2_idx": master_1_idx,
        }

    for report in interior_reports:
        t = report["sample"]
        location = report["location"]
        current = report["relations"]
        for i, j in sorted(common_keys):
            r0 = rel0[(i, j)]
            r1 = rel1[(i, j)]
            rt = current.get((i, j))
            if rt is None:
                continue

            stable_containment = r0 == r1 == CONTAINMENT or r0 == r1 == CONTAINED
            if stable_containment and rt != r0 and rt != COINCIDENT:
                problem = base_problem(i, j, t, location, rt)
                problem.update(
                    {
                        "type": "contour_containment",
                        "issue": ("intersects" if rt == INTERSECTS else "escaped"),
                        "relation": rt,
                        "relation_1": r0,
                        "relation_2": r1,
                    }
                )
                yield problem
                continue

            if (
                rt == INTERSECTS
                and r0 != INTERSECTS
                and r1 != INTERSECTS
                and not (r0 == r1 and r0 in (CONTAINMENT, CONTAINED))
            ):
                # Stable-containment crossings are already reported above as
                # containment failures; this branch covers pairs that are
                # disjoint/shared at the masters and newly cross mid-way.
                problem = base_problem(i, j, t, location, rt)
                problem.update(
                    {
                        "type": "contour_intersection",
                        "issue": INTERSECTS,
                        "relation": rt,
                        "relation_1": r0,
                        "relation_2": r1,
                    }
                )
                yield problem


def _primary_contour(i, j, endpoint_relation, current_relation):
    """Pick the moving/inner contour for the conventional ``contour`` field."""
    if endpoint_relation == CONTAINMENT:
        # i contains j -> j is the inner contour
        return j
    if endpoint_relation == CONTAINED:
        return i
    return j


# Convention-friendly alias mirroring test_contour_order/test_starting_point.
def test_containment(recordings0, recordings1, **kwargs):
    """List form of :func:`containment_findings_for_pair`."""
    return list(containment_findings_for_pair(recordings0, recordings1, **kwargs))


# It takes required arguments, so never let pytest collect it as a test case.
test_containment.__test__ = False
