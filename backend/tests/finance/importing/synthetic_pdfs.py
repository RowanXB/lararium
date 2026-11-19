"""Synthetic PDF statements for the layout tests, drawn with reportlab.

They reproduce only each bank's layout: the labels, type codes and positions the layouts
read (and Lloyds' hidden screen-reader layer). Every name, number and amount is invented
("Example Bank" data).
"""

import io
from dataclasses import dataclass, field
from decimal import Decimal

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

CJK = "STSong-Light"
pdfmetrics.registerFont(UnicodeCIDFont(CJK))

type Extra = tuple[int, float, float, str]  # page, x0, top, text


class _Pdf:
    def __init__(self, width: float, height: float, *, encrypt: str | None = None) -> None:
        self.buffer = io.BytesIO()
        self.canvas = Canvas(self.buffer, pagesize=(width, height), encrypt=encrypt)
        self.height = height
        self.page = 0

    def text(
        self,
        x: float,
        top: float,
        value: str,
        *,
        size: float = 8,
        font: str = "Times-Roman",
        right: bool = False,
        hidden: bool = False,
    ) -> None:
        """Draws value with its top at `top`; right=True aligns its right edge on x."""
        canvas = self.canvas
        canvas.setFont(font, size)
        canvas.setFillColorRGB(1, 1, 1) if hidden else canvas.setFillColorRGB(0, 0, 0)
        baseline = self.height - top - size * 0.75
        if right:
            canvas.drawRightString(x, baseline, value)
        else:
            canvas.drawString(x, baseline, value)

    def spaced(self, x: float, top: float, value: str, *, gap: float, size: float = 8) -> None:
        """Draws each word on its own, `gap` points apart and with no space character."""
        for word in value.split():
            self.text(x, top, word, size=size)
            x += pdfmetrics.stringWidth(word, "Times-Roman", size) + gap

    def extras(self, extra: list[Extra]) -> None:
        for page, x, top, value in extra:
            if page == self.page:
                self.text(x, top, value)

    def next_page(self) -> None:
        self.canvas.showPage()
        self.page += 1

    def finish(self) -> bytes:
        self.canvas.save()
        return self.buffer.getvalue()


def _money(value: Decimal, *, overdrawn: str | None = None) -> str:
    """1,234.50; a negative balance with the bank's marker (" D") when given, else "-"."""
    if value < 0 and overdrawn is not None:
        return f"{-value:,.2f}{overdrawn}"
    return f"{value:,.2f}"


def _plain(printed: str) -> Decimal:
    return Decimal(printed.replace(",", "").replace("£", ""))


# HSBC UK -------------------------------------------------------------------------------


@dataclass(frozen=True)
class HsbcRow:
    type_code: str
    details: tuple[str, ...]
    paid_out: str | None = None
    paid_in: str | None = None
    date: str | None = None  # printed on the row's first line; None: the day continues
    balance: str | None = None  # printed on the row's last line


@dataclass
class HsbcStatement:
    pages: list[list[HsbcRow]]
    opening: str = "100.00"
    period: str = "6 May to 5 June 2027"
    brought_forward_date: str | None = "05 May 27"  # None: a first statement
    summary: dict[str, str] = field(default_factory=dict[str, str])
    sheet: str = "7"
    closing_day: str = "05 Jun 27"
    credit_rate: bool = False
    late_tier: bool = False  # a tier printed after a gap below the table
    extra: list[Extra] = field(default_factory=list[Extra])


_HSBC_LINE = 11.25


def _hsbc_header(pdf: _Pdf, period: str, top: float, *, first: bool, sheet: str) -> None:
    pdf.text(437.6, 136, "Your Statement", size=12)
    if not first:
        pdf.text(52.4, 124, period, size=9)
        pdf.text(52.4, top, "Account Name", size=9)
        pdf.text(368.3, top, "Sortcode", size=9)
        pdf.text(414.8, top, "Account Number", size=9)
        pdf.text(495.5, top, "Sheet Number", size=9)
        pdf.text(52.4, top + 12, "Mr A Example", size=9)
        pdf.text(368.3, top + 12, "40-12-34", size=9)
        pdf.text(414.8, top + 12, "12345678", size=9)
        pdf.text(496.1, top + 13, sheet, size=9)


def _hsbc_table_header(pdf: _Pdf, top: float) -> None:
    pdf.text(53.3, top - 15, "Your Bank Account details", size=10)
    pdf.text(53.3, top, "Date")
    pdf.text(116.6, top, "Payment type and details")
    pdf.text(349.7, top, "£")
    pdf.text(356.6, top, "Paid out")
    pdf.text(437.9, top, "£")
    pdf.text(444.8, top, "Paid in")
    pdf.text(513.2, top, "£")
    pdf.text(520.1, top, "Balance")


