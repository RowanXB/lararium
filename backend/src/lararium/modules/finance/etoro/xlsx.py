"""Reads an xlsx workbook's cells as text, on the standard library only (design §4.5).

Every cell is the text the file stores: a shared or inline string as written, a number as its
digits, a boolean as TRUE or FALSE, a formula as its cached value. Nothing is converted; the
caller types each column itself.

The file is the owner's upload, so the reader assumes it may be hostile. It refuses any DOCTYPE
or ENTITY declaration (in the raw bytes before parsing, and in the parser for other encodings),
never follows an external relationship, and has hard limits on the file, its parts, its
cells and the text they return, each checked before the work it bounds. Errors name the
problem and the part (a fixed xlsx part or a sheet name), never a cell's content.
"""

import io
import lzma
import posixpath
import re
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from xml.parsers import expat

from lararium.core.errors import DisplayableError

MAX_PART_BYTES = 16 * 1024 * 1024  # the file, and each part once decompressed
MAX_TOTAL_BYTES = 64 * 1024 * 1024  # every part read, decompressed, together
MAX_ENTRIES = 1000
# Every cell of every row as returned, the padding of gaps included; an empty row counts as one.
MAX_CELLS = 100_000
# The text the reader returns, summed over every cell: a shared string is counted at each cell
# that names it, so one long string named many times cannot fan out.
MAX_TEXT_CHARS = 64 * 1024 * 1024
MAX_CELL_CHARS = 32_767  # Excel's own limit on one cell's text
_MAX_COLUMNS = 16_384  # Excel's own limit, column XFD
_END_OF_CENTRAL_DIRECTORY = b"PK\x05\x06"
_END_RECORD_BYTES = 22

_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_REFERENCE = re.compile(r"([A-Z]{1,3})[1-9][0-9]*")
_INDEX = re.compile(r"[0-9]{1,9}")  # more digits than any MAX_CELLS index
# Transitional and Strict OOXML name the relationship attribute's namespace differently.
_RELATIONSHIP_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "http://purl.oclc.org/ooxml/officeDocument/relationships",
    }
)
_BOOLEANS = {"1": "TRUE", "0": "FALSE"}
_UNZIP_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    lzma.LZMAError,
    EOFError,
    OSError,
    ValueError,
    RuntimeError,  # an encrypted part
    NotImplementedError,  # an unsupported compression method
)


class XlsxError(DisplayableError, ValueError):
    """The file is not an xlsx this reader accepts; the message names no cell content."""


@dataclass(frozen=True)
class Sheet:
    name: str
    # Each row padded to its last cell; rows in the file's order, absent rows left out.
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _Relationship:
    type: str
    target: str
    external: bool


def _local(name: str) -> str:
    """An element or attribute's name without its namespace (the parser joins them by a space)."""
    return name.rpartition(" ")[2]


def _parse(
    data: bytes,
    what: str,
    start: Callable[[str, dict[str, str]], None],
    end: Callable[[str], None] | None = None,
    text: Callable[[str], None] | None = None,
) -> None:
    def refuse(*_: object) -> None:
        raise XlsxError(f"{what} has a DOCTYPE or ENTITY declaration, which is refused")

    def refuse_external(*_: object) -> int:
        raise XlsxError(f"{what} refers to an external entity, which is refused")

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse_external
    parser.buffer_text = True
    parser.StartElementHandler = start
    if end is not None:
        parser.EndElementHandler = end
    if text is not None:
        parser.CharacterDataHandler = text
    try:
        parser.Parse(data, True)
    except expat.ExpatError:
        raise XlsxError(f"{what} is not well-formed XML") from None


def _entries(body: bytes) -> int:
    """The entry count the end-of-central-directory record states, read before the central
    directory is parsed. A Zip64 count (0xFFFF) is more than any xlsx has."""
    start = max(0, len(body) - _END_RECORD_BYTES - 0xFFFF)  # the record and its comment
    end = body.rfind(_END_OF_CENTRAL_DIRECTORY, start)
    if end < 0 or len(body) < end + _END_RECORD_BYTES:
        raise XlsxError("the file is not an xlsx workbook")
    return int.from_bytes(body[end + 10 : end + 12], "little")


