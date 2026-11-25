# Copyright 2013 Google, Inc. All Rights Reserved.
#
# Google Author(s): Behdad Esfahbod, Roozbeh Pournader

from fontTools import ttLib, cffLib
from fontTools.misc.psCharStrings import T2WidthExtractor, calcSubrBias
from fontTools.ttLib.tables.DefaultTable import DefaultTable
from fontTools.merge.base import add_method, mergeObjects
from fontTools.merge.cmap import computeMegaCmap
from fontTools.merge.util import *
import logging

log = logging.getLogger("fontTools.merge")


ttLib.getTableClass("maxp").mergeMap = {
    "*": max,
    "tableTag": equal,
    "tableVersion": equal,
    "numGlyphs": sum,
    "maxStorage": first,
    "maxFunctionDefs": first,
    "maxInstructionDefs": first,
    # TODO When we correctly merge hinting data, update these values:
    # maxFunctionDefs, maxInstructionDefs, maxSizeOfInstructions
}
ttLib.getTableClass("MAXP").mergeMap = ttLib.getTableClass("maxp").mergeMap

headFlagsMergeBitMap = {
    "size": 16,
    "*": bitwise_or,
    1: bitwise_and,  # Baseline at y = 0
    2: bitwise_and,  # lsb at x = 0
    3: bitwise_and,  # Force ppem to integer values. FIXME?
    5: bitwise_and,  # Font is vertical
    6: lambda bit: 0,  # Always set to zero
    11: bitwise_and,  # Font data is 'lossless'
    13: bitwise_and,  # Optimized for ClearType
    14: bitwise_and,  # Last resort font. FIXME? equal or first may be better
    15: lambda bit: 0,  # Always set to zero
}


def mergeUnitsPerEm(lst):
    lst = list(lst)
    if any(item != lst[0] for item in lst[1:]):
        raise ValueError(
            "Cannot merge fonts with different unitsPerEm values: %s" % lst
        )
    return lst[0]


ttLib.getTableClass("head").mergeMap = {
    "tableTag": equal,
    "tableVersion": max,
    "fontRevision": max,
    "checkSumAdjustment": lambda lst: 0,  # We need *something* here
    "magicNumber": equal,
    "flags": mergeBits(headFlagsMergeBitMap),
    "unitsPerEm": mergeUnitsPerEm,
    "created": current_time,
    "modified": current_time,
    "xMin": min,
    "yMin": min,
    "xMax": max,
    "yMax": max,
    "macStyle": first,
    "lowestRecPPEM": max,
    "fontDirectionHint": lambda lst: 2,
    "indexToLocFormat": first,
    "glyphDataFormat": equal,
}

ttLib.getTableClass("hhea").mergeMap = {
    "*": equal,
    "tableTag": equal,
    "tableVersion": max,
    "ascent": max,
    "descent": min,
    "lineGap": max,
    "advanceWidthMax": max,
    "minLeftSideBearing": min,
    "minRightSideBearing": min,
    "xMaxExtent": max,
    "caretSlopeRise": first,
    "caretSlopeRun": first,
    "caretOffset": first,
    "numberOfHMetrics": recalculate,
}
ttLib.getTableClass("HHEA").mergeMap = ttLib.getTableClass("hhea").mergeMap

ttLib.getTableClass("vhea").mergeMap = {
    "*": equal,
    "tableTag": equal,
    "tableVersion": max,
    "ascent": max,
    "descent": min,
    "lineGap": max,
    "advanceHeightMax": max,
    "minTopSideBearing": min,
    "minBottomSideBearing": min,
    "yMaxExtent": max,
    "caretSlopeRise": first,
    "caretSlopeRun": first,
    "caretOffset": first,
    "numberOfVMetrics": recalculate,
}
ttLib.getTableClass("VHEA").mergeMap = ttLib.getTableClass("vhea").mergeMap

os2FsTypeMergeBitMap = {
    "size": 16,
    "*": lambda bit: 0,
    1: bitwise_or,  # no embedding permitted
    2: bitwise_and,  # allow previewing and printing documents
    3: bitwise_and,  # allow editing documents
    8: bitwise_or,  # no subsetting permitted
    9: bitwise_or,  # no embedding of outlines permitted
}


