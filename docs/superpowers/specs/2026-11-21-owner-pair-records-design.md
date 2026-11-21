# Design: the owner pairs two records as one transfer

- Date: 2026-11-21
- Status: proposed (short design, a small change)
- Builds on: `main` at `149f75c`; the M4 design (`2026-11-15-transfer-matching-design.md`, §5.7 and §5.9)

## 1. Intent

Some moves between the owner's own accounts can't be recognised by M4, because neither record names the other side's bank account. The case that prompted this: four BOC rows to and from the owner's BOC deposit account, which print only the owner's name, a short reference and the bank's name, with no sort code or account number. The deposit account has no statements. Its side will be entered by hand, as a manual `transactions`-mode account.

Both sides then sit on Unassigned, so each move would count as spending on one account and income on the other. The owner wants to say "these two records are one transfer" so they move to In transit and link, as an M4 transfer does.

M4 already has this decision: an owner `pair` decision is its strongest tier (`pairs.DECIDED`). It ignores the window and links any two own records on different accounts with the same instrument and opposite, equal amounts (`pairs._decided_edges`). The only gap is the entry point: `POST /transfers/{id}/pair` needs an open transfer, and a transfer opens only when a record names a registered account. Neither record here opens one.

## 2. Goals, non-goals, anti-goals

**Goals**

1. The owner can pair any two own records that could be a transfer's legs, through the API, whether or not either is in a transfer.
2. The owner can list a record's candidates for that.
3. Undo is the existing Unpair on the resulting matched transfer.

**Non-goals**

- A web UI. The owner chose the API only (2026-11-21). A button can come later if this recurs.
- A web page that lists or enters transactions.
- Marking a record as a transfer without the other side (approach B of the transfer-evidence design, which was rejected).
- Recognising the BOC deposit account automatically (its rows print no account number, so the transfer-evidence design's evidence can't name it).

**Anti-goals**

- A second matching mechanism. This only stores M4's existing `pair` decision and runs M4's heal.
- A pair that silently overrides an owner's rejection.

## 3. Approaches

- **Chosen: enter the other side by hand, then pair the two records.** It uses the existing decision. The engine doesn't change.
- **Rejected: one action makes the other side** (an owner-supplied "this record names account X", which would post M4's placeholder on X). It is less typing, but it brings in placeholders, the §5.8 gates and `before_history`, and with them the risk of a placeholder beside a real row. It is too much for four rows.
- **Rejected for now: a button on the import page rows.** The owner chose API only.

## 4. Design

### Endpoints (owner only, in `transfers_api.py`)

- `GET /transactions/{transaction_id}/pair-candidates`. Returns the records it could be paired with: own records on another account, in the same instrument, with the opposite and equal amount, that aren't voided, aren't legs of a matched transfer, and aren't kept apart from it by a `never_pair` decision. Nearest booking date first, at most 20. No window applies, as for a decided pair.
- `POST /transactions/{transaction_id}/pair` with `{"transaction_id": <other>}`. It runs these checks, each a 422 on failure:
  - Both records are eligible own records (`contra.eligible_records`: not void, not derived, exactly two postings, the contra on Unassigned or In transit).
  - The records are on different accounts, in the same instrument, with opposite and equal amounts.
  - Neither record is a leg of a matched transfer. To re-pair one, the owner unpairs it first.
  - Neither record is rejected. Undo the rejection first, because a pair must never silently override a reject.

  If all checks pass, it does what `decisions.pair` does after its candidate check: lock, forget `never_pair` both ways, store the `pair` decision, match, heal. It returns the matched transfer (`TransferOut`), or 422 when the pair still can't take effect, as `pair` does today.

The shared part of `decisions.pair` (forget, store, match, heal, load) becomes one function that both entry points call.

### Undo

Unpair on the matched transfer, unchanged. It stores `never_pair` both ways and matches each record again. Records that name nobody then go back to Unassigned and leave the Transfers page. Pairing them again is this same endpoint, which forgets the `never_pair`.

### Docs

README.md and README.zh-CN.md (the Transfers section): how to pair two records through the API. The M4 design §5.9 gets one line on the second entry point.

## 5. Left to the builder

- The schema names and the OpenAPI regeneration (`make openapi`; the web client is generated but not used).
- Whether the candidate query reuses `eligible_records` directly or the pair relation.

## 6. Verification

Synthetic tests:

- Two Unassigned records that name nobody pair: both contras are on In transit, the transfer is `matched`, and a second heal changes nothing.
- A manual record pairs with an imported row.
- Each refusal above returns 422 and stores nothing.
- Unpair separates them. The candidates endpoint then still lists the other record, and pairing it again works.
- The candidates endpoint orders by date distance, excludes matched legs and `never_pair` partners, and caps the list at 20.

Then `make check` on docker-test.

## 7. After deploy (production data, done by hand)

1. Create a manual account for the deposit account: asset, `transactions` mode, opened on the day of its first deposit.
2. Post its records on it, each against Unassigned: one per move (each the opposite of its BOC row), plus one per interest payment (the amount a withdrawal returned beyond what was deposited). The account's balance ends at 0.
3. Pair each move with its BOC row.
4. Re-read the net worth series and the four transfers.
