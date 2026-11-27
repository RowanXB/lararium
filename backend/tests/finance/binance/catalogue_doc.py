"""Reads the endpoint-field catalogue document, so a test can hold the code to it.

The document is `docs/superpowers/specs/2026-11-22-binance-endpoint-fields.md`. Its "Index of
labels" gives each endpoint's method, host and path; the table under a label's heading gives
the label's fields in its first column.
"""

import re
from dataclasses import dataclass
from pathlib import Path

DOC = (
    Path(__file__).parents[4]
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-11-22-binance-endpoint-fields.md"
)

# `snapshot` marks which account type a row belongs to; the path is what follows.
KIND_PREFIX = re.compile(r"^(?:SPOT \+ MARGIN|SPOT|MARGIN|FUTURES): ")
BARE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class IndexRow:
    label: str
    method: str
    host: str
    path: str
    fields: str


@dataclass(frozen=True)
class DocField:
    path: str
    type: str
    meaning: str


def read() -> list[str]:
    return DOC.read_text().splitlines()


def index(lines: list[str]) -> dict[str, IndexRow]:
    """The "Index of labels" table."""
    start = lines.index("## Index of labels")
    end = lines.index("## Totals")
    rows: dict[str, IndexRow] = {}
    for line in lines[start:end]:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].startswith("`"):
            label = cells[0].strip("`")
            assert label not in rows, f"{label} is listed twice in the Index"
            rows[label] = IndexRow(label, cells[1], cells[2], cells[3], cells[4])
    return rows


def _table_after(lines: list[str], heading: int) -> list[str] | None:
    """The table that follows a heading, if one comes before the next heading."""
    first: int | None = None
    for number in range(heading + 1, len(lines)):
        line = lines[number]
        if line.startswith("#"):
            break
        if line.startswith("|"):
            first = number
            break
    if first is None:
        return None
    table: list[str] = []
    for line in lines[first:]:
        if not line.startswith("|"):
            break
        table.append(line)
    return table


def _heading_of(lines: list[str], label: str) -> int:
    # Some headings read "`label` (not probed) — GET ...".
    pattern = re.compile(rf"`{re.escape(label)}`( \([^)]*\))? — ")
    found = [n for n, line in enumerate(lines) if line.startswith("#") and pattern.search(line)]
    assert len(found) == 1, f"{label}: {len(found)} headings name it"
    return found[0]


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def field_rows(lines: list[str], label: str) -> list[DocField]:
    """The rows of the field table under the label's heading (header and rule excluded)."""
    table = _table_after(lines, _heading_of(lines, label))
    assert table is not None, f"{label}: no field table under its heading"
    rows: list[DocField] = []
    for line in table[2:]:
        cells = _cells(line)
        rows.append(
            DocField(cells[0].replace("`", ""), cells[1], cells[2] if len(cells) > 2 else "")
        )
    return rows


def expand(cell: str) -> list[str]:
    """The paths one first-column cell stands for.

    A cell can hold several names (`a, b`, `a / b`, or `x.a, .b`); a name with no dot or
    bracket takes the first path's parent. A row starting with an ellipsis describes variants
    of the row above and holds no path.
    """
    if cell.startswith("…"):
        return []
    cell = KIND_PREFIX.sub("", cell)
    names = [name.strip() for name in re.split(r", | / ", cell)]
    first = names[0]
    parent = first.rpartition(".")[0]
    paths = [first]
    for name in names[1:]:
        if name.startswith("."):
            paths.append(parent + name)
        elif BARE_NAME.match(name):
            paths.append(f"{parent}.{name}" if parent else name)
        else:
            paths.append(name)
    return paths


def paths_of(rows: list[DocField]) -> list[str]:
    """Every field path the table states, in order, with duplicates kept."""
    paths: list[str] = []
    for row in rows:
        if row.path.endswith(".*"):
            # "same 10 fields as `baseAsset` for the quote leg"
            sibling = re.search(r"as `(\w+)`", row.meaning)
            assert sibling is not None, f"{row.path}: no sibling named"
            parent = row.path[:-2]
            twin = parent.rpartition(".")[0] + "." + sibling.group(1)
            paths += [parent + p[len(twin) :] for p in list(paths) if p.startswith(twin)]
        else:
            paths += expand(row.path)
    return paths


def portfolio_margin_names(lines: list[str], path: str) -> set[str]:
    """The field names section 12's table gives for a Portfolio Margin endpoint."""
    start = lines.index("## 12. Portfolio Margin (host `papi.binance.com`) — brief, not probed")
    for line in lines[start:]:
        cells = _cells(line)
        if cells[0] == f"GET {path}":
            return set(re.findall(r"`([A-Za-z]+)`", cells[2]))
    raise AssertionError(f"{path} is not in section 12's table")