def mergeOs2FsType(lst):
    lst = list(lst)
    if all(item == 0 for item in lst):
        return 0

    # Compute least restrictive logic for each fsType value
    for i in range(len(lst)):
        # unset bit 1 (no embedding permitted) if either bit 2 or 3 is set
        if lst[i] & 0x000C:
            lst[i] &= ~0x0002
        # set bit 2 (allow previewing) if bit 3 is set (allow editing)
        elif lst[i] & 0x0008:
            lst[i] |= 0x0004
        # set bits 2 and 3 if everything is allowed
        elif lst[i] == 0:
            lst[i] = 0x000C

    fsType = mergeBits(os2FsTypeMergeBitMap)(lst)
    # unset bits 2 and 3 if bit 1 is set (some font is "no embedding")
    if fsType & 0x0002:
        fsType &= ~0x000C
    return fsType


ttLib.getTableClass("OS/2").mergeMap = {
    "*": first,
    "tableTag": equal,
    "version": max,
    "xAvgCharWidth": first,  # Will be recalculated at the end on the merged font
    "fsType": mergeOs2FsType,  # Will be overwritten
    "panose": first,  # FIXME: should really be the first Latin font
    "ulUnicodeRange1": bitwise_or,
    "ulUnicodeRange2": bitwise_or,
    "ulUnicodeRange3": bitwise_or,
    "ulUnicodeRange4": bitwise_or,
    "fsFirstCharIndex": min,
    "fsLastCharIndex": max,
    "sTypoAscender": max,
    "sTypoDescender": min,
    "sTypoLineGap": max,
    "usWinAscent": max,
    "usWinDescent": max,
    # Version 1
    "ulCodePageRange1": onlyExisting(bitwise_or),
    "ulCodePageRange2": onlyExisting(bitwise_or),
    # Version 2, 3, 4
    "sxHeight": onlyExisting(max),
    "sCapHeight": onlyExisting(max),
    "usDefaultChar": onlyExisting(first),
    "usBreakChar": onlyExisting(first),
    "usMaxContext": onlyExisting(max),
    # version 5
    "usLowerOpticalPointSize": onlyExisting(min),
    "usUpperOpticalPointSize": onlyExisting(max),
}


@add_method(ttLib.getTableClass("OS/2"))
def merge(self, m, tables):
    DefaultTable.merge(self, m, tables)
    if self.version < 2:
        # bits 8 and 9 are reserved and should be set to zero
        self.fsType &= ~0x0300
    if self.version >= 3:
        # Only one of bits 1, 2, and 3 may be set. We already take
        # care of bit 1 implications in mergeOs2FsType. So unset
        # bit 2 if bit 3 is already set.
        if self.fsType & 0x0008:
            self.fsType &= ~0x0004
    return self


ttLib.getTableClass("post").mergeMap = {
    "*": first,
    "tableTag": equal,
    "formatType": max,
    "isFixedPitch": min,
    "minMemType42": max,
    "maxMemType42": lambda lst: 0,
    "minMemType1": max,
    "maxMemType1": lambda lst: 0,
    "mapping": onlyExisting(sumDicts),
    "extraNames": lambda lst: [],
}

ttLib.getTableClass("vmtx").mergeMap = ttLib.getTableClass("hmtx").mergeMap = {
    "tableTag": equal,
    "metrics": sumDicts,
}
ttLib.getTableClass("HMTX").mergeMap = ttLib.getTableClass("hmtx").mergeMap
ttLib.getTableClass("VMTX").mergeMap = ttLib.getTableClass("vmtx").mergeMap

ttLib.getTableClass("name").mergeMap = {
    "tableTag": equal,
    "names": first,  # FIXME? Does mixing name records make sense?
}

ttLib.getTableClass("loca").mergeMap = {
    "*": recalculate,
    "tableTag": equal,
}
ttLib.getTableClass("LOCA").mergeMap = ttLib.getTableClass("loca").mergeMap

ttLib.getTableClass("glyf").mergeMap = {
    "tableTag": equal,
    "glyphs": sumDicts,
    "glyphOrder": sumLists,
    "_reverseGlyphOrder": recalculate,
    "axisTags": equal,
}
ttLib.getTableClass("GLYF").mergeMap = ttLib.getTableClass("glyf").mergeMap


@add_method(ttLib.getTableClass("glyf"), ttLib.getTableClass("GLYF"))
def merge(self, m, tables):
    for i, table in enumerate(tables):
        for g in table.glyphs.values():
            if i:
                # Drop hints for all but first font, since
                # we don't map functions / CVT values.
                g.removeHinting(extended=table.extended)
            # Expand composite glyphs to load their
            # composite glyph names.
            if g.isComposite():
                g.expand(table)
    return DefaultTable.merge(self, m, tables)


ttLib.getTableClass("prep").mergeMap = lambda self, lst: first(lst)
ttLib.getTableClass("fpgm").mergeMap = lambda self, lst: first(lst)
ttLib.getTableClass("cvt ").mergeMap = lambda self, lst: first(lst)
ttLib.getTableClass("gasp").mergeMap = lambda self, lst: first(
    lst
)  # FIXME? Appears irreconcilable