def _hsbc_balance_line(
    pdf: _Pdf, top: float, label: str, balance: Decimal, day: str | None
) -> None:
    if day is not None:
        pdf.text(53.3, top, day)
    pdf.spaced(140.3, top, label, gap=1.9)
    pdf.text(552.4, top, _money(abs(balance)), right=True)
    if balance < 0:
        pdf.text(555.5, top, "D")


def hsbc_pdf(statement: HsbcStatement) -> bytes:
    pdf = _Pdf(595.44, 841.68)
    running = _plain(statement.opening)
    paid_in = sum(
        (_plain(row.paid_in) for page in statement.pages for row in page if row.paid_in), Decimal(0)
    )
    paid_out = sum(
        (_plain(row.paid_out) for page in statement.pages for row in page if row.paid_out),
        Decimal(0),
    )
    summary = {
        "Opening Balance": f"£{_plain(statement.opening):,.2f}",
        "Payments In": f"£{paid_in:,.2f}",
        "Payments Out": f"£{paid_out:,.2f}",
        "Closing Balance": f"£{running + paid_in - paid_out:,.2f}",
    } | statement.summary
    for number, rows in enumerate(statement.pages):
        first = number == 0
        if first:
            _hsbc_header(pdf, statement.period, 155, first=True, sheet=statement.sheet)
            for n, line in enumerate(
                ["Mr A Example", "1 Example Street", "Exampletown", "EX1 2MP"]
            ):
                pdf.text(60.8, 155 + 11 * n, line, size=9)
            pdf.text(360.2, 199, "Account Summary", size=10)
            for n, (label, value) in enumerate(summary.items()):
                pdf.text(360.2, 216 + 14 * n, label, size=9)
                pdf.text(548.0, 216 + 14 * n, value, size=9, right=True)
            pdf.text(355.7, 315, "International Bank Account Number", size=9)
            pdf.text(355.7, 326, "GB99 EXAM 4012 3412 3456 78".replace(" ", ""), size=9)
            pdf.text(52.4, 341, statement.period, size=12)
            pdf.text(355.7, 341, "Bank Identifier Code", size=9)
            pdf.text(355.7, 352, "EXAMGB2L", size=9)
            pdf.text(52.4, 380, "Account Name", size=9)
            pdf.text(355.7, 380, "Sortcode", size=9)
            pdf.text(414.8, 380, "Account Number", size=9)
            pdf.text(493.1, 380, "Sheet Number", size=9)
            pdf.text(52.4, 392, "Mr A Example", size=9)
            pdf.text(355.7, 392, "40-12-34", size=9)
            pdf.text(414.8, 392, "12345678", size=9)
            pdf.text(493.1, 392, statement.sheet, size=9)
            header = 442.0
        else:
            _hsbc_header(
                pdf, statement.period, 155, first=False, sheet=str(int(statement.sheet) + number)
            )
            header = 220.0
        _hsbc_table_header(pdf, header)
        top = header + 27 if first else header + 20
        if first and statement.brought_forward_date is None:
            top = header + 38
        elif first:
            _hsbc_balance_line(
                pdf, top, "BALANCE BROUGHT FORWARD", running, statement.brought_forward_date
            )
            top += _HSBC_LINE
        else:
            _hsbc_balance_line(pdf, top, "BALANCE BROUGHT FORWARD", running, None)
            top += _HSBC_LINE
        for row in rows:
            if row.date is not None:
                pdf.text(53.3, top, row.date)
            pdf.text(113.0, top, row.type_code)
            for n, line in enumerate(row.details):
                if n:
                    top += _HSBC_LINE
                pdf.spaced(140.3, top, line, gap=2.0)
            if row.paid_out is not None:
                pdf.text(390.1, top, row.paid_out, right=True)
                running -= _plain(row.paid_out)
            if row.paid_in is not None:
                pdf.text(472.4, top, row.paid_in, right=True)
                running += _plain(row.paid_in)
            if row.balance is not None:
                printed = row.balance.removesuffix(" D")
                pdf.text(552.4, top, printed, right=True)
                if row.balance.endswith(" D"):
                    pdf.text(555.5, top, "D")
            top += _HSBC_LINE
        last = number == len(statement.pages) - 1
        _hsbc_balance_line(
            pdf,
            top + 1,
            "BALANCE CARRIED FORWARD",
            running,
            statement.closing_day if last else None,
        )
        if last:
            rates = top + 30
            pdf.text(53.6, rates, "Information about the Example Deposit Scheme", size=9)
            rates += 40
            pdf.text(272.0, rates, "AER", size=7)
            pdf.text(314.3, rates, "Overdraft", size=7)
            pdf.text(545.9, rates, "EAR", size=7)
            pdf.text(476.3, rates + 9, "balance", size=7)
            pdf.text(532.1, rates + 9, "variable", size=7)
            pdf.text(53.3, rates + 10, "Credit Interest Rates", size=7)
            pdf.text(207.2, rates + 10, "balance", size=7)
            pdf.text(258.2, rates + 10, "variable", size=7)
            pdf.text(314.3, rates + 10, "Interest Rates", size=7)
            if statement.credit_rate:
                pdf.text(180.0, rates + 22, "upto", size=7)
                pdf.text(235.4, rates + 22, "5,000", size=7, right=True)
                pdf.text(287.8, rates + 22, "1.25%", size=7, right=True)
            else:
                pdf.text(50.6, rates + 22, "Credit interest is not paid", size=7)
            pdf.text(410.0, rates + 25, "upto", size=7)
            pdf.text(507.4, rates + 25, "50", size=7, right=True)
            pdf.text(558.2, rates + 25, "0.00%", size=7, right=True)
            pdf.text(409.7, rates + 38, "over", size=7)
            pdf.text(507.4, rates + 38, "50", size=7, right=True)
            pdf.text(557.7, rates + 38, "35.00%", size=7, right=True)
            if statement.late_tier:
                pdf.text(409.7, rates + 70, "over", size=7)
                pdf.text(507.4, rates + 70, "500", size=7, right=True)
                pdf.text(557.7, rates + 70, "45.00%", size=7, right=True)
        pdf.text(52.4, 809, "1 Example Square Exampletown EX1 1AA", size=9)
        pdf.extras(statement.extra)
        if not last:
            pdf.next_page()
    return pdf.finish()


