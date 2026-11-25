"""Coordinates and bounds queries for VARC variable-composite glyphs.

VARC glyphs can be empty shells in the ``glyf`` table, so reading
``glyph.coordinates`` / calling ``glyph.recalcBounds()`` on the raw glyf glyph
used to return empty/zero geometry even though the glyph-set drawing path
expands the real components. These tests lock in the component-aware query
API at default and non-default locations, including nesting, shared
components, cycles, invalid indices and a save/reopen round-trip.
"""

import os
from io import BytesIO

import pytest

from fontTools.ttLib import TTFont, TTLibError
from fontTools.ttLib.tables.otTables import VarComponentFlags
from fontTools.pens.boundsPen import BoundsPen, ControlBoundsPen
from fontTools.pens.recordingPen import RecordingPen

DATADIR = os.path.join(os.path.dirname(__file__), "data")
SMALL_FONT = "varc-ac00-ac01.ttf"
CONDITIONAL_FONT = "varc-ac01-conditional.ttf"


def font_path(name):
    return os.path.join(DATADIR, name)


def varc_names(font):
    return list(font["VARC"].table.Coverage.glyphs)


def coordinate_extrema(glyph):
    coords, end_pts, flags = glyph.getCoordinates()
    if not len(coords):
        return None
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return (min(xs), min(ys), max(xs), max(ys))


@pytest.mark.parametrize("location", [None, {}, {"wght": 600}])
def test_varc_glyphset_coordinates_and_bounds_match_drawing(location):
    font = TTFont(font_path(SMALL_FONT))
    glyph_set = font.getGlyphSet(location=location)

    for name in varc_names(font):
        glyph = glyph_set[name]

        coords, end_pts, flags = glyph.getCoordinates()
        assert len(coords) > 0, name
        assert end_pts, name
        assert end_pts[-1] == len(coords) - 1
        # Contour end indexes are unique and strictly increasing.
        assert all(b > a for a, b in zip(end_pts, end_pts[1:]))
        assert set(flags).issubset({0, 1})
        assert len(flags) == len(coords)

        # Bounds agree exactly with the independent pen/drawing path.
        tight = BoundsPen(glyph_set)
        glyph.draw(tight)
        assert glyph.bounds == tight.bounds

        control = ControlBoundsPen(glyph_set)
        glyph.draw(control)
        assert glyph.controlBounds == control.bounds

        extrema = coordinate_extrema(glyph)
        assert extrema == pytest.approx(control.bounds, abs=1e-9)

        # Integer glyf-style bounds and lazy xMin..xMax attributes.
        xmin, ymin, xmax, ymax = glyph.recalcBounds()
        assert (xmin, ymin, xmax, ymax) == glyph.getBoundingBox()
        assert (glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax) == (
            xmin,
            ymin,
            xmax,
            ymax,
        )
        assert xmin <= xmax and ymin <= ymax


def test_raw_glyf_queries_reflect_varc_components_at_default_location():
    font = TTFont(font_path(SMALL_FONT))
    glyf = font["glyf"]
    glyph_set = font.getGlyphSet()

    for name in varc_names(font):
        raw = glyf[name]
        # The test font stores these VARC glyphs as true empty shells.
        assert raw.numberOfContours == 0

        coords, end_pts, flags = raw.getCoordinates(glyf)
        facade_coords, facade_end_pts, _ = glyph_set[name].getCoordinates()
        assert len(coords) == len(facade_coords)
        assert list(end_pts) == facade_end_pts

        raw.recalcBounds(glyf)
        facade_bbox = glyph_set[name].recalcBounds()
        assert (raw.xMin, raw.yMin, raw.xMax, raw.yMax) == facade_bbox


@pytest.mark.parametrize("location", [None, {"wght": 600}])
def test_varc_coordinates_and_bounds_survive_save_reopen(location):
    font = TTFont(font_path(SMALL_FONT))
    names = varc_names(font)

    def snapshot(f):
        gs = f.getGlyphSet(location=location)
        result = {}
        for name in names[:2]:
            g = gs[name]
            result[name] = (
                len(g.getCoordinates()[0]),
                g.recalcBounds(),
                g.bounds,
            )
        return result

    before = snapshot(font)

    stream = BytesIO()
    font.save(stream)
    stream.seek(0)
    reopened = TTFont(BytesIO(stream.getvalue()))
    after = snapshot(reopened)

    for name in names[:2]:
        n_before, bbox_before, bounds_before = before[name]
        n_after, bbox_after, bounds_after = after[name]
        assert n_after == n_before
        assert bbox_after == bbox_before
        assert bounds_after == pytest.approx(bounds_before, abs=1e-6)

    # Raw glyf queries are location-agnostic and expand at the default
    # location, so only compare them against the default-location snapshot.
    if location:
        return
    glyf = reopened["glyf"]
    name = names[0]
    default_bbox = TTFont(font_path(SMALL_FONT)).getGlyphSet()[name].recalcBounds()
    coords, _, _ = glyf[name].getCoordinates(glyf)
    assert len(coords) == len(
        TTFont(font_path(SMALL_FONT)).getGlyphSet()[name].getCoordinates()[0]
    )
    glyf[name].recalcBounds(glyf)
    assert (
        glyf[name].xMin,
        glyf[name].yMin,
        glyf[name].xMax,
        glyf[name].yMax,
    ) == default_bbox


