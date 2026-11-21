# Owner Pairs Two Records Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner, through the API, list a record's pair candidates and pair any two own records as one transfer, even when neither record is in a transfer yet.

**Architecture:** The pair relation already has an owner `pair` decision at its strongest tier (`pairs.DECIDED`). It links any two own records regardless of the window, so nothing in it changes. A new domain function stores that decision for two records named directly, reusing the tail of `decisions.pair`. A new query lists candidates for a single record. Two routes in `transfers_api.py` expose them.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy (async), PostgreSQL 18, pytest with testcontainers on docker-test.

**Spec:** `docs/superpowers/specs/2026-11-21-owner-pair-records-design.md`

## Global Constraints

- No engine change: `pairs.py`, `engine.py`, `heal.py` and `contra.py` stay as they are, apart from reading their existing functions.
- The decision stored is the existing `pair` kind, keyed by `source_ref`, exactly as `decisions.pair` stores it.
- Owner-only API; route names are OpenAPI operationIds: `list_transaction_pair_candidates`, `pair_transactions`.
- Every refusal is `FinanceValidationError`, so the API returns 422 with `detail.error == "invalid_input"`. An unknown transaction is `TransactionNotFoundError`, so 404.
- Candidates: at most 20, nearest booking date first. No window applies.
- Synthetic test data only, with no real names, amounts or bank details. `Decimal` amounts.
- English docs. README.md and README.zh-CN.md are updated together.
- Signed commits, staged by explicit path (never `git add -A`). Titles follow `<type>: <summary>`.
- Run tests with `DOCKER_HOST=ssh://docker-test` (from `backend/`).

## Review Focus

1. A record that waits in an inferred transfer with a placeholder, paired to a hand-entered record on the placeholder's account: the placeholder must go, and nothing may be counted twice (test in Task 1).
2. The same transaction given as both sides: 422, nothing stored (Task 1).
3. Records in different instruments: 422 (Task 1).
4. A rejected record must not show up as a candidate, since pairing it is refused (Task 1). This is my call, following the spec's refusal.
5. Pairing, then a second heal: nothing changes (Task 1).

---

### Task 1: Domain: pair two records, and list a record's candidates

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/decisions.py` (`pair`, and the new `pair_records`)
- Modify: `backend/src/lararium/modules/finance/transfers/queries.py` (new `PairCandidateRecord`, `pair_candidates`)
- Modify: `backend/src/lararium/modules/finance/transfers/__init__.py` (export the new names)
- Test: `backend/tests/finance/transfers/test_decisions.py`

**Interfaces:**
- Produces:
  - `async def pair_records(ctx: CommandContext, transaction_id: UUID, other_id: UUID) -> TransferRecord` in `decisions.py`
  - `@dataclass(frozen=True) class PairCandidateRecord: transaction_id: UUID; account_id: UUID; quantity: Decimal; occurred_at: datetime; booking_date: date; description: str` in `queries.py`
  - `async def pair_candidates(session: AsyncSession, transaction_id: UUID, limit: int = 20) -> list[PairCandidateRecord]` in `queries.py`
  - `PAIR_CANDIDATES_LIMIT = 20` in `queries.py`

Records for the tests: hand-entered records are `ledger.post_transaction` with `origin="manual"` and two postings, the account and Unassigned (`ids.unassigned`). Connector records come from the `connector_tx` fixture with `identifier_id=None` and `bank_transfer=False`, so they name nobody. Accounts come from `connected_bank` and `manual_bank(identifier=None, snapshot=None)`. Call `run(pair_records, …)` the way the existing tests call `run(pair, …)`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_pair_records_links_two_records_that_name_nobody(session, run, ids, connected_bank, manual_bank, connector_tx, heal):
    # A connector record on A (-100, names nobody) and a hand-entered +100 on manual B.
    transfer = await run(pair_records, out_record.id, in_record.id)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == ("matched", out_record.id, in_record.id)
    assert await contra_account(session, out_record.id, a.account.id) == ids.in_transit
    assert await contra_account(session, in_record.id, b.id) == ids.in_transit
    assert await heal() == 0                       # a second heal changes nothing
    await assert_consistent(session)
    # The decision stored is M4's pair decision, by source reference:
    # ("pair", "connection", f"{a.connection_id}:out_1", "transaction", str(in_record.id))

async def test_pair_records_replaces_a_waiting_placeholder(...):
    # out_record on A names B's registered identifier, so it waits as "inferred" with a placeholder on manual B.
    # Pair it with a hand-entered +100 on B.
    assert transfer.status == "matched" and await placeholders(session) == []
    assert (await balances_on(session))[b.id] == <B's balance before> + Decimal("100")   # counted once

async def test_pair_records_refusals_store_nothing(...):
    # Each of these raises FinanceValidationError, and no transfer_decisions row is added:
    #   the same id twice; two records on one account; -100 against +90; GBP against USD;
    #   a voided record; a record with a fee posting (3 postings);
    #   a leg of a matched transfer (message says to unpair it first);
    #   a rejected record (message says to undo the rejection first).

async def test_pair_records_after_unpair(...):
    # pair_records, then unpair(transfer.id): both back on Unassigned with never_pair both ways.
    # pair_candidates(out_record.id) still lists in_record; pair_records again matches them,
    # and the never_pair rows are gone.

async def test_pair_candidates_order_and_exclusions(...):
    # Out record -100 on A, booked on DAY. Opposite +100 records on other accounts booked
    # DAY+3, DAY-1, DAY+1 -> listed DAY-1, DAY+1, DAY+3 (DAY-1 and DAY+1 are both 1 day away;
    # the earlier occurred_at goes first).
    # Excluded: same account; +90; a voided one; a leg of a matched transfer; a rejected one;
    # one kept apart by never_pair. 25 eligible ones -> exactly 20 returned.
    # An ineligible subject (voided) raises FinanceValidationError; an unknown id raises TransactionNotFoundError.
```