# Lloyds --------------------------------------------------------------------------------


@dataclass(frozen=True)
class LloydsRow:
    date: str
    description: tuple[str, ...]
    type_code: str
    money_in: str | None = None
    money_out: str | None = None
    balance: str | None = None  # None: the running balance


@dataclass
class LloydsStatement:
    rows: list[LloydsRow]
    opening: str = "10.00"
    start: str = "01 June 2027"
    end: str = "30 June 2027"
    document_date: str = "02 July 2027"
    summary: dict[str, str] = field(default_factory=dict[str, str])
    extra: list[Extra] = field(default_factory=list[Extra])


def _lloyds(pdf: _Pdf, x: float, top: float, value: str, *, right: bool = False) -> None:
    pdf.text(x, top, value, size=9, font="Helvetica", right=right)


def _hidden(pdf: _Pdf, x: float, top: float, value: str) -> None:
    pdf.text(x, top, value, size=0.1, font="Helvetica", hidden=True)


def lloyds_pdf(statement: LloydsStatement) -> bytes:
    pdf = _Pdf(595, 842)
    opening = _plain(statement.opening)
    money_in = sum((_plain(row.money_in) for row in statement.rows if row.money_in), Decimal(0))
    money_out = sum((_plain(row.money_out) for row in statement.rows if row.money_out), Decimal(0))
    summary = {
        "in": f"£{money_in:,.2f}",
        "out": f"£{money_out:,.2f}",
        "opening": f"£{opening:,.2f}",
        "closing": f"£{opening + money_in - money_out:,.2f}",
    } | statement.summary
    _hidden(pdf, 57.0, 12, "logo, Example Bank.")
    _lloyds(pdf, 490.5, 25, "Page 1 of 1")
    _lloyds(pdf, 305.5, 26, statement.document_date)
    _lloyds(pdf, 305.5, 87, "Your Account")
    _lloyds(pdf, 57.0, 91, "Document requested by:")
    _lloyds(pdf, 57.0, 101, "Mr A Example")
    _lloyds(pdf, 57.0, 113, "1 Example Street")
    _lloyds(pdf, 305.5, 113, "Sort Code")
    _lloyds(pdf, 392.5, 113, "30-12-34")
    _lloyds(pdf, 57.0, 125, "Exampletown")
    _lloyds(pdf, 305.5, 125, "Account Number")
    _lloyds(pdf, 392.5, 125, "12345678")
    _lloyds(pdf, 57.0, 137, "EX1 2MP")
    _lloyds(pdf, 57.0, 187, "EXAMPLE CURRENT")
    _lloyds(pdf, 538.0, 187, f"{statement.start} to {statement.end}", right=True)
    _hidden(pdf, 108.3, 196, ".")
    _lloyds(pdf, 57.0, 229, "Money In")
    _lloyds(pdf, 291.0, 229, summary["in"], right=True)
    _lloyds(pdf, 304.0, 229, f"Balance on {statement.start}")
    _lloyds(pdf, 538.0, 229, summary["opening"], right=True)
    _hidden(pdf, 96.0, 236, ".")
    _hidden(pdf, 291.0, 236, ".")
    _lloyds(pdf, 57.0, 246, "Money Out")
    _lloyds(pdf, 291.0, 246, summary["out"], right=True)
    _lloyds(pdf, 304.0, 246, f"Balance on {statement.end}")
    _lloyds(pdf, 538.0, 246, summary["closing"], right=True)
    _lloyds(pdf, 57.0, 282, "Your Transactions")
    for x in (57.0, 122.2, 270.8, 374.9, 453.1, 537.8):
        _hidden(pdf, x, 315, "Column")
    _lloyds(pdf, 57.0, 317, "Date")
    _lloyds(pdf, 122.2, 317, "Description")
    _lloyds(pdf, 270.8, 317, "Type")
    _lloyds(pdf, 375.1, 317, "Money In (£)", right=True)
    _lloyds(pdf, 453.3, 317, "Money Out (£)", right=True)
    _lloyds(pdf, 538.0, 317, "Balance (£)", right=True)
    _hidden(pdf, 76.5, 324, ".")
    running = opening
    top = 342.0
    for row in statement.rows:
        _hidden(pdf, 57.0, top - 2, "Date")
        _hidden(pdf, 122.2, top - 2, "Description")
        _hidden(pdf, 270.8, top - 2, "Type")
        _lloyds(pdf, 57.0, top, row.date)
        _lloyds(pdf, 270.8, top, row.type_code)
        if row.money_in is not None:
            _lloyds(pdf, 375.1, top, row.money_in, right=True)
            running += _plain(row.money_in)
        else:
            _hidden(pdf, 374.9, top + 2, "blank.")
        if row.money_out is not None:
            _lloyds(pdf, 453.3, top, row.money_out, right=True)
            running -= _plain(row.money_out)
        else:
            _hidden(pdf, 453.0, top + 2, "Money Out (£) blank.")
        _lloyds(pdf, 538.0, top, row.balance or _money(running), right=True)
        for n, line in enumerate(row.description):
            _lloyds(pdf, 122.2, top + 10 * n, line)
        _hidden(pdf, 96.5, top + 7, ".")
        top += 25 + 10 * (len(row.description) - 1)
    _lloyds(pdf, 57.0, 636, "Transaction types")
    _hidden(pdf, 537.9, 640, "blank.")
    _lloyds(pdf, 59.0, 666, "BGC Bank Giro Credit")
    _lloyds(pdf, 180.6, 666, "BP Bill Payments")
    _lloyds(pdf, 302.1, 666, "DEB Debit Card")
    _lloyds(pdf, 59.0, 698, "FPI Faster Payment In")
    _lloyds(pdf, 302.1, 698, "FPO Faster Payment Out")
    _hidden(pdf, 185.1, 737, "blank. blank.")
    _lloyds(pdf, 57.0, 776, "Example Bank plc. Registered Office: 1 Example Street, Exampletown.")
    pdf.extras(statement.extra)
    return pdf.finish()


