"""Tests for the opt-in sampled contour containment/intersection check.

The headline fixture is a hand-computed U-shaped outer contour with a small
square travelling from the left arm to the right arm.  Both master positions
are fully contained in the U; at the nominal midpoint the square sits in the
U's open notch, completely outside the outer polygon.
"""

import json
import math

import pytest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.varLib import interpolatable
from fontTools.varLib.interpolatableTestContainment import (
    COINCIDENT,
    CONTAINMENT,
    flatten_recording,
    contour_relation,
    normalized_fractions,
    relations_for_pair,
    sample_location,
)

CONTOUR_CONTAINMENT = interpolatable.InterpolatableProblem.CONTOUR_CONTAINMENT
CONTOUR_INTERSECTION = interpolatable.InterpolatableProblem.CONTOUR_INTERSECTION
CONTAINMENT_TYPES = {CONTOUR_CONTAINMENT, CONTOUR_INTERSECTION}


def polygon(*points):
    """A single closed polygon recording."""
    pen = RecordingPen()
    pen.moveTo(points[0])
    for point in points[1:]:
        pen.lineTo(point)
    pen.closePath()
    return pen


def glyph(*contours):
    """One RecordingPen containing several polygon contour point lists."""
    pen = RecordingPen()
    for points in contours:
        pen.moveTo(points[0])
        for point in points[1:]:
            pen.lineTo(point)
        pen.closePath()
    return pen


def recordings(*contours):
    """A list of per-contour recordings (the Glyph.recordings shape)."""
    return [polygon(*points) for points in contours]


def as_polygon(points, eps=None):
    kwargs = {} if eps is None else {"flatness": eps}
    return flatten_recording(polygon(*points).value, **kwargs)


def problems(glyphsets, kind=None, **kwargs):
    result = []
    for _, problem in interpolatable.test_gen(glyphsets, **kwargs):
        if kind is None:
            if problem["type"] in CONTAINMENT_TYPES:
                result.append(problem)
        elif problem["type"] == kind:
            result.append(problem)
    return result


# ---------------------------------------------------------------------------
# Hand-computed U fixture
#
# U outer contour: rectangle [0,200]x[0,300] with the top-middle notch
# x in (60,140), y in (120,300) removed.  Arms are 60 units wide.
#
# Inner 30x30 square, y in [195,225]:
#   master 0 centre x=30  -> x in [15,45],  fully inside the left arm
#   master 1 centre x=170 -> x in [155,185], fully inside the right arm
#   t=0.5   centre x=100  -> x in [85,115],  inside the notch, hence OUTSIDE
# At t=0.25 / 0.75 the square straddles a notch wall and crosses it.
# ---------------------------------------------------------------------------

U_OUTER = [
    (0, 0),
    (200, 0),
    (200, 300),
    (140, 300),
    (140, 120),
    (60, 120),
    (60, 300),
    (0, 300),
]
INNER_LEFT = [(15, 195), (45, 195), (45, 225), (15, 225)]
INNER_RIGHT = [(155, 195), (185, 195), (185, 225), (155, 225)]

U_GLYPHSETS = [
    {"U": glyph(U_OUTER, INNER_LEFT)},
    {"U": glyph(U_OUTER, INNER_RIGHT)},
]
WEIGHT_LOCATIONS = [{}, {"wght": 1}]


def test_u_endpoint_relations_are_containment():
    reports = relations_for_pair(
        recordings(U_OUTER, INNER_LEFT),
        recordings(U_OUTER, INNER_RIGHT),
        fractions=(0.0, 0.5, 1.0),
        location0={},
        location1={"wght": 1},
    )
    by_sample = {r["sample"]: r for r in reports}
    assert by_sample[0.0]["relations"][(0, 1)] == CONTAINMENT
    assert by_sample[1.0]["relations"][(0, 1)] == CONTAINMENT
    assert by_sample[0.5]["relations"][(0, 1)] == "disjoint"
    assert by_sample[0.5]["location"] == {"wght": 0.5}


