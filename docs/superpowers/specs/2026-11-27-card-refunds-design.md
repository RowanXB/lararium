# Design: card refunds and reversed card authorisations

- Date: 2026-11-27
- Status: proposed (decisions taken with the owner, 2026-11-27)
- Builds on: `main` at `9c30eb0`; the eToro cancelled withdrawal design (`2026-11-27-etoro-cancelled-withdrawal-design.md`, its cancellation link and §4.4); the M4 design (`2026-11-15-transfer-matching-design.md`, §5.5)

## 1. Intent

The owner returned goods to a shop and was refunded on the card, but the refund did not leave spending. The cash-flow report decides income or spending by each flow's sign alone (`cashflow.py`): the purchase is spending, and the refund, arriving on the card, is income. The month's net is right, but spending and income are each too high by the refund, and so is the shop's line among payees.

The deployed database shows three shapes, all at one online shop (amounts here are invented):

| Source | What happened | What ties the two |
|---|---|---|
| Monzo | A card authorisation of −10.00, reversed by the merchant hours later: +10.00 with `metadata.is_reversal` `"true"` | The same `metadata.mastercard_lifecycle_id`; the authorisation never settles (`settled` stays empty) |
| Revolut PDF statement | A purchase of −100.00, and a week later a refund of +60.00 for part of the order | The same order code in the counterparty name |
| HSBC PDF statement | A subscription charge of −5.00 and a refund of +5.00 on the same day | Nothing but the amount and the day |

Every positive card transaction in the database that is not a transfer is a refund or a reversal (seven, across three sources).

The Monzo authorisation has a second fault. The normalizer posts a Monzo transaction as pending while `settled` is empty, and a reversed authorisation never settles, so it stays pending for ever. The sync fetches from the oldest pending transaction on every run (`monzo/sync.py`), so 89 days after such an authorisation every sync warns that its window was limited, and goes on warning.

## 2. Goals, non-goals, anti-goals

**Goals**

1. A refund counts as negative spending, under its own category and payee, in the period it is booked. It is not income.
2. A Monzo authorisation that its reversal cancels exactly, and that reversal, count as neither spending nor income, in whichever periods they fall.
3. A reversed authorisation stops being pending, so the sync no longer fetches it or warns about it.
4. All these transactions stay booked with their source's times, amounts and descriptions; balances are unchanged.
5. Every source whose data says a transaction was paid by card is covered, not only the owner's (owner's rule for an open-source project).
6. The ledger is the same whatever order the records arrive in, and the same when regenerated from raw records.

**Non-goals**

- Linking a refund to the purchase it refunds. Totals do not need it, and most sources give nothing to link by.
- Putting a refund back in the purchase's period (owner, 2026-11-27: the refund's own period).
- A manual override of the refund mark (owner, 2026-11-27: the rule only, for now).
- Splitting an authorisation that a reversal cancels only in part (§7).
- A transaction list page. None exists; a later one can show a cancelled authorisation as "reversed" from the link.
- Card spending that a connector does not read today (Trading 212, IBKR, Binance: §7).

**Anti-goals**

- A positive transaction that is not a card refund being counted as negative spending: a cheque paid in, interest, a bank transfer from the shop, a payment pushed to the card by someone else.
- A refund's mark changing what can pair as a transfer.
- A cancelled pair leaving a balance on In transit.
- A change to any transaction that is not a refund, an authorisation reversal or the authorisation it cancels.

## 3. Approaches

**A. Every refund on In transit, as eToro's cancelled withdrawal.** Rejected. A partial refund would leave the difference on In transit, which the integrity job requires to sum to zero; In transit means money between the owner's own accounts, and a shop is not one; and the purchase did happen.

**B. A refund linked to its purchase.** Rejected for now. It would let a refund take its purchase's category, but only Monzo's reversals and some Revolut rows give anything to link by, and the totals are right without it.

**C. A refund mark that the report reads as negative spending, and a cancelled pair for an exact Monzo reversal (chosen by the owner, 2026-11-27).** The source marks a refund when it posts it. The report counts a marked refund's flow as negative spending. A Monzo reversal that cancels its authorisation exactly reuses the cancellation link of the eToro design, so the transfer engine needs no change. Its costs: a refund in a later month than its purchase leaves that month's spending low, possibly below zero for a category; and the mark is only as good as each source's card signal (§4.2).