# BOC UK --------------------------------------------------------------------------------


@dataclass(frozen=True)
class BocRow:
    trade_date: str
    value_date: str
    details: tuple[str, ...]
    paid_out: str | None = None
    paid_in: str | None = None
    balance: str | None = None  # None: the running balance


@dataclass
class BocStatement:
    pages: list[list[BocRow]]
    opening: str = "140.00"
    start: str = "01/05/2027"
    end: str = "31/05/2027"
    currencies: tuple[str, ...] = ("GBP",)
    summary: dict[str, str] = field(default_factory=dict[str, str])
    terms: bool = True
    extra: list[Extra] = field(default_factory=list[Extra])
    iban: str = "GB99 EXAM 4012 3490 1234 56"


def _boc(pdf: _Pdf, x: float, top: float, value: str, *, right: bool = False) -> None:
    pdf.text(x, top, value, size=10, font="Helvetica", right=right)


def _boc_label(pdf: _Pdf, x: float, top: float, value: str) -> None:
    pdf.text(x, top, value, size=10, font=CJK)


def _rule(pdf: _Pdf, top: float) -> None:
    pdf.text(8.8, top, "─" * 56, size=10, font=CJK)


def _page_number(pdf: _Pdf, number: int, total: int) -> None:
    _boc_label(pdf, 211.0, 744, f"Page {number} of {total} /第 {number} 页/共 {total} 页")
    pdf.text(54.0, 775, "Example Bank (UK) Limited", size=9)
    pdf.text(54.0, 788, "1 Example Street Tel: 0 80 0 00 00000 (UK)", size=8)


