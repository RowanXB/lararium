"""The xlsx reader: cell values as text, and its limits against hostile files."""

import time
import tracemalloc
from typing import Literal

import pytest

from lararium.modules.finance.etoro import xlsx
from lararium.modules.finance.etoro.xlsx import Sheet, XlsxError, read_workbook

from .statement_fixtures import build_workbook, workbook_parts, zip_parts

SECRET = "SYNTHETIC-SECRET-CELL"


def test_reads_shared_and_inline_strings() -> None:
    body = build_workbook(
        [
            (
                "First",
                [
                    [("shared", "Label"), ("inline", "inline text"), ("number", "-12.50")],
                    [("rich", "rich text"), None, ("bool", "1"), ("bool", "0")],
                    [("formula", "computed"), ("shared", "a & <b>")],
                ],
            ),
            ("Second", [[None, None, ("inline", "  spaced  ")]]),
            ("Empty", []),
        ]
    )
    assert read_workbook(body) == (
        Sheet(
            "First",
            (
                ("Label", "inline text", "-12.50"),
                ("rich text", "", "TRUE", "FALSE"),
                ("computed", "a & <b>"),
            ),
        ),
        Sheet("Second", (("", "", "  spaced  "),)),
        Sheet("Empty", ()),
    )


def test_cells_without_references_follow_each_other() -> None:
    parts = workbook_parts([("S", [[("inline", "x")]])])
    parts["xl/worksheets/sheet1.xml"] = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<sheetData><row><c><v>1</v></c><c><v>2</v></c><c r="D1"><v>4</v></c></row>'
        b"<row><c><v>5</v></c></row></sheetData></worksheet>"
    )
    assert read_workbook(zip_parts(parts)) == (Sheet("S", (("1", "2", "", "4"), ("5",))),)


def test_prefixed_namespaces_read() -> None:
    parts = workbook_parts([("S", [])])
    parts["xl/worksheets/sheet1.xml"] = (
        b'<x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<x:sheetData><x:row r="1"><x:c r="B1" t="inlineStr"><x:is><x:t>y</x:t></x:is></x:c>'
        b"</x:row></x:sheetData></x:worksheet>"
    )
    assert read_workbook(zip_parts(parts)) == (Sheet("S", (("", "y"),)),)


_PARTS = ["xl/worksheets/sheet1.xml", "xl/sharedStrings.xml", "xl/workbook.xml", "_rels/.rels"]


@pytest.mark.parametrize("part", _PARTS)
@pytest.mark.parametrize(
    "declaration",
    [b'<!DOCTYPE x [<!ENTITY e "v">]>', b"<!DOCTYPE x>", b'<!ENTITY e "v">'],
)
def test_doctype_refused(part: str, declaration: bytes) -> None:
    parts = workbook_parts([("S", [["a", ("inline", SECRET)]])])
    head, _, rest = parts[part].partition(b"\n")
    parts[part] = head + b"\n" + declaration + rest
    with pytest.raises(XlsxError, match="DOCTYPE or ENTITY") as error:
        read_workbook(zip_parts(parts))
    assert SECRET not in str(error.value)


def test_doctype_refused_in_any_encoding() -> None:
    # Past the byte check: the parser refuses it as well.
    parts = workbook_parts([("S", [])])
    parts["xl/worksheets/sheet1.xml"] = (
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY e "v">]>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData/></worksheet>"
    ).encode("utf-16")
    with pytest.raises(XlsxError, match="DOCTYPE or ENTITY"):
        read_workbook(zip_parts(parts))


