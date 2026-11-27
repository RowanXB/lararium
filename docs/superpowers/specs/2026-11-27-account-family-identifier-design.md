# Design: an identifier held by every account of a family

Status: draft for the owner's approval, 2026-11-27. Builds on PR #48 (a payee bank account held by several own accounts, migration 0045). Replaces §9 of the account type catalog design (`2026-11-26-account-type-catalog-design.md`) and closes the last known limitation of its §8.1.

## 1. Intent

A provider may move money between two of the owner's accounts with a record that does not name the other account. Trading 212 does: its TRANSFER item has only `reference`, `type`, `amount`, `currency` and `dateTime` (its API reference, checked 2026-11-27), and "Move cash" moves money between Invest, Stocks ISA, Cash ISA, SIPP and Crypto accounts (its help centre).

Today such a record names nobody. With exactly two family accounts the connector names the other one (§9's sibling); with three (production since 2026-11-26: Invest, Stocks ISA, Cash ISA) it names nobody. A record naming nobody on an account funded by bank transfer may be taken as an external top-up (§8.2). So a TRANSFER into Invest can pair with a same-amount Monzo payment to the shared payee (§8.1), a card payment (`card_tier`), or, the other way round, a TRANSFER out with a Monzo receipt from the payee. When the real record arrives the wrong pair dissolves to Unassigned with no review item, and one booked after the window leaves it in place.

The rule, generic (owner, 2026-11-27): **when a provider's move between its own accounts doesn't name the other account, the accounts of one family jointly hold one identifier, and the move names it. It then pairs only within the family, by amount and date, uniquely, and is never an external top-up.** The family is the account type catalog's `family`. Only `trading212` needs it today; a future provider whose types share a `family` gets it from its catalog entry alone.

## 2. What exists

- `account_types.AccountType.family`; `family_keys(family)`; `finance.accounts.type_key`, set on every Trading 212 account (connector) and on a manual Cash ISA (Add account).
- The held-identifier model (#48): an identifier with no `account_id` and holder rows in `finance.account_identifier_holders`. A record naming it carries `Pairable.payee_holders`; `registry_tier` pairs it only with a record on one of the holders that identifies no account and may be a transfer (`_names_held`); the coverage gate holds every manual holder to the window (`_one_sided_known`); its contra is Unassigned until it pairs.
- §9's sibling (`trading212/accounts.sibling`): with exactly two family accounts, a TRANSFER names the other's `trading212_account` registry row. The connection stores a digest of the family (`FAMILY_KEY`) and replays when it changes; `records.unidentified_transfers` re-posts TRANSFERs once a sibling appears.
- Other connectors don't need this (checked by the owner's session, 2026-11-27): Binance posts a wallet-to-wallet move as one transaction with two postings; IBKR names the other `ibkr_account`; eToro names an `etoro_account`; Monzo registers its pots. None changes.

Production, read-only, 2026-11-27: one account of each Trading 212 type; two TRANSFERs, both on 2026-11-26, both outgoing (one on Invest, one on Stocks ISA), the same amount, unpaired, naming nobody. Both went into the Cash ISA (owner, 2026-11-27), whose export so far ends before them. So these were two moves, each written as the sending side with a negative amount: the sign Trading 212 gives a sending TRANSFER is as the connector takes it (`records.py` takes it as given). A receiving TRANSFER's sign is still unseen and stays flagged. No pair comes apart when they are re-posted; once the next Cash ISA export brings the two incoming rows, each TRANSFER has them as candidates (§5.3).

## 3. Goals, non-goals, anti-goals

Goals
1. A move naming a family pairs only with a record on another account of that family.
2. Such a move is never taken as an external top-up or withdrawal, by any tier (save one of the same amount within two minutes of it, §8).
3. With exactly one other family account, the move names that account, as §9 does today, and also when that account is manual (a Cash ISA).
4. Family membership is read from `type_key` when pairing, so adding, typing or retyping an account takes effect at the next heal without a replay.
5. Generic: nothing in pairing or the identifier code names Trading 212.

Non-goals
- Changing any connector other than Trading 212.
- Making a Cash ISA import row name the family: its export writes an internal move as an ordinary `Deposit` (§5.3, §9).
- A review item for a move naming a set that can't pair (a general limitation of held identifiers, §8.1).

Anti-goals
- A second copy of the family membership that can drift from `type_key` (the owner chose this, 2026-11-27: holders are read from the types, not registered).
- Provider data (account ids, bank details) in code.

## 4. Approaches (the owner picked B, 2026-11-27)

- **A. Registered holders.** Write each family account into `account_identifier_holders` when its type gets a family. A second copy of `type_key` that must stay in step; registration refuses closed accounts while §9 counts them; with two accounts the move names a set of one, losing today's identified behaviour (an awaiting transfer, `before_history`, a placeholder on a manual account).
- **B. Holders read from the types (chosen).** No holder rows. When pairing, a record naming a family's identifier is held by the family's other accounts, read from `type_key` as §8.2 reads funding. One account → it names that account; several → the set.
- **C. A flag on the transaction instead of an identifier** (dropped): a new concept beside identifiers that every tier would have to learn, where an identifier already says "the other side is there".

## 5. The design

### 5.1 The identifier

- New kind `account_family`. Its value is a family key from the catalog (`trading212`): `normalize_identifier` accepts only a key some type in `ACCOUNT_TYPES` has as its `family`. One row per family, created on first use (`get_or_create_identifier`).
- It never has an `account_id` and never has holder rows: assigning, unassigning, registering holders and dismissing refuse it, and it is not a suggestion (`queries` suggested kinds unchanged). Its holders are derived (§5.2).
- Migration 0046 (the next free number after #48's 0045; renumber if main has moved): add `account_family` to `account_identifiers_kind_check`; a CHECK that an `account_family` row has `account_id IS NULL`; the holders table's kind CHECK stays `('card_merchant', 'uk_bank_account')`, so it can't hold one. The value is checked in code, not SQL (the catalog is code). Downgrade: clear `counterparty_identifier_id` on transactions naming such a row, delete the rows, restore the CHECK.

### 5.2 Whom a record naming a family names

One rule, used everywhere a record's identifier is resolved to accounts (`contra.eligible_records`' `holder` and `payee_holders`, `transfer_contra`, and any other reader of "the account an identifier names"):

> A record naming a family's identifier names the family's other accounts: every account whose `type_key` is one of `family_keys(family)`, other than the record's own, whatever its status or opening date (as §9 counts them). With exactly one, it names that account: `holder` is that account, exactly as if the record named that account's own identifier. With two or more, it is identified to that set: `holder` is NULL and `payee_holders` is the set. With none, `holder` is NULL and the set is empty.

Consequences (with whom it pairs is §5.3):
- **One other account** (two family accounts, either kind, manual or connected): the record is identified. It pairs MUTUAL with a record there naming the family back, ONE_SIDED with a record there naming nobody within the near window (§5.3); its contra is In transit at posting; an unpaired one opens an awaiting transfer, settles `before_history`, or posts a placeholder on a manual account, as any identified record does. This is §9's rule, now also for a manual Cash ISA (today a Cash ISA as the second account names nothing).
- **Several:** held, as a shared payee is. Pairs only with a record on one of them; contra Unassigned until it pairs; coverage gate as §5.4.
- **None** (only one family account in the ledger): it names somebody, so it can't be an external top-up, and pairs with nothing automatically. The owner pairs it by hand.

`transfer_contra` resolves the same way at posting time (In transit only with exactly one other account). The rule lives in one Python function in `identifiers.py` and one SQL expression in `eligible_records`, each documented as the other's twin, as `identifier_account` and `holder` are today. *(Builder's call how to share the family-key list with SQL: a literal array per family, as `keys_funded_by` is passed today.)*

