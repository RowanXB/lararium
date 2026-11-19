"""The visible words of a PDF with their positions: what every layout reads."""

import io
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

from lararium.modules.finance.errors import FileFormatError

# Words whose tops differ by at most this much are on one line: a label and its amount can
# sit a point apart.
LINE_TOLERANCE = Decimal(2)
# Characters smaller than this are not meant to be seen (Lloyds' screen-reader layer is
# 0.1 pt).
_MIN_SIZE = 1
_POINT = Decimal("0.01")


@dataclass(frozen=True)
class Word:
    """A word as printed, in points from the page's top-left corner; page counts from 0."""

    text: str
    x0: Decimal
    x1: Decimal
    top: Decimal
    page: int


def _white(color: object) -> bool:
    """Gray 1, RGB (1, 1, 1) or CMYK (0, 0, 0, 0); a pattern name or no colour is not white."""
    if not isinstance(color, tuple | list):
        return False
    components = list(cast(Sequence[object], color))
    if len(components) in (1, 3):
        return all(component == 1 for component in components)
    return len(components) == 4 and all(component == 0 for component in components)


def _visible(obj: dict[str, Any]) -> bool:
    if obj.get("object_type") != "char":
        return True
    return obj["size"] >= _MIN_SIZE and not _white(obj.get("non_stroking_color"))


def _position(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_POINT)


def _group(words: list[Word]) -> list[list[Word]]:
    words.sort(key=lambda word: (word.page, word.top, word.x0))
    lines: list[list[Word]] = []
    for word in words:
        line = lines[-1] if lines else None
        if line is None or line[0].page != word.page or word.top - line[0].top > LINE_TOLERANCE:
            lines.append([word])
        else:
            line.append(word)
    for line in lines:
        line.sort(key=lambda word: word.x0)
    return lines


def visible_lines(content: bytes, *, x_tolerance: Decimal) -> list[list[Word]]:
    """The words of every page, without characters smaller than 1 pt or filled white, grouped
    into lines by top and sorted by x0. Characters further apart than x_tolerance (with no
    space between them) are separate words."""
    if not content.startswith(b"%PDF-"):
        raise FileFormatError("not a PDF file")
    # pdfplumber measures in binary fractions; hundredths are what the layouts need.
    tolerance = int(x_tolerance / _POINT) / 100
    words: list[Word] = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for number, page in enumerate(pdf.pages):
                for found in page.filter(_visible).extract_words(x_tolerance=tolerance):
                    words.append(
                        Word(
                            text=found["text"],
                            x0=_position(found["x0"]),
                            x1=_position(found["x1"]),
                            top=_position(found["top"]),
                            page=number,
                        )
                    )
    except PdfminerException as error:
        if error.args and isinstance(error.args[0], PDFPasswordIncorrect):
            raise FileFormatError("this PDF is encrypted; export it without a password") from None
        raise FileFormatError("not a readable PDF file") from None
    except Exception:
        # pdfminer raises many kinds of errors on a damaged file.
        raise FileFormatError("not a readable PDF file") from None
    if not words:
        raise FileFormatError("this PDF has no text layer; scanned statements are not supported")
    return _group(words)