def test_billion_laughs_fails_fast() -> None:
    entities = "".join(f'<!ENTITY l{n} "{f"&l{n - 1};" * 10}">' for n in range(1, 10))
    bomb = (
        f'<?xml version="1.0"?><!DOCTYPE w [<!ENTITY l0 "lol">{entities}]>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&l9;</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    parts = workbook_parts([("S", [])])
    parts["xl/worksheets/sheet1.xml"] = bomb.encode()
    started = time.monotonic()
    with pytest.raises(XlsxError, match="DOCTYPE or ENTITY"):
        read_workbook(zip_parts(parts))
    assert time.monotonic() - started < 1


def test_too_many_cells_refused() -> None:
    rows = [[("number", "1")] * 1000 for _ in range(100)]
    rows.append([("number", "1")])
    with pytest.raises(
        XlsxError, match="more than 100000 cells: upload a statement of a shorter period"
    ):
        read_workbook(build_workbook([("S", rows)]))
    # Exactly the cap reads.
    assert len(read_workbook(build_workbook([("S", rows[:-1])]))[0].rows) == 100


def test_cells_count_across_sheets() -> None:
    rows = [[("number", "1")] * 1000 for _ in range(50)]
    with pytest.raises(
        XlsxError, match="more than 100000 cells: upload a statement of a shorter period"
    ):
        read_workbook(build_workbook([("A", rows), ("B", rows), ("C", [[("number", "1")]])]))


def test_padding_counts_as_cells() -> None:
    # One far cell per row would pad each row to 16384 cells.
    parts = workbook_parts([("S", [])])
    rows = "".join(f'<row r="{r}"><c r="XFD{r}"><v>1</v></c></row>' for r in range(1, 8))
    parts["xl/worksheets/sheet1.xml"] = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows}</sheetData></worksheet>"
    ).encode()
    with pytest.raises(
        XlsxError, match="more than 100000 cells: upload a statement of a shorter period"
    ):
        read_workbook(zip_parts(parts))


def test_empty_rows_count_as_cells() -> None:
    parts = workbook_parts([("S", [])])
    parts["xl/worksheets/sheet1.xml"] = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b"<sheetData>" + b"<row/>" * 100_001 + b"</sheetData></worksheet>"
    )
    with pytest.raises(
        XlsxError, match="more than 100000 cells: upload a statement of a shorter period"
    ):
        read_workbook(zip_parts(parts))


@pytest.mark.parametrize("ref", ["XFE1", "A0", "1A", "a1"])
def test_cell_reference_out_of_range_refused(ref: str) -> None:
    parts = workbook_parts([("S", [])])
    parts["xl/worksheets/sheet1.xml"] = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData><row r="1"><c r="{ref}"><v>1</v></c></row></sheetData></worksheet>'
    ).encode()
    with pytest.raises(XlsxError, match="cell reference"):
        read_workbook(zip_parts(parts))


def test_zip_bomb_part_refused_before_decompressing() -> None:
    parts = workbook_parts([("S", [])])
    parts["xl/worksheets/sheet1.xml"] = b" " * (xlsx.MAX_PART_BYTES + 1)
    body = zip_parts(parts)
    assert len(body) < 100_000
    with pytest.raises(XlsxError, match="larger than 16 MiB"):
        read_workbook(body)


def test_part_whose_header_understates_its_size_refused() -> None:
    # The read stops at the stated size, so the part fails its CRC instead of growing.
    body = bytearray(zip_parts(workbook_parts([("S", [[("inline", SECRET)]])])))
    name = b"xl/worksheets/sheet1.xml"
    entry = body.find(b"PK\x01\x02")
    while body[entry + 46 : entry + 46 + len(name)] != name:
        entry = body.find(b"PK\x01\x02", entry + 4)
    body[entry + 24 : entry + 28] = (10).to_bytes(4, "little")  # uncompressed size
    with pytest.raises(XlsxError, match="not an xlsx") as error:
        read_workbook(bytes(body))
    assert SECRET not in str(error.value)


def test_total_decompressed_size_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsx, "MAX_TOTAL_BYTES", 2000)
    rows = [[("inline", "x" * 100)] for _ in range(30)]
    with pytest.raises(XlsxError, match="larger than"):
        read_workbook(build_workbook([("S", rows)]))


def test_too_many_entries_refused() -> None:
    parts = workbook_parts([("S", [])])
    for n in range(xlsx.MAX_ENTRIES):
        parts[f"xl/media/x{n}.bin"] = b""
    with pytest.raises(XlsxError, match="more than 1000 parts"):
        read_workbook(zip_parts(parts))


def test_body_too_large_refused() -> None:
    with pytest.raises(XlsxError, match="larger than 16 MiB"):
        read_workbook(b"PK" + b"\0" * xlsx.MAX_PART_BYTES)