def _isCIDFont(topDict):
    return hasattr(topDict, "FDSelect")


def _getCID(glyphName):
    """Return the CID encoded in a CID-keyed charset entry name.

    Handles ``cidNNNNN`` names, disambiguation suffixes (``cid00001.1``), and
    the ``.notdef`` slot (CID 0, including a renamed ``.notdef.1``).
    """
    if glyphName == ".notdef" or glyphName.startswith(".notdef."):
        return 0
    baseName = glyphName.split(".", 1)[0]
    if not baseName.startswith("cid") or not baseName[3:].isdigit():
        raise ValueError(
            "CID-keyed CFF charset entry %r is not a valid CID glyph name" % glyphName
        )
    return cffLib.getCIDfromName(baseName, None)


def _validateSubrReferences(charString, localSubrs, globalSubrs, where):
    """Make sure every callsubr/callgsubr operand in ``charString`` resolves.

    Raises ``ValueError`` instead of letting an ``IndexError``/``AssertionError``
    escape deep in the charstring machinery, so a broken font never produces a
    merged font that saves fine but draws the wrong outlines.
    """
    try:
        charString.decompile()
    except (IndexError, AssertionError) as e:
        raise ValueError(
            "Charstring %s has an unresolvable subroutine reference: %s" % (where, e)
        ) from e
    program = charString.program
    localBias = calcSubrBias(localSubrs)
    globalBias = calcSubrBias(globalSubrs)
    for index in range(1, len(program)):
        token = program[index]
        if token == "callsubr":
            subrIndex = program[index - 1]
            if not isinstance(subrIndex, int) or not (
                -localBias <= subrIndex < len(localSubrs) - localBias
            ):
                raise ValueError(
                    "Charstring %s references missing local subroutine %d"
                    % (where, subrIndex)
                )
        elif token == "callgsubr":
            subrIndex = program[index - 1]
            if not isinstance(subrIndex, int) or not (
                -globalBias <= subrIndex < len(globalSubrs) - globalBias
            ):
                raise ValueError(
                    "Charstring %s references missing global subroutine %d"
                    % (where, subrIndex)
                )


def _remapGSubrReferences(charString, oldBias, offset, newBias):
    """Rewrite callgsubr operands after global subroutines are concatenated.

    ``offset`` is the index at which the font's own global subroutines were
    appended to the merged global subroutine INDEX.
    """
    charString.decompile()
    program = charString.program
    for index in range(1, len(program)):
        if program[index] == "callgsubr":
            assert isinstance(program[index - 1], int)
            program[index - 1] = (program[index - 1] + oldBias + offset) - newBias


def _iterCIDCharStrings(font, globalSubrs):
    """Yield every T2CharString in a CID-keyed font that can hold subroutine
    calls: the glyph charstrings, the local subroutines of every FontDict, and
    the global subroutines.
    """
    for glyphName in font.charset:
        charString, _ = font.CharStrings.getItemAndSelector(glyphName)
        yield charString
    for fd in font.FDArray:
        localSubrs = getattr(fd.Private, "Subrs", None)
        if localSubrs:
            for subr in localSubrs:
                yield subr
    for subr in globalSubrs:
        yield subr


@add_method(ttLib.getTableClass("CFF "))
def merge(self, m, tables):
    if any(_isCIDFont(table.cff[0]) for table in tables):
        return _mergeCIDCFF(tables)
    return _mergeNameKeyedCFF(tables)


