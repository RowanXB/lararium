"""A chain address names the other side of a deposit or withdrawal, so the transfer engine can
pair it with the owner's own wallet (design 5.12)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import exc, text

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.identifiers import (
    assign_identifier,
    get_or_create_identifier,
    list_identifiers,
    register_card_merchant,
)
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    post_transaction,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    suggestions,
    transfer_contra,
)
from lararium.modules.finance.transfers.queries import list_transfers
from lararium.modules.finance.valuation import balances_asof

from .fixtures import BASE_TIME_MS, HOUR_MS, NOW_MS, FakeBinance, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    keys,
    normalize_rows,
    plain,
    postings,
    run_normalize,
    transaction,
)
from .syncing import binance_connection, later_config, run_binance_sync
from .test_records_capital import _deposit, _text_time, _withdrawal

WALLET_ADDRESS = "0x" + "a" * 40
OTHER_ADDRESS = "0x" + "b" * 40


async def _heal(session) -> int:
    async with command_context(
        session, Actor("system", "transfers"), "finance.heal_transfers", {}
    ) as ctx:
        return await heal_transfers(ctx)


async def _wallet(run, address: str | None = WALLET_ADDRESS):
    account = await run(
        create_account,
        name="Own wallet",
        ledger_type="asset",
        subtype="other",
        balance_mode="hybrid",
    )
    if address is not None:
        await run(assign_identifier, account.id, "chain_address", address)
    return account


async def _holdings(session, day) -> dict[str, Decimal]:
    """Quantity of each account on the day, by name."""
    rows = await balances_asof(session, day)
    names = {
        r.id: r.name for r in await session.execute(text("select id, name from finance.accounts"))
    }
    return {names[r.account_id]: r.quantity for r in rows if r.quantity != 0}


def _day():
    return (at(BASE_TIME_MS) + timedelta(days=2)).date()


async def test_withdrawal_to_a_registered_address_is_a_transfer(session, run) -> None:
    connection = await binance_connection(run)
    wallet = await _wallet(run)
    row = _withdrawal(6, address=WALLET_ADDRESS)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))
    await _heal(session)

    key = f"withdrawal:{row['id']}"
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(-2)),
        ("In transit", "AAA", Decimal(2)),
    ]
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.to_account_id) == ("inferred", wallet.id)
    assert transfer.placeholder_transaction_id is not None
    held = await _holdings(session, _day())
    # The placeholder stands in for the wallet's receipt: only the fee left net worth.
    assert held["Own wallet"] == Decimal(2)
    assert held["Binance Spot"] == Decimal("-2.1")
    assert "In transit" not in held
    assert held["Own wallet"] + held["Binance Spot"] == Decimal("-0.1")
    assert await find_transfer_violations(session) == []
    await assert_balanced_and_quiet(session, connection.id)


async def test_withdrawal_to_an_unknown_address_appears_in_suggestions(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, address=OTHER_ADDRESS)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))
    await _heal(session)

    assert plain(await postings(session, f"withdrawal:{row['id']}"))[1][0] == "Unassigned"
    [suggestion] = await suggestions(session)
    assert (suggestion.kind, suggestion.value) == ("chain_address", OTHER_ADDRESS)
    assert (suggestion.sort_code, suggestion.account_number) == (None, None)
    assert (suggestion.outgoing, suggestion.incoming) == (1, 0)
    assert suggestion.names == ("NET1 address",)  # the network, never the address
    assert await list_transfers(session) == []


async def test_only_kinds_the_owner_can_register_are_suggested(session, run, make_account) -> None:
    # IBKR names another IBKR account by `ibkr_account`, which Suggestions cannot register.
    account = await make_account(name="Broker", balance_mode="transactions")
    eur = await get_currency(session, "EUR")
    unassigned = await run(get_or_create_system_account, "unassigned")
    other = await run(get_or_create_identifier, "ibkr_account", "U0000009")
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=at(BASE_TIME_MS),
            description="to another IBKR account",
            postings=[
                PostingInput(account.id, eur.id, Decimal("-5")),
                PostingInput(unassigned, eur.id, Decimal("5")),
            ],
            counterparty=CounterpartyFields(identifier_id=other),
        ),
    )

    assert await suggestions(session) == []


async def test_registering_later_re_evaluates_past_withdrawals(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, address=WALLET_ADDRESS)
    await normalize_rows(session, connection.id, ("withdrawals", [row]))
    await _heal(session)
    assert await list_transfers(session) == []

    await _wallet(run)
    await _heal(session)

    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"
    assert plain(await postings(session, f"withdrawal:{row['id']}"))[1][0] == "In transit"
    assert await suggestions(session) == []
    assert await find_transfer_violations(session) == []


async def test_deposit_from_a_registered_address(session, run) -> None:
    connection = await binance_connection(run)
    wallet = await _wallet(run)
    row = _deposit(1, sourceAddress=WALLET_ADDRESS)

    await normalize_rows(session, connection.id, ("deposits", [row]))
    await _heal(session)

    assert plain(await postings(session, f"deposit:{row['id']}")) == [
        ("Binance Spot", "AAA", Decimal(5)),
        ("In transit", "AAA", Decimal(-5)),
    ]
    [transfer] = await list_transfers(session)
    assert (transfer.from_account_id, transfer.to_account_id) == (
        wallet.id,
        (await _spot(session)),
    )
    held = await _holdings(session, _day())
    assert held == {"Binance Spot": Decimal(5), "Own wallet": Decimal(-5)}
    assert await find_transfer_violations(session) == []


async def test_a_synced_deposit_from_a_registered_address_is_a_transfer(
    session, run, fake: FakeBinance, connector
) -> None:
    # Through the sync: the address comes back only because the walk asks for it.
    wallet = await _wallet(run)
    moment = NOW_MS - 2 * HOUR_MS
    row = _deposit(1, sourceAddress=WALLET_ADDRESS, insertTime=moment, completeTime=moment)
    fake.add("deposits", row)
    fake.set_rows("spot-account", [build_row("spot-account", asset="AAA", free="5", locked="0")])
    connection = await binance_connection(run, config=later_config())

    after = await run_binance_sync(session, connection.id, fake.handler)
    await _heal(session)

    assert after.status == "healthy", after.last_error
    assert plain(await postings(session, f"deposit:{row['id']}"))[1][0] == "In transit"
    [transfer] = await list_transfers(session)
    assert (transfer.from_account_id, transfer.to_account_id) == (wallet.id, await _spot(session))


async def _spot(session):
    return await session.scalar(text("select id from finance.accounts where name = 'Binance Spot'"))


async def test_internal_binance_transfer_is_unidentified(session, run) -> None:
    connection = await binance_connection(run)
    await _wallet(run)
    row = _deposit(1, transferType=1, txId="Off-chain transfer 1", sourceAddress=WALLET_ADDRESS)
    out = _withdrawal(
        6, id="w-int", transferType=1, txId="Internal transfer 2", address=WALLET_ADDRESS
    )

    await normalize_rows(session, connection.id, ("deposits", [row]), ("withdrawals", [out]))
    await _heal(session)

    assert [r.kind for r in await list_identifiers(session)] == ["chain_address"]  # the wallet's
    assert await list_transfers(session) == []
    for key in (f"deposit:{row['id']}", f"withdrawal:{out['id']}"):
        assert "In transit" not in {p[0] for p in plain(await postings(session, key))}
    assert await suggestions(session) == []


@pytest.mark.parametrize("spelling", [WALLET_ADDRESS, "0x" + "A" * 40, "0X" + "a" * 40])
async def test_evm_addresses_match_whatever_their_case(session, run, spelling) -> None:
    connection = await binance_connection(run)
    await _wallet(run, WALLET_ADDRESS)
    row = _withdrawal(6, address=spelling)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))
    await _heal(session)

    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"
    values = {r.value for r in await list_identifiers(session)}
    assert values == {WALLET_ADDRESS}


async def test_binance_deposit_address_is_not_registered(session, run) -> None:
    connection = await binance_connection(run)
    row = _deposit(1, address=OTHER_ADDRESS, sourceAddress=WALLET_ADDRESS)
    bare = _deposit(1, id="2002", address=OTHER_ADDRESS, sourceAddress=None)

    await normalize_rows(session, connection.id, ("deposits", [row, bare]))

    assert [r.value for r in await list_identifiers(session)] == [WALLET_ADDRESS]
    assert plain(await postings(session, f"deposit:{bare['id']}"))[1][0] == "Unassigned"
    await assert_balanced_and_quiet(session, connection.id)


async def test_chain_address_checked_by_database(session, run) -> None:
    for bad in ("0x123", "0x" + "g!" * 20, "a" * 129):
        with pytest.raises(exc.IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "insert into finance.account_identifiers (kind, value, created_by)"
                        " values ('chain_address', :v, 'user')"
                    ),
                    {"v": bad},
                )
    await run(get_or_create_identifier, "chain_address", WALLET_ADDRESS)


async def test_an_address_that_is_no_chain_address_is_flagged_not_failed(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, address="short")
    deposit = _deposit(1, sourceAddress="has spaces and is long enough to pass length")

    report = await normalize_rows(
        session, connection.id, ("withdrawals", [row]), ("deposits", [deposit])
    )

    assert sorted(report.new_flags) == [
        "deposits.sourceAddress is not a chain address",
        "withdrawals.address is not a chain address",
    ]
    assert len(await keys(session, connection.id)) == 3  # both, and the fee
    assert await list_identifiers(session) == []
    assert all("short" not in flag and "spaces" not in flag for flag in report.new_flags)


async def test_failed_withdrawal_to_a_registered_address_stops_being_a_transfer(
    session, run
) -> None:
    connection = await binance_connection(run)
    await _wallet(run)
    processing = _withdrawal(4, address=WALLET_ADDRESS, completeTime=None)
    await normalize_rows(session, connection.id, ("withdrawals", [processing]))
    await _heal(session)
    assert len(await list_transfers(session)) == 1

    refunded_ms = BASE_TIME_MS + 2 * HOUR_MS
    failed = _withdrawal(5, address=WALLET_ADDRESS, completeTime=_text_time(refunded_ms))
    await normalize_rows(session, connection.id, ("withdrawals", [failed]))
    await _heal(session)

    key = f"withdrawal:{failed['id']}"
    assert await list_transfers(session) == []
    derived = await session.scalar(
        text(
            "select count(*) from finance.transactions where origin in ('inference', 'rule')"
            " and status <> 'void'"
        )
    )
    assert derived == 0
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(-2)),
        ("Unassigned", "AAA", Decimal(2)),
    ]
    assert len(await postings(session, f"{key}:refund")) == 3
    assert (await transaction(session, f"{key}:refund")).status == "booked"
    assert await _holdings(session, _day()) == {}
    assert await find_transfer_violations(session) == []
    assert await _heal(session) == 0
    await assert_balanced_and_quiet(session, connection.id)


async def test_counterparty_name_is_the_network_only(session, run) -> None:
    connection = await binance_connection(run)
    named = _withdrawal(6, id="w-1", address=OTHER_ADDRESS, network="NET1")
    bare = _withdrawal(6, id="w-2", address=WALLET_ADDRESS, network=None)
    internal = _withdrawal(6, id="w-3", transferType=1, txId="Internal transfer 3")
    invalid = _withdrawal(6, id="w-4", address="short")

    await normalize_rows(session, connection.id, ("withdrawals", [named, bare, internal, invalid]))

    names = {
        r.external_id: r.counterparty_name
        for r in await session.execute(
            text(
                "select external_id, counterparty_name from finance.transactions"
                " where external_id like 'withdrawal:w-_'"
            )
        )
    }
    assert names == {
        "withdrawal:w-1": "NET1 address",
        "withdrawal:w-2": "address",
        "withdrawal:w-3": None,
        "withdrawal:w-4": None,
    }


# Only a deposit and a withdrawal can be a transfer leg (design 5.7): every other record the
# connector posts says so, though its two postings (one on Unassigned) have a leg's shape. A
# card payment of the same amount, to a merchant registered on the wallet, pairs with a leg
# only.

MERCHANT = "merch_TESTBIN"
_REWARD = build_row(
    "earn-flexible-rewards", asset="AAA", rewards="0.25", type="REALTIME", time=BASE_TIME_MS
)
_DIVIDEND = build_row("dividends", asset="AAA", amount="0.7", direction=1, divTime=BASE_TIME_MS)
# Inside Binance (no chain address), so a leg names nobody and the card tier can pair it.
_INSIDE = {"transferType": 1, "txId": "Internal transfer 1"}
_NO_FEE = {"transactionFee": "0", **_INSIDE}
# Each case: the label, the rows normalised one after the other, the key's suffix, and
# whether the record can be a transfer leg.
_CASES = {
    "deposit": ("deposits", [_deposit(1, **_INSIDE)], "", True),
    "withdrawal": ("withdrawals", [_withdrawal(6, **_INSIDE)], "", True),
    "withdrawal fee": ("withdrawals", [_withdrawal(6, **_INSIDE)], ":fee", False),
    "zero-fee refund": (
        "withdrawals",
        [
            _withdrawal(4, completeTime=None, **_NO_FEE),
            _withdrawal(5, completeTime=_text_time(BASE_TIME_MS + HOUR_MS), **_NO_FEE),
        ],
        ":refund",
        False,
    ),
    "dividend": ("dividends", [_DIVIDEND], "", False),
    "realtime reward": ("earn-flexible-rewards", [_REWARD], "", False),
}


async def _own_posting(session, connection_id, key: str):
    """The one posting of the transaction `key` on a Binance wallet, with its time."""
    return (
        await session.execute(
            text(
                "select p.account_id, p.instrument_id, p.quantity, t.occurred_at"
                " from finance.postings p join finance.transactions t on t.id = p.transaction_id"
                " join finance.accounts a on a.id = p.account_id"
                " where t.connection_id = :c and t.external_id = :k"
                " and a.source_connection_id = :c"
            ),
            {"c": connection_id, "k": key},
        )
    ).one()


async def _card_payment(session, run, account_id, instrument_id, quantity, occurred_at):
    """A card connection's payment naming the merchant, the wallet's opposite amount."""
    card = await run(create_connection, connector="monzo", display_name="Card", config={})
    bank = await run(
        create_account,
        name="Card",
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        source_connection_id=card.id,
    )
    merchant = await run(register_card_merchant, MERCHANT, [account_id])
    contra = await transfer_contra(
        session,
        source=("connection", f"{card.id}:card_1"),
        own_account_id=bank.id,
        counterparty_identifier_id=merchant.id,
        other_postings=0,
    )
    await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=occurred_at,
            description="Binance",
            origin="connector",
            connection_id=card.id,
            external_id="card_1",
            postings=[
                PostingInput(bank.id, instrument_id, -quantity),
                PostingInput(contra, instrument_id, quantity),
            ],
            counterparty=CounterpartyFields(identifier_id=merchant.id, bank_transfer=False),
        ),
    )


@pytest.mark.parametrize("case", list(_CASES))
async def test_only_a_deposit_or_withdrawal_pairs_with_a_card_payment(
    session, run, case: str
) -> None:
    label, rows, suffix, leg = _CASES[case]
    connection = await binance_connection(run)
    for row in rows:
        await normalize_rows(session, connection.id, (label, [row]))
    [key] = [
        k
        for k in await keys(session, connection.id)
        if (k.endswith(suffix) if suffix else not k.endswith((":fee", ":refund")))
    ]
    account_id, instrument_id, quantity, occurred_at = await _own_posting(
        session, connection.id, key
    )
    await _card_payment(session, run, account_id, instrument_id, quantity, occurred_at)

    await _heal(session)

    assert len(await list_transfers(session)) == (1 if leg else 0)
    never = await session.scalar(
        text("select never_transfer from finance.transactions where external_id = :k"),
        {"k": key},
    )
    assert never is not leg
    assert await find_transfer_violations(session) == []


async def test_a_connection_normalised_before_marks_its_records_on_the_next_run(
    session, run
) -> None:
    # Normalised by binance-norm-1, a reward was left unmarked: the new version replays it.
    connection = await binance_connection(run)
    await normalize_rows(session, connection.id, ("earn-flexible-rewards", [_REWARD]))
    [key] = await keys(session, connection.id)
    await session.execute(
        text("update finance.transactions set never_transfer = false where external_id = :k"),
        {"k": key},
    )
    await session.execute(
        text(
            "update core.connections set config = config"
            ' || \'{"normalizer_version": "binance-norm-1"}\' where id = :c'
        ),
        {"c": connection.id},
    )

    await run_normalize(session, connection.id)

    never = await session.scalar(
        text("select never_transfer from finance.transactions where external_id = :k"),
        {"k": key},
    )
    assert never is True


# On-chain BTC movements carry the chain, hash and address the Bitcoin wallet pairs on.
BTC_HASH = "ab" * 32
BTC_ADDRESS = "bc1q" + "x" * 20


async def _chain_of(session, key: str):
    return (
        await session.execute(
            text(
                "select chain, tx_hash, chain_address from finance.transactions"
                " where external_id = :k"
            ),
            {"k": key},
        )
    ).one()


async def test_btc_withdrawal_carries_chain_fields(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, network="BTC", txId=BTC_HASH.upper(), address=BTC_ADDRESS)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))

    found = await _chain_of(session, f"withdrawal:{row['id']}")
    assert tuple(found) == ("bitcoin", BTC_HASH, BTC_ADDRESS)


async def test_btc_deposit_carries_chain_fields(session, run) -> None:
    connection = await binance_connection(run)
    row = _deposit(1, network="BTC", txId=BTC_HASH, address=BTC_ADDRESS)

    await normalize_rows(session, connection.id, ("deposits", [row]))

    found = await _chain_of(session, f"deposit:{row['id']}")
    assert tuple(found) == ("bitcoin", BTC_HASH, BTC_ADDRESS)


async def test_segwitbtc_maps_to_bitcoin(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, network="SEGWITBTC", txId=BTC_HASH, address=BTC_ADDRESS)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))

    assert (await _chain_of(session, f"withdrawal:{row['id']}")).chain == "bitcoin"


async def test_bsc_btc_has_no_chain_fields(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, network="BSC", txId="0x" + BTC_HASH, address=WALLET_ADDRESS)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))

    found = await _chain_of(session, f"withdrawal:{row['id']}")
    assert tuple(found) == (None, None, None)


async def test_lightning_has_none(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, network="LIGHTNING", txId=BTC_HASH, address="lnbc1invented")

    await normalize_rows(session, connection.id, ("withdrawals", [row]))

    found = await _chain_of(session, f"withdrawal:{row['id']}")
    assert tuple(found) == (None, None, None)


async def test_a_hash_that_is_not_64_hex_has_none(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, network="BTC", txId="0x" + BTC_HASH, address=BTC_ADDRESS)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))

    found = await _chain_of(session, f"withdrawal:{row['id']}")
    assert tuple(found) == (None, None, None)


async def test_fee_and_refund_have_none(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(5, network="BTC", txId=BTC_HASH, address=BTC_ADDRESS)
    key = f"withdrawal:{row['id']}"
    live = _withdrawal(4, network="BTC", txId=BTC_HASH, address=BTC_ADDRESS, completeTime=None)
    await normalize_rows(session, connection.id, ("withdrawals", [live]))
    await normalize_rows(session, connection.id, ("withdrawals", [row]))

    assert tuple(await _chain_of(session, f"{key}:fee")) == (None, None, None)
    assert tuple(await _chain_of(session, f"{key}:refund")) == (None, None, None)


async def test_ended_withdrawal_has_none(session, run) -> None:
    connection = await binance_connection(run)
    live = _withdrawal(4, network="BTC", txId=BTC_HASH, address=BTC_ADDRESS, completeTime=None)
    await normalize_rows(session, connection.id, ("withdrawals", [live]))
    key = f"withdrawal:{live['id']}"
    assert (await _chain_of(session, key)).chain == "bitcoin"

    failed = _withdrawal(5, network="BTC", txId=BTC_HASH, address=BTC_ADDRESS)
    await normalize_rows(session, connection.id, ("withdrawals", [failed]))

    assert tuple(await _chain_of(session, key)) == (None, None, None)


async def test_version_bump_fills_existing_rows(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, network="BTC", txId=BTC_HASH, address=BTC_ADDRESS)
    await normalize_rows(session, connection.id, ("withdrawals", [row]))
    key = f"withdrawal:{row['id']}"
    await session.execute(
        text(
            "update finance.transactions set chain = null, tx_hash = null, chain_address = null"
            " where external_id = :k"
        ),
        {"k": key},
    )
    await session.execute(
        text(
            "update core.connections set config = config"
            ' || \'{"normalizer_version": "binance-norm-2"}\' where id = :c'
        ),
        {"c": connection.id},
    )

    await run_normalize(session, connection.id)

    assert tuple(await _chain_of(session, key)) == ("bitcoin", BTC_HASH, BTC_ADDRESS)