def test_external_sheet_relationship_refused() -> None:
    parts = workbook_parts([("S", [])])
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b'Target="worksheets/sheet1.xml"',
        b'Target="https://example.invalid/sheet.xml" TargetMode="External"',
    )
    with pytest.raises(XlsxError, match="external"):
        read_workbook(zip_parts(parts))


def test_external_relationship_never_followed_when_unused() -> None:
    parts = workbook_parts([("S", [[("inline", "kept")]])])
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"</Relationships>",
        b'<Relationship Id="rIdX" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        b'relationships/externalLink" Target="file:///etc/passwd" TargetMode="External"/>'
        b"</Relationships>",
    )
    assert read_workbook(zip_parts(parts)) == (Sheet("S", (("kept",),)),)


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("_rels/.rels", "_rels/.rels"),
        ("xl/workbook.xml", "workbook"),
        ("xl/_rels/workbook.xml.rels", "workbook's relationships"),
        ("xl/worksheets/sheet1.xml", "sheet S"),
        ("xl/sharedStrings.xml", "shared strings"),
    ],
)
def test_missing_part_refused(missing: str, message: str) -> None:
    parts = workbook_parts([("S", [["shared"]])])
    del parts[missing]
    with pytest.raises(XlsxError, match=message):
        read_workbook(zip_parts(parts))


def test_shared_string_index_out_of_range_refused() -> None:
    parts = workbook_parts([("S", [["shared"]])])
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b"<v>0</v>", b"<v>7</v>"
    )
    with pytest.raises(XlsxError, match="shared string"):
        read_workbook(zip_parts(parts))


@pytest.mark.parametrize("body", [b"", b"not a zip", b"PK\x03\x04broken"])
def test_not_an_xlsx_refused(body: bytes) -> None:
    with pytest.raises(XlsxError, match="not an xlsx"):
        read_workbook(body)


def test_malformed_xml_names_the_part_not_its_content() -> None:
    parts = workbook_parts([("S", [])])
    parts["xl/worksheets/sheet1.xml"] = f"<worksheet><sheetData>{SECRET}<row>".encode()
    with pytest.raises(XlsxError, match="sheet S is not well-formed XML") as error:
        read_workbook(zip_parts(parts))
    assert SECRET not in str(error.value)


def _sheet(cells: bytes) -> bytes:
    parts = workbook_parts([("S", [])])
    parts["xl/worksheets/sheet1.xml"] = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b"<sheetData>" + cells + b"</sheetData></worksheet>"
    )
    return zip_parts(parts)


def test_cells_without_references_past_the_last_column_refused() -> None:
    body = _sheet(b"<row>" + b"<c/>" * 200_000 + b"</row>")
    tracemalloc.start()
    try:
        with pytest.raises(XlsxError, match="cell past the last column"):
            read_workbook(body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 20 * 1024 * 1024


def test_cells_counted_as_they_close(monkeypatch: pytest.MonkeyPatch) -> None:
    # The row never ends: the cap stops it at its 11th cell, before the parser reaches the
    # broken rest of the part.
    monkeypatch.setattr(xlsx, "MAX_CELLS", 10)
    with pytest.raises(
        XlsxError, match="more than 10 cells: upload a statement of a shorter period"
    ):
        read_workbook(_sheet(b"<row>" + b"<c><v>1</v></c>" * 1000 + b"<broken"))


def test_full_rows_of_cells_without_references_counted() -> None:
    row = b"<row>" + b"<c><v>1</v></c>" * 16_384 + b"</row>"
    with pytest.raises(
        XlsxError, match="more than 100000 cells: upload a statement of a shorter period"
    ):
        read_workbook(_sheet(row * 7))


def test_shared_string_fan_out_capped() -> None:
    # One string of Excel's longest, named by enough cells to pass 64 Mi characters.
    count = xlsx.MAX_TEXT_CHARS // xlsx.MAX_CELL_CHARS + 1
    parts = workbook_parts([("S", [[("shared", "x" * xlsx.MAX_CELL_CHARS)]])])
    parts["xl/worksheets/sheet1.xml"] = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b"<sheetData>"
        + b"".join(b'<row><c t="s"><v>0</v></c></row>' for _ in range(count))
        + b"</sheetData></worksheet>"
    )
    with pytest.raises(XlsxError, match=f"more than {xlsx.MAX_TEXT_CHARS} characters"):
        read_workbook(zip_parts(parts))
    # One cell fewer reads.
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b'<row><c t="s"><v>0</v></c></row>', b"", 1
    )
    assert len(read_workbook(zip_parts(parts))[0].rows) == count - 1