def _column(reference: str, what: str) -> int:
    match = _REFERENCE.fullmatch(reference)
    if match is None:
        raise XlsxError(f"{what} has a malformed cell reference")
    index = 0
    for letter in match.group(1):
        index = index * 26 + ord(letter) - 64
    if index > _MAX_COLUMNS:
        raise XlsxError(f"{what} has a cell reference past the last column")
    return index - 1


class _Workbook:
    def __init__(self, body: bytes) -> None:
        if len(body) > MAX_PART_BYTES:
            raise XlsxError("the file is larger than 16 MiB")
        if _entries(body) > MAX_ENTRIES:
            raise XlsxError(f"the file has more than {MAX_ENTRIES} parts")
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(body))
            infos = self._zip.infolist()
        except _UNZIP_ERRORS:
            raise XlsxError("the file is not an xlsx workbook") from None
        if len(infos) > MAX_ENTRIES:
            raise XlsxError(f"the file has more than {MAX_ENTRIES} parts")
        self._infos = {info.filename: info for info in infos}
        self._read = 0
        self._cells = 0
        self._chars = 0
        self._strings: list[str] = []

    def part(self, name: str, what: str) -> bytes:
        info = self._infos.get(name)
        if info is None:
            raise XlsxError(f"the xlsx is missing {what}")
        if info.file_size > MAX_PART_BYTES:
            raise XlsxError(f"{what} is larger than 16 MiB")
        try:
            with self._zip.open(info) as stream:
                data = stream.read(MAX_PART_BYTES + 1)
        except _UNZIP_ERRORS:
            raise XlsxError("the file is not an xlsx workbook: a part does not unzip") from None
        if len(data) > MAX_PART_BYTES:
            raise XlsxError(f"{what} is larger than 16 MiB")
        self._read += len(data)
        if self._read > MAX_TOTAL_BYTES:
            raise XlsxError("the xlsx's parts are larger than 64 MiB together")
        if _DECLARATION.search(data):
            raise XlsxError(f"{what} has a DOCTYPE or ENTITY declaration, which is refused")
        return data

    def relationships(self, name: str, what: str) -> dict[str, _Relationship]:
        found: dict[str, _Relationship] = {}

        def start(tag: str, attributes: dict[str, str]) -> None:
            if _local(tag) == "Relationship":
                found[attributes.get("Id", "")] = _Relationship(
                    attributes.get("Type", ""),
                    attributes.get("Target", ""),
                    attributes.get("TargetMode", "") == "External",
                )

        _parse(self.part(name, what), what, start)
        return found

    def spend(self, cells: int) -> None:
        self._cells += cells
        if self._cells > MAX_CELLS:
            raise XlsxError(f"the workbook has more than {MAX_CELLS} cells")

    def read_strings(self, name: str) -> None:
        what = "the shared strings"
        strings = self._strings
        runs: list[str] = []
        depth = {"si": 0, "rPh": 0, "t": 0}

        def start(tag: str, _: dict[str, str]) -> None:
            local = _local(tag)
            if local in depth:
                depth[local] += 1
            if local == "si":
                runs.clear()

        def end(tag: str) -> None:
            local = _local(tag)
            if local in depth:
                depth[local] -= 1
            if local == "si":
                strings.append(_checked("".join(runs)))
                if len(strings) > MAX_CELLS:
                    raise XlsxError(f"the workbook has more than {MAX_CELLS} shared strings")

        def text(data: str) -> None:
            # A phonetic hint (rPh) is not part of the string.
            if depth["si"] and depth["t"] and not depth["rPh"]:
                runs.append(data)

        _parse(self.part(name, what), what, start, end, text)

    def read_sheet(self, name: str, what: str) -> tuple[tuple[str, ...], ...]:
        rows: list[tuple[str, ...]] = []
        row: dict[int, str] | None = None
        column = 0
        kind = "n"
        value: list[str] = []
        inline: list[str] = []
        depth = {"c": 0, "v": 0, "is": 0, "rPh": 0, "t": 0}
        position = 0

        def start(tag: str, attributes: dict[str, str]) -> None:
            nonlocal row, position, column, kind
            local = _local(tag)
            if local in depth:
                depth[local] += 1
            if local == "row":
                row = {}
                position = 0
            elif local == "c":
                reference = attributes.get("r")
                column = position if reference is None else _column(reference, what)
                if column >= _MAX_COLUMNS:
                    raise XlsxError(f"{what} has a cell past the last column")
                kind = attributes.get("t", "n")
                value.clear()
                inline.clear()

        def end(tag: str) -> None:
            nonlocal row, position
            local = _local(tag)
            if local in depth:
                depth[local] -= 1
            if local == "c" and row is not None:
                if column in row:
                    raise XlsxError(f"{what} has two cells in one place")
                # Counted as it closes, so a row cannot grow past the cap before it ends.
                self.spend(1)
                row[column] = self._value(kind, "".join(value), "".join(inline))
                position = column + 1
            elif local == "row" and row is not None:
                width = max(row) + 1 if row else 0
                self.spend(max(width, 1) - len(row))  # the padding, or 1 for an empty row
                rows.append(tuple(row.get(index, "") for index in range(width)))
                row = None

        def text(data: str) -> None:
            if not depth["c"]:
                return
            if depth["v"]:
                value.append(data)
            elif depth["is"] and depth["t"] and not depth["rPh"]:
                inline.append(data)

        _parse(self.part(name, what), what, start, end, text)
        return tuple(rows)

    def _value(self, kind: str, value: str, inline: str) -> str:
        text = _checked(self._text(kind, value, inline))
        self._chars += len(text)
        if self._chars > MAX_TEXT_CHARS:
            raise XlsxError(f"the workbook has more than {MAX_TEXT_CHARS} characters of text")
        return text

    def _text(self, kind: str, value: str, inline: str) -> str:
        if kind == "s":
            index = int(value) if _INDEX.fullmatch(value) else len(self._strings)
            if index >= len(self._strings):
                raise XlsxError("a cell names a shared string the xlsx does not have")
            return self._strings[index]
        if kind == "inlineStr":
            return inline
        if kind == "b":
            return _BOOLEANS.get(value, value)
        return value  # a number, a formula's string, an error code or an ISO date, as stored