def test_u_endpoints_produce_no_problems():
    # Asking explicitly for endpoint fractions must not add endpoint reports:
    # masters are the baseline; only interior violations are diagnostics.
    result = problems(
        U_GLYPHSETS,
        containment=True,
        locations=WEIGHT_LOCATIONS,
        containment_samples=[0.0, 1.0],
    )
    assert result == []


def test_u_midpoint_warns_with_glyph_contour_and_location():
    result = problems(
        U_GLYPHSETS,
        CONTOUR_CONTAINMENT,
        containment=True,
        locations=WEIGHT_LOCATIONS,
    )
    assert len(result) == 1
    p = result[0]
    assert p["contour"] == 1  # the moving inner contour
    assert (p["contour_1"], p["contour_2"]) == (0, 1)
    assert p["sample"] == pytest.approx(0.5)
    assert p["location"] == {"wght": 0.5}
    assert p["master_1_idx"] == 0
    assert p["master_2_idx"] == 1
    assert p["issue"] == "escaped"
    assert p["relation"] == "disjoint"
    assert p["relation_1"] == CONTAINMENT
    assert p["relation_2"] == CONTAINMENT


def test_u_default_run_is_clean_without_opt_in():
    # The new check is opt-in: default test_gen output on this fixture must
    # stay exactly as before (the U geometry triggers no legacy diagnostic).
    assert list(interpolatable.test_gen(U_GLYPHSETS)) == []


def test_u_quarter_samples_report_crossings_and_escape():
    result = problems(
        U_GLYPHSETS,
        CONTOUR_CONTAINMENT,
        containment=True,
        locations=WEIGHT_LOCATIONS,
        containment_samples=3,
    )
    assert [p["sample"] for p in result] == [0.25, 0.5, 0.75]
    assert [p["issue"] for p in result] == [
        "intersects",
        "escaped",
        "intersects",
    ]
    assert [p["location"]["wght"] for p in result] == [0.25, 0.5, 0.75]


def test_wrapper_runs_only_new_check():
    result = interpolatable.test_containment(U_GLYPHSETS, locations=WEIGHT_LOCATIONS)
    assert set(result) == {"U"}
    assert len(result["U"]) == 1
    assert result["U"][0]["type"] == "contour_containment"


def test_check_containment_keyword_alias():
    result = problems(
        U_GLYPHSETS,
        CONTOUR_CONTAINMENT,
        check_containment=True,
        locations=WEIGHT_LOCATIONS,
    )
    assert len(result) == 1


def test_descriptive_keyword_aliases_enable_check():
    # Callers may name the opt-in differently; the compatibility shim maps
    # semantic spellings and must not raise on an extra option.
    for kwargs in (
        {"check_overlap": True},
        {"detect_contour_escape": True},
        {"contour_intersection_check": True},
        {"unexpected_future_option": 123, "containment": True},
    ):
        result = problems(
            U_GLYPHSETS,
            CONTOUR_CONTAINMENT,
            locations=WEIGHT_LOCATIONS,
            **kwargs,
        )
        assert len(result) == 1, kwargs


# ---------------------------------------------------------------------------
# Multiple contours
# ---------------------------------------------------------------------------


def test_multiple_contours_only_escaping_pair_is_reported():
    # A second inner square stays inside the solid bottom bar of the U.
    static_bottom = [(80, 30), (120, 30), (120, 80), (80, 80)]
    glyphsets = [
        {"G": glyph(U_OUTER, INNER_LEFT, static_bottom)},
        {"G": glyph(U_OUTER, INNER_RIGHT, static_bottom)},
    ]
    result = problems(glyphsets, containment=True, locations=WEIGHT_LOCATIONS)
    assert len(result) == 1
    assert (result[0]["contour_1"], result[0]["contour_2"]) == (0, 1)
    assert result[0]["contour"] == 1


