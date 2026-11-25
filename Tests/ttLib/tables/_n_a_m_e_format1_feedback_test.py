# -*- coding: utf-8 -*-
"""Tests for OpenType 'name' table format 1 BCP 47 language tags.

These tests use a hand-coded two-language binary sample to verify the
LangTagRecord array, the 0x8000+ language references, byte-exact
round-tripping, XML import/export and locatable error reporting.
"""

import logging
import struct
import unittest
from io import BytesIO

from fontTools.misc import sstruct
from fontTools.misc.loggingTools import CapturingLogHandler
from fontTools.misc.testTools import parseXML
from fontTools.misc.xmlWriter import XMLWriter
from fontTools.ttLib.tables._n_a_m_e import (
    LANGUAGE_TAG_INDEX_BASE,
    LangTagRecord,
    NameRecord,
    NameTableError,
    langTagRecordFormat,
    langTagRecordSize,
    log as nameLog,
    nameRecordFormat,
    nameRecordSize,
    table__n_a_m_e,
)


def build_two_language_blob():
    """Hand-coded format 1 blob with English 'Foo' and German 'Füße'."""
    enName = "Foo".encode("utf-16-be")
    deName = "Füße".encode("utf-16-be")
    enTag = "en".encode("utf-16-be")
    deTag = "de".encode("utf-16-be")

    nameStrings = enName + deName
    enTagOffset = len(nameStrings)
    deTagOffset = enTagOffset + len(enTag)
    stringData = nameStrings + enTag + deTag

    count = 2
    langTagCount = 2
    stringOffset = 6 + count * nameRecordSize + 2 + langTagCount * langTagRecordSize

    blob = struct.pack(">HHH", 1, count, stringOffset)
    blob += sstruct.pack(
        nameRecordFormat,
        {
            "platformID": 3,
            "platEncID": 1,
            "langID": 0x8000,
            "nameID": 1,
            "length": len(enName),
            "offset": 0,
        },
    )
    blob += sstruct.pack(
        nameRecordFormat,
        {
            "platformID": 3,
            "platEncID": 1,
            "langID": 0x8001,
            "nameID": 1,
            "length": len(deName),
            "offset": len(enName),
        },
    )
    blob += struct.pack(">H", langTagCount)
    blob += sstruct.pack(
        langTagRecordFormat, {"length": len(enTag), "offset": enTagOffset}
    )
    blob += sstruct.pack(
        langTagRecordFormat, {"length": len(deTag), "offset": deTagOffset}
    )
    blob += stringData
    return blob, stringOffset


BLOB, STRING_OFFSET = build_two_language_blob()


def parse(xml):
    return [item for item in parseXML(xml) if not isinstance(item, str)]


class Format1BinaryTest(unittest.TestCase):
    def test_hand_coded_sample_layout(self):
        # Lock down the hand-coded sample's key offsets.
        self.assertEqual(STRING_OFFSET, 40)
        fmt, count, stringOffset = struct.unpack(">HHH", BLOB[:6])
        self.assertEqual((fmt, count, stringOffset), (1, 2, 40))
        self.assertEqual(langTagRecordSize, 4)

    def test_decompile_tags_and_names(self):
        t = table__n_a_m_e()
        t.decompile(BLOB, None)

        self.assertEqual(t.format, 1)
        self.assertEqual(len(t.langTags), 2)
        # Lists of records compare equal to plain tag strings.
        self.assertEqual(t.langTags, ["en", "de"])
        self.assertIsInstance(t.langTags[0], LangTagRecord)
        self.assertIsInstance(t.langTags[0], str)
        self.assertEqual(t.langTags[0].tag, "en")
        self.assertEqual(t.langTags[1].langTag, "de")

        self.assertEqual([n.langID for n in t.names], [0x8000, 0x8001])
        self.assertEqual([n.toUnicode() for n in t.names], ["Foo", "Füße"])
        self.assertEqual([n.langTag for n in t.names], ["en", "de"])
        self.assertEqual(t.getLangTag(0x8000), "en")
        self.assertEqual(t.getLangTag(0x8001), "de")
        # Ordinary platform language IDs are not tag references.
        self.assertIsNone(t.getLangTag(0x409))

    def test_record_order_preserved(self):
        t = table__n_a_m_e()
        t.decompile(BLOB, None)
        self.assertEqual(
            [(n.nameID, n.langID) for n in t.names],
            [(1, 0x8000), (1, 0x8001)],
        )

    def test_compile_byte_exact_roundtrip(self):
        t = table__n_a_m_e()
        t.decompile(BLOB, None)
        compiled = t.compile(None)
        self.assertEqual(compiled, BLOB)

        # Decompile the recompiled bytes once more.
        t2 = table__n_a_m_e()
        t2.decompile(compiled, None)
        self.assertEqual(t2.langTags, ["en", "de"])
        self.assertEqual([n.toUnicode() for n in t2.names], ["Foo", "Füße"])

    def test_compile_format1_from_scratch(self):
        t = table__n_a_m_e()
        t.format = 1
        t.langTags = ["en", "de"]
        for langID, text in ((0x8000, "Foo"), (0x8001, "Füße")):
            rec = NameRecord()
            rec.nameID = 1
            rec.platformID = 3
            rec.platEncID = 1
            rec.langID = langID
            rec.string = text
            t.names.append(rec)
        self.assertEqual(t.compile(None), BLOB)

    def test_ordinary_lang_ids_coexist(self):
        t = table__n_a_m_e()
        t.decompile(BLOB, None)
        ordinary = NameRecord()
        ordinary.nameID = 1
        ordinary.platformID = 3
        ordinary.platEncID = 1
        ordinary.langID = 0x409
        ordinary.string = "Foo"
        t.names.append(ordinary)
        data = t.compile(None)
        t2 = table__n_a_m_e()
        t2.decompile(data, None)
        self.assertEqual(t2.getName(1, 3, 1, 0x409).toUnicode(), "Foo")
        self.assertEqual(t2.getName(1, 3, 1, 0x8001).toUnicode(), "Füße")
        self.assertEqual(t2.langTags, ["en", "de"])
        # Windows English preference still works with mixed IDs.
        self.assertEqual(t2.getDebugName(1), "Foo")

    def test_format0_unchanged(self):
        t = table__n_a_m_e()
        rec = NameRecord()
        rec.nameID = 1
        rec.platformID = 3
        rec.platEncID = 1
        rec.langID = 0x409
        rec.string = "Hello"
        t.names.append(rec)
        data = t.compile(None)
        fmt, count, stringOffset = struct.unpack(">HHH", data[:6])
        self.assertEqual((fmt, count, stringOffset), (0, 1, 18))
        t2 = table__n_a_m_e()
        t2.decompile(data, None)
        self.assertEqual(t2.format, 0)
        self.assertEqual(t2.getName(1, 3, 1, 0x409).toUnicode(), "Hello")