- [ ] **Step 2: Run them to see them fail**

Run: `DOCKER_HOST=ssh://docker-test uv run --locked pytest -q tests/finance/transfers/test_decisions.py -k "pair_records or pair_candidates"`
Expected: an ImportError for `pair_records` / `pair_candidates`.

- [ ] **Step 3: Implement**

  - **`decisions.py`:** move the tail of `pair` into a private function, `async def _store_pair(ctx: CommandContext, present: UUID, chosen: UUID) -> TransferRecord`. It covers the lines from the two `source_ref` calls to the end: forget the `never_pair`, store `pair`, `match_transaction(ctx, present)`, heal, load the matched transfer, or raise the existing "did not take effect" error. `pair` keeps its own checks and then calls it.
  - **`pair_records`:** call `lock_transfers(ctx)` first, then refuse, in this order:
    - the two ids are equal;
    - either record's `own_leg` is `None` ("cannot be a transfer's leg"). An unknown id raises `TransactionNotFoundError` from `source_ref`, so call `source_ref` first;
    - the accounts are the same, the instruments differ, or `quantity` isn't exactly the negation;
    - either record is a leg of a `matched` transfer (query `models.Transfer`);
    - `is_rejected(session, source_ref(...))` holds for either.

    Then return `await _store_pair(ctx, transaction_id, other_id)`.
  - **`pair_candidates`:**
    - Refuse an ineligible subject as `pair_records` does.
    - Select from `contra.eligible_records(system)`: rows with a different account, the same instrument, `quantity == -subject.quantity`, and not the subject.
    - Exclude legs of matched transfers, records whose `source_ref` is rejected, and records with a `never_pair` decision against the subject's ref (either direction).
    - Join `models.Transaction` for the description and booking date.
    - Order by `abs(booking_date - subject booking_date)`, then `occurred_at`, then `transaction_id`; `limit(limit)`.

- [ ] **Step 4: Run them to see them pass, plus the existing decision tests**

Run: `DOCKER_HOST=ssh://docker-test uv run --locked pytest -q tests/finance/transfers/test_decisions.py tests/api/test_transfers_api.py`
Expected: all pass, the existing `pair` tests included.

- [ ] **Step 5: Commit**

