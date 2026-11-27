"""The finance account of a Trading 212 connection (design §5.5), and its sibling (§5.12).

One account per connection (one key pair is one account), created on first sight: `asset`,
`broker`, balance mode `transactions`, named `Trading 212 Invest ···<last 4 of id>` or
`Trading 212 ISA ···<last 4>` by the connection's account kind, its tax wrapper `gia` or `isa`,
and registered under its Trading 212 account id (`trading212_account`). The account map in
the connection's config remembers it.

A TRANSFER names no other account. When the owner has exactly two accounts of the Trading
212 family (account_types: Invest, Stocks ISA, Cash ISA; open or closed), the other is the only
account it can be with: it is the sibling, and a TRANSFER names its Trading 212 id's registry
row, so the pair is mutually identified. A third account of the family, or none registered
under a Trading 212 id (a manual Cash ISA), names nothing.
"""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.entities import get_or_create_organization
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.account_types import family_keys
from lararium.modules.finance.accounts import (
    SourceDetails,
    TaxWrapper,
    accounts_of_types,
    create_account,
    get_account,
    set_source_details,
    set_type_key,
)
from lararium.modules.finance.errors import AccountNotFoundError
from lararium.modules.finance.identifiers import (
    ensure_identifier,
    get_or_create_identifier,
    normalize_identifier,
)
from lararium.modules.finance.trading212.state import Record, Run, Trading212DataError, text_at

ACCOUNT_MAP_KEY = "account_map"
INSTITUTION = "Trading 212"
# The family (account_types) of the accounts a TRANSFER can be with.
FAMILY = "trading212"
# The connection's account kind: the account's name and tax wrapper.
KINDS: Mapping[str, tuple[str, TaxWrapper]] = {
    "invest": ("Trading 212 Invest", "gia"),
    "isa": ("Trading 212 ISA", "isa"),
}
# The account type (finance/account_types.py) of each account kind.
TYPE_KEYS: Mapping[str, str] = {"invest": "trading212.invest", "isa": "trading212.stocks_isa"}


def mapped_id(config: Mapping[str, JSONValue]) -> str | None:
    """The Trading 212 account id a connection's account map holds, when it holds one."""
    stored = config.get(ACCOUNT_MAP_KEY)
    if isinstance(stored, dict) and len(stored) == 1:
        [trading212_id] = stored
        return trading212_id
    return None


async def account(run: Run, summary: Record | None) -> None:
    """The connection's finance account, found or created; its details and registry row."""
    stored = run.config.get(ACCOUNT_MAP_KEY)
    run.account_map = dict(stored) if isinstance(stored, dict) else {}
    if summary is not None:
        trading212_id = normalize_identifier("trading212_account", text_at(summary.content, "id"))
    elif (mapped := mapped_id(run.config)) is not None:
        trading212_id = mapped
    else:
        raise Trading212DataError("Trading 212's account summary is missing")
    kind = run.config.get("account_kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise Trading212DataError("the connection's account kind is neither invest nor isa")
    name, wrapper = KINDS[kind]
    run.trading212_id = trading212_id

    account_id: UUID | None = None
    mapped_account = run.account_map.get(trading212_id)
    if isinstance(mapped_account, str):
        try:
            account_id = (await get_account(run.session, UUID(mapped_account))).id
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
            name=f"{name} ···{trading212_id[-4:]}",
            ledger_type="asset",
            subtype="broker",
            balance_mode="transactions",
            institution_entity_id=institution,
            tax_wrapper=wrapper,
            source_connection_id=run.connection_id,
            type_key=TYPE_KEYS[kind],
        )
        account_id = created.id
        run.account_map[trading212_id] = str(account_id)
    run.account_id = account_id
    changed = await set_source_details(run.command, account_id, SourceDetails(tax_wrapper=wrapper))
    changed |= await set_type_key(run.command, account_id, TYPE_KEYS[kind])
    # The row exists before ensure_identifier runs, so it takes no lock in this transaction.
    await run.shared(
        ("identifier", "trading212_account", trading212_id),
        lambda cmd: get_or_create_identifier(cmd, "trading212_account", trading212_id),
    )
    changed |= await ensure_identifier(run.command, account_id, "trading212_account", trading212_id)
    if created_now:
        run.report.accounts_created += 1
    elif changed:
        run.report.accounts_updated += 1
    else:
        run.report.accounts_unchanged += 1


async def family(session: AsyncSession) -> list[UUID]:
    """The owner's accounts of a Trading 212 type (account_types), whatever their status."""
    return await accounts_of_types(session, family_keys(FAMILY))


async def sibling(run: Run) -> UUID | None:
    """The registry row of the sibling account (module doc): when the family has exactly two
    accounts and this one is one of them, the other's Trading 212 id; else None."""
    if run.sibling is not None:
        return run.sibling
    ids = await family(run.session)
    if len(ids) != 2 or run.account not in ids:
        return None
    [other] = [i for i in ids if i != run.account]
    i = models.AccountIdentifier
    registered = list(
        await run.session.scalars(
            select(i.id).where(i.account_id == other, i.kind == "trading212_account")
        )
    )
    # One account, one Trading 212 id: none (a manual account's) or more names no one.
    if len(registered) != 1:
        return None
    run.sibling = registered[0]
    return run.sibling
