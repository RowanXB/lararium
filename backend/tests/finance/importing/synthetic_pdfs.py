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


# Tembo ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class TemboRow:
    date: str
    type: str
    amount: str  # as printed, "£" included
    balance: str | None = None  # None: the running balance; "": none printed


@dataclass
class TemboStatement:
    pages: list[list[TemboRow]]
    start: str | None = None  # None: the first row's date, as Tembo prints it
    end: str | None = None  # None: the last row's date
    account_no: str = "ACC-EX01AB-CD23EF"
    omit_page: int | None = None  # a page (from 1) left out of the file, its number kept
    # A page (from 1) whose table has no text layer (an image, say); its number still has.
    unreadable_page: int | None = None
    # A page (from 1) whose table header has a text layer but whose rows don't.
    unreadable_rows_page: int | None = None
    plan_value: str | None = None  # None: the rows' last balance
    available: str | None = None  # None: the plan value
    newest_first: bool = False
    terms: bool = True
    extra: list[Extra] = field(default_factory=list[Extra])


def _tembo(pdf: _Pdf, x: float, top: float, value: str, *, right: bool = False) -> None:
    pdf.text(x, top, value, size=8.2, font="Helvetica", right=right)


def _tembo_heading(pdf: _Pdf, x: float, top: float, value: str, *, size: float = 11.2) -> None:
    pdf.text(x, top, value, size=size, font="Times-Bold")


def _tembo_footer(pdf: _Pdf, number: int, total: int) -> None:
    pdf.text(
        64.0,
        791,
        "Tembo, Tembo Savings, Example Saver and Example are trading names of Example "
        "Savings Limited, which is authorised and",
        size=6,
        font="Helvetica",
    )
    pdf.text(56.6, 797, "regulated by the Example Authority (FRN: 000000).", size=6)
    _tembo(pdf, 297.6, 815, f"{number} / {total}")


def _tembo_money(value: Decimal) -> str:
    return f"-£{-value:,.2f}" if value < 0 else f"£{value:,.2f}"


def _tembo_signed(row: TemboRow) -> Decimal:
    if not row.amount:
        return Decimal(0)
    value = _plain(row.amount.lstrip("+-"))
    if row.type in ("Withdrawal", "Transfer Out") or row.amount.startswith("-"):
        return -value
    return value


def tembo_pdf(statement: TemboStatement) -> bytes:
    pdf = _Pdf(595.276, 841.89)
    running = Decimal(0)
    balances: dict[int, str] = {}
    flat = [row for page in statement.pages for row in page]
    for n, row in enumerate(flat):
        running += _tembo_signed(row)
        balances[n] = _tembo_money(running) if row.balance is None else row.balance
    plan_value = statement.plan_value or _tembo_money(running)
    total = len(statement.pages) + (1 if statement.terms else 0)
    start = statement.start or (flat[0].date if flat else "01/05/2027")
    end = statement.end or (flat[-1].date if flat else "31/05/2027")

    _tembo_heading(pdf, 62.0, 127.9, "Personal details")
    _tembo(pdf, 62.0, 149.3, "Alex Example")
    _tembo(pdf, 62.0, 159.8, "FLAT 1 Example House 2 Example Road")
    _tembo(pdf, 62.0, 180.8, "Exampletown")
    _tembo(pdf, 62.0, 191.3, "EX1 2MP")
    _tembo_heading(pdf, 62.0, 227.7, "Account summary")
    for top, left, left_value, right, right_value in [
        (249.0, "Account no:", statement.account_no, "Available to withdraw*:", None),
        (259.5, "Account open date:", "03/02/2027", "Plan value:", plan_value),
        (270.0, "Account type:", "Regular Saver", "Valuation date:", "01/06/2027"),
    ]:
        _tembo(pdf, 62.0, top, left)
        _tembo(pdf, 283.5, top, left_value, right=True)
        _tembo(pdf, 311.8, top, right)
        _tembo(pdf, 533.3, top, right_value or statement.available or plan_value, right=True)
    _tembo_heading(pdf, 62.0, 314.7, "Transactions:")
    _tembo_heading(pdf, 138.8, 316.4, f"{start} - {end}", size=9)

    position = 0
    order = list(range(len(flat)))
    if statement.newest_first:
        order.reverse()
    for number, page_rows in enumerate(statement.pages, start=1):
        if number == statement.omit_page:
            position += len(page_rows)
            continue
        if number > 1:
            pdf.next_page()
        if number == statement.unreadable_page:
            position += len(page_rows)
            _tembo_footer(pdf, number, total)
            continue
        top = 347.3 if number == 1 else 62.0
        _tembo(pdf, 62.8, top, "Date")
        _tembo(pdf, 191.1, top, "Transaction type")
        _tembo(pdf, 399.7, top, "Amount", right=True)
        _tembo(pdf, 518.3, top, "Balance", right=True)
        top += 34.4
        for _ in page_rows:
            if number == statement.unreadable_rows_page:
                position += 1
                continue
            n = order[position]
            row = flat[n]
            _tembo(pdf, 62.8, top, row.date)
            _tembo(pdf, 191.1, top, row.type)
            _tembo(pdf, 399.7, top, row.amount, right=True)
            if balances[n]:
                _tembo(pdf, 518.3, top, balances[n], right=True)
            position += 1
            top += 34.4
        _tembo_footer(pdf, number, total)
        pdf.extras(statement.extra)
    if statement.terms and statement.omit_page != total:
        pdf.next_page()
        _tembo_heading(pdf, 62.0, 62.0, "Statement terms")
        _tembo(pdf, 62.0, 90.0, "Plan value - Amount in your account, including contributions")
        _tembo_heading(pdf, 62.0, 130.0, "Transaction types")
        for n, line in enumerate(
            [
                "Deposit - Money added to your account",
                "Withdrawal - Money removed from your account",
                "Interest Adjustment - Adjustment to previously accrued interest",
            ]
        ):
            _tembo(pdf, 62.0, 150.0 + 12 * n, line)
        _tembo_footer(pdf, total, total)
        pdf.extras(statement.extra)
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
    # The page count the footers print, when not the real one (a statement missing a page).
    printed_pages: int | None = None
    # A page (from 1) whose table header doesn't print as the layout knows it.
    unreadable_header_page: int | None = None
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
    total = statement.printed_pages or len(statement.pages) + info_pages

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
            if number != statement.unreadable_header_page:
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


