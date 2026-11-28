# Design: income statistics with the owner's categories

- Date: 2026-11-27
- Status: proposed
- Builds on: `main` at `9c30eb0`; the cash-flow report (`backend/src/lararium/modules/finance/cashflow.py`); source references (`transfers/contra.py`, `source_columns`)

## 1. Intent

The owner wants to see where income comes from. Money that arrives from outside (a salary, money from family) carries nothing that says what it is: the owner knows from memory, looking at the day it arrived and the account it arrived in. Income an account produces itself (interest, dividends) is known to the source and should be counted alongside without the owner doing anything.

Today the cash-flow report already totals income by period and by the source's category, but:

- only Monzo supplies a category, and nothing lets the owner add one;
- no endpoint or page lists single transactions, so the owner cannot see an income entry, let alone mark it;
- what a source knows about self-produced income is not on the transaction. Trading 212 and eToro put it in the description only ("Interest on cash", "Dividend"); a statement import knows an interest row (`ImportRow.interest`) but uses it only to say the row is no bank transfer.

**The owner's rule for this work (2026-11-27):** the source's own marks are never overwritten. The owner's category is a new, separate mark stored beside them.

## 2. Goals, non-goals, anti-goals

**Goals**

1. An Income page lists every income entry of a range of days, filtered by the account it arrived in, with totals by category and by period.
2. The owner marks one or many entries with a category of their own wording (salary, family, …), changes it, or removes it.
3. Income the source states an account produced itself carries a typed kind (interest, dividend, share lending, reward), set by the source's normalizer, and is grouped under it with no action by the owner.
4. Income paid in an asset other than money (a Binance Earn reward paid in a coin) counts, valued in the reporting currency on the day it arrived. The owner asked for this on 2026-11-27, knowing the Overview's income changes with it.
5. The source's category, the source's income kind and the owner's category are three separate stored values. Setting or clearing the owner's never changes the other two.
6. The owner's category survives what rebuilds a transaction from the same source: a rollback and re-import of the same file, a connector replay under a new normalizer version. This is the guarantee transfer decisions have, with the same limits (§4.1).
7. The Income page and the Overview agree: for the same range and no account filter, the page's total plus money moved in from hidden accounts equals the Overview's income.

**Non-goals**