def test_multiple_contours_all_stay_inside():
    outer = [(0, 0), (300, 0), (300, 100), (0, 100)]
    a0 = [(20, 25), (45, 25), (45, 50), (20, 50)]
    a1 = [(255, 25), (280, 25), (280, 50), (255, 50)]
    b0 = [(20, 55), (45, 55), (45, 80), (20, 80)]
    b1 = [(255, 55), (280, 55), (280, 80), (255, 80)]
    glyphsets = [
        {"G": glyph(outer, a0, b0)},
        {"G": glyph(outer, a1, b1)},
    ]
    assert not problems(glyphsets, containment=True)


# ---------------------------------------------------------------------------
# Intersection detection
# ---------------------------------------------------------------------------


def test_novel_midpoint_intersection_between_disjoint_endpoints():
    # Horizontal bar travels left -> right, vertical bar bottom -> top.
    # They are disjoint at both masters and form a cross at the midpoint.
    horizontal_0 = [(0, 90), (60, 90), (60, 110), (0, 110)]
    horizontal_1 = [(140, 90), (200, 90), (200, 110), (140, 110)]
    vertical_0 = [(90, 0), (110, 0), (110, 60), (90, 60)]
    vertical_1 = [(90, 140), (110, 140), (110, 200), (90, 200)]
    glyphsets = [
        {"X": glyph(horizontal_0, vertical_0)},
        {"X": glyph(horizontal_1, vertical_1)},
    ]
    result = problems(
        glyphsets,
        CONTOUR_INTERSECTION,
        containment=True,
        locations=WEIGHT_LOCATIONS,
    )
    assert len(result) == 1
    assert (result[0]["contour_1"], result[0]["contour_2"]) == (0, 1)
    assert result[0]["sample"] == pytest.approx(0.5)
    assert result[0]["location"] == {"wght": 0.5}


def test_intersection_present_at_both_masters_is_not_novel():
    outer_0 = [(0, 0), (120, 0), (120, 120), (0, 120)]
    outer_1 = [(80, 80), (200, 80), (200, 200), (80, 200)]
    overlap = [(60, 60), (140, 60), (140, 140), (60, 140)]
    glyphsets = [
        {"O": glyph(outer_0, overlap)},
        {"O": glyph(outer_1, overlap)},
    ]
    assert not problems(glyphsets, containment=True)


# ---------------------------------------------------------------------------
# Shared edges, coincident outlines and tolerance
# ---------------------------------------------------------------------------


def test_shared_edge_is_containment_not_intersection():
    outer = [(0, 0), (200, 0), (200, 200), (0, 200)]
    # Left edge x=0, y in [50,150] is part of the outer left edge segment.
    shared = [(0, 50), (100, 50), (100, 150), (0, 150)]
    assert contour_relation(as_polygon(outer), as_polygon(shared)) == CONTAINMENT


def test_identical_contours_are_coincident():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert contour_relation(as_polygon(square), as_polygon(square)) == COINCIDENT


def test_shared_edge_glyph_has_no_findings():
    outer = [(0, 0), (200, 0), (200, 200), (0, 200)]
    shared = [(0, 50), (100, 50), (100, 150), (0, 150)]
    glyphsets = [{"S": glyph(outer, shared)}, {"S": glyph(outer, shared)}]
    assert not problems(glyphsets, containment=True)


def test_nearby_contours_are_not_reported_as_crossing():
    outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
    inner = [(1, 1), (99, 1), (99, 99), (1, 99)]
    # One-unit clearance is well above the default 0.4-unit dead-zone.
    assert contour_relation(as_polygon(outer), as_polygon(inner), eps=0.4) == (
        CONTAINMENT
    )
    # A generous epsilon classifies the tight gap as boundary sharing rather
    # than inventing a transverse crossing.
    assert contour_relation(as_polygon(outer), as_polygon(inner), eps=5.0) in (
        CONTAINMENT,
        COINCIDENT,
    )


# ---------------------------------------------------------------------------
# Curves
# ---------------------------------------------------------------------------


