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
            amount_format=_GROUPED,
            extract=[
                CsvExtract(column="Description", patterns=lemfi.DESCRIPTION_PATTERNS),
                CsvExtract(column="Details", patterns=lemfi.DETAILS_PATTERNS),
            ],
        ),
    ),
)