# Revolut --------------------------------------------------------------------------------


@dataclass(frozen=True)
class RevolutRow:
    date: str  # "Oct 2, 2027"
    description: str
    paid_out: str | None = None  # as printed, "£12.50"
    paid_in: str | None = None
    balance: str | None = None  # printed on completed rows only
    details: tuple[str, ...] = ()  # "Reference: …", "To: …", "Card: …"
    # A payment in another currency: the rate line's text and the amount in that currency.
    rate: tuple[str, str] | None = None


@dataclass
class RevolutCurrentStatement:
    pages: list[list[RevolutRow]]  # the completed rows, by page
    opening: str = "£100.00"
    money_out: str = "£0.00"
    money_in: str = "£0.00"
    closing: str = "£100.00"
    total: tuple[str, str, str, str] | None = None  # None: the product's row repeated
    period: tuple[str, str] = ("October 1, 2027", "October 31, 2027")
    generated: str = "Nov 2, 2027"
    pending: list[RevolutRow] = field(default_factory=list[RevolutRow])
    reverted: list[RevolutRow] = field(default_factory=list[RevolutRow])
    # Another currency's section before it: (code, symbol, its summary's four amounts).
    other: tuple[str, str, tuple[str, str, str, str]] | None = (
        "USD",
        "$",
        ("$0.00", "$0.00", "$0.00", "$0.00"),
    )
    extra: list[Extra] = field(default_factory=list[Extra])


_REVOLUT_FONT = "Helvetica"


def _rv(pdf: _Pdf, x: float, top: float, value: str, *, right: bool = False) -> None:
    pdf.text(x, top, value, size=8, font=_REVOLUT_FONT, right=right)


def _revolut_letterhead(pdf: _Pdf, title: str, generated: str, *, top: float = 69.58) -> None:
    pdf.text(555.6, top, title, size=12, font=_REVOLUT_FONT, right=True)
    _rv(pdf, 553.5, top + 22.4, f"Generated on {generated}", right=True)
    _rv(pdf, 555.6, top + 34.8, "Revolut Bank UK Ltd", right=True)


def _revolut_footer(pdf: _Pdf, number: int, total: int) -> None:
    _rv(pdf, 85.43, 741.23, "Report lost or stolen card")
    _rv(pdf, 188.25, 741.23, "Your deposit is eligible for protection under the FSCS.")
    _rv(pdf, 555.58, 790.05, f"Page {number} of {total}", right=True)


