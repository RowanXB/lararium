"""IBKR Flex: the commodities and securities parts of each Cash Report amount, as typed columns
of finance.ibkr_cash_reports.

With the currency breakout, the Cash Report gives every amount (startingCash, endingCash,
deposits, ...) also split by segment, e.g. endingCashCom and endingCashSec; each part is a
numeric column named in snake_case, like the amounts 0019 made. Rows stored before it are filled
by the replay that NORMALIZER_VERSION ibkr-norm-3 sets off.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-20
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CASH_AMOUNTS = """startingCash endingCash endingSettledCash deposits withdrawals
depositWithdrawals internalTransfers accountTransfers commissions dividends brokerInterest
brokerFees otherFees otherIncome withholdingTax paymentInLieu transactionTax netTradesSales
netTradesPurchases fxTranslationGainLoss advisorFees billPay billableCommissions
billableSalesTax bondInterest carbonCredits cashSettlingMtm cfdCharges clientFees
commissionCreditsRedemption debitCardActivity donations excessFundSweep feesReceivables
insuredDepositInterest ipoSubscription linkingAdjustments other paxosTransfers realizedForexVm
realizedVm referralFee salesTax slbEndingCashCollateral slbNetCash
slbNetSecuritiesLentActivity slbNetSettledCash slbStartingCashCollateral taxReceivables
withholding871m withholdingCollectedTax"""


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


_COLUMNS = [
    _snake(f"{amount}{segment}") for amount in _CASH_AMOUNTS.split() for segment in ("Com", "Sec")
]


def upgrade() -> None:
    added = ",\n      ".join(
        f"ADD COLUMN \"{c}\" numeric CHECK (\"{c}\" NOT IN ('NaN', 'Infinity', '-Infinity'))"
        for c in _COLUMNS
    )
    op.execute(f"ALTER TABLE finance.ibkr_cash_reports\n      {added}")


def downgrade() -> None:
    dropped = ",\n      ".join(f'DROP COLUMN "{c}"' for c in _COLUMNS)
    op.execute(f"ALTER TABLE finance.ibkr_cash_reports\n      {dropped}")