def boc_pdf(statement: BocStatement) -> bytes:
    pdf = _Pdf(595.276, 841.89)
    rows = [row for page in statement.pages for row in page]
    opening = _plain(statement.opening)
    paid_in = [_plain(row.paid_in) for row in rows if row.paid_in]
    paid_out = [_plain(row.paid_out) for row in rows if row.paid_out]
    closing = opening + sum(paid_in, Decimal(0)) - sum(paid_out, Decimal(0))
    summary = {
        "closing": _money(closing),
        "out_volume": str(len(paid_out)),
        "out": _money(sum(paid_out, Decimal(0))),
        "in_volume": str(len(paid_in)),
        "in": _money(sum(paid_in, Decimal(0))),
    } | statement.summary
    total = len(statement.pages) + 1
    pdf.text(44.8, 150, "MR A EXAMPLE", size=10, font="Helvetica")
    pdf.text(44.8, 164, "1 EXAMPLE STREET", size=10, font="Helvetica")
    pdf.text(44.8, 178.5, "EXAMPLETOWN", size=10, font="Helvetica")
    pdf.text(44.8, 193, ".", size=10, font="Helvetica")  # an empty address line, as BOC prints it
    pdf.text(44.8, 207.5, "EX1 2MP", size=10, font="Helvetica")
    _boc_label(pdf, 161.1, 309, "Example Bank (UK) Limited/示例银行(英国)有限公司")
    _boc_label(pdf, 211.0, 324, "ACCOUNT STATEMENT/对账单")
    _rule(pdf, 338)
    _boc_label(pdf, 8.8, 353, "Account Type/账户类型:Personal Example GBP Account")
    _boc_label(pdf, 8.8, 367, "Statement Reference Number/账单序号:2031007")
    _boc_label(pdf, 8.8, 382, f"From/起始日期:{statement.start}")
    _boc_label(pdf, 8.8, 396, f"To/截止日期:{statement.end}")
    _boc_label(pdf, 8.8, 411, "Customer Number/客户号:1234567890")
    _boc_label(
        pdf, 8.8, 425, "Account Opening Branch/开户行:Example Bank (UK) Limited, Example Branch"
    )
    _boc_label(
        pdf,
        8.8,
        440,
        "Account Opening Branch Address/开户行地址:1 Example Street, Exampletown, EX1 1AA",
    )
    _boc_label(pdf, 8.8, 469, "Account Name/账户名称:MR A EXAMPLE")
    _boc_label(pdf, 8.8, 483, "Account Number/账户号码:123400100000001")
    _boc_label(pdf, 8.8, 498, "UK Clearing Account Number/英国清算账号:90123456")
    _boc_label(pdf, 8.8, 512, "Sort Code/银行代码:401234")
    _boc_label(pdf, 8.8, 527, "SWIFT Code/全球银行间金融电信协会代码:EXAMGB2LXXX")
    _boc_label(
        pdf,
        8.8,
        541,
        f"International Bank Account Number(IBAN)/国际银行账号:{statement.iban}",
    )
    _boc_label(pdf, 8.8, 570, "Account Balance Summary/账户余额概览")
    _rule(pdf, 585)
    _boc_label(pdf, 14.3, 599, "Currency")
    _boc_label(pdf, 118.6, 599, "Balance")
    _boc_label(pdf, 22.6, 614, "币种")
    _boc_label(pdf, 125.7, 614, "余额")
    _rule(pdf, 628)
    top = 643.0
    for currency in statement.currencies:
        _boc(pdf, 22.6, top, currency)
        _boc(pdf, 160.1, top, _money(closing), right=True)
        top += 14
    _rule(pdf, top)
    top += 29
    _boc_label(pdf, 8.8, top, "Transaction Summary/交易汇总")
    for currency in statement.currencies:
        _boc_label(pdf, 8.8, top + 15, f"Currency/币种:{currency}")
        _boc_label(pdf, 8.8, top + 29, f"From/起始日期:{statement.start}")
        _boc_label(pdf, 8.8, top + 44, f"Opening Balance/上期余额:{_money(opening)}")
        top += 44
    _page_number(pdf, 1, total)
    running = opening
    for number, page_rows in enumerate(statement.pages, start=2):
        pdf.next_page()
        _boc_label(pdf, 8.8, 92, "Account Number/账户号码:123400100000001")
        _rule(pdf, 106)
        for x, label in [(19.9, "Trans."), (70.3, "Value"), (159.6, "Details")]:
            _boc(pdf, x, 121, label)
        _boc(pdf, 306.5, 121, "Paid out")
        _boc(pdf, 412.4, 121, "Paid in")
        _boc(pdf, 515.0, 121, "Balance")
        _boc(pdf, 25.4, 135, "Date")
        _boc(pdf, 74.2, 135, "Date")
        _boc_label(pdf, 114.7, 135, "凭证号码/业务编号/用途/摘要")
        _boc_label(pdf, 315.0, 135, "支出")
        _boc_label(pdf, 418.1, 135, "收入")
        _boc_label(pdf, 524.0, 135, "余额")
        _boc_label(pdf, 19.9, 150, "交易日")
        _boc_label(pdf, 69.2, 150, "起息日")
        _rule(pdf, 164)
        top = 179.0
        if number > 2:
            _boc_label(
                pdf,
                8.8,
                top,
                f"Balance Carried Over From Previous Page /承前页余额:{_money(running)}",
            )
            top += 14.5
        for row in page_rows:
            _boc(pdf, 17.1, top, row.trade_date)
            _boc(pdf, 67.1, top, row.value_date)
            if row.paid_out is not None:
                _boc(pdf, 347.4, top, row.paid_out, right=True)
                running -= _plain(row.paid_out)
            if row.paid_in is not None:
                _boc(pdf, 451.0, top, row.paid_in, right=True)
                running += _plain(row.paid_in)
            _boc(pdf, 558.0, top, row.balance or _money(running), right=True)
            for n, line in enumerate(row.details):
                _boc(pdf, 114.3 if n == 0 else 114.0, top, line)
                top += 14.5
        last = number == total
        if not last:
            _boc_label(
                pdf, 8.8, top, f"Balance Carried Forward To Next Page/转下页余额:{_money(running)}"
            )
            top += 14.5
        _rule(pdf, top)
        if last:
            _boc_label(pdf, 8.8, top + 14, f"To/截止日期:{statement.end}")
            _boc_label(pdf, 290.5, top + 14, f"Closing Balance/本期余额:{summary['closing']}")
            _boc_label(pdf, 8.8, top + 29, f"Payments Out Volume/支出笔数:{summary['out_volume']}")
            _boc_label(pdf, 290.0, top + 29, f"Payments Out/支出总计:{summary['out']}")
            _boc_label(pdf, 8.8, top + 43, f"Payments In Volume/存入笔数:{summary['in_volume']}")
            _boc_label(pdf, 290.6, top + 43, f"Payments In/存入总计:{summary['in']}")
            pdf.text(
                8.8,
                top + 72,
                "Note for Personal Banking customers: All current accounts are non-interest "
                "bearing.",
                size=10,
                font="Helvetica",
            )
        _page_number(pdf, number, total)
        pdf.extras(statement.extra)
    if statement.terms:
        pdf.next_page()
        pdf.text(199.4, 55, "About your statement", size=9, font="Helvetica")
        pdf.text(
            48.6,
            209,
            "'Local Clearing and Outward Remittance' means an example payment "
            "to another example bank.",
            size=6,
            font="Helvetica",
        )
        pdf.text(
            48.6,
            529,
            "example deposits are protected up to £99,000.",
            size=6,
            font="Helvetica",
        )
        pdf.text(400.0, 529, "1,000.00", size=6, font="Helvetica")
    return pdf.finish()