def _checked(text: str) -> str:
    if len(text) > MAX_CELL_CHARS:
        raise XlsxError(f"a cell's text is longer than {MAX_CELL_CHARS} characters")
    return text


def _target(base: str, relationship: _Relationship, what: str) -> str:
    if relationship.external:
        raise XlsxError(f"{what} is an external link, which is refused")
    target = relationship.target
    path = target[1:] if target.startswith("/") else posixpath.join(base, target)
    return posixpath.normpath(path)


def read_workbook(body: bytes) -> tuple[Sheet, ...]:
    """Every sheet of the workbook, in the workbook's order, its cells as text."""
    book = _Workbook(body)
    package = book.relationships("_rels/.rels", "the package relationships (_rels/.rels)")
    office = [r for r in package.values() if r.type.endswith("/officeDocument")]
    if not office:
        raise XlsxError("the xlsx names no workbook")
    workbook = _target("", office[0], "the workbook")
    base = posixpath.dirname(workbook)
    relationships = book.relationships(
        posixpath.join(base, "_rels", f"{posixpath.basename(workbook)}.rels"),
        "the workbook's relationships",
    )

    entries: list[tuple[str, str]] = []

    def start(tag: str, attributes: dict[str, str]) -> None:
        if _local(tag) != "sheet":
            return
        ids = [
            v
            for k, v in attributes.items()
            if _local(k) == "id" and k.rpartition(" ")[0] in _RELATIONSHIP_NAMESPACES
        ]
        entries.append((attributes.get("name", ""), ids[0] if ids else ""))

    _parse(book.part(workbook, "the workbook"), "the workbook", start)

    for relationship in relationships.values():
        if relationship.type.endswith("/sharedStrings"):
            book.read_strings(_target(base, relationship, "the shared strings"))
            break

    sheets: list[Sheet] = []
    for name, rid in entries:
        what = f"sheet {name}"
        relationship = relationships.get(rid)
        if relationship is None:
            raise XlsxError(f"the xlsx is missing {what}")
        path = _target(base, relationship, what)
        # A chart or dialog sheet has no cells.
        rows = book.read_sheet(path, what) if relationship.type.endswith("/worksheet") else ()
        sheets.append(Sheet(name, rows))
    return tuple(sheets)