The owner considered treating an exact reversal as a refund too, and rejected it: an authorisation late on one month's last day and its reversal on the next month's first is ordinary, and would move spending between the two months.

## 4. Design

### 4.1 The refund mark

A new column on `finance.transactions`, `refund boolean NOT NULL DEFAULT false`, carried by `TransactionDetails` and `upsert_connector_transaction` like the other typed details, and returned on the transaction record and in the API schema.

It means: the source says this is money back from a merchant on a card, and it moved money into the owner's account. Only the sources in §4.2 set it. It changes only how the cash-flow report reads the transaction's flow (§4.5); the transfer engine does not read it.

### 4.2 Which transactions are refunds

A transaction is marked a refund when its own account's posting is positive and the source's own data says it was paid by card:

**Monzo.** `scheme` is `mastercard` (every Monzo card is a Mastercard), and it is not one of a cancelled pair (§4.3).

**eToro Money.** `transactionType` is `card` and `transactionSubtype` is `refund`, with `direction` `credit` and `status` `settled` (only settled items post today). eToro documents a refund subtype, so its rule is narrower than "a positive card payment": a card credit of another subtype (`paymentReceived`, a payment pushed to the card) is not a refund and posts as today. eToro does not document which subtypes a card item takes, so a settled card credit of any subtype but `refund` is also flagged with its subtype (`run.flag_value`), for the owner to see what eToro sends.

**File imports.** The row is a card payment, which is known from either of these:

