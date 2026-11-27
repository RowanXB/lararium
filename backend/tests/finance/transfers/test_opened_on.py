"""An account's own opening date vouches for the days before its first upload (account type
catalog design §8.3 item 2): a batch whose first full day is on or before its account's
`opened_on` speaks for all time before, as one listing in full from its statement's
`account_opened_on` does (pairs._load_known). Without it, or with the batch's first full
day after it, M4 §5.8's import-coverage gate is unchanged."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import list_transfers, load_transfer_settings
from lararium.modules.finance.transfers.pairs import ONE_SIDED, pair_relation

from .conftest import (
    B_IDENTIFIER,
    DAY,
    ConnectorTx,
    Heal,
    MakeConnected,
    Run,
    StatementRow,
    assert_consistent,
)

NEXT = DAY + timedelta(days=1)


@pytest.mark.parametrize(
    ("opened_on", "complete_from", "pairs"),
    [
        (DAY, DAY, True),  # opened the day its first upload lists in full from
        (None, DAY, False),  # no opening date: the gate is unchanged
        (DAY, NEXT, False),  # the upload lists in full only from after the opening
    ],
)
async def test_an_account_s_opening_date_vouches_for_the_days_before(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
    opened_on: date | None,
    complete_from: date,
    pairs: bool,
) -> None:
    """A's bank transfer on DAY names the manual account B, so B's counterpart may be
    booked from the day before (its window). B's one upload lists in full from
    `complete_from`, with a same-amount bank-transfer row on DAY."""
    a = await connected_bank("A")
    b = await run(
        create_account,
        name="B",
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        opened_on=opened_on,
    )
    await run(assign_identifier, b.id, "uk_bank_account", B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    sent = await connector_tx(
        a, "-100", occurred_at=datetime.combine(DAY, time(12), UTC), identifier_id=identifier
    )
    row = await statement_row(b, "100", complete=(complete_from, DAY + timedelta(days=60)))
    await heal()

    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    [transfer] = [t for t in await list_transfers(session) if t.out_transaction_id == sent.id]
    if pairs:
        assert relation.tiers[sent.id][row] == ONE_SIDED
        assert (transfer.status, transfer.in_transaction_id) == ("matched", row)
    else:
        assert row not in relation.tiers.get(sent.id, {})
        assert transfer.in_transaction_id is None
    assert await heal() == 0
    await assert_consistent(session)