def _revolut_summary(
    pdf: _Pdf, amounts: tuple[str, str, str, str], total: tuple[str, str, str, str]
) -> None:
    pdf.text(39.67, 264.76, "Balance summary", size=11, font=_REVOLUT_FONT)
    _rv(pdf, 526.05, 293.78, "Closing")
    for x, label in ((42.67, "Product"), (252.98, "Opening balance"), (335.06, "Money out")):
        _rv(pdf, x, 299.97, label)
    _rv(pdf, 417.11, 299.97, "Money in")
    _rv(pdf, 526.76, 306.15, "balance")
    for top, label, values in (
        (325.28, "Account (Current Account)", amounts),
        (344.03, "Total", total),
    ):
        _rv(pdf, 42.67, top, label)
        for x, value in zip((252.98, 335.06, 417.11), values[:3], strict=True):
            _rv(pdf, x, top, value)
        _rv(pdf, 555.58, top, values[3], right=True)
    _rv(
        pdf,
        39.67,
        363.2,
        "The balance on your statement might differ from the balance in your app.",
    )


def _revolut_mailing(pdf: _Pdf, details: list[tuple[str, str]]) -> None:
    _rv(pdf, 39.67, 144.02, "A EXAMPLE")
    for n, line in enumerate(("1 Example Street", "Flat 2", "Exampletown", "EX1 2MP")):
        _rv(pdf, 39.67, 170.03 + 12.37 * n, line)
    for n, (label, value) in enumerate(details):
        _rv(pdf, 352.99, 170.03 + 12.37 * n, label)
        _rv(pdf, 419.48, 170.03 + 12.37 * n, value)


def _revolut_rows(pdf: _Pdf, rows: list[RevolutRow], top: float, *, balance: bool) -> float:
    for row in rows:
        _rv(pdf, 42.67, top, row.date)
        _rv(pdf, 124.76, top, row.description)
        if row.paid_out is not None:
            _rv(pdf, 335.06, top, row.paid_out)
        if row.paid_in is not None:
            _rv(pdf, 417.15, top, row.paid_in)
        if balance and row.balance is not None:
            _rv(pdf, 555.58, top, row.balance, right=True)
        line = top + 10.16
        if row.rate is not None:
            _rv(pdf, 124.76, line, row.rate[0])
            _rv(pdf, 335.06 if row.paid_out is not None else 417.15, line, row.rate[1])
            line += 5.25
        for detail in row.details:
            _rv(pdf, 124.76, line, detail)
            line += 5.25
        top = max(top + 18.75, line + 8.6)
    return top


def _revolut_table(pdf: _Pdf, top: float, *, start: str) -> None:
    _rv(pdf, 42.67, top, start)
    _rv(pdf, 124.76, top, "Description")
    _rv(pdf, 335.06, top, "Money out")
    _rv(pdf, 417.15, top, "Money in")
    if start == "Date":
        _rv(pdf, 555.59, top, "Balance", right=True)


def revolut_current_pdf(statement: RevolutCurrentStatement) -> bytes:
    pdf = _Pdf(595, 842)
    total_pages = len(statement.pages) + (1 if statement.reverted else 0)
    total_pages += 1 if statement.other else 0
    number = 1
    if statement.other is not None:
        code, symbol, amounts = statement.other
        del symbol
        _revolut_letterhead(pdf, f"{code} Statement", statement.generated)
        _revolut_mailing(pdf, [("IBAN", "GB33REVO00996912345678"), ("BIC", "REVOGB21")])
        _revolut_summary(pdf, amounts, amounts)
        _revolut_footer(pdf, number, total_pages)
        pdf.next_page()
        number += 1
    amounts = (statement.opening, statement.money_out, statement.money_in, statement.closing)
    start, end = statement.period
    for index, rows in enumerate(statement.pages):
        _revolut_letterhead(pdf, "GBP Statement", statement.generated)
        if index == 0:
            _revolut_mailing(
                pdf,
                [
                    ("Sort Code", "040075"),
                    ("Account Number", "12345678"),
                    ("IBAN", "GB33REVO00996912345678"),
                    ("BIC", "REVOGB21"),
                ],
            )
            _revolut_summary(pdf, amounts, statement.total or amounts)
            top = 405.4
            if statement.pending:
                pdf.text(39.67, top, f"Pending from {start} to {end}", size=11)
                _revolut_table(pdf, top + 29, start="Start date")
                top = _revolut_rows(pdf, statement.pending, top + 48, balance=False) + 12
            pdf.text(39.67, top, f"Account transactions from {start} to {end}", size=11)
            _revolut_table(pdf, top + 29, start="Date")
            _revolut_rows(pdf, rows, top + 48, balance=True)
        else:
            _revolut_table(pdf, 142.28, start="Date")
            _revolut_rows(pdf, rows, 161.22, balance=True)
        pdf.extras(statement.extra)
        _revolut_footer(pdf, number, total_pages)
        pdf.next_page()
        number += 1
    if statement.reverted:
        _revolut_letterhead(pdf, "GBP Statement", statement.generated)
        pdf.text(39.67, 144.02, f"Reverted from {start} to {end}", size=11)
        _revolut_table(pdf, 173.03, start="Start date")
        _revolut_rows(pdf, statement.reverted, 192.15, balance=False)
        _revolut_footer(pdf, number, total_pages)
    return pdf.finish()