# LemFi ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class LemfiRow:
    date: str  # "30 May, 2027"
    description: tuple[str, ...]  # its lines, as wrapped
    details: tuple[str, ...] = ()  # printed smaller, under the description
    paid_out: str | None = None
    paid_in: str | None = None
    time: str | None = "13:28"
    balance: str | None = None  # None: the running balance


@dataclass
class LemfiStatement:
    pages: list[list[LemfiRow]]
    savings: bool = False
    opening: str = "0.00"
    period: str = "01 May, 2027 - 31 May, 2027"
    # Overrides of the summary's figures, by label ("Money in": "12.34").
    summary: dict[str, str] = field(default_factory=dict[str, str])
    iban: str = "GB99EXAM40123412345678"
    sort_code: str = "401234"
    bank: str = "Example Bank"
    rate: str = (
        "4.10% AER (4.02% Gross, paid monthly) from 02 May 2027 to 29 October 2027. Interest "
        "statements are provided monthly and yearly."
    )
    # The last row of each page but the last prints its time and further lines on the next.
    carry_over: bool = False
    extra: list[Extra] = field(default_factory=list[Extra])


_LEMFI_FONT, _LEMFI_BOLD = "Helvetica", "Helvetica-Bold"
_LEMFI_OUT, _LEMFI_IN, _LEMFI_BALANCE = 357.0, 460.0, 563.0