class Format1XMLTest(unittest.TestCase):
    def test_toXML_contains_inline_tags(self):
        t = table__n_a_m_e()
        t.decompile(BLOB, None)
        writer = XMLWriter(BytesIO())
        t.toXML(writer, None)
        xml = writer.file.getvalue().decode("utf-8")
        self.assertIn('langID="0x8000" langTag="en"', xml)
        self.assertIn('langID="0x8001" langTag="de"', xml)

    def test_inline_xml_roundtrip(self):
        t = table__n_a_m_e()
        t.decompile(BLOB, None)
        writer = XMLWriter(BytesIO())
        t.toXML(writer, None)
        xml = writer.file.getvalue().decode("utf-8")
        xmlBody = xml.split("?>", 1)[1]

        t2 = table__n_a_m_e()
        for name, attrs, content in parse(xmlBody):
            t2.fromXML(name, attrs, content, None)
        self.assertEqual(t2.format, 1)
        self.assertEqual(t2.langTags, ["en", "de"])
        self.assertEqual([n.toUnicode() for n in t2.names], ["Foo", "Füße"])
        self.assertEqual(t2.compile(None), BLOB)

    def test_standalone_langtag_elements(self):
        for snippet in (
            '<LangTagRecord tag="fr"/>',
            '<langtagrecord tag="fr"/>',
            '<langTagRecord langTag="fr"/>',
            "<langtag>fr</langtag>",
            '<LanguageTag tag="fr"/>',
        ):
            t = table__n_a_m_e()
            for name, attrs, content in parse(snippet):
                t.fromXML(name, attrs, content, None)
            self.assertEqual(t.format, 1)
            self.assertEqual(t.langTags, ["fr"], snippet)

    def test_indexed_and_wrapper_elements(self):
        t = table__n_a_m_e()
        snippet = (
            "<langTags>"
            '<langTag index="1" tag="de"/>'
            '<langTag index="0" tag="en"/>'
            "</langTags>"
        )
        for name, attrs, content in parse(snippet):
            t.fromXML(name, attrs, content, None)
        self.assertEqual(t.langTags, ["en", "de"])

    def test_namerecord_xml_line(self):
        rec = NameRecord()
        rec.nameID = 1
        rec.platformID = 3
        rec.platEncID = 1
        rec.langID = 0x8000
        rec.langTag = "en"
        rec.string = "Foo".encode("utf-16-be")
        writer = XMLWriter(BytesIO())
        rec.toXML(writer, None)
        lines = writer.file.getvalue().decode("utf-8").splitlines()
        firstLine = next(line for line in lines if line.startswith("<namerecord"))
        self.assertEqual(
            firstLine,
            '<namerecord nameID="1" platformID="3" platEncID="1" '
            'langID="0x8000" langTag="en">',
        )