def _mergeNameKeyedCFF(tables):
    for table in tables:
        table.cff.desubroutinize()

    newcff = tables[0]
    newfont = newcff.cff[0]
    private = newfont.Private
    newDefaultWidthX, newNominalWidthX = private.defaultWidthX, private.nominalWidthX
    storedNamesStrings = []
    glyphOrderStrings = []
    glyphOrder = set(newfont.getGlyphOrder())

    for name in newfont.strings.strings:
        if name not in glyphOrder:
            storedNamesStrings.append(name)
        else:
            glyphOrderStrings.append(name)

    chrset = list(newfont.charset)
    newcs = newfont.CharStrings
    log.debug("FONT 0 CharStrings: %d.", len(newcs))

    for i, table in enumerate(tables[1:], start=1):
        font = table.cff[0]
        defaultWidthX, nominalWidthX = (
            font.Private.defaultWidthX,
            font.Private.nominalWidthX,
        )
        widthsDiffer = (
            defaultWidthX != newDefaultWidthX or nominalWidthX != newNominalWidthX
        )
        font.Private = private
        fontGlyphOrder = set(font.getGlyphOrder())
        for name in font.strings.strings:
            if name in fontGlyphOrder:
                glyphOrderStrings.append(name)
        cs = font.CharStrings
        gs = table.cff.GlobalSubrs
        log.debug("Font %d CharStrings: %d.", i, len(cs))
        chrset.extend(font.charset)
        if newcs.charStringsAreIndexed:
            for i, name in enumerate(cs.charStrings, start=len(newcs)):
                newcs.charStrings[name] = i
                newcs.charStringsIndex.items.append(None)
        for name in cs.charStrings:
            if widthsDiffer:
                c = cs[name]
                defaultWidthXToken = object()
                extractor = T2WidthExtractor([], [], nominalWidthX, defaultWidthXToken)
                extractor.execute(c)
                width = extractor.width
                if width is not defaultWidthXToken:
                    # The following will be wrong if the width is added
                    # by a subroutine. Ouch!
                    c.program.pop(0)
                else:
                    width = defaultWidthX
                if width != newDefaultWidthX:
                    c.program.insert(0, width - newNominalWidthX)
            newcs[name] = cs[name]

    newfont.charset = chrset
    newfont.numGlyphs = len(chrset)
    newfont.strings.strings = glyphOrderStrings + storedNamesStrings

    return newcff


def _mergeCIDCFF(tables):
    """Merge two or more compatible CID-keyed CFF1 fonts.

    The fonts must share the same ROS and use disjoint sets of CIDs.  Each
    font keeps its own FontDicts (and hence its Private dictionaries and
    local subroutines); the glyphs of every font after the first are
    reassigned to those FontDicts, which are appended to the first font's
    FDArray.  Global subroutines are concatenated and their references are
    remapped.  Incompatible inputs raise rather than producing a font that
    saves but renders incorrectly.
    """
    baseCFF = tables[0]
    baseFont = baseCFF.cff[0]
    baseROS = baseFont.ROS

    # All fonts must be CID-keyed and share the ROS.
    for table in tables[1:]:
        font = table.cff[0]
        if not _isCIDFont(font):
            raise ValueError(
                "Cannot merge a name-keyed CFF font with a CID-keyed CFF font"
            )
        if font.ROS != baseROS:
            raise ValueError(
                "Cannot merge CID-keyed CFF fonts with different ROS values: "
                "%r != %r" % (baseROS, font.ROS)
            )

    # CIDs must not collide. CID 0 is the .notdef CID and is shared.
    seenCIDs = set()
    for table in tables:
        font = table.cff[0]
        cids = {_getCID(name) for name in font.charset if _getCID(name) != 0}
        overlap = seenCIDs & cids
        if overlap:
            raise ValueError(
                "Cannot merge CID-keyed CFF fonts: conflicting CIDs: %s"
                % ", ".join("cid%05d" % cid for cid in sorted(overlap))
            )
        seenCIDs |= cids

    newCharStrings = baseFont.CharStrings
    newFDSelect = baseFont.FDSelect
    newFDArray = baseFont.FDArray
    newGlobalSubrs = baseCFF.cff.GlobalSubrs

    # Validate every font's subroutine references before moving anything.
    for table in tables:
        _validateCIDSubrReferences(table.cff[0], table.cff.GlobalSubrs)

    # Compute the size (and hence bias) of the fully concatenated global
    # subroutine INDEX up front.  Writing every remapped operand against this
    # final bias means a threshold crossing mid-merge never needs a second,
    # error-prone correction pass (which would also touch the base font's
    # charstrings that were never remapped).
    totalGlobalSubrs = len(newGlobalSubrs) + sum(
        len(table.cff.GlobalSubrs) for table in tables[1:]
    )
    finalGlobalBias = _biasForLength(totalGlobalSubrs)

    # The base font's own charstrings keep global-subroutine INDEX position 0,
    # but if concatenation changes the bias their callgsubr operands must be
    # rewritten against the final bias as well.
    baseGlobalBias = calcSubrBias(newGlobalSubrs)
    if baseGlobalBias != finalGlobalBias:
        for charString in _iterCIDCharStrings(baseFont, newGlobalSubrs):
            _remapGSubrReferences(charString, baseGlobalBias, 0, finalGlobalBias)

    newCharset = list(baseFont.charset)

    for table in tables[1:]:
        font = table.cff[0]
        globalSubrs = table.cff.GlobalSubrs

        oldGlobalBias = calcSubrBias(globalSubrs)
        globalSubrOffset = len(newGlobalSubrs)

        # Append the font's FontDicts.  Local subroutines stay attached to
        # each FontDict's Private dict, so callsubr operands stay valid.
        fdOffset = len(newFDArray)
        for fd in font.FDArray:
            newFDArray.append(fd)

        # Reparent and remap callgsubr operands on every charstring in the
        # font: glyphs, local subroutines (which may call global subroutines),
        # and the global subroutines themselves.
        for charString in _iterCIDCharStrings(font, globalSubrs):
            if globalSubrs:
                _remapGSubrReferences(
                    charString, oldGlobalBias, globalSubrOffset, finalGlobalBias
                )
            charString.globalSubrs = newGlobalSubrs
        for subr in globalSubrs:
            newGlobalSubrs.append(subr)

        for glyphIndex, glyphName in enumerate(font.charset):
            charString, fdIndex = font.CharStrings.getItemAndSelector(glyphName)

            # Reparent the glyph to the appended copy of its FontDict.
            charString.private = font.FDArray[fdIndex].Private

            newFDSelect.append(fdIndex + fdOffset)
            # The charset and CharStrings keys keep the OpenType glyph names
            # (including a renamed duplicate .notdef).  The CID charset
            # packer maps any .notdef* name to CID 0.
            newCharset.append(glyphName)

            if newCharStrings.charStringsAreIndexed:
                newIndex = len(newCharStrings.charStringsIndex)
                newCharStrings.charStrings[glyphName] = newIndex
                newCharStrings.charStringsIndex.items.append(charString)
            else:
                newCharStrings.charStrings[glyphName] = charString

    baseFont.charset = newCharset
    baseFont.numGlyphs = len(newCharset)
    # Force the smallest valid FDSelect representation to be chosen at compile.
    newFDSelect.format = None

    return baseCFF