def _lemfi_title(pdf: _Pdf, statement: LemfiStatement) -> None:
    title = "Savings statement" if statement.savings else "GBP Statement"
    pdf.text(563.0, 37.0, title, size=20, font=_LEMFI_BOLD, right=True)
    pdf.text(563.0, 62.4, "Generated on 19 Jun, 2027", size=10, font=_LEMFI_FONT, right=True)


def _lemfi_header(pdf: _Pdf, top: float) -> None:
    pdf.text(32.0, top, "DATE", size=10, font=_LEMFI_BOLD)
    pdf.text(102.0, top, "DESCRIPTION", size=10, font=_LEMFI_BOLD)
    for x, label, right in (
        (269.8, "MONEY OUT", _LEMFI_OUT),
        (383.4, "MONEY IN", _LEMFI_IN),
        (480.0, "BALANCE", _LEMFI_BALANCE),
    ):
        pdf.text(x, top, label, size=10, font=_LEMFI_BOLD)
        pdf.text(right, top + 0.7, "(GBP)", size=9, font=_LEMFI_FONT, right=True)


def _lemfi_footer(pdf: _Pdf, number: int, total: int) -> None:
    pdf.text(32.0, 798.4, "© 2027 LemFi. All rights reserved.", size=10, font=_LEMFI_FONT)
    pdf.text(563.0, 798.3, f"Page {number} of {total}", size=10, font=_LEMFI_FONT, right=True)


def _lemfi_lines(row: LemfiRow) -> list[tuple[float, str, float]]:
    """A row's lines below its first: (offset from the row's top, text, size)."""
    lines = [(16.0 * n, line, 10.0) for n, line in enumerate(row.description[1:], start=1)]
    below = 16.0 * len(row.description) - 1.3
    lines += [(below + 12.6 * n, line, 9.0) for n, line in enumerate(row.details)]
    return lines


def _wrapped(value: str, width: int) -> list[str]:
    lines: list[str] = []
    for word in value.split():
        if lines and len(lines[-1]) + len(word) < width:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    return lines