def test_varc_location_results_do_not_pollute_each_other():
    font = TTFont(font_path(SMALL_FONT))
    low = font.getGlyphSet(location={"wght": 400})["uniAC00"]
    high = font.getGlyphSet(location={"wght": 700})["uniAC00"]

    low_coords = tuple(low.getCoordinates()[0].array)
    high_coords = tuple(high.getCoordinates()[0].array)
    low_bounds = low.bounds
    high_bounds = high.bounds

    # Re-query in interleaved order; each located glyph keeps its own result.
    assert tuple(high.getCoordinates()[0].array) == high_coords
    assert tuple(low.getCoordinates()[0].array) == low_coords
    assert high.bounds == high_bounds
    assert low.bounds == low_bounds

    assert low_coords != high_coords
    assert low_bounds != high_bounds

    # Independent fresh glyph sets agree.
    fresh_low = TTFont(font_path(SMALL_FONT)).getGlyphSet(location={"wght": 400})[
        "uniAC00"
    ]
    assert tuple(fresh_low.getCoordinates()[0].array) == low_coords


def test_shared_nested_component_expands_per_reference():
    font = TTFont(font_path(SMALL_FONT), lazy=False)
    glyph_set = font.getGlyphSet(location={"wght": 600})

    # glyph00004 is shared (via VARC glyph00003 and glyph00007) but placed at
    # different axis locations / transforms; both branches must expand.
    first = glyph_set["glyph00003"].getCoordinates()[0]
    second = glyph_set["glyph00007"].getCoordinates()[0]
    assert len(first) > 0 and len(second) > 0
    assert first[0] != second[0]


def test_invalid_condition_index_raises_clear_error():
    font = TTFont(font_path(SMALL_FONT))
    varc = font["VARC"].table
    component = varc.VarCompositeGlyphs.VarCompositeGlyph[1].components[0]
    component.flags |= VarComponentFlags.HAVE_CONDITION
    component.conditionIndex = 999

    glyph = font.getGlyphSet()["uniAC01"]
    with pytest.raises(IndexError) as exc_info:
        glyph.getCoordinates()
    assert isinstance(exc_info.value, TTLibError)

    with pytest.raises(IndexError):
        glyph.draw(RecordingPen())


def test_invalid_axis_indices_index_raises_clear_error():
    font = TTFont(font_path(SMALL_FONT))
    varc = font["VARC"].table
    component = varc.VarCompositeGlyphs.VarCompositeGlyph[2].components[0]
    component.axisIndicesIndex = 999

    with pytest.raises(IndexError) as exc_info:
        font.getGlyphSet()["glyph00003"].getCoordinates()
    assert isinstance(exc_info.value, TTLibError)


def test_invalid_condition_axis_index_raises_clear_error():
    font = TTFont(font_path(CONDITIONAL_FONT))
    condition = font["VARC"].table.ConditionList.ConditionTable[0]
    assert condition.Format == 1
    condition.AxisIndex = 99

    glyph_set = font.getGlyphSet(location={"wght": 800})
    with pytest.raises(IndexError) as exc_info:
        glyph_set["uniAC01"].draw(RecordingPen())
    assert isinstance(exc_info.value, TTLibError)


def test_component_cycles_are_broken_not_infinite():
    font = TTFont(font_path(SMALL_FONT))
    varc = font["VARC"].table

    # Direct self-reference on uniAC00's first component.
    varc.VarCompositeGlyphs.VarCompositeGlyph[0].components[0].glyphName = "uniAC00"
    glyph = font.getGlyphSet()["uniAC00"]

    coords, end_pts, flags = glyph.getCoordinates()
    assert len(coords) >= 0
    # The non-cyclic sibling component still contributes geometry.
    assert len(coords) > 0
    assert glyph.bounds is not None
    pen = RecordingPen()
    glyph.draw(pen)  # must terminate


def test_indirect_component_cycle_is_broken():
    font = TTFont(font_path(SMALL_FONT))
    varc = font["VARC"].table

    # uniAC00 -> glyph00003 -> uniAC00
    varc.VarCompositeGlyphs.VarCompositeGlyph[2].components[0].glyphName = "uniAC00"
    glyph = font.getGlyphSet()["uniAC00"]
    coords, _, _ = glyph.getCoordinates()
    assert len(coords) > 0
    assert glyph.bounds is not None


def test_ordinary_glyf_glyph_coordinates_are_unchanged():
    font = TTFont(font_path(SMALL_FONT))
    glyf = font["glyf"]
    glyph_set = font.getGlyphSet()

    for name in ["glyph00004", "glyph00006", "glyph00010"]:
        raw_coords, raw_end, raw_flags = glyf[name].getCoordinates(
            glyf, _expandVARC=False
        )
        facade_coords, facade_end, _ = glyph_set[name].getCoordinates()
        assert list(facade_end) == list(raw_end)
        assert len(facade_coords) == len(raw_coords)
        for a, b in zip(facade_coords, raw_coords):
            assert a[0] == b[0] and a[1] == b[1]
