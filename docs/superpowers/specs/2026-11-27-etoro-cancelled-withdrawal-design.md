# Design: an eToro withdrawal the owner cancelled

- Date: 2026-11-27
- Status: proposed (short design, a small change)
- Builds on: `main` at `379e4d6`; the M6 design (`2026-11-22-etoro-trading212-design.md`, §5.7 and §5.12); the M4 design (`2026-11-15-transfer-matching-design.md`, §5.5)

## 1. Intent

The owner asked eToro to withdraw cash from the Trading account and cancelled the request a few minutes later. The statement's Account Activity sheet lists such a withdrawal as five rows (amounts and times here are invented):

| Time (UTC) | Type | Amount |
|---|---|---|
| 10:00:00 | Withdraw Request | −100.00 |
| 10:00:00 | Withdraw Fee | −5 |
| 10:04:00 | Withdraw Request Cancelled | +100.00 |
| 10:04:00 | Withdraw Fee Cancelled | +5 |
| 10:04:00 | Withdraw Fee | +5 |

Today each row that moves cash posts against Unassigned (M6 design §5.7). The cash-flow report reads Unassigned, so the month shows the withdrawal as spending and its cancellation as income, though neither happened, and the same for the fee. The request is also still a bank transfer waiting for a counterpart, so the transfer engine could pair it with an unrelated receipt of the same amount.

The owner wants three things to hold together:

1. **History.** Both events stay as transactions. They happened: the owner asked, then cancelled.
2. **Balance.** The Trading account's cash follows the statement at every second: down at the request, back at the cancellation.
3. **Income, spending and net worth.** Unaffected. The money was the owner's the whole time.

Voiding the transactions was rejected for this reason: a void says the record is not a fact, and it removes all three at once.

## 2. Goals, non-goals, anti-goals

**Goals**

1. A cancelled eToro withdrawal request and its cancellation count as neither spending nor income, and neither does a cancelled withdrawal fee and its refund.
2. All four stay booked transactions with their statement times, amounts and descriptions.
3. The Trading account's cash equals the statement's balance at every second, as it does today.
4. Neither transaction of a cancelled pair can be paired with another record as a transfer.
5. The ledger is the same whatever order the statements are uploaded in, and the same when regenerated from raw records.

**Non-goals**

- Any other connector. Only eToro's statement writes the new link (owner, 2026-11-27).
- A web page that lists transactions. There is none today; these transactions are booked and linked, so a later one can show them as cancelled.
- Showing the pair on the Transfers page.
- A partial cancellation (a cancellation of another amount than its request). The owner's statements show none.
- Linking by a withdrawal id. The statement gives none (`Details` is "-" on both rows), and the M6 design takes the Trading account's cash events from the statement only (§5.7). eToro's API documentation was not read again for this design.

**Anti-goals**

- A withdrawal that was not cancelled being treated as cancelled.
- The cancelled pair leaving a balance on In transit.
- A change in how any transaction without the link is posted, paired or reported.

## 3. Approaches

**A. Void both transactions.** Small (one source file). Rejected: a void means the record is not a fact, the account's cash would stop following the statement during the pending period, and a request already posted would be removed from history when its cancellation arrived.

**B1. A transfer from the Trading account to itself.** The pair appears on the Transfers page today. Rejected: it says the money went to the Trading account when it was meant for a bank; the fee and its refund are not a transfer; it needs the check `from_account_id <> to_account_id` dropped, which today protects every transfer, and a review of 42 uses of the two columns; heal would need an exemption, and the owner's reject and unpair actions would need a meaning for it.

**B2. A cancellation link, both transactions on In transit (chosen by the owner, 2026-11-27).** The cancellation records which transaction it cancels. Both post their contra on In transit, an asset account the cash-flow report does not read. About 140 lines over 7 files, no change to the web UI, and no change for any transaction without the link. Its costs: the pair is not visible in the app until a transaction history page exists, and the ledger gains a second relation between transactions besides the transfer.

## 4. Design

### 4.1 Which rows are a cancelled pair (eToro statement only)

A function of the connection's stored Account Activity rows, as `_fee_links` is, so the result does not depend on which statement brought which row.

- Rows are taken in order of time, then row id.
- A "Withdraw Request Cancelled" row cancels the latest earlier "Withdraw Request" row whose cash is the same amount with the opposite sign and that no other row cancels.
- A "Withdraw Fee Cancelled" row marks a refund: the "Withdraw Fee" row with positive cash of the same amount within one second of it, before or after, is the refund (owner, 2026-11-27). In the owner's statement the two share a second; the margin is for an operation eToro writes across a second's boundary. Each "Withdraw Fee Cancelled" row marks one refund: the nearest in time that no other marks, the earlier row on a tie. The refund cancels the latest earlier "Withdraw Fee" row with negative cash of that amount that no other row cancels. A positive "Withdraw Fee" row with no "Withdraw Fee Cancelled" row beside it is not a refund and posts as today.
- The request's pair and the fee's pair are found independently. A withdrawal with no fee has only the first: its request and cancellation are a pair with no fee rows at all.
- A cancellation or refund with no such earlier row cancels nothing. It posts as today (against Unassigned).