def lemfi_pdf(statement: LemfiStatement) -> bytes:
    pdf = _Pdf(595.28, 841.88)
    rows = [row for page in statement.pages for row in page]
    opening = _plain(statement.opening)
    interest = [_plain(r.paid_in) for r in rows if r.paid_in and "Interest" in r.description[0]]
    paid_in = [_plain(r.paid_in) for r in rows if r.paid_in and "Interest" not in r.description[0]]
    paid_out = [_plain(row.paid_out) for row in rows if row.paid_out]
    total_in, total_out = sum(paid_in, Decimal(0)), sum(paid_out, Decimal(0))
    closing = opening + total_in + sum(interest, Decimal(0)) - total_out
    labels = {
        "Opening balance": _money(opening),
        "Money out": _money(total_out),
        "Money in": _money(total_in),
    }
    if statement.savings:
        labels["Interest accrued"] = _money(sum(interest, Decimal(0)))
    labels |= {"Closing balance": _money(closing)} | statement.summary
    info_pages = 1 if statement.savings else 0
    total = len(statement.pages) + info_pages

    _lemfi_title(pdf, statement)
    pdf.text(32.0, 123.8, "A Example", size=16, font=_LEMFI_BOLD)
    for n, line in enumerate(("1 Example Street,Exampletown,", "Exampleshire", "EX1 2MP")):
        pdf.text(32.0, 151.3 + 15.6 * n, line, size=12, font=_LEMFI_FONT)
    if not statement.savings:
        for top, label, value in (
            (122.6, ("Account", "number"), statement.iban),
            (157.8, ("Bank", "name"), statement.bank),
            (193.0, ("Sort code",), statement.sort_code),
        ):
            for n, part in enumerate(label):
                pdf.text(331.2, top + 15.6 * n, part, size=12, font=_LEMFI_BOLD)
            pdf.text(400.5, top, value, size=12, font=_LEMFI_FONT)
    pdf.text(32.0, 239.0, f"Statement period {statement.period}", size=16, font=_LEMFI_BOLD)
    step = 530.8 / len(labels)
    for n, (label, value) in enumerate(labels.items()):
        x = 32.0 + step * n
        pdf.text(x, 295.5, label, size=12, font=_LEMFI_FONT)
        # A long figure wraps below its currency.
        if len(value) >= 9:
            pdf.text(x, 315.9, "GBP", size=14, font=_LEMFI_BOLD)
            pdf.text(x, 334.0, value, size=14, font=_LEMFI_BOLD)
        else:
            pdf.text(x, 315.9, f"GBP {value}", size=14, font=_LEMFI_BOLD)
    pdf.text(32.0, 389.9, "Your transactions", size=14, font=_LEMFI_BOLD)
    _lemfi_header(pdf, 435.1)

    running = opening
    top = 470.0
    carried: list[tuple[float, str, float]] = []
    for number, page_rows in enumerate(statement.pages, start=1):
        if number > 1:
            pdf.next_page()
            _lemfi_title(pdf, statement)
            _lemfi_header(pdf, 120.0)
            top = 155.0
            for offset, line, size in carried:
                pdf.text(32.0 if size == 0 else 102.0, top + offset, line, size=size or 10)
            top += (carried[-1][0] + 34.0) if carried else 0
            carried = []
        for n, row in enumerate(page_rows):
            pdf.text(32.0, top, row.date, size=10, font=_LEMFI_FONT)
            pdf.text(102.0, top, row.description[0], size=10, font=_LEMFI_FONT)
            for amount, x in ((row.paid_out, _LEMFI_OUT), (row.paid_in, _LEMFI_IN)):
                if amount is not None:
                    pdf.text(x, top, amount, size=10, font=_LEMFI_FONT, right=True)
            running += _plain(row.paid_in or "0") - _plain(row.paid_out or "0")
            balance = row.balance or _money(running)
            pdf.text(_LEMFI_BALANCE, top, balance, size=10, font=_LEMFI_FONT, right=True)
            below = _lemfi_lines(row)
            # size 0 marks the time, printed in the date column.
            if row.time is not None:
                below = [(16.0, row.time, 0.0), *below]
            last = n == len(page_rows) - 1 and number < len(statement.pages)
            if statement.carry_over and last:
                carried = [(offset - below[0][0], line, size) for offset, line, size in below]
                below = []
            for offset, line, size in below:
                pdf.text(32.0 if size == 0 else 102.0, top + offset, line, size=size or 10)
            top += max([offset for offset, _, _ in below], default=0.0) + 34.0
        if number == len(statement.pages):
            pdf.text(
                32.0,
                top + 10,
                "LemFi is a trading name of Example Payments Limited.",
                size=10,
                font=_LEMFI_FONT,
            )
        _lemfi_footer(pdf, number, total)
        pdf.extras(statement.extra)
    if statement.savings:
        pdf.next_page()
        _lemfi_title(pdf, statement)
        pdf.text(32.0, 120.0, "Account information", size=14, font=_LEMFI_BOLD)
        pdf.text(40.0, 150.0, "Name of account", size=10, font=_LEMFI_FONT)
        pdf.text(230.0, 150.0, "Example Instant Saver", size=10, font=_LEMFI_FONT)
        pdf.text(40.0, 180.0, "Interest rate", size=10, font=_LEMFI_FONT)
        lines = _wrapped(statement.rate, 60)
        for n, line in enumerate(lines):
            pdf.text(230.0, 180.0 + 15.0 * n, line, size=10, font=_LEMFI_FONT)
        top = 180.0 + 15.0 * len(lines) + 15.0
        pdf.text(40.0, top, "Additional information", size=10, font=_LEMFI_FONT)
        pdf.text(230.0, top, "See the example FAQs.", size=10, font=_LEMFI_FONT)
        _lemfi_footer(pdf, total, total)
    return pdf.finish()


# Other files ---------------------------------------------------------------------------


def scanned_pdf() -> bytes:
    """One page holding only an image, like a scanned statement."""
    pdf = _Pdf(595, 842)
    image = Image.new("RGB", (40, 40), "white")
    pdf.canvas.drawImage(ImageReader(image), 50, 700, width=200, height=100)
    return pdf.finish()


def encrypted_pdf() -> bytes:
    pdf = _Pdf(595, 842, encrypt="example-password")
    pdf.text(50, 50, "Example Bank")
    return pdf.finish()