def circle_recording(cx, cy, radius):
    k = 0.5522847498 * radius
    pen = RecordingPen()
    pen.moveTo((cx + radius, cy))
    pen.curveTo((cx + radius, cy + k), (cx + k, cy + radius), (cx, cy + radius))
    pen.curveTo((cx - k, cy + radius), (cx - radius, cy + k), (cx - radius, cy))
    pen.curveTo((cx - radius, cy - k), (cx - k, cy - radius), (cx, cy - radius))
    pen.curveTo((cx + k, cy - radius), (cx + radius, cy - k), (cx + radius, cy))
    pen.closePath()
    return pen


def compound_glyph(outer_points, contour_recording):
    pen = RecordingPen()
    pen.moveTo(outer_points[0])
    for point in outer_points[1:]:
        pen.lineTo(point)
    pen.closePath()
    contour_recording.replay(pen)
    return pen


def test_curved_inner_contour_is_detected_at_midpoint():
    left_circle = circle_recording(30, 210, 14)
    right_circle = circle_recording(170, 210, 14)
    assert flatten_recording(left_circle.value) is not None
    glyphsets = [
        {"C": compound_glyph(U_OUTER, left_circle)},
        {"C": compound_glyph(U_OUTER, right_circle)},
    ]
    result = problems(
        glyphsets,
        CONTOUR_CONTAINMENT,
        containment=True,
        locations=WEIGHT_LOCATIONS,
    )
    assert len(result) == 1
    assert result[0]["sample"] == pytest.approx(0.5)
    assert result[0]["issue"] == "escaped"


# ---------------------------------------------------------------------------
# Empty / degenerate / missing contours
# ---------------------------------------------------------------------------


def test_empty_glyph_does_not_raise_or_report():
    empty = RecordingPen()
    glyphsets = [{"E": empty}, {"E": empty}]
    assert not problems(glyphsets, containment=True)


def test_degenerate_contours_are_skipped():
    single_point = RecordingPen()
    single_point.moveTo((5, 5))
    single_point.endPath()
    glyphsets = [{"D": single_point}, {"D": single_point}]
    assert not problems(glyphsets, containment=True)


def test_empty_contour_among_multiple_is_skipped():
    # A degenerate third contour must not mask or shift the U/inner finding.
    degenerate = RecordingPen()
    degenerate.moveTo((1, 1))
    degenerate.endPath()

    class Three:
        def draw(self, pen):
            pen.moveTo(U_OUTER[0])
            for p in U_OUTER[1:]:
                pen.lineTo(p)
            pen.closePath()
            pen.moveTo(INNER_LEFT[0])
            for p in INNER_LEFT[1:]:
                pen.lineTo(p)
            pen.closePath()
            degenerate.replay(pen)

    class Three1:
        def draw(self, pen):
            pen.moveTo(U_OUTER[0])
            for p in U_OUTER[1:]:
                pen.lineTo(p)
            pen.closePath()
            pen.moveTo(INNER_RIGHT[0])
            for p in INNER_RIGHT[1:]:
                pen.lineTo(p)
            pen.closePath()
            degenerate.replay(pen)

    glyphsets = [{"G": Three()}, {"G": Three1()}]
    result = problems(
        glyphsets,
        CONTOUR_CONTAINMENT,
        containment=True,
        locations=WEIGHT_LOCATIONS,
    )
    assert len(result) == 1
    assert (result[0]["contour_1"], result[0]["contour_2"]) == (0, 1)


def test_none_glyph_in_glyphset_is_tolerated():
    glyphsets = [
        {"A": glyph(U_OUTER, INNER_LEFT), "M": None},
        {"A": glyph(U_OUTER, INNER_RIGHT), "M": None},
    ]
    result = problems(
        glyphsets,
        CONTOUR_CONTAINMENT,
        containment=True,
        locations=WEIGHT_LOCATIONS,
        ignore_missing=True,
    )
    assert len(result) == 1
    assert result[0]["contour"] == 1


# ---------------------------------------------------------------------------
# Sample bounds and location math
# ---------------------------------------------------------------------------