@pytest.mark.parametrize("kind", ["shared", "inline", "number"])
def test_cell_longer_than_excel_allows_refused(kind: Literal["shared", "inline", "number"]) -> None:
    text = "7" * (xlsx.MAX_CELL_CHARS + 1)
    with pytest.raises(XlsxError, match="longer than 32767 characters") as error:
        read_workbook(build_workbook([("S", [[(kind, text)]])]))
    assert text not in str(error.value)
    assert read_workbook(build_workbook([("S", [[(kind, text[1:])]])]))[0].rows == ((text[1:],),)


@pytest.mark.parametrize("index", ["-1", "+0", " 0", "\u0660", "9" * 5000])
def test_shared_string_index_not_plain_digits_refused(index: str) -> None:
    parts = workbook_parts([("S", [["shared"]])])
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b"<v>0</v>", f"<v>{index}</v>".encode()
    )
    with pytest.raises(XlsxError, match="shared string"):
        read_workbook(zip_parts(parts))


def test_shared_string_spaces_preserved() -> None:
    body = build_workbook([("S", [[("shared", "  padded  "), ("rich", " two  runs ")]])])
    assert read_workbook(body)[0].rows == (("  padded  ", " two  runs "),)


def _unopened(monkeypatch: pytest.MonkeyPatch) -> None:
    def unopened(*_: object) -> None:
        raise AssertionError("zipfile parsed the central directory")

    monkeypatch.setattr(xlsx.zipfile, "ZipFile", unopened)


def _end_record(body: bytes | bytearray) -> int:
    return body.rfind(b"PK\x05\x06")


def test_lying_entry_count_refused_before_the_directory_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = workbook_parts([("S", [])])
    for n in range(xlsx.MAX_ENTRIES + 10 - len(parts)):
        parts[f"xl/media/x{n}.bin"] = b""
    body = bytearray(zip_parts(parts))
    end = _end_record(body)
    assert int.from_bytes(body[end + 10 : end + 12], "little") == xlsx.MAX_ENTRIES + 10
    # The record claims one entry; the directory still holds them all.
    body[end + 8 : end + 12] = (1).to_bytes(2, "little") * 2
    _unopened(monkeypatch)
    with pytest.raises(XlsxError, match="more than 1000 parts"):
        read_workbook(bytes(body))


def test_entries_within_the_limit_read_despite_a_lying_count() -> None:
    parts = workbook_parts([("S", [[("inline", "kept")]])])
    body = bytearray(zip_parts(parts))
    end = _end_record(body)
    body[end + 8 : end + 12] = (1).to_bytes(2, "little") * 2
    assert read_workbook(bytes(body)) == (Sheet("S", (("kept",),)),)


def test_zip64_locator_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    body = build_workbook([("S", [])])
    end = _end_record(body)
    locator = b"PK\x06\x07" + b"\0" * 16
    _unopened(monkeypatch)
    with pytest.raises(XlsxError, match="Zip64"):
        read_workbook(body[:end] + locator + body[end:])


@pytest.mark.parametrize("where", ["shared", "inline", "number"])
def test_cell_text_refused_as_it_grows(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    # The text never ends: the cap stops it while it is read, before the parser reaches the
    # broken rest of the part (text arrives in chunks of the parser's buffer, 8 KiB).
    monkeypatch.setattr(xlsx, "MAX_CELL_CHARS", 10)
    parts = workbook_parts([("S", [[("shared", "x")]])])
    endless = b"x" * 20_000 + b"<broken"
    if where == "shared":
        parts["xl/sharedStrings.xml"] = (
            b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>'
            + endless
        )
    else:
        cell = b'<c t="inlineStr"><is><t>' if where == "inline" else b"<c><v>"
        parts["xl/worksheets/sheet1.xml"] = (
            b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b"<sheetData><row>" + cell + endless
        )
    with pytest.raises(XlsxError, match="longer than 10 characters"):
        read_workbook(zip_parts(parts))
