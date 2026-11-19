"""What a PDF layout reads from a statement, and the helpers the layouts share."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.parsed import (
    ParsedRow,
    StatementAccount,
    StatementRate,
    parse_amount,
)
from lararium.modules.finance.importing.pdf.words import Word
from lararium.modules.finance.importing.profiles import AmountFormat

_AMOUNTS = AmountFormat(thousands_separator=",")


@dataclass(frozen=True)
class LayoutRow:
    """A row as the statement prints it. row.quantity is paid in minus paid out; amounts
    counts the amounts printed in the paid in and paid out columns, which must be one."""

    row: ParsedRow
    page: int
    paid_in: Decimal | None
    paid_out: Decimal | None
    amounts: int


@dataclass(frozen=True)
class PrintedBalance:
    """A balance the statement prints after its first `after` rows (0: before any row)."""

    after: int
    amount: Decimal
    page: int


@dataclass(frozen=True)
class LayoutResult:
    rows: list[LayoutRow]
    period: tuple[date, date]
    # None when the statement doesn't print it (Tembo prints neither an opening balance
    # nor totals): the checks then take the opening from the first printed balance.
    opening_balance: Decimal | None
    money_in: Decimal | None
    money_out: Decimal | None
    closing_balance: Decimal
    balances: list[PrintedBalance]
    # Words inside the transaction table that belong to no row field.
    unplaced: list[Word]
    payments_in: int | None = None
    payments_out: int | None = None
    account: StatementAccount | None = None
    currency: str | None = None
    statement_reference: str | None = None
    document_date: date | None = None
    rates: list[StatementRate] = field(default_factory=list[StatementRate])
    available_balance: Decimal | None = None
    available_balance_date: date | None = None
    account_opened_on: date | None = None


def text(words: list[Word]) -> str:
    return " ".join(word.text for word in words)


def find(line: list[Word], phrase: str) -> int | None:
    """Where the phrase's words start in the line, if they appear one after another."""
    wanted = phrase.split()
    for start in range(len(line) - len(wanted) + 1):
        if all(line[start + n].text == part for n, part in enumerate(wanted)):
            return start
    return None


def after(line: list[Word], phrase: str) -> list[Word] | None:
    """The words after the phrase, or None when the line doesn't hold it."""
    start = find(line, phrase)
    return None if start is None else line[start + len(phrase.split()) :]


def word_of(line: list[Word], phrase: str) -> Word | None:
    """The phrase's first word."""
    start = find(line, phrase)
    return None if start is None else line[start]


def last_word_of(line: list[Word], phrase: str) -> Word | None:
    start = find(line, phrase)
    return None if start is None else line[start + len(phrase.split()) - 1]


def amount(printed: str, what: str) -> Decimal:
    """An amount as printed ("1,234.50", "£0.29", "-5.00"); FileFormatError otherwise."""
    try:
        return parse_amount(printed, _AMOUNTS)
    except ValueError as error:
        raise FileFormatError(f"{what}: {error}") from None


def parse_date(printed: str, pattern: str, what: str) -> date:
    """A date as printed; FileFormatError otherwise."""
    try:
        return datetime.strptime(printed, pattern).replace(tzinfo=UTC).date()
    except ValueError:
        raise FileFormatError(f"{what} is not a date: {printed!r}") from None


@dataclass(frozen=True)
class SplitAmounts:
    """A row's amounts: what it pays in or out by column, how many such amounts it prints,
    and its balance word, if any."""

    paid: dict[str, Decimal]
    moves: int
    balance: Word | None


def split_amounts(amounts: list[tuple[str, Word]], balance_column: str, where: str) -> SplitAmounts:
    """Splits a row's (column, word) amounts. A second balance would replace the first and
    escape the balance check, so it refuses the file."""
    paid: dict[str, Decimal] = {}
    moves = 0
    balance: Word | None = None
    for column, word in amounts:
        if column == balance_column:
            if balance is not None:
                raise FileFormatError(f"{where} prints two balances")
            balance = word
        else:
            paid[column] = amount(word.text, f"{where}'s {column.lower()}")
            moves += 1
    return SplitAmounts(paid, moves, balance)


def nearest(word: Word, right_edges: dict[str, Decimal], within: Decimal) -> str | None:
    """The column whose right edge is closest to the word's, if within the distance."""
    column, edge = min(right_edges.items(), key=lambda item: abs(item[1] - word.x1))
    return column if abs(edge - word.x1) <= within else None


def mailing_block(
    lines: list[list[Word]], start: int, x_limit: Decimal
) -> tuple[str | None, str | None]:
    """(addressee, address) from the mailing block whose first line is lines[start]: the
    run of lines that begin where it begins, each close below the last, read up to x_limit.
    The statement's other column shares some of those lines, and a line of its own there
    is passed over, not taken as the block's end. The first line is the addressee; the
    rest, less lines with nothing but punctuation, the address."""
    first = lines[start][0]
    block: list[str] = []
    previous = first.top
    for line in lines[start:]:
        head = line[0]
        if head.page != first.page:
            break
        if head.x0 >= x_limit:
            continue
        if abs(head.x0 - first.x0) > 1 or head.top - previous > 20:
            break
        previous = head.top
        printed = text([word for word in line if word.x0 < x_limit])
        if any(character.isalnum() for character in printed):
            block.append(printed)
    if not block:
        return None, None
    return block[0], ", ".join(block[1:]) or None


def pages(lines: list[list[Word]]) -> dict[int, list[list[Word]]]:
    by_page: dict[int, list[list[Word]]] = {}
    for line in lines:
        by_page.setdefault(line[0].page, []).append(line)
    return by_page
