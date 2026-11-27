"""The formats each supported bank exports, with a ready profile config (Decision 14).

The UI offers these as "Add profile from catalog". For each institution the most
information-complete format is the recommended one.
"""

from dataclasses import dataclass

from lararium.modules.finance.importing.pdf import lemfi
from lararium.modules.finance.importing.profiles import (
    AmountFormat,
    CsvColumns,
    CsvExtract,
    CsvProfileConfig,
    OfxProfileConfig,
    PdfProfileConfig,
    ProfileConfigModel,
    QifProfileConfig,
)


@dataclass(frozen=True)
class CatalogEntry:
    institution: str
    label: str  # the format as the bank names it
    config: ProfileConfigModel
    recommended: bool = False

    @property
    def format(self) -> str:
        return self.config.format


_UK_DATE = "%d/%m/%Y"
_GROUPED = AmountFormat(thousands_separator=",")
# Card (Visa, contactless), cash machine and cheque codes, which are no bank transfers.
_HSBC_CARD_TYPES = [")))", "VIS", "ATM", "CHQ"]
_LLOYDS_CARD_TYPES = ["DEB", "CPT", "CHQ"]
_REVOLUT_CARD_TYPES = ["Card Payment", "Card Refund"]
# Revolut pays savings interest itself, never from another bank account.
_REVOLUT_INTEREST = ["Gross Interest"]
_REVOLUT_AMOUNTS = AmountFormat(thousands_separator=",")

CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        "HSBC UK",
        "PDF statement",
        PdfProfileConfig(format="pdf", layout="hsbc_uk_statement", card_types=_HSBC_CARD_TYPES),
        recommended=True,
    ),
    CatalogEntry(
        "HSBC UK",
        "midata CSV",
        CsvProfileConfig(
            format="csv",
            columns=CsvColumns(
                date="Date",
                type="Type",
                description=["Merchant/Description"],
                amount="Debit/Credit",
                balance="Balance",
            ),
            date_format="%Y-%m-%d",
            row_order="newest_first",
            skip_footer_rows=1,
            overdraft_footer_label="Arranged overdraft limit",
            amount_format=_GROUPED,
            card_types=_HSBC_CARD_TYPES,
        ),
    ),
    CatalogEntry("HSBC UK", "OFX", OfxProfileConfig(format="ofx")),
    CatalogEntry(
        "HSBC UK",
        "CSV (transaction list)",
        CsvProfileConfig(
            format="csv",
            header=["Date", "Description", "Amount"],
            columns=CsvColumns(date="Date", description=["Description"], amount="Amount"),
            date_format=_UK_DATE,
            row_order="newest_first",
            amount_format=_GROUPED,
        ),
    ),
    CatalogEntry("HSBC UK", "QIF", QifProfileConfig(format="qif", date_format=_UK_DATE)),
    CatalogEntry(
        "Lloyds",
        "CSV",
        CsvProfileConfig(
            format="csv",
            columns=CsvColumns(
                date="Transaction Date",
                type="Transaction Type",
                description=["Transaction Description"],
                inflow="Credit Amount",
                outflow="Debit Amount",
                balance="Balance",
                account_sort_code="Sort Code",
                account_number="Account Number",
            ),
            date_format=_UK_DATE,
            row_order="newest_first",
            amount_format=_GROUPED,
            card_types=_LLOYDS_CARD_TYPES,
        ),
        recommended=True,
    ),
    CatalogEntry(
        "Lloyds",
        "PDF statement",
        PdfProfileConfig(format="pdf", layout="lloyds_statement", card_types=_LLOYDS_CARD_TYPES),
    ),
    CatalogEntry("Lloyds", "QIF", QifProfileConfig(format="qif", date_format=_UK_DATE)),
    CatalogEntry(
        "BOC UK",
        "PDF statement",
        PdfProfileConfig(format="pdf", layout="boc_uk_statement"),
        recommended=True,
    ),
    # Two accounts with different exports, so two entries: each account's PDF statement
    # carries the most (the account, its summary, and for the current account each payment's
    # other side, reference, card and exchange rates).
    CatalogEntry(
        "Revolut Current",
        "PDF statement",
        PdfProfileConfig(format="pdf", layout="revolut_current_statement"),
        recommended=True,
    ),
    CatalogEntry(
        "Revolut Current",
        "CSV",
        CsvProfileConfig(
            format="csv",
            columns=CsvColumns(
                date="Completed Date",
                started_at="Started Date",
                type="Type",
                description=["Description"],
                amount="Amount",
                fee="Fee",
                currency="Currency",
                balance="Balance",
                account_type="Product",
            ),
            date_format="%Y-%m-%d %H:%M:%S",
            # Pending and reverted payments have not moved the balance.
            include_only={"State": ["COMPLETED"]},
            card_types=_REVOLUT_CARD_TYPES,
        ),
    ),
    CatalogEntry(
        "Revolut Savings",
        "PDF statement",
        PdfProfileConfig(
            format="pdf", layout="revolut_savings_statement", card_types=_REVOLUT_INTEREST
        ),
        recommended=True,
    ),
    CatalogEntry(
        "Revolut Savings",
        "CSV",
        CsvProfileConfig(
            format="csv",
            columns=CsvColumns(
                date="Date",
                # The description is the row's kind: Deposit, Withdrawal or Gross Interest.
                type="Description",
                description=["Description"],
                inflow="Money in",
                outflow="Money out",
                balance="Balance",
            ),
            date_format="%b %d, %Y",
            amount_format=_REVOLUT_AMOUNTS,
            card_types=_REVOLUT_INTEREST,
        ),
    ),
    CatalogEntry(
        "Tembo",
        "PDF statement",
        # Interest is paid by Tembo, never from another bank account.
        PdfProfileConfig(
            format="pdf", layout="tembo_statement", card_types=["Interest", "Interest Adjustment"]
        ),
        recommended=True,
    ),
    # One layout and one CSV profile serve both the GBP account and the savings account.
    CatalogEntry(
        "LemFi",
        "PDF statement",
        PdfProfileConfig(format="pdf", layout="lemfi_statement"),
        recommended=True,
    ),
    CatalogEntry(
        "LemFi",
        "CSV",
        CsvProfileConfig(
            format="csv",
            columns=CsvColumns(
                date="Date",
                description=["Description"],
                inflow="Money in (GBP)",
                outflow="Money out (GBP)",
                balance="Balance (GBP)",
            ),
            date_format="%d %b, %Y %H:%M",
            currency="GBP",
            amount_format=_GROUPED,
            interest_descriptions=[lemfi.INTEREST_PAYOUT],
            extract=[
                CsvExtract(column="Description", patterns=lemfi.DESCRIPTION_PATTERNS),
                CsvExtract(column="Details", patterns=lemfi.DETAILS_PATTERNS),
            ],
        ),
    ),
    CatalogEntry(
        "Trading 212 Cash ISA",
        "History CSV",
        CsvProfileConfig(
            format="csv",
            columns=CsvColumns(
                date="Time (UTC)",
                type="Action",
                description=["Action"],
                memo="Notes",
                external_id="ID",
                amount="Total",
                currency="Currency (Total)",
            ),
            date_format="%Y-%m-%d %H:%M:%S%z",
            timezone="UTC",
            # A Cash ISA takes bank transfers only (Trading 212's payment methods): every row is
            # one. An interest row's Action goes here once a real export shows it.
            card_types=[],
            # The export states no period; Trading 212 names it by the one chosen.
            period_from_filename=r"from_(?P<start>\d{4}-\d{2}-\d{2})_to_(?P<end>\d{4}-\d{2}-\d{2})",
            # Notes: a deposit by bank transfer gives Trading 212's transaction id; a withdrawal
            # the bank account it was sent to, which names the own account it went to. An
            # internal move leaves Notes empty. Any other Notes makes the row an error, so a
            # new form is seen rather than dropped.
            extract=[
                CsvExtract(
                    column="Notes",
                    patterns=[
                        r"Transaction ID: [0-9A-Fa-f-]+",
                        r"Sent to Bank Account (?P<counterparty_sort_code>\d{2}-\d{2}-\d{2})"
                        r" / (?P<counterparty_account_number>\d{8})",
                    ],
                )
            ],
        ),
        recommended=True,
    ),
)
