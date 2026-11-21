# M4 design: transfer matching

- Date: 2026-11-15
- Status: implemented on feat/claude-transfer-matching (2026-11-15)
- Builds on: `main` at `bdcb64a` (M0, M1, M3 merged; migrations up to `0012`)
- Parent spec: `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (§6.3, §6.6, §8, §10.2, §13 M4, §14)
- Related: the M2 manual import plan (`docs/superpowers/plans/2026-11-12-manual-import.md`, being revised on its own branch). M2 imports must go through the matching described here.

## 1. Intent

When money moves between two of the owner's own accounts at different institutions, both balances must change, and net worth must neither drop nor count the money twice, at any point, including while the money is on its way, except while a transfer needs review (Known caveats, §5.8). Today every synced Monzo transfer posts against the system account "Unassigned": Monzo's balance is right, but the other account never changes.

M4 builds the matching **rules and interfaces** once, as a generic engine in the finance module. Monzo sync is the first caller; M2 imports (CSV, OFX, QIF, midata and PDF) and later connectors (IBKR in M5, eToro and Trading 212 in M6, Binance in M7, wallets in M8) call the same engine.

### What the owner's data shows (checked read-only on 2026-11-15; no values are recorded here)

- Monzo Faster Payments carry the counterparty's name, sort code and account number in `counterparty`, in both directions. The payment reference is in `notes` (identical to `metadata.notes`).
- Transfers to and from the owner's own accounts go both ways and are all GBP Faster Payments, settling almost at once, with the same amount on both sides.
- The owner's name appears in different forms on the same counterparty account (case, word order, initials), so names cannot identify an account. Sort code plus account number can.
- Some payees are an institution's collection account, not an account in the owner's name (a stocks-and-shares broker, an ISA provider, a remittance service). Paying one of them is still a deposit into the owner's account there.
- No transfer arrived in parts. Some transfers were declined (declined transactions are never posted, so nothing to match).
- No manual accounts exist yet: the owner will create them (with an opening snapshot) and register their identifiers through the UI.

## 2. Goals, non-goals, anti-goals

### Goals

1. A registry of the owner's account identifiers (spec §6.3 "my accounts").
2. Every transfer between two own accounts is recognised automatically when the counterparty identifier is registered, in either direction, for the whole history already in the ledger as well as for new transactions.
3. A manual receiving account's balance changes at once (spec §15: "Monzo self-transfers automatically increase or decrease the counterpart account's balance").
4. Net worth is correct at every point, except while a transfer needs review (Known caveats, §5.8): no dip while money is on its way, no double count when the other side's own record arrives later.
5. One mechanism for every case: manual receiver, connected receiver, either direction, either arrival order.
6. A matching interface M2 calls for imported rows, so an imported row that is the other side of a known transfer never becomes a duplicate.
7. The owner is not bothered by transfers that match cleanly. The review page lists only what needs a decision.
8. The owner's decisions (reject a match, pick a candidate, dismiss a suggestion) survive re-syncs and later ledger regeneration (spec §6.6).

### Non-goals (in this milestone)

- Statement import itself (M2), and PDF parsing (M2).
- IBKR, broker, Binance and wallet connectors (M5 to M9). M4's connected-receiver path is exercised with two synthetic Monzo connections.
- Pairing by on-chain transaction hash (M7) and identifier kinds that only those milestones need (IBAN, wallet address, xpub, card digits).
- Automatic FX or fee postings for transfers whose two sides differ in currency or amount. M4 detects them and asks the owner (§5.7); automatic handling comes with the first connector that produces them.
- A transaction list page. The transfers page shows one row per transfer.
- Reconciliation alerts comparing manual snapshots with the ledger (M9).
- Moving an account from manual to connected (still open: M5 does not need it, as the owner has no manual IBKR account, 2026-11-18).

### Anti-goals (failure even if everything else works)

- Net worth dips while a transfer is on its way, or counts a transfer twice.
- A connected account's ledger balance differs from what its institution reports because another source wrote to it.
- The result depends on which source synced first.
- A re-sync or regeneration undoes the owner's decision.
- A new subsystem or abstraction beyond what these cases need.

## 3. Constraints

Everything in the parent spec and the earlier plans' global constraints applies: Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL 18 with raw-SQL Alembic migrations, `Decimal`/`NUMERIC`/strings (no floats), UUIDv7, audited writes through `command_context`, `core` stays finance-agnostic, import-linter contracts, English UI copy and docs, synthetic test data only (no real bank data in the repo, tests or PR text), domain checks before every rule the database enforces, 4xx (never 500) for request errors.

Existing behaviour M4 builds on:

- `upsert_connector_transaction` replaces a connector transaction's fields and postings in place when they differ, and writes nothing when they are identical. So whatever decides a connector transaction's contra account must give the same answer on every re-sync, or every sync would flip it.
- Monzo pot moves already post directly against the pot's account. M4 does not touch them.
- System accounts are created by `get_or_create_system_account(ctx, key)`, remembered in settings, serialised by an advisory lock.
- Valuation (`finance.v1_balances_asof`): a `hybrid` account's balance is its latest snapshot plus the postings that occurred after it; a `transactions` account's is the sum of its postings; a `snapshots` account ignores postings.

## 4. Approaches considered

1. **The first-seen side is the truth (e.g. "Monzo is the source"); the other side's records only confirm.** One transaction posts both sides; when the receiver's own record arrives it is absorbed, and only a conflict is reported. It is simple, and with today's data (same-currency instant transfers) it gives the right balances. Rejected because:
   - a slow receiver (a broker credits a bank transfer a day or two later) would show more than its institution reports for those days, failing spec §14's "each data source's balances match what the institution reports";
   - a receiver in another currency, or one that takes a fee, would be a conflict on every transfer;
   - in the other direction the broker reports the withdrawal before Monzo sees the deposit, so the money has nowhere to go for a day and net worth dips, and the broker's own transaction would have to be rewritten later by someone other than its connector;
   - transfers not involving Monzo (broker to exchange, exchange to wallet) need a rule anyway;
   - absorbing a connector's own record makes that account's ledger depend on another source's sync order.
2. **Post directly to a manual receiver; use an in-transit account only for a connected receiver.** One transaction in the common case. Rejected because the split is not principled: once M2 exists, a manual account also reports its own records (by import), so both kinds are "receivers that report later". It would be two mechanisms for one situation.
3. **Chosen: one rule for every transfer.** Each account keeps only its own institution's records, and the money in between sits in an asset account **"In transit"**. The receiving side's record is either the receiver's own record (from its connector or an import), or, for an account that cannot report on its own, an **inferred placeholder** posted at once and replaced when the real record arrives. The owner picked this on 2026-11-15, after asking for a single unified approach.

## 5. Design

### 5.1 The rule

A transfer from own account A to own account B is, in the ledger:

- **A's record**: A's own transaction (from A's source), with its other leg against **In transit** instead of Unassigned.
- **B's record**: B's own transaction (from B's source), with its other leg against **In transit** instead of Unassigned; or, while B has none, an inferred placeholder transaction posting In transit → B.

When both sides are present, In transit nets to zero for that transfer. While only A's side is present, the money shows in In transit, which counts in net worth, so net worth never dips. The direction does not matter: an incoming transfer to Monzo from a manual account is the same rule with the roles swapped.

Who supplies B's side depends only on whether B can report on its own:

| Receiver B | B's side |
|---|---|
| has a connection (another Monzo connection today; IBKR from M5) | wait for B's own record; alert when it is overdue |
| manual (`source_kind = 'manual'`) | an inferred placeholder at once; replaced by B's imported record when it arrives (M2) |

The same holds for the sender side: if B's record is seen first and A's side is missing, B's record goes to In transit and the transfer waits for A (or, for a manual A, gets a placeholder on A).

### 5.2 Accounts

- **In transit**: a new system account (key `in_transit`), `ledger_type = 'asset'`, `subtype = 'in_transit'`, `balance_mode = 'transactions'`, one account for all instruments. Being an asset, it counts in net worth. `get_or_create_system_account` gains the key and creates it with these attributes (the existing keys stay equity). An account is a system account when a `finance.system_account.*` setting holds its id; account records carry that as `system` (the API's `AccountOut.system`, which the web reads instead of guessing from the subtype, which owners may also pick). The owner can't close, rename or snapshot a system account (422): closing In transit would drop the transfers it holds from net worth.
- **Manual receiving accounts** should be `hybrid` with an opening snapshot, so a placeholder moves the balance from the snapshot on, and placeholders dated before the latest snapshot are already contained in it (no double count). A manual hybrid account's history starts at its first snapshot in each instrument, as a connected account's starts at its first record: a transfer whose own record is at or before that snapshot's `as_of` (valuation's own cut-off, `occurred_at <= as_of`) is already inside it, so it is `before_history`, settled against Opening balances, with no placeholder (`queries.manual_predates`). Otherwise valuation, which sums a hybrid account's postings from zero on days before its first snapshot, would value the account at minus those transfers on every earlier day. The rule is per instrument (a GBP snapshot settles no USD transfer). A `transactions`-mode manual account ignores its snapshots. With statement imports (M2, added when M2 was wired in, 2026-11-17), its own records are known up to the last day its committed imports list in full in the instrument: each such day is a statement's, listing the real rows, or lies before one, inside its opening or gap adjustment. So a transfer whose window (the days its own side may be booked on) ends by that day is `before_history` the same way, settled against Opening balances, and the account's balance stays the statements'. Only a side that may fall after it, not imported yet, gets a placeholder. Importing further statements or rolling one back moves the line, and heal reopens or settles accordingly. Without imports such an account's history is all its own records, so it gets placeholders. Recording a snapshot heals at once, and an earlier snapshot moves the transfers it no longer contains back to `inferred`. A `transactions`-mode manual account without imports is correct only if its whole history is entered. M4 does not change valuation and leaves the mode to the owner; the README says to create manual accounts as `hybrid`.
- **A `snapshots`-mode account cannot carry identifiers.** Valuation ignores its postings, so a placeholder there would change nothing and net worth would dip until the next snapshot. Adding an identifier to one is refused (422) with a message saying to switch it to `hybrid`, which values it exactly as before (the latest snapshot) plus whatever is posted after it. The registry UI offers the switch. (Builder-check call.)
- **No difference postings in M4.** All of the owner's own accounts deal in GBP and none takes a fee on arrival (owner, 2026-11-15), so a transfer's two sides are always the same instrument and amount. A pair that differs is a case that needs review the owner can only reject or unpair; difference postings (FX, fees) come with the first connector that produces them (M6: IBKR's transfers are same-currency, 2026-11-18).

### 5.3 The registry (`finance.account_identifiers`)

- One row per bank account any source has named: `kind`, normalised `value`, `account_id` (nullable), `dismissed_at` (nullable), `created_by`, `created_at`. Unique on `(kind, value)`.
  - With `account_id`: one of the owner's accounts.
  - Without: a counterparty account not (yet) known to be the owner's. These are the suggestions (§5.10).
  - `dismissed_at`: the owner said it is not theirs.
- M4 has one kind, `uk_bank_account`: value `"<6-digit sort code>:<8-digit account number>"`, digits only. Input with dashes and spaces is normalised; anything else is a 422. Further kinds (IBAN, wallet address, xpub) are added by the milestones that need them.
- Registering an identifier assigns the row to an account (creating it if no source has named it yet); removing one unassigns it (the row stays, as transactions reference it).
- A row that resolves to the transaction's own account is not a transfer.
- **Connectors register their own accounts.** Monzo's `/accounts` response carries each account's sort code and account number, so the Monzo normalizer assigns them to its accounts (actor `connector`, audited) on every sync, never taking a row the owner assigned elsewhere. A row the owner dismissed is assigned all the same: a connector's own account is a fact, so the dismissal was a mistake. (Builder-check call.)

### 5.4 Counterparty fields on transactions

The owner's data-model rule (2026-11-15): every field a source provides is stored as a typed field, unless it can be computed from other data or has a better structured home. The counterparty's bank account has one: the registry. So `finance.transactions` gains, with no JSON:

- `counterparty_identifier_id` → the registry row of the counterparty's bank account;
- `counterparty_name` (as the source gave it);
- `payment_reference`;
- `bank_transfer` (boolean): the source itself marks the record as a payment between bank accounts (a Faster Payment or BACS for Monzo; a bank's transfer type code in an import; a broker's deposit or withdrawal record). It gates the amount-and-date fallback in §5.7.

They are part of the transaction, so `upsert_connector_transaction` compares them like the other fields. Monzo fills them from `counterparty` and `notes` (the payment reference); M2 fills what its formats give, through the same registry function. **Existing history** gets them too: the Monzo transactions already in the ledger are past the incremental overlap, so the plan fills them once from the stored raw records, and registering an identifier re-evaluates the whole history.

### 5.5 Transfers (`finance.transfers`)

One row per transfer between two own accounts:

- `from_account_id`, `to_account_id`, `instrument_id`, `amount` (positive, as sent);
- `out_transaction_id` (A's own record), `in_transaction_id` (B's own record), `placeholder_transaction_id`: each nullable, with at least one own record unless the transfer is `rejected` (a rejection outlives records rolled back, §5.9); a transaction is a leg of at most one transfer;
- `status`: `awaiting` (one own record, the other side is connected and has not reported), `inferred` (one own record plus a placeholder), `matched` (both own records), `before_history` (the other side is connected but the transfer predates that account's history, §5.7), `rejected` (the owner said it is not a transfer); and, apart from the status, a `review_reason` (`several_candidates` or `amount_differs`) when the rule could not decide, which is what "needs review" means below;
- `opened_at` (the earliest of its own records' occurrence times, whichever arrived first), `matched_at`, `notified_at` (overdue alert sent).

**The invariant**, enforced in the service layer and checked by the integrity job: a transaction's contra leg is In transit if and only if it is a leg (out, in, or placeholder) of a transfer that is not `rejected`. Every other connector or import transaction's contra stays Unassigned. This covers every recorded transaction with a posting on In transit, whatever its shape: a leg that stops looking like one (a re-sync adds a fee posting, say) loses its transfer, and its In transit posting goes back to Unassigned.

### 5.6 Deciding a transaction's contra

One function, `transfer_contra(session, source, own account, counterparty registry row, other postings)`, used by every caller:

1. a stored owner decision for this transaction's stable reference (§5.9) wins: rejected → Unassigned; and a transaction with postings besides its own and the contra (a fee) is no leg, so Unassigned (the caller passes how many; one rule, `LEG_POSTINGS`, shared with eligibility). The Monzo normalizer passes its fee postings (typed Monzo fields, PR #11), so a payment with a fee stays on Unassigned; matching fee'd transfers, gross or net, is left to a later change;
2. otherwise, if the transaction is already a leg of an active transfer → In transit;
3. otherwise, if the transaction's counterparty registry row belongs to an own account other than the transaction's own account → In transit;
4. otherwise → Unassigned.

The Monzo normalizer calls it before each upsert, so a re-sync gives the same answer and writes nothing. Matching itself (§5.7) runs after the sync has committed, in a short transaction of its own, so its lock is never held for the length of a sync. In between, a newly identified record already sits in In transit, so net worth is right even before its transfer row exists. When something outside a sync changes the answer (an identifier is added or removed, the owner decides, the matcher links a transaction found by amount and date), the engine re-points the contra leg in place through a ledger function that only swaps a posting's account between Unassigned and In transit. The next re-sync then computes the same contra and writes nothing.

### 5.7 Matching

`match_transaction(ctx, transaction_id)` runs for a new or changed transaction. Linking is decided on the set of records, never on the order they arrived in:

- **Candidates.** Two own records are candidates for one transfer when they are on different accounts, in the same instrument, of opposite sign, the receiving one booked inside the sending one's window (counted in booking dates, below), neither rejected nor decided never to pair with the other, and at least one of them is identified through the registry. Each identified one must name the other's account; each one that names no registered account must have `bank_transfer` true, so a card refund of the same amount is never linked.
- **Tiers.** Candidates are ranked by their evidence: an owner `pair` decision, then mutually identified (each names the other's account), then one-sided (one names the other's account, the other is only a bank transfer). A record's candidates are those in the strongest tier it has any in, and only those for which that tier is also the candidate's strongest, so weaker evidence (an unrelated same-amount bank payment) never makes a stronger pair ambiguous.
- **Link only on a mutual singleton.** Two records link only when each is the other's only candidate. One exception, added 2026-11-19: n records whose candidates are exactly the same n others, and the reverse, each side alike (same account, instrument, amount and booking date, as when a transfer is split into equal parts), link in a fixed order (by time, then by reference). Any pairing of them gives the same ledger, and the order doesn't depend on which side arrived first (`pairs._twins`). Where one side names no account, it is found by amount and date, as a single counterpart is (one-sided): an unrelated row of the amount and day can be among it while a real one is still to arrive (§5.8), and when that arrives the group no longer matches and goes to review. A row previewed without the reference it will have can't be told its place in such a group, so `preview_match` gives none. A registry-identified record left unlinked has a transfer of its own: `inferred` with a placeholder if the other account is manual, `awaiting` if it is connected (either `before_history` instead when the other account's history starts after it, below and §5.2) (needing review if it has several candidates, below). A record naming no counterparty bank account (a broker's deposit, an imported row) links to the transfer waiting on its account under the same rule; linked to an `inferred` transfer, it replaces the placeholder (§5.8).
- **Ambiguity or difference**: a record with several candidates gets `review_reason = several_candidates`. A mutually identified counterpart of another amount gets `amount_differs`; an unidentified bank payment of another amount is no evidence, so it is not flagged. During review the ledger stays as it was: a registry-identified own record is a leg and stays in In transit (it is certainly a transfer; only its counterpart is open), a placeholder stays posted, and candidates found by amount and date are not legs and stay Unassigned until the owner pairs them. So the invariant in §5.5 holds for legs. But the open side (the identified record's In transit leg, or the placeholder) still holds the amount while the real counterpart, one of the candidates, counts on its account against Unassigned. Net worth is therefore off by the amount, in the candidates' direction, until the owner pairs (Known caveats, §5.8).
- **Before the other side's history**: when the other account is connected and the last booking date the transfer's counterpart could have (the window's end) is before the booking date of that account's earliest record from its connection, not counting its opening balance adjustment (a record posted against "Opening balances", which a connector dates before the history it accounts for), its counterpart will never arrive: it is already inside that account's opening balance adjustment. The matcher marks it `before_history` and posts the amount from In transit to the system account "Opening balances", so In transit nets to zero and nothing is alerted. If older history is fetched later and the counterpart appears, the matcher reverses that posting and matches normally. (Builder-check call.) A manual hybrid counterpart has the same status by its first snapshot instead (§5.2), and its real record, imported later, replaces the settlement as it replaces a placeholder.
- **Order independence**: candidates and tiers are functions of the set of records and decisions, so no link depends on which source synced first. Matching never changes them (it only re-points contras and opens or dissolves transfers), so the pair relation is computed once per heal pass, set-wise, over the records that can pair at all (a bank transfer, one naming a bank account, or one the owner paired): a card payment costs nothing however long the ledger. The matcher is also query-driven: after any change it looks for every record whose state disagrees with the rule (identified but not linked, linked but no longer fitting, a review reason a fresh evaluation would not give, a placeholder out of step with its record) and matches those, so a crash between a sync's commit and its matching is repaired by the next run.
- **Concurrency**: matching is serialised by a transaction-scoped advisory lock taken before reading candidates, so two syncs of different connections cannot both link the same transfer. Tests use real concurrent sessions.
- **Defaults** (settings, changeable): the receiving side's record must be booked from 1 calendar day before to 5 after the sending side's, both included (a Faster Payment arrives the same day; BACS takes 3 working days, up to 5 calendar days over a weekend). The window is counted in booking dates, not instants: a statement row carries only its date (stored at 00:00), so a window of 5 × 24 hours from it would miss a Monzo record later on the 5th day, and only in that direction. `pairs.counterpart_bounds` and `queries.counterpart_window` are its one definition, in Python and SQL, and `before_history` compares the window's last date with the history's first booking date; an `awaiting` transfer is overdue after 7 days. Each setting is a whole number of days from 1 to 3650 (checked before anything is computed from it, so no value can overflow a date), the two windows together must be shorter than 7 days, and a setting that makes them longer is refused, so a weekly recurrence's window never reaches the next occurrence (§6).

### 5.8 Placeholders

- An inferred placeholder is a transaction with `origin = 'inference'`, `status = 'inferred'`, no connection, posting In transit and the manual account, dated as the own record it mirrors.
- It follows that record: if the record's amount, date or status changes (a pending payment settles for another amount), the placeholder is updated; if the record is voided, the transfer is dissolved and the placeholder deleted. (Matching dissolves and reopens a transfer whenever its records change, so a transfer's id is not stable; decisions are therefore keyed by records, §5.9.)
- **Replaced by the real record.** When the manual account's own record arrives (an M2 import), the matcher links it as the transfer's other own record, re-points its contra to In transit, and deletes the placeholder (it is derived data, so no void rows pile up as imports come and go). The transfer becomes `matched`. If that import is rolled back, the transfer goes back to `inferred` and the placeholder is posted again. The parent spec §8 (item 2) was changed to match in M4: the placeholder used to become `confirmed`, with no new transaction. The outcome is the same (no duplicate, the transfer is confirmed), and this way the manual account's ledger holds the statement's own dates and description, and an import goes through the same path as a connector record.

**Known caveats** (accepted, all temporary and visible in the valuation only):

- While a transfer needs review because several records could be its other side (on a connected or a manual account, in either direction, or because several identified records share one candidate), the open side (In transit leg or placeholder) and the real counterpart both count. Net worth is off by the amount, in the candidates' direction (overstated when they are inflows, understated when they are outflows), until the owner pairs. The transfer is under Needs attention and alerted.
- A placeholder is dated as the connected record it mirrors. If the owner records a snapshot of the manual account dated between the two banks' bookings of a multi-day transfer (BACS), the snapshot and the placeholder disagree about which side of it the money is on, so the manual account is understated by the amount until its next snapshot after both bookings, or until its own imported row replaces the placeholder. No review or alert fires.
- A counterpart booked later than the window (more than 5 calendar days after the sending side, over a long bank holiday say) is no candidate, so it does not pair. When the other account is connected, the transfer stays `awaiting`: In transit and the real row (on its account, against Unassigned) both count until the owner pairs them (a pair decision ignores the window), and the overdue alert fires. When the other account is manual and its late row names nobody, the placeholder and the real row both count until the owner pairs them, and nothing fires: `inferred` is not alerted. Either way the account is double counted by the amount. (Offering such a row as a review candidate is left as a minor.)
- **An unrelated row can stand in for a counterpart that is not in the ledger** (found by the property tests, 2026-11-16; closed at the start of the days imported when M2 was wired in, PR #12). The amount-and-date fallback (§5.7) assumed that a manual account's own row of a transfer, if it exists, is in the ledger. It may not be: the row can lie inside the account's opening, or after the statements imported so far, and an unrelated imported row of the same amount inside the window would be taken as the counterpart. **The fix:** a one-sided pair (one record identified, the other found by amount and date) whose unidentified record is on a manual account is taken only when the identified record's window doesn't start before the first day that account's committed imports list in full in the instrument (the first `complete_from` of batches with live rows in the instrument; `pairs.Known`). Before it, the real row may be inside the opening (snapshot or first statement), never to be imported, and a row taken for it would never be found out; a day inside an opening snapshot is never listed in full, so this covers the snapshot case without a rule of its own. After the last day imported a row is taken (2026-11-19; until then the window had to end by the last day listed in full too, see the cost below): should it not be the real one, the next statement brings the real row, and with two candidates the transfer goes to review. A whole statement left out between two imports is not detected here. A row not yet imported is previewed with the first day its own import would add (`preview_match(complete=...)`). The property test of the opening case asserts the caveat's absence (`test_no_unrelated_receipt_stands_in_*`); the end case is the cost below.
- **An unrelated row can stand in after the last day imported, until the next statement** (the cost of the fix above, 2026-11-19). When a manual account's own row of a transfer lies after the last day imported, and another imported bank transfer of the same amount, naming nobody, is booked inside the window, that row is taken for it. Net worth is off by the amount (the real side isn't anticipated by a placeholder) until the next statement brings the real row; then the transfer has two candidates and goes to review. Chosen (owner's decision, 2026-11-19) over the placeholder the window used to keep until a statement listed every day of it, which counted the real row twice whenever it was already imported, the usual case: a transfer in a statement's last days. The property tests name the cost `stand_in_until_next_statement`; a window starting before the days imported keeps its placeholder or settlement (`window_starts_before_imports`). M2's opening walk leaves placeholders out: an adjustment is sized from statements and real rows only, so matching posting or taking off a placeholder later (another account's import, a sync) never leaves it stale, and the next statement takes no adjustment for the difference. No placeholder lies on days a transactions account's imports account for: a transfer there is `before_history` (§5.2). For such an account this also removes the double count of a receipt booked after its window: the transfer is settled and the row counts once. That transfer shows as `before_history`, not as needing review, and its row stays on Unassigned (owner's decision, 2026-11-18; see the M2 plan).

**The interface M2 calls:**

- after committing a batch: `match_transaction` for each committed row (or a batch form);
- while staging: `preview_match(session, account_id, instrument_id, quantity, occurred_at, counterparty, source, booking_date)` (`booking_date` optional, resolved as posting resolves it, `ledger.resolve_booking_date`, since the window is counted in it), returning the own record the row would pair with (its unique partner in the pair relation matching reads), or none, so the staging view can show "pairs with transfer from Monzo on …" from that record's account, date and description. Posting the row links it with that record, completing the record's open transfer or replacing whatever transfer it is in;
- on batch rollback: the rows are deleted and `unlink_transaction` restores the affected transfers (placeholder posted again).
- imported rows post their contra through `transfer_contra`, like connector transactions.

### 5.9 The owner's decisions

Stored in `finance.transfer_decisions`, keyed by the stable reference of the transaction they concern: a generic `(source_kind, source_ref)` pair, which is `('connection', '<connection_id>:<external_id>')` for connector rows and whatever stable key M2 gives an import row (M4 lands first, per the owner, so it ships only this generic key and a simulated import source in tests). A placeholder has none and is never decided on directly.

- `reject` (this is not a transfer): applies to the whole transfer, and is stored for every own record in it, so neither leg re-opens a transfer through the registry on the next sync. Each reject also names its rejected transfer (`transfer_id`, deleted with it), which matching never dissolves. Each own record's contra goes back to Unassigned, the transfer is `rejected` and its placeholder (or `before_history` settlement) deleted. A record rolled back (`unlink_transaction`) leaves the rejected transfer, which stays even with no record left, decision and all; imported again under the same reference, the record goes back into it (matching, and heal for any it missed). Withdrawing it (`unreject`, "Undo" in the UI) deletes every decision naming it and the rejected transfer, and matches its records again;
- `unpair` (these two records are not the same transfer, but each still is one): stored as "never pair X with Y"; the transfer is split and each record goes back through matching on its own. Each record's transfer then lists the other as a candidate (reason `unpaired`), so a mistaken unpair can be undone with `pair`;
- `pair` (link this record with that one): resolves a case that needs review with several candidates, or undoes an unpair (its "never pair" decisions are withdrawn); allowed only when both records have the same instrument and amount (§5.2). It is the strongest tier of evidence (§5.7), so it outranks any other candidate. If another pair decision still leaves it ambiguous, it is refused and nothing is stored. The same decision now has a second entry point, `POST /transactions/{id}/pair`, which needs no open transfer (owner pairs two own records; design `2026-11-21-owner-pair-records-design.md`);

(Builder-check call: the parent spec's "reject a match" covers both a rejected transfer and a wrong pairing, so both exist.)

`transfer_contra` and the matcher consult them first, so they hold across re-syncs and, once regeneration exists, after the ledger is rebuilt (spec §6.6). Every decision is audited.

### 5.10 Suggestions

Registry rows with no account and not dismissed that transactions name, with the names they appeared under, the count in each direction and the latest date. The owner either registers one against an existing account (creating a manual account first if needed) or dismisses it (`dismissed_at`). Registering re-evaluates every past transaction naming it. Ranking by name similarity is not done: the owner's names vary and the self entity carries no real name, so the list is sorted by count and recency instead.

### 5.11 Notifications

A periodic job (every 30 minutes, after healing) sends one notification per transfer that has been `awaiting` longer than the overdue setting, and one when a transfer starts needing review, through the existing notifier. `notified_at` keeps them from repeating. When one run has more than 3 due (the first run after deploy may find a whole history), it sends one summary ("N transfers need your attention: see Transfers") instead, and marks them all notified.

### 5.12 API and UI

- **API** (under `/api/v1`, owner-only like the rest):
  - identifiers: list, add, delete per account; switching an account's balance mode (to `hybrid`, so it can carry identifiers);
  - transfers: list, reject, unreject, unpair, pair (pick a candidate), list candidates for a transfer that needs review;
  - suggestions: list, dismiss (registering one is adding the identifier to an account).
- **Transfers page** (`/transfers`), usable on a phone:
  - **Needs attention**: needing review and overdue `awaiting` transfers, each with its candidates (Pair on those of the same amount) and Reject;
  - **In transit**: `awaiting` transfers with days in transit;
  - **Suggestions**: unregistered counterparty accounts, with register or dismiss; the register picker offers only accounts that can carry identifiers, and one line names the `snapshots`-mode accounts left out and says to switch them to `hybrid`;
  - **Recent**: the other transfers, one row each ("Monzo → Bank A · 100.00 GBP · inferred"), collapsed by default: Unpair on a matched one, Undo on a rejected one, Reject on the rest.
- **Registry**: an identifiers section on each asset or liability account (on the Accounts page), add and remove; a `snapshots`-mode account offers the switch to `hybrid` instead.

## 6. Edge cases (parent spec §8.7) and how this covers them

| Case | Handling |
|---|---|
| Pot moves | Unchanged: posted directly against the pot. |
| Pending payment that settles for another amount, or is reversed | The placeholder follows its record (§5.8). |
| Refund or returned payment | An incoming record from the same own account is a transfer in the other direction; it matches like any other. A payment returned by the receiving bank is a pending payment reversed, or an incoming credit from the registered account; both are covered above. |
| Two own Monzo accounts (e.g. joint), each its own connection | Both sides are connected: `awaiting`, then `matched` (the M4 end-to-end test for the connected path). |
| Recurring transfers | Each occurrence is its own transfer; with weekly or rarer recurrences the window leaves one candidate each. Same-amount transfers less than a window apart go to review. |
| One transfer arriving in parts | Not handled automatically; it needs review (the owner reports none). |
| Intermediary as an own account with a balance (a remittance service account) | An ordinary own account: each leg matches separately. |
| Intermediary as a pure conduit | Not built in M4 (no such use found in the data); the registry kind and the `awaiting` state are where it would go. |
| Binance ↔ wallet by transaction hash | M7. |

## 7. Left for later milestones

- **M2**: posts imported rows through `transfer_contra`, calls the matcher after commit and `unlink_transaction` on rollback, shows `preview_match` (the own record a row would pair with) in staging, and gives each row a stable reference for decisions. M2 should reuse the system account "Unassigned" rather than its own "Unassigned (imports)".
- **M5**: IBKR deposits and withdrawals carry no Monzo details, so they match by amount and date (§5.7). (Changed 2026-11-18: FX and fee postings moved to M6, and what happens to an account's earlier placeholders when it gets a connection stays open, as the owner has no manual IBKR account; see `2026-11-18-ibkr-flex-design.md`.)
- **M6**: FX and fee postings for the first transfers whose two sides differ (eToro).
- **M8**: transaction-hash matching.
- **M10**: comparing manual snapshots with inferred balances.

## 8. Left to the builder

- Table and column names, indexes, and the exact constraint set (as long as the invariant in §5.5 is enforced by the service and, where possible, by the database).
- How existing Monzo transactions get their counterparty fields from raw records (a data migration or a one-off job), as long as the owner's existing history is covered on deploy.
- The batch form of the matcher, the integrity-check query, the notification wording, and the UI layout.
- Endpoint paths and payload shapes, following the existing finance API's conventions.

## 9. Answers from the owner (2026-11-15)

- **Accounts.** Transfers go to and from several UK banks and fintechs, a remittance service account with its own balance, a stocks-and-shares broker and a trading platform; nothing arrives in parts. The owner keeps the mapping of their real identifiers privately and registers them through the UI.
- **No deposit references.** At the one provider paid through a collection account, the owner holds a single product, so a sort code and account number always identify one account. The registry has no reference field; it can be added with a kind that needs it.
- **GBP only.** Every own account sends and receives GBP and none takes a fee, so M4 posts no FX or fee differences (§5.2).
- **Order.** M4 lands its rules and interfaces first; M2 imports, PDF included, then go through the same matching.

## 10. Verification

- Tests use synthetic data only: hand-written Monzo transactions with synthetic sort codes, account numbers and names, and synthetic manual accounts.
- The tests that pin the goals:
  - outgoing to a manual account: Monzo down, the manual account up, In transit zero, net worth unchanged;
  - the same in the other direction;
  - two Monzo connections, both arrival orders: `awaiting` then `matched`, same ledger either way, net worth never dips;
  - registering an identifier later re-points past transactions and creates their placeholders; removing it reverts them;
  - re-syncing writes nothing; a rejection survives a re-sync;
  - a simulated import row replaces the placeholder; rolling it back restores the placeholder;
  - two candidates → needs review; a different amount → needs review;
  - concurrent matching of one transfer from two sessions links it once;
  - a transfer older than the connected counterpart's history is `before_history`, In transit nets to zero, nothing is alerted;
  - a `snapshots`-mode account refuses an identifier;
  - a same-amount card refund on the waiting account is not linked.
- The owner verifies locally against the real Monzo history after registering identifiers (balances of the manual accounts against their own statements), never committed.