- the row carries a card number (`card_number`, Revolut's PDF statement), as `is_bank_transfer` already reads it; or
- its type code is one of the profile's **card payment types**, a new profile setting, separate from `card_types`.

`card_types` does not mean "a card": it lists the codes that are not bank transfers, which include cash machines, cheques and interest. Its name stays as it is. The new setting, per catalog entry:

| Catalog entry | Card payment types | Source |
|---|---|---|
| HSBC UK PDF statement, midata CSV | `)))`, `VIS` | HSBC's codes: contactless, Visa |
| Lloyds CSV, PDF statement | `DEB` | Lloyds's codes: debit card (`CPT` is a cash machine) |
| Revolut Current CSV | `Card Payment`, `Card Refund` | Revolut's types |
| Revolut Current PDF statement | none (the card number marks it) | |
| BOC UK PDF statement | `Pre-Authorization Completed` | The kind the parser reads (`pdf/boc_uk.py`), taken to be a completed card pre-authorisation; the builder confirms it against the statement's printed terms before relying on it, and leaves the entry with none if they say otherwise |
| HSBC UK OFX | `POS` | The OFX specification's TRNTYPE for a point-of-sale payment; also the default for an OFX profile the owner defines |
| HSBC UK CSV (transaction list), HSBC UK QIF, Lloyds QIF | none | The format gives no type code (§7) |
| Revolut Savings, Tembo, LemFi, Trading 212 Cash ISA | none | No card |

A profile the owner defines starts with none, except an OFX one (`POS`).

### 4.3 A Monzo authorisation and its reversal

**Typed fields.** The Monzo normalizer stores two metadata fields it now leaves in the raw records: `mastercard_lifecycle_id` and `is_reversal`, as typed columns of the transaction (names left to the builder, §8), as every field a source returns is stored typed.

**The pair.** A Monzo transaction R is the reversal of an authorisation A when:

- R has `is_reversal` `"true"`, a positive amount and scheme `mastercard`;
- A has the same `mastercard_lifecycle_id`, the opposite amount in the same currency, an empty `settled`, no decline, and was created before R;
- no other live reversal cancels A.

Of several such authorisations, R cancels the latest. A shared lifecycle id is not enough on its own: Transport for London clears one authorisation as two settled payments under one lifecycle.

**Posting.** Both post their contra on In transit, chosen directly, not through `transfer_contra`. R carries `cancels_transaction_id` = A's id and is not marked a refund. A is posted **booked**, not pending, with `value_date` empty as Monzo gives it. Nothing else about either changes.

Booked, not a new status (owner, 2026-11-27): a status says whether the bank may still change the record, and a reversed authorisation is closed. That it was cancelled is the link's to say, as for eToro's cancelled withdrawal; a second record of it in the status could disagree with the link.

**Arrival order.** A run that posts R re-posts A from its stored record, so an authorisation posted pending against Unassigned by an earlier sync moves to In transit and booked. A run that posts A asks whether a live R names it. Until R arrives, A is pending and the sync keeps fetching from it (`oldest_pending`), so R normally arrives in a run that also fetches A. The match reads the latest stored copy of every Monzo transaction the connection fetched (the copies the replay reads), not only this sync's, so it holds when R comes later than that.

**Dissolving.** When R is voided (Monzo returns it declined or zero) the pair dissolves: A is posted again as its record says, pending against Unassigned. The eToro rule is the same: a pair with a void side is no pair.

**A reversal that matches nothing.** An `is_reversal` transaction with no authorisation as above (a partial reversal, or one whose authorisation Lararium never fetched) is marked a refund, so its money is counted as negative spending. The sync also adds a warning that names it, so the owner sees the first real case. Monzo's documentation does not describe these fields, and no partial reversal has been seen, so its shape is unknown.

### 4.4 The transfer engine

No change. Its cancelled-pair cases (`contra.cancelled_pairs`, `in_cancelled_pair`, `queries.stray_in_transit`, `engine._release_stray`, `contra.eligible_records`) read only `cancels_transaction_id`, so a Monzo pair is neither stray nor a pairing candidate, and the integrity job checks that its In transit postings sum to zero.

A refund mark does not touch pairing. A refund that is the leg of a transfer (Trading 212 paying a card deposit back to the Monzo card, M6 design §5.12) posts on In transit once paired, so it is no flow, and the mark does nothing. Before it pairs it is on Unassigned, where it now counts as negative spending instead of income.

### 4.5 The cash-flow report

`_FLOWS` also selects `t.refund`. A flow that is positive and whose transaction is a refund counts as spending of minus its amount: the period's spending, its categories' spending (through the category splits, as spending is shared today), and its payee's spending each go down by it. A refund the source marks `include_in_spending` false is left out, as such spending is.

The flow keeps its own booking day. A refund in a later month than its purchase lowers that month's spending, and can take a category or a payee below zero for the month. Income less spending still equals the change in net worth for each period.

Nothing else in the report changes: hidden accounts, transfers' settlements and opening balances are read as today.

### 4.6 Versions, migration and existing data

- **Migration** (number taken at merge, after `main`'s latest): adds `finance.transactions.refund` and the two Monzo typed columns. It then:
  - adds the card payment types of §4.2 to each stored profile created from a catalog entry, matching the entry as migration `0028` matches LemFi's, with a new version;
  - adds them to the profile config of staged batches that were not committed, so they commit marked;
  - marks as refunds the transactions of committed import rows that §4.2 marks: positive, not void, and paid by card by the card number or by the new types of their batch's catalog entry.
- **`NORMALIZER_VERSION`**: Monzo's and eToro's each go up by one, so the next sync replays their stored records. That marks their refunds, pairs the Monzo reversals, and books their authorisations.
- **After deployment, in production:**
  - the two Monzo reversals and their authorisations post on In transit, and each authorisation is booked;
  - In transit's balance is unchanged;
  - the Monzo sync stops fetching from the oldest of those authorisations;
  - each month with a refund shows spending and income each lower by that month's refunds;
  - the payee of the Revolut partial refund shows what was kept.

## 5. Tests

- **Cash flow:** a refund lowers the period's, the category's and the payee's spending and adds no income; a refund in a later month than its purchase; a refund marked `include_in_spending` false; a refund with category splits; a positive flow that is not a refund is still income.
- **Imports:** for each catalog entry of §4.2, a positive card row is a refund and a positive row of another type (a cheque, a cash deposit, interest, a bank credit) is not; a card number marks a row whatever its type; a negative card row is not a refund; a profile with no card payment types marks nothing.
- **Migration:** stored catalog profiles gain the types and a version; the owner's own profiles are untouched; committed rows are marked by the rule; a staged batch commits marked.
- **eToro Money:** a settled card `refund` credit is a refund; a card `paymentReceived` credit is not, and is flagged; replaying the statement gives the same ledger.
- **Monzo:**
  - an exact reversal pairs, both post on In transit, the authorisation is booked, and the reversal links to it and is not a refund;
  - a reversal in a later sync than its authorisation;
  - a reversal whose authorisation is outside the fetched window but in the ledger;
  - two authorisations of one amount on one lifecycle;
  - a lifecycle with two settled clearings and no reversal pairs nothing;
  - a partial reversal is a refund and raises the warning;
  - a voided reversal dissolves the pair;
  - `oldest_pending` no longer returns a paired authorisation;
  - a positive mastercard transaction that is not a reversal is a refund.
- **Properties:** the transfer engine's existing generated histories hold unchanged; a generated Monzo history with authorisations, reversals and refunds in any arrival order gives the same ledger, and every cancelled pair sums to zero on In transit.

## 6. Documents to amend

- The module documentation of `cashflow.py` ("positive is income, negative is spending").
- The module documentation of `monzo/normalize.py` (a reversed authorisation), `etoro/records.py` (card refunds) and `importing/profiles.py` (card payment types beside card types).
- The eToro cancelled withdrawal design §2: the cancellation link now has a second writer, Monzo's normalizer.
- The M4 design §5.5, whose invariant gained the cancelled pair, now names a Monzo reversal as one.
- The Binance design's Pay row: when Phase B builds `pay/transactions`, a `PAY_REFUND` is marked a refund.

## 7. Known limits

- **Formats with no card signal.** HSBC's transaction-list CSV and the QIF files give no type code, so a refund there is still income. HSBC's OFX is covered only if HSBC writes a refund as `POS`; it may write `CREDIT`, which would leave it as income.
- **A payment pushed to a card** (a payout to the card through the card network) is marked a refund in a source that cannot tell it apart from one (Monzo, the imports). None has been seen. eToro can tell them apart.
- **A refund paid by bank transfer** is income: nothing says it was a refund.
- **A refund in a later month** lowers that month, not the purchase's.
- **A payee whose refund carries another name** (HSBC writes the subscription and its refund differently) shows the purchase under one payee and the refund under another.
- **A partial reversal** is counted as a refund, not as a cancellation of part of the authorisation, so a pre-authorisation reduced across a month boundary moves spending between the two months. The sync warns of it. A partial cancellation can be built once a real record shows Monzo's shape for it; raising the normalizer version replays the history then.
- **An owner's void can take a reversal's match.** Pairs are found over every stored copy before the owner's voids are set aside. Suppose one lifecycle holds two unsettled authorisations of one amount, and the owner voided the later one. Then the reversal matches the voided one and pairs nothing: it counts as a refund, and the earlier authorisation stays pending. Monzo has given no lifecycle with two unsettled authorisations, and the void is the owner's own act (owner, 2026-11-27: left as a limit).
- **Trading 212:** its API returns no card spending (its transaction types are deposits, withdrawals, fees, transfers and interest). Its CSV export's card actions are not documented; an action the connector does not know is already flagged.
- **IBKR:** a Flex query's Debit Card Activity section lists card transactions (date, category, merchant, amount, pending or settled), but the connector does not read it, and IBKR documents no refund signal, no status and no id for it, nor who may hold the card. Reading it is a feature of its own.
- **Binance:** no documented endpoint returns Binance Card spending. `pay/transactions` and `fiat/payments` are not built yet (Phase B).

## 8. Left to the builder

- The names of the two Monzo typed columns, and whether they live on `finance.transactions` beside `payment_scheme` or on a Monzo table.
- The name of the card payment types setting, and how the catalog passes it to the profile.
- How the Monzo run finds a pair's other side in one sync (a pass over the run's records first, or a lookup as each is posted), so long as the result does not depend on arrival order.
- The wording of the unmatched-reversal warning.
- How the web API types are regenerated, as the repository does it.
