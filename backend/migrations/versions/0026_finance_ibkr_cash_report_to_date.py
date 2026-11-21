"""IBKR Flex: the month and year to date totals of the Cash Report's flows, as typed columns of
finance.ibkr_cash_reports.

A short statement gives each flow (deposits, withdrawals, dividends, ...) also as depositsMTD and
depositsYTD; balances, FX translation, linking adjustments and other have none. Each total is a
numeric column named in snake_case, like the amounts 0019 made. Rows stored before it are filled
by the replay that NORMALIZER_VERSION ibkr-norm-3 sets off.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-21
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FLOWS = """deposits withdrawals depositWithdrawals internalTransfers accountTransfers commissions
dividends brokerInterest brokerFees otherFees otherIncome withholdingTax paymentInLieu
transactionTax netTradesSales netTradesPurchases advisorFees billPay billableCommissions
billableSalesTax bondInterest carbonCredits cashSettlingMtm cfdCharges clientFees
commissionCreditsRedemption debitCardActivity donations excessFundSweep feesReceivables
insuredDepositInterest ipoSubscription paxosTransfers realizedForexVm realizedVm referralFee
salesTax taxReceivables withholding871m withholdingCollectedTax"""


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


_COLUMNS = [_snake(f"{flow}{period}") for flow in _FLOWS.split() for period in ("MTD", "YTD")]


def upgrade() -> None:
    added = ",\n      ".join(
        f"ADD COLUMN \"{c}\" numeric CHECK (\"{c}\" NOT IN ('NaN', 'Infinity', '-Infinity'))"
        for c in _COLUMNS
    )
    op.execute(f"ALTER TABLE finance.ibkr_cash_reports\n      {added}")


def downgrade() -> None:
    dropped = ",\n      ".join(f'DROP COLUMN "{c}"' for c in _COLUMNS)
    op.execute(f"ALTER TABLE finance.ibkr_cash_reports\n      {dropped}")