def _biasForLength(length):
    """calcSubrBias() for an INDEX that does not exist yet."""
    if length < 1240:
        return 107
    elif length < 33900:
        return 1131
    return 32768


def _validateCIDSubrReferences(font, globalSubrs):
    """Check every glyph charstring and every local/global subroutine in a
    CID-keyed font for resolvable subroutine references."""
    for glyphName in font.charset:
        charString, fdIndex = font.CharStrings.getItemAndSelector(glyphName)
        localSubrs = getattr(font.FDArray[fdIndex].Private, "Subrs", [])
        _validateSubrReferences(
            charString, localSubrs, globalSubrs, "glyph %r" % glyphName
        )
    for fdIndex, fd in enumerate(font.FDArray):
        localSubrs = getattr(fd.Private, "Subrs", None)
        if not localSubrs:
            continue
        for index, subr in enumerate(localSubrs):
            _validateSubrReferences(
                subr,
                localSubrs,
                globalSubrs,
                "FD %d local subroutine %d" % (fdIndex, index),
            )
    for index, subr in enumerate(globalSubrs):
        _validateSubrReferences(subr, [], globalSubrs, "global subroutine %d" % index)


@add_method(ttLib.getTableClass("cmap"))
def merge(self, m, tables):
    if not hasattr(m, "cmap"):
        computeMegaCmap(m, tables)
    cmap = m.cmap

    glyphMap = {
        glyphName: glyphID
        for glyphID, glyphName in enumerate(getattr(m, "glyphOrder", ()))
    }
    cmapBmpOnly = {
        uni: gid
        for uni, gid in cmap.items()
        if uni <= 0xFFFF and glyphMap.get(gid, 0) <= 0xFFFF
    }
    self.tables = []
    module = ttLib.getTableModule("cmap")
    if len(cmapBmpOnly) != len(cmap):
        # format-12 required for non-BMP codepoints or glyph IDs beyond 64k.
        cmapTable = module.cmap_classes[12](12)
        cmapTable.platformID = 3
        cmapTable.platEncID = 10
        cmapTable.language = 0
        cmapTable.cmap = cmap
        self.tables.append(cmapTable)
    # always create format-4
    cmapTable = module.cmap_classes[4](4)
    cmapTable.platformID = 3
    cmapTable.platEncID = 1
    cmapTable.language = 0
    cmapTable.cmap = cmapBmpOnly
    # ordered by platform then encoding
    self.tables.insert(0, cmapTable)

    uvsDict = m.uvsDict
    if uvsDict:
        # format-14
        uvsTable = module.cmap_classes[14](14)
        uvsTable.platformID = 0
        uvsTable.platEncID = 5
        uvsTable.language = 0
        uvsTable.cmap = {}
        uvsTable.uvsDict = uvsDict
        # ordered by platform then encoding
        self.tables.insert(0, uvsTable)
    self.tableVersion = 0
    self.numSubTables = len(self.tables)
    return self