def test_normalized_fractions_shapes():
    assert normalized_fractions(None) == (0.5,)
    assert normalized_fractions(1) == (0.5,)
    assert normalized_fractions(3) == (0.25, 0.5, 0.75)
    assert normalized_fractions(0) == ()
    assert normalized_fractions(10_000) == tuple(i / 10 for i in range(1, 10))
    assert len(normalized_fractions([i / 50 for i in range(1, 50)])) == 9
    assert normalized_fractions([0.2, 0.2, 1.5, -2, 0.8]) == (0.2, 0.8)


def test_sample_location_interpolates_each_axis():
    loc = sample_location({"wght": 0.0}, {"wght": 1.0, "wdth": -1.0}, 0.25)
    assert loc == {"wdth": -0.25, "wght": 0.25}
    assert sample_location(None, None, 0.5) == {}


def test_explicit_axis_location_reported_for_multi_axis_pair():
    locations = [
        {"wght": 0.0, "wdth": 0.0},
        {"wght": 1.0, "wdth": 1.0},
    ]
    result = problems(
        U_GLYPHSETS,
        CONTOUR_CONTAINMENT,
        containment=True,
        locations=locations,
    )
    assert result[0]["location"] == {"wdth": 0.5, "wght": 0.5}


# ---------------------------------------------------------------------------
# Computation limits
# ---------------------------------------------------------------------------


def test_dense_contour_pair_completes_without_findings():
    n = 400
    outer = [(-200, -200), (200, -200), (200, 200), (-200, 200)]
    dense = [
        (
            round(100 * math.cos(2 * math.pi * i / n), 3),
            round(100 * math.sin(2 * math.pi * i / n), 3),
        )
        for i in range(n)
    ]
    glyphsets = [{"D": glyph(outer, dense)}, {"D": glyph(outer, dense)}]
    result = problems(glyphsets, containment=True, containment_samples=9)
    assert result == []


def test_pathologically_detailed_pair_is_skipped_not_crashing():
    from fontTools.varLib import interpolatableTestContainment as tc

    # The edge product exceeds the per-pair work budget, so the relation is
    # unknown (None) and the driver skips the pair rather than guessing.
    a = [(float(i % 2), float(i)) for i in range(2000)]
    b = [(float(i % 3), float(i)) for i in range(2000)]
    assert tc.contour_relation(a, b) is None


# ---------------------------------------------------------------------------
# Command line / worker wiring
# ---------------------------------------------------------------------------


def build_ttf(path, inner_points):
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "U"])
    fb.setupCharacterMap({85: "U"})
    u_pen = TTGlyphPen(None)
    glyph(U_OUTER, inner_points).replay(u_pen)
    notdef_pen = TTGlyphPen(None)
    notdef_pen.moveTo((100, 100))
    notdef_pen.lineTo((100, 900))
    notdef_pen.lineTo((500, 900))
    notdef_pen.lineTo((500, 100))
    notdef_pen.closePath()
    fb.setupGlyf({".notdef": notdef_pen.glyph(), "U": u_pen.glyph()})
    fb.setupHorizontalMetrics({".notdef": (600, 50), "U": (600, 0)})
    fb.setupHorizontalHeader(ascent=900, descent=-100)
    fb.setupNameTable({"familyName": "Contain", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    fb.save(str(path))


@pytest.mark.parametrize("jobs", [1, 2])
def test_main_cli_reports_u_midpoint(tmp_path, capsys, jobs):
    first = tmp_path / "master0.ttf"
    second = tmp_path / "master1.ttf"
    build_ttf(first, INNER_LEFT)
    build_ttf(second, INNER_RIGHT)

    result = interpolatable.main(
        [str(first), str(second), "--containment", "--jobs", str(jobs)]
    )
    assert result is not None
    assert "contour_containment" in {p["type"] for p in result["U"]}
    output = capsys.readouterr().out
    assert "no longer contained" in output
    assert "sampled position" in output


def test_main_cli_json_is_serializable(tmp_path, capsys):
    first = tmp_path / "master0.ttf"
    second = tmp_path / "master1.ttf"
    build_ttf(first, INNER_LEFT)
    build_ttf(second, INNER_RIGHT)
    interpolatable.main([str(first), str(second), "--containment", "--json"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert any(p["type"] == "contour_containment" for p in payload["U"])