```bash
git add backend/src/lararium/modules/finance/transfers/decisions.py backend/src/lararium/modules/finance/transfers/queries.py backend/src/lararium/modules/finance/transfers/__init__.py backend/tests/finance/transfers/test_decisions.py
git commit -S -m "feat: let the owner pair two records as one transfer"
```

### Task 2: API routes, OpenAPI and docs

**Files:**
- Modify: `backend/src/lararium/modules/finance/schemas.py` (new `PairCandidateOut`)
- Modify: `backend/src/lararium/modules/finance/transfers_api.py` (two routes)
- Modify: `web/openapi.json` and `web/src/api/*` (regenerated by `make openapi`)
- Modify: `README.md`, `README.zh-CN.md` (the Transfers section), `docs/superpowers/specs/2026-11-15-transfer-matching-design.md` (§5.9, one line)
- Test: `backend/tests/api/test_transfers_api.py`

**Interfaces:**
- Consumes: `transfers.pair_records`, `transfers.pair_candidates` and `PairCandidateRecord` from Task 1; the existing `TransferPair` body and `TransferOut`, plus `healing_command`, `_args` and `_transfers_out` in `transfers_api.py`.
- Produces:
  - `class PairCandidateOut(BaseModel): transaction_id: UUID; account_id: UUID; account_name: str; quantity: DecimalStr; occurred_at: AwareDatetime; booking_date: date; description: str`
  - `GET /api/v1/finance/transactions/{transaction_id}/pair-candidates`, named `list_transaction_pair_candidates`, returning `list[PairCandidateOut]`
  - `POST /api/v1/finance/transactions/{transaction_id}/pair`, named `pair_transactions`, with body `TransferPair`, returning `TransferOut`. It runs in `healing_command(session, actor, "pair_transactions", args)`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_pair_two_records_through_the_api(client, session, run):
    # A connector record on A (-100, names nobody) and a +100 hand-posted through POST /transactions on manual B.
    found = (await client.get(f"{FINANCE}/transactions/{out_id}/pair-candidates")).json()
    assert [(x["transaction_id"], x["account_name"], x["quantity"]) for x in found] == [(in_id, "B", "100")]
    response = await client.post(f"{FINANCE}/transactions/{out_id}/pair", json={"transaction_id": in_id})
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["out_transaction_id"], response.json()["in_transaction_id"]) == ("matched", out_id, in_id)
    assert await _transfers(client) == [response.json()]
    # Unpair through the existing route, then pair again: 200 and matched.

async def test_pair_transactions_refusals(client, session, run):
    # The same id twice -> 422 invalid_input; -100 against +90 -> 422; unknown id -> 404 transaction_not_found
    # (for both routes).
```

- [ ] **Step 2: Run them to see them fail**

Run: `DOCKER_HOST=ssh://docker-test uv run --locked pytest -q tests/api/test_transfers_api.py -k "pair_two_records or pair_transactions"`
Expected: 404 or 405 from the missing routes.

- [ ] **Step 3: Implement**

  - Add the schema and the two routes, next to `pair_transfer`. `account_name` comes from `accounts.list_accounts`, as `list_transfer_candidates` does.
  - Run `make openapi` from the repository root.
  - Docs:
    - In both READMEs, a short paragraph in the Transfers section: when neither record names the other account, the owner can list a record's candidates and pair it with one through these two routes, and undo it with Unpair.
    - In the M4 design §5.9, one line saying the `pair` decision now also has this second entry point, which needs no open transfer.

- [ ] **Step 4: Run the full check**

Run: `DOCKER_HOST=ssh://docker-test make check` (from the repository root)
Expected: exits 0. `openapi-check` passes, because the regenerated files are staged.

- [ ] **Step 5: Commit**

```bash
git add backend/src/lararium/modules/finance/schemas.py backend/src/lararium/modules/finance/transfers_api.py backend/tests/api/test_transfers_api.py web/openapi.json web/src/api README.md README.zh-CN.md docs/superpowers/specs/2026-11-15-transfer-matching-design.md
git commit -S -m "feat: add API routes to list pair candidates and pair two records"
```