@dataclass(frozen=True)
class RevolutSavingsRow:
    date: str
    kind: str  # Deposit, Withdrawal or Gross Interest
    paid_out: str | None = None
    paid_in: str | None = None
    balance: str | None = None


@dataclass
class RevolutSavingsStatement:
    pages: list[list[RevolutSavingsRow]]
    totals: dict[str, str]  # "Opening Balance", "Total Deposit", … as printed
    period: str = "Oct 1, 2027 \u2013 Oct 31, 2027"
    generated: str = "Nov 2, 2027"
    product: str = "Example account - Instant Access - Example Bank"
    extra: list[Extra] = field(default_factory=list[Extra])


_SAVINGS_TOTALS = (
    "Opening Balance",
    "Total Deposit",
    "Total Withdrawal",
    "Total Interest Paid",
    "Closing Balance",
)


def revolut_savings_pdf(statement: RevolutSavingsStatement) -> bytes:
    pdf = _Pdf(595, 842)
    for index, rows in enumerate(statement.pages):
        pdf.text(555.6, 65.92, "Savings Statement", size=16, font=_REVOLUT_FONT, right=True)
        _rv(pdf, 553.32, 100.66, f"Generated on {statement.generated}", right=True)
        _rv(pdf, 555.58, 112.62, f"Period: {statement.period}", right=True)
        top = 144.42
        if index == 0:
            for n, line in enumerate(
                ("A EXAMPLE", "1 Example Street", "Exampletown", "EX1 2MP", "United Kingdom")
            ):
                _rv(pdf, 39.67, 138.79 + 15 * n, line)
            _rv(pdf, 39.67, 235.66, "Account number: 12345678")
            _rv(pdf, 39.67, 250.62, "Sort Code: 040075")
            pdf.text(44.17, 292.8, "Balance summary", size=11, font=_REVOLUT_FONT)
            for n, label in enumerate(_SAVINGS_TOTALS):
                _rv(pdf, 45.3, 321.34 + 21.71 * n, label)
                _rv(pdf, 248.89, 321.34 + 21.71 * n, statement.totals[label])
            _rv(pdf, 44.17, 444.8, statement.product)
            top = 474.46
        _rv(pdf, 45.3, top, "Date")
        _rv(pdf, 154.54, top, "Description")
        _rv(pdf, 270.34, top, "Money out")
        _rv(pdf, 450.06, top, "Money in", right=True)
        _rv(pdf, 549.97, top, "Balance", right=True)
        top += 23.58
        for row in rows:
            _rv(pdf, 45.3, top, row.date)
            _rv(pdf, 154.54, top, row.kind)
            if row.paid_out is not None:
                _rv(pdf, 270.34, top, row.paid_out)
            if row.paid_in is not None:
                _rv(pdf, 450.05, top, row.paid_in, right=True)
            if row.balance is not None:
                _rv(pdf, 549.94, top, row.balance, right=True)
            top += 21.71
        _rv(pdf, 77.18, 737.82, "Report lost or stolen card")
        _rv(pdf, 188.4, 729.98, "Your deposit is eligible for protection under the FSCS.")
        _rv(pdf, 555.59, 801.87, f"Page {index + 1} of {len(statement.pages) + 1}", right=True)
        pdf.extras(statement.extra)
        pdf.next_page()
    _rv(pdf, 39.67, 147.67, "Interest")
    _rv(pdf, 39.67, 182.37, "Your interest is variable as set out in our terms.")
    return pdf.finish()


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