- Rules that categorise future income automatically. The owner categorises from memory; a rule can be added later on the same store.
- A page for categorising spending. The store and the API take any transaction, and the report honours an owner's category on spending, but no page sets one.
- Counting any other movement in an asset other than money. A fee paid in a coin (a Binance trade's BNB commission), a security arriving from a corporate action and a share transfer in kind stay out of income and spending, as today: only a transaction the source marks with an income kind is counted in a non-money asset.
- Gains from a held asset's price moving after it arrived. Those are the holding's change in net worth, not income.
- Splitting one entry between several owner categories.
- A fixed list of categories.

**Anti-goals**

- Writing an owner's category, or an income kind, into `transactions.category`.
- A second definition of what income is. The page reads the same flows as the cash-flow report, so a change to that definition (PR #53 moves card refunds out of income) reaches both.
- Keying the owner's category by transaction id, which a re-import would orphan.

## 3. Approaches

**A. A column on the transaction (`owner_category`).** One join fewer. Rejected: imported transactions are voided and re-created by rollback and re-import, and connector replays rewrite transactions from raw records, so the mark would be lost or would need every writer to carry it over.

**B. The owner's category as a decision keyed by source reference (chosen).** A table keyed by the transaction's stable source reference (`("connection", "<connection_id>:<external_id>")`, `("import", "<dedupe key>")`, else `("transaction", "<id>")`), exactly as `finance.transfer_decisions` is keyed. The mark belongs to the event, not to the row that currently represents it, and it sits apart from the source's data by construction.

**C. Reuse `transactions.labels`.** Rejected: labels are a source field (Monzo's), so the owner's mark would mix with the source's, against the owner's rule.

For the self-produced kinds, two ways were considered: writing `interest` into `category` where it is empty, or a typed column of its own. The typed column is chosen: `category` stays what the source calls its category and nothing else, and the kind is a different fact (what sort of income the source says it is).

## 4. Design

### 4.1 Stored values

| Value | Where | Who sets it |
|---|---|---|
| Source category | `finance.transactions.category` (exists) | the source; unchanged by this work |
| Income kind | `finance.transactions.income_kind`, new, nullable text, CHECK in (`interest`, `dividend`, `share_lending`, `reward`) | the source's normalizer or the import commit |
| Owner category | `finance.transaction_owner_categories`, new | the owner |

`finance.transaction_owner_categories`: `source_kind` text, `source_ref` text (primary key together), `category` text not null, `decided_at` timestamptz, `decided_by` text. One row per event; clearing deletes the row.

An owner category is free text, stored normalised so the same word typed twice is one category: trimmed, inner whitespace collapsed to `_`, lower-cased, 1 to 64 characters, and not `uncategorised`, which the Income report's filter uses for no category (§4.5). This matches the source's style (`eating_out`), which `web/src/lib/category.ts` already turns into words.

Limits of the source reference, accepted as they are for transfer decisions (my call): an import's reference is its dedupe key, stable for the same file but not for the same event imported from another format after a rollback; and a transaction the ledger derives itself (a transfer's settlement) is referenced by its id and is re-created, so a mark on one can be orphaned. Such entries can be marked and are not protected.

### 4.2 The effective category

One SQL expression, used by every report, decides the category a flow is grouped under:

1. the owner's category, if the event has one. It is the owner's word for the whole transaction, so it applies to every flow of it, income or spending;
2. else, for a positive flow only, the income kind, if the source states one. A negative flow of the same transaction (the tax withheld from an eToro dividend, posted beside the gross amount) is not income of that kind and falls through;
3. else the source's category;
4. else none (shown as "Uncategorised").

`cashflow.py` already says `category` is "the one place a flow's category is decided"; this is that place. Where an owner's category applies, the transaction's category splits are not used: the owner's word covers the whole amount.

### 4.3 Which sources state an income kind

Only what each source's data states; nothing is guessed from free text.

| Source | Marks |
|---|---|
| Statement imports | an event with `interest` true on its committed row or on any committed duplicate of it → `interest` |
| Trading 212 | `INTEREST_ON_FREE_CASH` → `interest`; `LENDING_INTEREST` → `share_lending`; a dividend record → `dividend` |
| eToro | Account Activity types `interest`, `interest payment` → `interest`; `dividend` → `dividend`; `stocks lending` → `share_lending` |
| IBKR | cash transactions by their `type`. The builder reads the Flex Query reference for the full list of types and maps the dividend and interest-received types; a type whose meaning the reference leaves unclear is left unmarked |
| Binance | a Simple Earn reward posted on Earn (`earn-flexible-rewards`) → `reward`; an asset dividend (`assetDividend`) → `reward`. In the owner's data every asset dividend is the payout of a flexible Earn reward (`enInfo` "flexible", every one); Binance uses the same endpoint for airdrops and other distributions, which are free coins too, so they get the same kind and the source's `enInfo` stays on its typed row. The Binance products of the draft PR #49 that post rewards (locked Earn, staking, on-chain yields) set `reward` when that work lands |
| Monzo | none (Monzo states no such type) |

The kind is set whatever the amount's sign (a dividend's reversal is still a dividend record); whether a flow is income stays the report's rule (a positive flow). Taxes withheld and fees are not income kinds.

A statement event's kind is read from every committed row of it, not only the row that made the transaction: most CSV profiles cannot state interest while the PDF layouts do, so with the CSV committed first the PDF's rows land as duplicates and would otherwise say nothing. `transfers/contra.py` reads duplicates as counterparty evidence in the same way. The kind is therefore re-derived for the events a batch touches when it commits and when it rolls back (my call).

Entries are listed as the source posts them: an eToro dividend gross, with its tax as spending; a Trading 212 dividend as the single amount its API gives.

**Existing data.** Imports: the migration sets `income_kind = 'interest'` on the transaction of every event with a committed row, itself or a duplicate (`duplicate_of`), whose `interest` is true. Connectors: bump `NORMALIZER_VERSION` for Trading 212, eToro, IBKR and Binance so the next sync replays. The builder confirms the ledger's update path writes `income_kind` on replay, and how eToro statement rows are replayed.

### 4.4 Income in an asset other than money

Today the flows query keeps fiat postings only (`i.kind = 'fiat'`). It also keeps a non-fiat posting when its transaction has an income kind, whatever the posting's sign, so a reward and its reversal (a Binance asset dividend with `direction` −1) both count, as a fiat flow's sign does. A non-fiat posting of a transaction without an income kind stays out, as listed under non-goals.

Such a flow is valued the way net worth values a holding on a day (`valuation.py`): the asset's latest price on or before the booking day (manual first among equals, then the latest recorded), converted into the reporting currency with `finance.v1_fx_rate` on the booking day. For a Binance coin that is the EUR daily close of the booking day (UTC), as `binance/prices.py` records it. My call, the common practice for income received in kind: its value when it arrived. A reward with no price on or before its day is left out and counted in `missing_count`, as a fiat flow without an FX rate is today.

The entry shows the source's quantity and asset (for example `0.00012 BTC`) beside the reporting-currency amount. One helper values both a money flow and an asset flow, so the cash-flow report and the Income page use one rule.

Rewards are many and small: in the owner's data about one Earn reward a day per asset, two records a day where Binance both credits and pays out. The page groups them by the category like any other entry; the list shows them one by one, so the builder adds a filter on effective category, which the summary already drives (§4.6).

### 4.5 API

`GET /finance/income?start&end&bucket&account_id` — same defaults and range rules as `/finance/cash-flow`. Returns:

- `start`, `end`, `bucket`, `total`, `missing_count` (entries left out for lack of an FX rate);
- `periods`: every period of the range, each with its total and its amounts by category;
- `categories`: effective category and amount, largest first;
- the request takes an optional `category` too, filtering `entries` (not the totals) to one effective category, `uncategorised` for none;
- `entries`: newest first, one per transaction and instrument: `transaction_id`, `booking_date`, `account_id`, `account_name`, `description`, `counterparty_name`, `quantity` and `instrument` (its code: a currency or an asset's symbol) as the source gave them, `amount` in the reporting currency (null without a rate), `source_category`, `income_kind`, `owner_category`, `category` (the effective one);
- `known_categories`: every owner category in use, for the picker;
- `hidden_moved_in`: money moved in from hidden accounts over the range, which the Overview counts as income and this page does not list (goal 7). Zero with an account filter.

An entry's account is the asset or liability account the transaction posts to, other than In transit; with several, the first by posting position. `account_id` filters on it. Entries come from the same flows query as the cash-flow report (positive flows only), restructured so both share it. Unlike the cash-flow report, it does not share an entry between category splits: an entry has one effective category and the category totals add up the entries, so for a split income the per-category numbers can differ from the Overview's while the totals agree.

`PUT /finance/transaction-categories` — body `{transaction_ids: [1..500 ids], category: string | null}`. Sets the owner's category on each transaction's source reference, or clears it with null. All or nothing: an unknown or void transaction fails the whole request. Runs through `CommandContext`, so it is audited. It accepts any transaction, not only income.

`GET /finance/cash-flow` keeps its shape; its categories become the effective ones.

### 4.6 The Income page

A second tab in the Overview section, at `/income`.

```
Overview | Income

[30d] [90d] [1y] [Custom]      Account: [All accounts v]

Total 4,200.00     Uncategorised 300.00

By category                    By month (stacked by category)
 Salary        3,000.00  71%   ▇▇▇ ▇▇▇ ▇▇▇
 Family          800.00  19%
 Interest         80.00   2%
 Reward           20.00   0%
 Uncategorised   300.00   7%

[ ] Date        Account     From / description      Amount    Source      Category
[x] 2026-01-28  Current     ACME LTD                1,500.00  —           [Salary v]
[x] 2026-01-15  Current     A RELATIVE                400.00  —           [ choose v]
[ ] 2026-01-02  Savings     Gross Interest             12.00  Interest    [ choose v]
[ ] 2026-01-02  Earn        Earn reward BTC             0.85  Reward      [ choose v]
                            0.00001 BTC

2 selected:  Category [ Salary v ]  [Apply]  [Clear]
```

(Amounts invented.) The Source column shows what the source says (its income kind, else its category) and never changes. The Category column is the owner's, empty until set. The picker offers the known categories and takes a new one typed in; with none yet, it suggests `salary` and `family`. Clicking a category in the summary filters the list to it. The account filter lists the owner's accounts still in reports (not system accounts, not those left out of reports, which have no entries here). The page reuses `RangePicker`, `ShareChart`, the existing table and checkbox components, and `categoryLabel`.

### 4.7 The Overview

The owner asked (2026-11-27) for the Overview's "Where your money is" card, beside "Where it went" (spending by payee), to become an income card. It becomes **Where it came from**: income by effective category over the Overview's range, largest first, as bars, read from the cash-flow report's categories (their `income`), with a link to the Income page that opens it on the same dates (the range rides in the link's search params). Money moved in from hidden accounts stays in its own block, as today. Each account's value is still in the Holdings table below. Income by category rather than by payer is my call: the category is what the owner sets.

## 5. Constraints

- Migration number: `0049`, after #53's `0047` and #52's `0048`.
- PR #53 changes the flows query in `cashflow.py`; expect a merge conflict there and keep one shared definition.
- Goal 7 holds with asset income too: both views read the same flows.
- Decimal strings, never floats. Repo documents and tests use invented amounts and names.
- Tests: the invariants are pinned with property tests (the categories sum to the total; the periods sum to the total; goal 7; a reward counts as its quantity times its price and FX rate on its booking day, and a non-fiat posting without an income kind never counts; setting then clearing an owner's category leaves `category` and `income_kind` as they were; an owner's category survives rollback and re-import of the same file). Container tests run on the remote Docker host.

## 6. Left to the builder

- Names of Python functions, schema classes and components.
- The exact shape of the shared flows query.
- Whether the income list needs paging (a year of income is expected to be a few hundred rows; return all unless that proves wrong).
- Calls made here without the owner's word, open to correction: the three income kinds; free-text normalised categories; the page as a tab of Overview; the owner's category beating category splits and applying to every flow of its transaction; the income kind applying to positive flows only; interest read from duplicate rows; the limits of the source reference (§4.1); a reward valued at the booking day's price; every Binance asset dividend given the kind `reward`; non-fiat postings of an income-kind transaction counted whatever their sign; the API accepting any transaction.
