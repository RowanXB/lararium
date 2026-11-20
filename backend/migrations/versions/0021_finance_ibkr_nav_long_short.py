"""IBKR Flex: the long and short parts of each NAV amount, as typed columns of finance.ibkr_nav.

The NAV section gives every amount (stock, cash, total, ...) also split into its long and
short parts, e.g. stockLong and stockShort; each part is a numeric column named in snake_case,
like the amounts 0019 made. Rows stored before it are filled by the replay that
NORMALIZER_VERSION ibkr-norm-2 sets off.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-20
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAV_AMOUNTS = """total cash stock funds bonds options commodities notes crypto interestAccruals
dividendAccruals cgtWithholdingAccruals bondInterestAccrualsComponent brokerCashComponent
brokerFeesAccrualsComponent brokerInterestAccrualsComponent cfdUnrealizedPl
eventContractInterestAccruals fdicInsuredAccountInterestAccrualsComponent
fdicInsuredBankSweepAccountCashComponent forexCfdUnrealizedPl incentiveCouponAccruals
insuredBankDepositRedemptionCashComponent ipoSubscription liteSurchargeAccruals
marginFinancingChargeAccruals physDel providerCryptoCash slbCashCollateral
slbDirectSecuritiesBorrowed slbDirectSecuritiesLent softDollars"""


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


_COLUMNS = [
    _snake(f"{amount}{side}") for amount in _NAV_AMOUNTS.split() for side in ("Long", "Short")
]


def upgrade() -> None:
    added = ",\n      ".join(
        f"ADD COLUMN \"{c}\" numeric CHECK (\"{c}\" NOT IN ('NaN', 'Infinity', '-Infinity'))"
        for c in _COLUMNS
    )
    op.execute(f"ALTER TABLE finance.ibkr_nav\n      {added}")


def downgrade() -> None:
    dropped = ",\n      ".join(f'DROP COLUMN "{c}"' for c in _COLUMNS)
    op.execute(f"ALTER TABLE finance.ibkr_nav\n      {dropped}")