class Format1ErrorTest(unittest.TestCase):
    def test_invalid_reference_compile_raises_locatable(self):
        t = table__n_a_m_e()
        t.format = 1
        rec = NameRecord()
        rec.nameID = 7
        rec.platformID = 3
        rec.platEncID = 1
        rec.langID = 0x8005
        rec.string = "x"
        t.names.append(rec)
        with self.assertRaises((NameTableError, ValueError)) as ctx:
            t.compile(None)
        message = str(ctx.exception)
        self.assertIn("0x8005", message)
        self.assertIn("index 5", message)
        # Also catchable as a fontTools TTLibError.
        import fontTools.ttLib

        self.assertIsInstance(ctx.exception, fontTools.ttLib.TTLibError)

    def test_getLangTag_out_of_range_raises(self):
        t = table__n_a_m_e()
        t.langTags = ["en"]
        with self.assertRaises(NameTableError):
            t.getLangTag(0x8002)

    def test_truncated_langtag_count_is_logged(self):
        truncated = BLOB[: 6 + 2 * nameRecordSize + 1]
        t = table__n_a_m_e()
        with CapturingLogHandler(nameLog, "ERROR") as cap:
            t.decompile(truncated, None)
        self.assertTrue(
            any("langTagCount" in r.getMessage() for r in cap.records),
            [r.getMessage() for r in cap.records],
        )

    def test_truncated_langtag_record_is_logged(self):
        cut = 6 + 2 * nameRecordSize + 2 + 2  # header + count + 2 record bytes
        truncated = BLOB[:cut]
        t = table__n_a_m_e()
        with CapturingLogHandler(nameLog, "ERROR") as cap:
            t.decompile(truncated, None)
        self.assertTrue(
            any("truncated LangTagRecord #0" in r.getMessage() for r in cap.records),
            [r.getMessage() for r in cap.records],
        )

    def test_out_of_bounds_tag_offset_is_logged(self):
        bad = bytearray(BLOB)
        # First LangTagRecord offset field: stringOffset - 8 + 2 == 34.
        struct.pack_into(">H", bad, STRING_OFFSET - 8 + 2, 9999)
        t = table__n_a_m_e()
        with CapturingLogHandler(nameLog, "ERROR") as cap:
            t.decompile(bytes(bad), None)
        self.assertTrue(
            any("outside the string storage" in r.getMessage() for r in cap.records),
            [r.getMessage() for r in cap.records],
        )
        # The valid second tag still parses.
        self.assertEqual(t.langTags, ["de"])

    def test_invalid_reference_decompile_is_logged_but_id_preserved(self):
        bad = bytearray(BLOB)
        # Second name record starts at 6 + nameRecordSize; langID at +4.
        struct.pack_into(">H", bad, 6 + nameRecordSize + 4, 0x8009)
        t = table__n_a_m_e()
        with CapturingLogHandler(nameLog, "ERROR") as cap:
            t.decompile(bytes(bad), None)
        self.assertTrue(
            any("invalid language reference" in r.getMessage() for r in cap.records),
            [r.getMessage() for r in cap.records],
        )
        self.assertEqual(t.names[1].langID, 0x8009)
        self.assertIsNone(t.names[1].langTag)

    def test_existing_badoffset_behavior_still_skips_record(self):
        # Mirrors the pre-existing regression test (issue #525): a name
        # record with a wild offset is skipped rather than raising.
        badRecord = {
            "platformID": 1,
            "platEncID": 3,
            "langID": 7,
            "nameID": 1,
            "length": 3,
            "offset": 8765,
        }
        data = b"".join(
            [
                struct.pack(">HHH", 1, 1, 6 + nameRecordSize),
                sstruct.pack(nameRecordFormat, badRecord),
            ]
        )
        t = table__n_a_m_e()
        t.decompile(data, None)
        self.assertEqual(t.names, [])


class LangTagRecordObjectTest(unittest.TestCase):
    def test_string_like_and_attributes(self):
        tag = LangTagRecord("en-US")
        self.assertEqual(tag, "en-US")
        self.assertEqual(str(tag), "en-US")
        self.assertEqual(tag.tag, "en-US")
        self.assertEqual(tag.langTag, "en-US")
        self.assertEqual(tag.toBytes(), "en-US".encode("utf-16-be"))
        self.assertIsInstance(tag, str)

    def test_binary_field_aliases(self):
        tag = LangTagRecord("de", length=4, offset=14)
        self.assertEqual(tag.length, 4)
        self.assertEqual(tag.offset, 14)
        self.assertEqual(tag.langTagOffset, 14)

    def test_list_equality_to_strings(self):
        tags = [LangTagRecord("en"), LangTagRecord("de")]
        self.assertEqual(tags, ["en", "de"])
        self.assertIn("de", tags)
        self.assertEqual(tags.index("en"), 0)

    def test_addLangTag_returns_base_langid(self):
        t = table__n_a_m_e()
        id0 = t.addLangTag("en")
        id1 = t.addLangTag("de")
        id0again = t.addLangTag("en")
        self.assertEqual(id0, LANGUAGE_TAG_INDEX_BASE)
        self.assertEqual(id1, LANGUAGE_TAG_INDEX_BASE + 1)
        self.assertEqual(id0again, LANGUAGE_TAG_INDEX_BASE)
        self.assertEqual(t.langTags, ["en", "de"])

    def test_getName_accepts_tag_string(self):
        t = table__n_a_m_e()
        t.decompile(BLOB, None)
        rec = t.getName(1, 3, 1, "de")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.toUnicode(), "Füße")


if __name__ == "__main__":
    unittest.main()
