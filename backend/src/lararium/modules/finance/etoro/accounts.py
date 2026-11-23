"""The finance accounts of an eToro connection (design §5.5): one per account eToro's balances
list, created on first sight.

| eToro type | Account | Named | Mode | Tax wrapper |
|---|---|---|---|---|
| Trading | `asset`/`broker` | `eToro Trading` | `transactions` | `gia` |
| Cash (eToro Money) | `asset`/`bank` | `eToro Money <currency>` | `transactions` | `none` |
| MoneyFarm | `asset`/`broker` | `MoneyFarm ISA` | `snapshots` | `isa` |

Any other type is flagged and gets no account. Trading and eToro Money are registered under
their eToro account id (`etoro_account`); a snapshots account carries no identifier, so
MoneyFarm's id stays in finance.etoro_balance_accounts. The account map in the connection's
config remembers each (eToro account id → finance account id). The owner may rename them.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from lararium.core.entities import get_or_create_organization
from lararium.modules.finance.accounts import (
    AccountSubtype,
    BalanceMode,
    SourceDetails,
    TaxWrapper,
    create_account,
    get_account,
    set_source_details,
)
from lararium.modules.finance.errors import AccountNotFoundError
from lararium.modules.finance.etoro.state import EtoroDataError, Report, Run, objects_at, text_at
from lararium.modules.finance.identifiers import (
    ensure_identifier,
    get_or_create_identifier,
    normalize_identifier,
)

ACCOUNT_MAP_KEY = "account_map"
INSTITUTION = "eToro"


@dataclass(frozen=True)
class AccountKind:
    name: str
    subtype: AccountSubtype
    balance_mode: BalanceMode
    tax_wrapper: TaxWrapper
    # Registered under its eToro id: a snapshots account can carry no identifier.
    registered: bool


KINDS: Mapping[str, AccountKind] = {
    "Trading": AccountKind("eToro Trading", "broker", "transactions", "gia", True),
    "Cash": AccountKind("eToro Money {currency}", "bank", "transactions", "none", True),
    "MoneyFarm": AccountKind("MoneyFarm ISA", "broker", "snapshots", "isa", False),
}


async def _account(run: Run, etoro_id: str, kind: AccountKind, currency: str) -> None:
    mapped = run.account_map.get(etoro_id)
    account_id: UUID | None = None
    if isinstance(mapped, str):
        try:
            account_id = (await get_account(run.session, UUID(mapped))).id
        except AccountNotFoundError:
            account_id = None
    created_now = account_id is None
    if account_id is None:
        institution = await run.shared(
            ("organization", INSTITUTION),
            lambda cmd: get_or_create_organization(cmd, INSTITUTION),
        )
        created = await create_account(
            run.command,
            name=kind.name.format(currency=currency),
            ledger_type="asset",
            subtype=kind.subtype,
            balance_mode=kind.balance_mode,
            institution_entity_id=institution,
            tax_wrapper=kind.tax_wrapper,
            source_connection_id=run.connection_id,
        )
        account_id = created.id
        run.account_map[etoro_id] = str(account_id)
    run.accounts[etoro_id] = account_id
    changed = await set_source_details(
        run.command, account_id, SourceDetails(tax_wrapper=kind.tax_wrapper)
    )
    if kind.registered:
        # The row exists before ensure_identifier runs, so it takes no lock in this transaction.
        await run.shared(
            ("identifier", "etoro_account", etoro_id),
            lambda cmd: get_or_create_identifier(cmd, "etoro_account", etoro_id),
        )
        changed |= await ensure_identifier(run.command, account_id, "etoro_account", etoro_id)
    if created_now:
        run.report.accounts_created += 1
    elif changed:
        run.report.accounts_updated += 1
    else:
        run.report.accounts_unchanged += 1


async def accounts(run: Run, balances: Sequence[Report]) -> None:
    """The accounts every balances report lists (each once), found or created."""
    stored = run.config.get(ACCOUNT_MAP_KEY)
    run.account_map = dict(stored) if isinstance(stored, dict) else {}
    seen: dict[str, tuple[AccountKind, str]] = {}
    for report in balances:
        for entry in objects_at(report.content, "balances"):
            kind_name = text_at(entry, "accountType")
            kind = KINDS.get(kind_name)
            if kind is None:
                run.flag_value("balances", "balances[].accountType", kind_name)
                continue
            etoro_id = normalize_identifier("etoro_account", text_at(entry, "accountId"))
            seen[etoro_id] = (kind, text_at(entry, "currency").upper())
            run.account_types[etoro_id] = kind_name
    trading = sorted(i for i, t in run.account_types.items() if t == "Trading")
    if len(trading) > 1:
        raise EtoroDataError("eToro's balances list more than one Trading account")
    run.trading_id = trading[0] if trading else None
    for etoro_id, (kind, currency) in sorted(seen.items()):
        await _account(run, etoro_id, kind, currency)


async def trading_identifier(run: Run) -> UUID | None:
    """The registry row of the Trading account, which an eToro Money transfer to or from it
    names (design §5.12)."""
    if run.trading_id is None:
        return None
    trading_id = run.trading_id
    return await run.shared(
        ("identifier", "etoro_account", trading_id),
        lambda cmd: get_or_create_identifier(cmd, "etoro_account", trading_id),
    )
