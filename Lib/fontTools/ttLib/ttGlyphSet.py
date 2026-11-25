"""GlyphSets returned by a TTFont."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextlib import contextmanager
from copy import copy, deepcopy
from types import SimpleNamespace
from fontTools.misc.vector import Vector
from fontTools.misc.fixedTools import otRound, fixedToFloat as fi2fl
from fontTools.misc.roundTools import noRound
from fontTools.misc.loggingTools import deprecateFunction
from fontTools.misc.transform import Transform, DecomposedTransform
from fontTools.pens.transformPen import TransformPen, TransformPointPen
from fontTools.pens.recordingPen import (
    DecomposingRecordingPen,
    lerpRecordings,
    replayRecording,
)


class VARCIndexError(IndexError):
    """An index into a VARC table structure is missing or out of range.

    This is an :class:`IndexError` and, when :mod:`fontTools.ttLib` can be
    imported, also a :class:`~fontTools.ttLib.TTLibError`, so callers may
    catch either.  It is raised instead of letting a bare ``IndexError``
    escape with no indication of which VARC index was invalid.
    """


def _varcIndexError(message):
    # Build the dual-base error lazily to avoid a circular import at module
    # load time (ttGlyphSet is imported while fontTools.ttLib is initializing).
    try:
        from fontTools.ttLib import TTLibError
    except Exception:  # pragma: no cover - TTLibError is always available
        return VARCIndexError(message)

    class _VARCIndexError(VARCIndexError, TTLibError):
        pass

    return _VARCIndexError(message)


class _CoordinatesCollectingPointPen:
    """Accumulate absolute points in :py:meth:`Glyph.getCoordinates` order.

    ``addVarComponent`` raises :class:`AttributeError`, which makes
    ``_TTGlyphVARC`` decompose the component through a transform pen, so every
    collected point is already in absolute glyph coordinates and nested VARC
    components are flattened.
    """

    def __init__(self):
        self.coordinates = []
        self.flags = bytearray()
        self.endPts = []

    def beginPath(self, identifier=None, **kwargs):
        pass

    def addPoint(self, pt, segmentType=None, smooth=False, name=None, **kwargs):
        self.coordinates.append((pt[0], pt[1]))
        # glyf flag bit 0 is the "on curve" bit; the other flag bits describe
        # the binary encoding and are not meaningful for synthesized geometry.
        self.flags.append(1 if segmentType is not None else 0)

    def endPath(self):
        if self.coordinates:
            self.endPts.append(len(self.coordinates) - 1)

    def addComponent(self, glyphName, transformation, **kwargs):
        # Traditional glyf composites are decomposed by the glyf glyph itself
        # before reaching this pen. If a caller feeds us a raw component,
        # refuse loudly rather than silently dropping its points.
        raise NotImplementedError

    def addVarComponent(self, *args, **kwargs):
        # ttGlyphSet decomposes for us.
        raise AttributeError


def _getVariationAxes(font):
    if "fvar" in font:
        return font["fvar"].axes

    # VARC permits static fonts to retain gvar or CFF2 variation data for
    # component-internal coordinates without exposing axes through fvar.
    axisCount = 0
    gvar = font.get("GVAR" if "GVAR" in font else "gvar")
    if gvar is not None:
        axisCount = gvar.axisCount
    if "CFF2" in font:
        cff = next(iter(font["CFF2"].cff.values()))
        varStore = getattr(cff.CharStrings, "varStore", None)
        if varStore is not None:
            axisCount = max(
                axisCount, varStore.otVarStore.VarRegionList.RegionAxisCount
            )
    return [SimpleNamespace(axisTag=i) for i in range(axisCount)]


class _TTGlyphSet(Mapping):
    """Generic dict-like GlyphSet class that pulls metrics from hmtx and
    glyph shape from TrueType or CFF.
    """

    def __init__(
        self,
        font,
        location,
        glyphsMapping,
        *,
        hMetricsTag="hmtx",
        vMetricsTag="vmtx",
        recalcBounds=True,
    ):
        self.recalcBounds = recalcBounds
        self.font = font
        self.axes = _getVariationAxes(font)
        self.defaultLocationNormalized = {axis.axisTag: 0 for axis in self.axes}
        self.location = location if location is not None else {}
        self.rawLocation = {}  # VarComponent-only location
        self.originalLocation = location if location is not None else {}
        self.depth = 0
        self.locationStack = []
        self.rawLocationStack = []
        self.glyphsMapping = glyphsMapping
        self._hMetricsTag = hMetricsTag
        self._vMetricsTag = vMetricsTag
        self.hMetrics = font[hMetricsTag].metrics
        self.vMetrics = getattr(font.get(vMetricsTag), "metrics", None)
        self.hvarTable = None
        if location:
            from fontTools.varLib.varStore import VarStoreInstancer

            self.hvarTable = getattr(font.get("HVAR"), "table", None)
            if self.hvarTable is not None:
                self.hvarInstancer = VarStoreInstancer(
                    self.hvarTable.VarStore, self.axes, location
                )
            # TODO VVAR, VORG

    @contextmanager
    def pushLocation(self, location, reset: bool):
        self.locationStack.append(self.location)
        self.rawLocationStack.append(self.rawLocation)
        if reset:
            self.location = self.originalLocation.copy()
            self.rawLocation = self.defaultLocationNormalized.copy()
        else:
            self.location = self.location.copy()
            self.rawLocation = {}
        self.location.update(location)
        self.rawLocation.update(location)

        try:
            yield None
        finally:
            self.location = self.locationStack.pop()
            self.rawLocation = self.rawLocationStack.pop()

    @contextmanager
    def pushDepth(self):
        try:
            depth = self.depth
            self.depth += 1
            yield depth
        finally:
            self.depth -= 1

    def __contains__(self, glyphName):
        return glyphName in self.glyphsMapping

    def __iter__(self):
        return iter(self.glyphsMapping.keys())

    def __len__(self):
        return len(self.glyphsMapping)

    @deprecateFunction(
        "use 'glyphName in glyphSet' instead", category=DeprecationWarning
    )
    def has_key(self, glyphName):
        return glyphName in self.glyphsMapping


class _TTGlyphSetGlyf(_TTGlyphSet):
    def __init__(self, font, location, glyfTag, recalcBounds=True):
        self.glyfTable = font[glyfTag]
        super().__init__(
            font,
            location,
            self.glyfTable,
            hMetricsTag=self.glyfTable.hmtxTag,
            vMetricsTag=self.glyfTable.vmtxTag,
            recalcBounds=recalcBounds,
        )
        self.gvarTable = font.get(self.glyfTable.gvarTag)

    def __getitem__(self, glyphName):
        return _TTGlyphGlyf(self, glyphName, recalcBounds=self.recalcBounds)


class _TTGlyphSetCFF(_TTGlyphSet):
    def __init__(self, font, location):
        tableTag = "CFF2" if "CFF2" in font else "CFF "
        self.charStrings = list(font[tableTag].cff.values())[0].CharStrings
        super().__init__(font, location, self.charStrings)
        self.setLocation(location)

    def __getitem__(self, glyphName):
        return _TTGlyphCFF(self, glyphName)

    def setLocation(self, location):
        self.blender = None
        if location:
            # TODO Optimize by using instancer.setLocation()

            from fontTools.varLib.varStore import VarStoreInstancer

            varStore = getattr(self.charStrings, "varStore", None)
            if varStore is not None:
                instancer = VarStoreInstancer(varStore.otVarStore, self.axes, location)
                self.blender = instancer.interpolateFromDeltas
        else:
            self.blender = None

    @contextmanager
    def pushLocation(self, location, reset: bool):
        self.setLocation(location)
        with _TTGlyphSet.pushLocation(self, location, reset) as value:
            try:
                yield value
            finally:
                self.setLocation(self.location)


class _TTGlyphSetVARC(_TTGlyphSet):
    def __init__(self, font, location, glyphSet):
        self.glyphSet = glyphSet
        super().__init__(
            font,
            location,
            dict.fromkeys(font.getGlyphOrder()),
            hMetricsTag=glyphSet._hMetricsTag,
            vMetricsTag=glyphSet._vMetricsTag,
        )
        self.varcTable = font["VARC"].table

    def __getitem__(self, glyphName):
        varc = self.varcTable
        if glyphName not in varc.Coverage.glyphs:
            return self.glyphSet[glyphName]
        return _TTGlyphVARC(self, glyphName)


class _TTGlyph(ABC):
    """Glyph object that supports the Pen protocol, meaning that it has
    .draw() and .drawPoints() methods that take a pen object as their only
    argument. Additionally there are 'width' and 'lsb' attributes, read from
    the 'hmtx' table.

    If the font contains a 'vmtx' table, there will also be 'height' and 'tsb'
    attributes.
    """

    def __init__(self, glyphSet, glyphName, *, recalcBounds=True):
        self.glyphSet = glyphSet
        self.name = glyphName
        # Stored under a private name so the public ``recalcBounds`` below can
        # be the coordinate/bounds query method without an instance boolean
        # shadowing it. The constructor keyword is unchanged.
        self._recalcBounds = recalcBounds
        self.width, self.lsb = glyphSet.hMetrics[glyphName]
        if glyphSet.vMetrics is not None:
            self.height, self.tsb = glyphSet.vMetrics[glyphName]
        else:
            self.height, self.tsb = None, None
        if glyphSet.location and glyphSet.hvarTable is not None:
            varidx = (
                glyphSet.font.getGlyphID(glyphName)
                if glyphSet.hvarTable.AdvWidthMap is None
                else glyphSet.hvarTable.AdvWidthMap.mapping[glyphName]
            )
            self.width += glyphSet.hvarInstancer[varidx]
        # TODO: VVAR/VORG

    @abstractmethod
    def draw(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.basePen for details
        how that works.
        """
        raise NotImplementedError

    def drawPoints(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.pointPen for details
        how that works.
        """
        from fontTools.pens.pointPen import SegmentToPointPen

        self.draw(SegmentToPointPen(pen))

    def getCoordinates(self, glyfTable=None, *, round=noRound):
        """Return the flattened, variation-specific coordinates of the glyph.

        Returns a ``(coordinates, endPtsOfContours, flags)`` triple with the
        same conventions as
        :meth:`fontTools.ttLib.tables._g_l_y_f.Glyph.getCoordinates`.

        Unlike the raw ``glyf`` glyph, this goes through the glyph set, so for
        a VARC glyph the variable components are expanded (including their
        conditions, axis locations and affine transforms) at the glyph set's
        current location. Nested components are flattened into absolute
        coordinates.
        """
        from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates

        pen = _CoordinatesCollectingPointPen()
        self.drawPoints(pen)
        coordinates = GlyphCoordinates(pen.coordinates)
        if round is not noRound:
            coordinates.toInt(round=round)
        return coordinates, list(pen.endPts), pen.flags

    @property
    def coordinates(self):
        """Flattened absolute :class:`GlyphCoordinates` at this location."""
        return self.getCoordinates()[0]

    @property
    def endPtsOfContours(self):
        """End indices of the flattened contours."""
        return self.getCoordinates()[1]

    @property
    def flags(self):
        """Per-point on-curve flags for the flattened coordinates."""
        return self.getCoordinates()[2]

    def getBounds(self, *, control=False):
        """Return the glyph's bounding box at this location.

        By default this is the tight outline bounding box (equivalent to
        :class:`fontTools.pens.boundsPen.BoundsPen`). Pass ``control=True``
        to also include off-curve control points, equivalent to
        :class:`fontTools.pens.boundsPen.ControlBoundsPen`. ``None`` is
        returned for an empty glyph.
        """
        from fontTools.pens.boundsPen import BoundsPen, ControlBoundsPen

        pen = (ControlBoundsPen if control else BoundsPen)(self.glyphSet)
        self.draw(pen)
        return pen.bounds

    @property
    def bounds(self):
        """Tight outline bounds at this location, or ``None`` if empty."""
        return self.getBounds()

    @property
    def controlBounds(self):
        """Control-point-inclusive bounds, or ``None`` if empty."""
        return self.getBounds(control=True)

    def recalcBounds(self, glyfTable=None, *, boundsDone=None):
        """Compute the integer bounding box from the flattened coordinates.

        Mirrors :meth:`_g_l_y_f.Glyph.recalcBounds` and stores the result in
        the ``xMin``/``yMin``/``xMax``/``yMax`` attributes, which are also
        populated lazily on first access. The four-value tuple is returned.
        """
        coordinates, _, _ = self.getCoordinates(glyfTable, round=otRound)
        if not len(coordinates):
            bbox = (0, 0, 0, 0)
        else:
            bbox = tuple(otRound(v) for v in coordinates.calcBounds())
        self.xMin, self.yMin, self.xMax, self.yMax = bbox
        return bbox

    def getBoundingBox(self):
        """Integer ``(xMin, yMin, xMax, yMax)`` box at this location.

        Alias for :meth:`recalcBounds` without arguments.
        """
        return self.recalcBounds()

    def __getattr__(self, name):
        # Provide lazy glyf-style bounding-box attributes. Only invoked when
        # normal attribute lookup fails, so values explicitly set by
        # recalcBounds() (or during __init__) are returned directly.
        if name in ("xMin", "yMin", "xMax", "yMax"):
            bbox = self.recalcBounds()
            return bbox[("xMin", "yMin", "xMax", "yMax").index(name)]
        raise AttributeError(
            f"{type(self).__module__}.{type(self).__name__!r} "
            f"object has no attribute {name!r}"
        )


class _TTGlyphGlyf(_TTGlyph):
    def draw(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.basePen for details
        how that works.
        """
        glyph, offset = self._getGlyphAndOffset()

        with self.glyphSet.pushDepth() as depth:
            if depth:
                offset = 0  # Offset should only apply at top-level

            glyph.draw(pen, self.glyphSet.glyfTable, offset)

    def drawPoints(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.pointPen for details
        how that works.
        """
        glyph, offset = self._getGlyphAndOffset()

        with self.glyphSet.pushDepth() as depth:
            if depth:
                offset = 0  # Offset should only apply at top-level

            glyph.drawPoints(pen, self.glyphSet.glyfTable, offset)

    def _getGlyphAndOffset(self):
        if self.glyphSet.location and self.glyphSet.gvarTable is not None:
            glyph = self._getGlyphInstance()
        else:
            glyph = self.glyphSet.glyfTable[self.name]

        offset = self.lsb - glyph.xMin if hasattr(glyph, "xMin") else 0
        return glyph, offset

    def getCoordinates(self, glyfTable=None, *, round=noRound):
        # Delegate to the underlying (possibly variation-instanced) glyf glyph
        # so ordinary simple/traditional-composite glyphs return exactly the
        # established glyf coordinates, without the top-level lsb offset used
        # by drawing.
        glyph, _ = self._getGlyphAndOffset()
        if glyfTable is None:
            glyfTable = self.glyphSet.glyfTable
        return glyph.getCoordinates(glyfTable, round=round, _expandVARC=False)

    def _getGlyphInstance(self):
        from fontTools.varLib.iup import iup_delta
        from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
        from fontTools.varLib.models import supportScalar

        glyphSet = self.glyphSet
        glyfTable = glyphSet.glyfTable
        variations = glyphSet.gvarTable.variations[self.name]
        hMetrics = glyphSet.hMetrics
        vMetrics = glyphSet.vMetrics
        coordinates, _ = glyfTable._getCoordinatesAndControls(
            self.name, hMetrics, vMetrics
        )
        origCoords, endPts = None, None
        for var in variations:
            scalar = supportScalar(glyphSet.location, var.axes)
            if not scalar:
                continue
            delta = var.coordinates
            if None in delta:
                if origCoords is None:
                    origCoords, control = glyfTable._getCoordinatesAndControls(
                        self.name, hMetrics, vMetrics
                    )
                    endPts = (
                        control[1] if control[0] >= 1 else list(range(len(control[1])))
                    )
                delta = iup_delta(delta, origCoords, endPts)
            coordinates += GlyphCoordinates(delta) * scalar

        glyph = copy(glyfTable[self.name])  # Shallow copy
        width, lsb, height, tsb = _setCoordinates(
            glyph,
            coordinates,
            glyfTable,
            recalcBounds=self._recalcBounds,
        )
        self.lsb = lsb
        self.tsb = tsb
        if glyphSet.hvarTable is None:
            # no HVAR: let's set metrics from the phantom points
            self.width = width
            self.height = height
        return glyph


class _TTGlyphCFF(_TTGlyph):
    def draw(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.basePen for details
        how that works.
        """
        self.glyphSet.charStrings[self.name].draw(pen, self.glyphSet.blender)


def _evaluateCondition(condition, axes, location, instancer):
    if condition is None:
        # A null condition offset denotes True.
        return True

    if condition.Format == 1:
        # ConditionAxisRange
        axisIndex = condition.AxisIndex
        if not isinstance(axisIndex, int) or not (0 <= axisIndex < len(axes)):
            raise _varcIndexError(
                f"VARC condition axis index {axisIndex!r} is out of range "
                f"for {len(axes)} variation axis/axes"
            )
        axisTag = axes[axisIndex].axisTag
        axisValue = location.get(axisTag, 0)
        minValue = condition.FilterRangeMinValue
        maxValue = condition.FilterRangeMaxValue
        return minValue <= axisValue <= maxValue
    elif condition.Format == 2:
        # ConditionValue
        from fontTools.ttLib.tables.otTables import NO_VARIATION_INDEX

        value = condition.DefaultValue
        # NO_VARIATION_INDEX has a zero delta; the multi-store instancer returns
        # an empty vector because it does not know the number of values.
        if condition.VarIdx != NO_VARIATION_INDEX:
            value += instancer[condition.VarIdx][0]
        return value > 0
    elif condition.Format == 3:
        # ConditionAnd
        for subcondition in condition.ConditionTable:
            if not _evaluateCondition(subcondition, axes, location, instancer):
                return False
        return True
    elif condition.Format == 4:
        # ConditionOr
        for subcondition in condition.ConditionTable:
            if _evaluateCondition(subcondition, axes, location, instancer):
                return True
        return False
    elif condition.Format == 5:
        # ConditionNegate
        return not _evaluateCondition(
            condition.ConditionTable, axes, location, instancer
        )
    else:
        return False  # Unkonwn condition format


class _TTGlyphVARC(_TTGlyph):
    def _varcGlyphRecord(self, varc):
        """Return the matching VarCompositeGlyph record, validating indices."""
        coverage = varc.Coverage.glyphs
        try:
            idx = coverage.index(self.name)
        except ValueError:
            raise _varcIndexError(
                f"Glyph {self.name!r} is not listed in the VARC Coverage table"
            ) from None

        records = varc.VarCompositeGlyphs.VarCompositeGlyph
        if not (0 <= idx < len(records)):
            raise _varcIndexError(
                f"VARC glyph index {idx} for glyph {self.name!r} is out of "
                f"range: Coverage lists {len(coverage)} glyph(s) but "
                f"VarCompositeGlyphs contains {len(records)} record(s)"
            )
        return records[idx]

    @staticmethod
    def _conditionTable(varc, conditionIndex, glyphName):
        conditionList = getattr(varc, "ConditionList", None)
        table = getattr(conditionList, "ConditionTable", None)
        if (
            table is None
            or conditionIndex is None
            or not (0 <= conditionIndex < len(table))
        ):
            count = len(table) if table is not None else 0
            raise _varcIndexError(
                f"VARC condition index {conditionIndex!r} referenced by glyph "
                f"{glyphName!r} is out of range (0..{max(count - 1, -1)})"
            )
        return table[conditionIndex]

    @staticmethod
    def _axisIndices(varc, axisIndicesIndex, glyphName):
        item = getattr(getattr(varc, "AxisIndicesList", None), "Item", None)
        if (
            item is None
            or axisIndicesIndex is None
            or not (0 <= axisIndicesIndex < len(item))
        ):
            count = len(item) if item is not None else 0
            raise _varcIndexError(
                f"VARC axis-indices index {axisIndicesIndex!r} referenced by "
                f"glyph {glyphName!r} is out of range "
                f"(0..{max(count - 1, -1)})"
            )
        return item[axisIndicesIndex]

    def _draw(self, pen, isPointPen, *, _active=None):
        """Draw the glyph onto ``pen``. See fontTools.pens.basePen for details
        how that works.

        ``_active`` is the set of VARC glyph names already being expanded on
        the current recursion path; it breaks direct and indirect component
        cycles by falling back to the raw non-VARC glyph.
        """
        from fontTools.ttLib.tables.otTables import (
            VarComponentFlags,
            NO_VARIATION_INDEX,
        )

        glyphSet = self.glyphSet
        varc = glyphSet.varcTable
        glyph = self._varcGlyphRecord(varc)

        active = set(_active) if _active else set()
        active.add(self.name)

        from fontTools.varLib.multiVarStore import MultiVarStoreInstancer

        axes = glyphSet.axes
        instancer = MultiVarStoreInstancer(
            varc.MultiVarStore, axes, self.glyphSet.location
        )

        for comp in glyph.components:
            if comp.flags & VarComponentFlags.HAVE_CONDITION:
                condition = self._conditionTable(varc, comp.conditionIndex, self.name)
                if not _evaluateCondition(
                    condition, axes, self.glyphSet.location, instancer
                ):
                    continue

            location = {}
            if comp.axisIndicesIndex is not None:
                axisIndices = self._axisIndices(varc, comp.axisIndicesIndex, self.name)
                axisValues = Vector(comp.axisValues)
                if comp.axisValuesVarIndex != NO_VARIATION_INDEX:
                    axisValues += fi2fl(instancer[comp.axisValuesVarIndex], 14)
                assert len(axisIndices) == len(axisValues), (
                    len(axisIndices),
                    len(axisValues),
                )
                location = {axes[i].axisTag: v for i, v in zip(axisIndices, axisValues)}

            if comp.transformVarIndex != NO_VARIATION_INDEX:
                deltas = instancer[comp.transformVarIndex]
                comp = deepcopy(comp)
                comp.applyTransformDeltas(deltas)
            transform = comp.transform

            reset = comp.flags & VarComponentFlags.RESET_UNSPECIFIED_AXES
            with self.glyphSet.glyphSet.pushLocation(location, reset):
                with self.glyphSet.pushLocation(location, reset):
                    # A component whose glyph is already on the expansion path
                    # would recurse forever. Render its raw non-VARC (glyf/CFF)
                    # glyph once instead, matching the direct-self handling.
                    cyclic = comp.glyphName in active
                    shouldDecompose = cyclic

                    if not shouldDecompose:
                        try:
                            pen.addVarComponent(
                                comp.glyphName, transform, self.glyphSet.rawLocation
                            )
                        except AttributeError:
                            shouldDecompose = True

                    if shouldDecompose:
                        t = transform.toTransform()
                        if cyclic:
                            compGlyphSet = glyphSet.glyphSet
                            childActive = None
                        else:
                            compGlyphSet = self.glyphSet
                            childActive = active
                        g = compGlyphSet[comp.glyphName]
                        if isPointPen:
                            tPen = TransformPointPen(pen, t)
                            if isinstance(g, _TTGlyphVARC):
                                g.drawPoints(tPen, _active=childActive)
                            else:
                                g.drawPoints(tPen)
                        else:
                            tPen = TransformPen(pen, t)
                            if isinstance(g, _TTGlyphVARC):
                                g.draw(tPen, _active=childActive)
                            else:
                                g.draw(tPen)

    def draw(self, pen, *, _active=None):
        self._draw(pen, False, _active=_active)

    def drawPoints(self, pen, *, _active=None):
        self._draw(pen, True, _active=_active)


def _setCoordinates(glyph, coord, glyfTable, *, recalcBounds=True):
    # Handle phantom points for (left, right, top, bottom) positions.
    assert len(coord) >= 4
    leftSideX = coord[-4][0]
    rightSideX = coord[-3][0]
    topSideY = coord[-2][1]
    bottomSideY = coord[-1][1]

    for _ in range(4):
        del coord[-1]

    if glyph.isComposite():
        assert len(coord) == len(glyph.components)
        glyph.components = [copy(comp) for comp in glyph.components]  # Shallow copy
        for p, comp in zip(coord, glyph.components):
            if hasattr(comp, "x"):
                comp.x, comp.y = p
    elif glyph.numberOfContours == 0:
        assert len(coord) == 0
    else:
        assert len(coord) == len(glyph.coordinates)
        glyph.coordinates = coord

    if recalcBounds:
        glyph.recalcBounds(glyfTable)

    horizontalAdvanceWidth = otRound(rightSideX - leftSideX)
    verticalAdvanceWidth = otRound(topSideY - bottomSideY)
    leftSideBearing = otRound(glyph.xMin - leftSideX)
    topSideBearing = otRound(topSideY - glyph.yMax)
    return (
        horizontalAdvanceWidth,
        leftSideBearing,
        verticalAdvanceWidth,
        topSideBearing,
    )


class LerpGlyphSet(Mapping):
    """A glyphset that interpolates between two other glyphsets.

    Factor is typically between 0 and 1. 0 means the first glyphset,
    1 means the second glyphset, and 0.5 means the average of the
    two glyphsets. Other values are possible, and can be useful to
    extrapolate. Defaults to 0.5.
    """

    def __init__(self, glyphset1, glyphset2, factor=0.5):
        self.glyphset1 = glyphset1
        self.glyphset2 = glyphset2
        self.factor = factor

    def __getitem__(self, glyphname):
        if glyphname in self.glyphset1 and glyphname in self.glyphset2:
            return LerpGlyph(glyphname, self)
        raise KeyError(glyphname)

    def __contains__(self, glyphname):
        return glyphname in self.glyphset1 and glyphname in self.glyphset2

    def __iter__(self):
        set1 = set(self.glyphset1)
        set2 = set(self.glyphset2)
        return iter(set1.intersection(set2))

    def __len__(self):
        set1 = set(self.glyphset1)
        set2 = set(self.glyphset2)
        return len(set1.intersection(set2))


class LerpGlyph:
    def __init__(self, glyphname, glyphset):
        self.glyphset = glyphset
        self.glyphname = glyphname

    def draw(self, pen):
        recording1 = DecomposingRecordingPen(self.glyphset.glyphset1)
        self.glyphset.glyphset1[self.glyphname].draw(recording1)
        recording2 = DecomposingRecordingPen(self.glyphset.glyphset2)
        self.glyphset.glyphset2[self.glyphname].draw(recording2)

        factor = self.glyphset.factor

        replayRecording(lerpRecordings(recording1.value, recording2.value, factor), pen)