### 5.3 Pairing

What the sources write (owner's new Cash ISA export, 2026-11-27): Trading 212 books a received internal move as an ordinary deposit. The two moves of 2026-11-26 appear in the Cash ISA's History CSV as `Deposit` rows (with empty Notes, where a bank deposit's Notes carry `Transaction ID: …`). The API's receiving side of an Invest↔Stocks ISA move is very likely a DEPOSIT too. So a family move's other side is usually a deposit or withdrawal naming nobody, not a record naming the family.

`Pairable` gains `family: bool`: the record names a family's identifier (with one other account it is identified like any record, so pairing needs to know it is a family move). Pairing changes in three places:

1. **A family move names someone.** A record naming a family identifier counts as naming someone (`names_account` true, as for any identifier that is no card merchant; the builder may reword that column's doc to "names someone"). So `_funded_only` and `_card_receiver` exclude it: no shared-payee or card payment can take a family move, in either direction. Closes goal 2.
2. **A family move's counterpart.** On one of the accounts it names (its `holder`, or one of its set), a family move pairs with:
   - **a record naming the same family** (both sides are moves): MUTUAL with one other account (each is identified to the other's account), ONE_SIDED with a set. *(My call: with a set, an Invest→Stocks ISA pair is evidence from both sides but neither names the account; ONE_SIDED leaves a competing candidate to the owner rather than outranking it.)*
   - **a record naming nobody** (no identifier, no account number, no card merchant, identifying no account), a connector's record or an imported row, marked a bank transfer or not, **only when both records are timed** (`Pairable.timed`) **and their instants are within `near_window` (2 minutes)** of each other: ONE_SIDED. Owner's decision, 2026-11-27: Trading 212 books an internal move on both sides at about the same instant, and a real bank top-up practically never falls within two minutes of one, so a family move can't take a real top-up or withdrawal. This lets time decide whether a pair is possible, for family moves only: it narrows again the owner's rule that an imported row's time is display only (2026-11-21/24; already narrowed by the catalog design §8.3 item 4 and §5.7 here), with the owner's explicit approval.
   - nothing else. In particular `_may_be_transfer` no longer admits a counterpart of a family move: a bank transfer or a funded deposit naming nobody but further than two minutes away, or untimed, is not one. With one other account this replaces today's §9 behaviour, where a sibling TRANSFER could pair with any bank transfer or funded deposit on the sibling in the window.
   The day window still applies (the near window lies inside it), as do equal amounts, opposite signs, uniqueness and the coverage gate. The existing guard stays: a record with a set pairs only with a record on one of its accounts, on both sides.
3. **Family evidence comes first** *(my call)*. A record naming nobody that is a family move's counterpart by item 2 (within two minutes) gets no edge from a record naming a shared payee (`_names_held` through `payee_holders`) and none through its account's funding alone (`_funded_only`), as a card top-up already gets none through funding (`carded`). Otherwise a Cash ISA row of an internal move, a bank-transfer row naming nobody, would also be the candidate of any same-amount shared-payee payment in the day window, and neither would pair. By the owner's reasoning above, a row within two minutes of a same-amount family move is that move's other side.

Unchanged: card precedence (`carded`; a family move is never a card's), cross-currency (needs both identified to each other's account), the weak tier (needs both bank transfers), the owner's decisions.

Effect on the Cash ISA: its rows stay as imported, bank transfers naming nobody, timed to the second (`Time (UTC)`). An internal move's row pairs with the TRANSFER within two minutes of it; a bank deposit's row, two minutes from any family move, pairs with its shared-payee payment as #48 has it.

### 5.4 The coverage gate

No change to the rule (§8.3 of the catalog design, and #48's "every manual holder is held to the window"). With several other accounts, every manual one (the Cash ISA) must be listed in full from the start of the window, except the one the candidate is on, asked from its gate day. Effect: an Invest↔Stocks ISA move pairs by itself only once the Cash ISA's imports list in full from before it, which they do in production (its batch lists from its opening). Before that it is a candidate that can't be taken by itself, and the owner sees it on Unassigned.

### 5.5 The Trading 212 connector

- Every TRANSFER names the family identifier (`account_family:trading212`, created through `run.shared` as the `trading212_account` row is). DEPOSIT and WITHDRAW are unchanged (they name nobody: the card or bank payment pairs with them).
- Removed: `accounts.sibling`, `Run.sibling`, `records.unidentified_transfers`, the `FAMILY_KEY` digest and the replay it triggers, and the `_movements` code that adds unidentified transfers. Membership is no longer the normaliser's business.
- `NORMALIZER_VERSION = "trading212-norm-4"`: the next sync of each connection replays once, and every stored TRANSFER is re-posted naming the family. In production the two TRANSFERs were unpaired and stay so unless a candidate now fits.
- The `trading212_account` registry rows stay: each account keeps its own id.

### 5.6 When the family changes

Creating an account of a family type, setting or clearing an account's `type_key`, and a connector creating or typing its account all change whom family records name. Each already runs in a command that heals at its end (`healing_command`, or the sync's heal after normalising); the builder checks each path does, and that `records_to_heal` sees a record whose contra or pair no longer agrees with the new resolution (a TRANSFER posted In transit with one other account, when a third arrives). If a path doesn't heal, it calls `rematch_identifiers` for the family's identifier, as registering holders does.

### 5.7 Interchangeable rows on one side only (owner, 2026-11-27)

The real case: on 2026-11-26 the owner moved the same amount from Invest and from Stocks ISA into the Cash ISA, seconds apart. The Cash ISA's export will hold two identical rows (same account, instrument, amount, booking date). Each TRANSFER then has both rows as candidates and each row both TRANSFERs: a group of two by two. Today's interchangeable-records rule (`pairs._twins`) pairs such a group only when **both** sides are alike; the two senders are on different accounts, so the group would stay on Unassigned.

Owner's decision: a group pairs in the fixed order when **one** side's records are alike (as `_twins` defines alike today), even if the other side's are on different accounts. Any assignment then gives the same ledger: the same transfers between the same accounts for the same amounts, differing only in which of identical rows each links, so the balances, net worth and cash flow are the same and a fixed order can't be wrong. Everything else about the rule stays: every record of the group has exactly the other side as its candidates, both ways (n by n); a group with a pair the coverage gate refuses (`untakeable`) pairs none; the owner's decisions still win; the order is by time, then reference, on each side.

This is generic, not a family rule: it holds for any group whose one side is identical rows.

**Which links, by time (owner, 2026-11-27).** Inside a group that pairs (by today's rule or this one), when every record of the group is timed (`Pairable.timed`: a connector's record, or an imported row with `time_unit` "second", the notion #48's coverage gate uses) and every record has exactly one record of the other side within `near_window` (2 minutes) of its instant, the group pairs those by time. Otherwise it pairs in the fixed order. Time only chooses which record links to which inside a group that already pairs; it never decides whether a group pairs, so the ledger is the same either way, and each Cash ISA row then links the TRANSFER it came from. This narrows the owner's rule that an imported row's time is display only except at the coverage gate (2026-11-21/24, already narrowed by the catalog design §8.3 item 4): time now also picks the links inside such a group, and nothing else. Not yet verified: whether a Cash ISA History CSV row's `Time (UTC)` equals the API TRANSFER's `dateTime` for the same move. Check it once the owner's next Cash ISA export shows the two moves of 2026-11-26 (§10); With §5.3 item 2 that check matters more: a Cash ISA row more than two minutes from its TRANSFER is not its counterpart at all. In the real case the two TRANSFERs are 17 seconds apart, so each Cash ISA row is within two minutes of both: the group pairs (if the rows are within two minutes of them) and its links fall back to the fixed order, which orders each side by time, so the earlier TRANSFER links the earlier row.

### 5.8 A family account with a main account

A manual account may have a main account (`parent_account_id`); its bank transfers naming nobody are then with its main account (`eligible_records`' sub-account rule). A family-type account can be given one through the account create route. Then its rows are identified to the main account and never pair through the family set. *(My call: left as it is. Giving an account a main account is the owner saying its transfers are with that account; no production family account has one, 2026-11-27.)*

## 6. Tests

Hypothesis properties in the style of the existing pairs tests (`Pairable` strategies, the relation built in memory), plus a few integration tests on the remote Docker host:

- Resolution: for any family of n accounts and a record on one of them, `holder`/`payee_holders` are as §5.2 says (n = 1, 2, ≥ 3; closed accounts counted; an account outside the family never a holder; the record's own account never).
- Never a top-up: a record naming a family never pairs with a record naming a shared payee or a card merchant, whatever amounts and dates.
- Within the family: two opposite, equal, in-window records naming the family, on two family accounts, pair iff unique; MUTUAL with two accounts, ONE_SIDED with three; never with a record on a non-family account.
- Near window (§5.3 item 2): a family move and an opposite, equal record naming nobody on another family account (connector's or imported, bank transfer or not) are candidates iff both are timed and their instants are at most `near_window` apart; never when either is untimed or further apart, however close the booking dates.
- Family first (§5.3 item 3): a record naming nobody within the near window of a family move is no candidate of a shared-payee record or through its funding alone; further away, it is as #48 has it. The real shape: a Cash ISA internal-move row and a same-amount shared-payee payment in the day window, and the row pairs with its TRANSFER, the payment with its own deposit.
- Interchangeable on one side (§5.7): for an n-by-n group whose one side is alike and whose other side is not, every assignment yields the same balances per account and instrument, and the relation chooses the fixed-order one; a group with neither side alike, with a record having a candidate outside the group, or with an untakeable pair, pairs none. The real shape as an integration test: two equal TRANSFERs out of Invest and Stocks ISA, two identical Cash ISA rows, all four pair. Links by time: when every record is timed and each has exactly one counterpart within the near window, the pairs are those; when any record is untimed, or has none or two within it, the fixed order; in both cases the balances equal every other assignment's.
- Gate: with three accounts and the manual one unlisted for the window, an Invest↔Stocks ISA pair is a candidate but not takeable; listed, it is taken.
- Membership change: a TRANSFER paired MUTUAL with two accounts becomes held when a third family account is created, and the heal leaves the ledger agreeing with the rule (contra, pair) with no replay.
- Connector: `trading212-norm-4` re-posts TRANSFERs naming the family identifier; the identifier is created once per family; DEPOSIT/WITHDRAW unchanged.
- Identifier: `account_family` refuses an unknown family, an account, holders, dismissal; the migration's CHECKs.
- Catalog: every `family` value is a valid `account_family` value (an import-time check beside `check_catalog`'s).

Only these run locally (remote Docker host); CI runs the rest.

## 7. Docs to update

- Catalog design §9: replaced by this design (a pointer, and the "exactly two" rule restated as §5.2's one-other-account case); §8.1's last known limitation: closed, pointing here; §11's family test bullet.
- README's Trading 212 section: TRANSFERs name the family; the known limitation below.
- Module docs of `trading212/accounts.py`, `records.py`, `normalize.py`, `identifiers.py`, `contra.eligible_records`, `pairs`.

## 8. Known limitations

- Lararium knows only the accounts added to it. An owner with a Trading 212 account not added (a SIPP, a Crypto account, a Cash ISA not yet added) and exactly one other added gets moves to it named as that other account's, as today.
- A move naming several accounts that can't pair (several candidates, none, or refused by the gate) opens no transfer and no review item: it shows on Unassigned, as a shared-payee payment does.
- A real top-up or withdrawal of the same amount within two minutes of a family move, on another family account, would be taken as that move's other side (§5.3 item 2). The owner judges it practically impossible (2026-11-27).
- If a source's two sides of an internal move are more than two minutes apart, they never pair by themselves: the owner pairs them by hand. Whether Trading 212's are within two minutes is to be checked (§10 item 3).
- A Cash ISA internal-move row imported before its TRANSFER has synced has no family-move candidate yet, so a same-amount shared-payee payment in the window may take it; when the TRANSFER arrives the row has a family-move candidate within two minutes, item 3 of §5.3 removes the payment's edge, and the pair re-forms correctly at the next heal (the payment's pair dissolves with no review item, as #48's do). The connector syncs daily, so this lasts at most a day.
- Trading 212 doesn't document what TRANSFER covers. If an ISA transfer in from another provider were a TRANSFER, it would name the family wrongly; it then pairs with nothing (or, with one other account, awaits a record there and goes overdue), and the owner rejects it. Not seen.

## 9. Open, for the owner

- Answered (owner, 2026-11-27): the two production TRANSFERs of 2026-11-26 went into the Cash ISA. The owner's new export shows them as `Deposit` rows with empty Notes, so no Action tells an internal move apart, and no profile change is needed: the rows stay bank transfers naming nobody and pair by §5.3 item 2. (Their empty Notes, against a bank deposit's `Transaction ID: …`, are not used: the owner chose the time rule.)
- Separate, not this design: the parent session's small PR teaches the Cash ISA profile to read a Withdrawal's counterparty sort code and account number from its Notes.
- To check (§10 item 3): whether each Cash ISA row's `Time (UTC)` is within two minutes of its TRANSFER's `dateTime`.

## 10. After deploy

1. Each Trading 212 connection replays once at its next sync (`trading212-norm-4`); check both production TRANSFERs name `account_family:trading212` and how they pair (or why not).
2. Check no shared-payee or card pair includes a TRANSFER.
3. Once the owner uploads the Cash ISA export covering 2026-11-26: check each incoming row is within two minutes of its TRANSFER (§5.3 item 2) and the four records pair (§5.7). If they are further apart, the owner pairs them by hand and the near window is revisited with the real gap.