With two requests of one amount and one cancellation, the later request is the cancelled one. The statement does not say which it was; the choice changes only which request's time the remaining withdrawal carries.

### 4.2 The link

A new nullable column on `finance.transactions`, `cancels_transaction_id`: the transaction this one cancels.

- A foreign key to `finance.transactions (id)`, never the transaction's own id, and unique among transactions that are not void (a transaction is cancelled by at most one live transaction). A void transaction keeps its link as it was voided, since the owner's void is never rewritten, but holds nothing: an earlier cancellation a later upload brings can take the request.
- Written through `TransactionInput` and `upsert_connector_transaction`, which updates it in place when it changes, as it does the transaction's other fields.
- Returned on the transaction record and in the API schema.

It is a general column because the transfer engine reads it and must not read eToro's tables. Only the eToro statement ledger writes it.

### 4.3 Posting

For a row in a cancelled pair, the eToro statement ledger posts the same transaction it posts today (same key, time, description, amount, `bank_transfer`, and `is_fee` for a fee), with two differences:

- its contra is In transit, chosen directly, not through `transfer_contra`;
- the cancellation, or the refund, carries `cancels_transaction_id`: the id of the request's, or the fee's, transaction.

A run that brings one row of a pair also re-posts the other from its stored row, so a request posted against Unassigned by an earlier upload moves to In transit when its cancellation arrives. The request posts before its cancellation.

"Withdraw Fee Cancelled" still posts nothing.

### 4.4 The transfer engine

The M4 invariant (§5.5) gains one case. A transaction's contra is In transit if and only if it is a leg of a transfer that is not rejected, **or it is one of a cancelled pair**: it has `cancels_transaction_id`, or another transaction's `cancels_transaction_id` names it, and neither of the two is void.

A cancelled pair's two postings on In transit are in one instrument and sum to zero. The integrity job checks this, so the balance of In transit still means what it meant: money between two accounts.

Three places read the new case:

- `queries.stray_in_transit`: a transaction of a cancelled pair is not stray. `find_transfer_violations` and heal read this query, so neither reports or moves it.
- `engine._release_stray`: leaves it on In transit.
- `contra.eligible_records`: leaves it out, so it is never a candidate for pairing, automatic or by the owner.

### 4.5 Reports

No change to the code. The cash-flow report reads income, expense and equity accounts only, so the pair leaves it. Valuation already counts In transit as the owner's asset, so net worth is level across the pending period.

### 4.6 Version and deployment

- `NORMALIZER_VERSION` goes from `etoro-norm-3` to `etoro-norm-4`, so the next sync replays the stored statements.
- Migration `0045` adds the column (renumbered from `0041` after the Binance connector's `0041`–`0044` reached `main`).
- After deployment, in production: the owner's cancelled withdrawal and its fee (four transactions) post against In transit, the cancellation and the refund each carry the link, In transit's balance is unchanged, and November's spending and income are each lower by the pair's and the fee's value.

## 5. Tests

- **eToro statement ledger:** a request and its cancellation (both on In transit, linked, cash follows the statement); a fee, its refund and the "Withdraw Fee Cancelled" row; a cancellation uploaded in a later statement than its request; a cancellation with no request (posts as today); two requests of one amount and one cancellation; a positive "Withdraw Fee" row with no "Withdraw Fee Cancelled" row (posts as today). The two existing tests of these rows change to the new postings.
- **Properties:** the existing generated histories already include a withdraw request and a cancellation; the five properties must hold unchanged.
- **Transfer engine:** a cancelled pair is not a violation, is not healed off In transit and is not a pairing candidate; a pair whose In transit postings do not sum to zero is a violation; a pair with one side void is no pair.
- **Cash flow:** a cancelled pair adds nothing to income or spending.

## 6. Documents to amend

- M6 design §5.7 (the two bullets on "Withdraw Request" and "Withdraw Fee") and its table row "Withdraw Request then Withdraw Request Cancelled".
- M4 design §5.5 (the invariant).
- The module documentation of `etoro/statement_ledger.py`.

## 7. Known limits

- **Until the cancellation arrives**, the request is posted as any bank transfer with no counterpart yet: against Unassigned, so it shows as spending. It moves to In transit when a statement brings the cancellation.
- **The pair is found by amount and order**, since the statement gives nothing else.
- **A cancellation whose request is in a statement never uploaded** stays on Unassigned and shows as income.
- **While the Trading account is hidden from the reports and the request and its cancellation have different booking dates**, net worth shows the request's amount on In transit on the days between: `valuation._SHOWN_HOLDINGS` drops In transit money only for transfers to or from a hidden account.

## 8. Left to the builder

- The names of the helper functions and how `Run.post` carries the link.
- Whether the sum-to-zero check lives in `find_transfer_violations` or beside it.
- How the web API types are regenerated, as the repository does it.
