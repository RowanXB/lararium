# Design: more evidence for transfer matching

- Date: 2026-11-21
- Status: proposed; the owner chose approach A (§4) on 2026-11-21
- Builds on: `main` at `149f75c`; the M4 design (`2026-11-15-transfer-matching-design.md`, §5.3 to §5.8); the M2 plan (`docs/superpowers/plans/2026-11-12-manual-import.md`, Decision 16, coverage)

## 1. Intent

The owner wants to see income and spending without the money they move between their own accounts. M4's transfer matching is what marks such a move: a record that is a leg of a transfer posts against In transit, everything else against Unassigned, so a future income and spending view only has to leave In transit out.

After the Revolut, LemFi and Tembo statements were imported into production (2026-11-21), about 100 own transfers are not paired. Both of their sides sit on Unassigned, so each would show as spending on one account and income on the other. Balances and net worth are already right; only the classification is wrong. Separately, registering Tembo's bank account double counts Monzo's transfers into Tembo. This change uses details the statements already print so these transfers pair, without guessing from names or from amounts alone.

### What the production data shows (read-only, 2026-11-21; no values recorded here)

The counts below are over committed import rows. The statements were imported before the exports, so the committed rows are the richer PDF rows.

- **No imported row prints both a sort code and an account number for the other side**, so no import ever identifies it: M4 needs both (§5.3).
  - Revolut prints only the other side's account number (42 rows). 32 of them equal the account number of a registered own account (Monzo 15, BOC 9, LemFi 5, HSBC 2, Lloyds 1); the other 10 are payments to other people and match none.
  - LemFi prints only an account number on 11 rows (Monzo's 7, Revolut's 4), and a bank name with a masked ending ("Via MONZO *1234") on 9 rows. The endings don't identify an account: 5 "via MONZO" endings equal the end of LemFi's own number, not Monzo's.
  - BOC prints only the payee's sort code (30 rows). Each equals the sort code of exactly one registered own account (Monzo 11, Revolut 9, HSBC 6) or of none (4).
  - HSBC and Lloyds rows print no account details.
- **Savings sub-accounts print no account details at all.** Revolut Savings has 59 deposit and withdrawal rows, LemFi Savings 22 funding and withdrawal rows, and each has a row of the opposite amount on its main account within a day. The owner (2026-11-21): a savings account can't pay or be paid by anyone else, only by its own main account.
- **LemFi Savings' 3 interest payouts ("Savings Interest Payout") are marked bank transfers**, because LemFi prints no type codes, so they can be taken for a transfer by amount and date. Revolut's Gross Interest and Tembo's interest are already excluded (PR #22, PR #20).
- **Tembo.** The owner (2026-11-21): every Tembo statement lists everything from the account's opening to the day it is made. Its layout treats a statement like an export instead (only the days strictly between its first and last row are listed in full), so it never lists a day in full. A one-sided pair into Tembo is therefore never taken (M4 §5.8), and once Tembo's bank account is registered, the Monzo transfer gets a placeholder beside the imported deposit, counting it twice. This happened in production on 2026-11-21 and was undone by removing the registration.
- **The £1 case.** A £1 Monzo → LemFi transfer had three candidates on LemFi: one in the same minute, one from LemFi Savings, one from the owner's Zopa account. The owner paired it by hand.
- Monzo's pots already have `parent_account_id` set to Monzo by the connector. None of their records can pair: pot moves post directly between pot and main account.

## 2. Goals, non-goals, anti-goals

### Goals

1. A row that prints only the other side's account number names the own account whose registered UK bank account has that number, when exactly one does. This is the same evidence a full sort code and account number give today.
2. The owner can make an account a sub-account of a main account. Every bank transfer on the sub-account that names no bank account then names its main account, as strong evidence.
3. A row that prints only a sort code pairs with a row on the one own account with that sort code, but only when both rows are there and each is the other's only candidate. It never opens a transfer on its own.
4. A Tembo statement lists in full every day from the account's opening to the day before it was made.
5. LemFi's interest payouts are not bank transfers.
6. All of it applies to the history already imported, on deploy, without importing anything again.

### Non-goals

- The income and spending view itself.
- Own accounts that aren't in lararium (the Zopa account, say). Transfers to and from them stay income and spending.
- Telling several same-amount candidates apart by time of day or by the bank name a row prints (the £1 case). The owner keeps choosing on the Transfers page; it happened once.
- Masked account endings (LemFi's "*1234"): they don't identify an account (§1).
- Names: their spelling varies (M4 §1, owner 2026-11-15).
- The first day of an account's imports. M4's gate (§5.8) keeps a one-sided pair whose window starts before the first day its counterpart's account lists in full. On today's data, that leaves the 4 Revolut Savings transfers of 1 August with their Revolut side on Unassigned. Accepted. (A statement that lists from the account's opening date isn't affected, §5.4.)
- Hybrid manual accounts. Every manual account the new evidence touches is `transactions`-mode in production: HSBC, Lloyds, BOC, Revolut, Revolut Savings, LemFi, LemFi Savings and Tembo. On a `hybrid` manual account with imports, the new evidence behaves as a full identifier already does there (M4 §5.2: history starts at its first snapshot), including M4's first-day behaviour. That is unchanged here.

### Anti-goals (failure even if everything else works)

- A real payment or receipt paired as a transfer, which hides it from income and spending.
- Any of the new evidence posting a placeholder beside a real imported row (a double count).
- Results that depend on the order of imports or syncs, or a transfer matched today changing partner.
- A second matching mechanism next to M4's, or a new subsystem.
- More than a handful of new review items on today's data (§6).

## 3. Constraints

- M4's rules stand:
  - the tiers, the mutual singleton rule and the twins rule;
  - the §5.5 invariant (contra In transit if and only if a leg of a transfer that isn't rejected);
  - the §5.8 gates, placeholders and `before_history`;
  - the owner's decisions, and order independence;
  - `pairs.pair_relation` stays the one definition that matching, heal, review and `preview_match` read.
- The registry (`finance.account_identifiers`) keeps full identifiers only. Partial details never create a registry row or a suggestion: a suggestion has to be something the owner can register.
- The project's standing constraints:
  - raw-SQL Alembic migrations, numbered after `0024`;
  - `Decimal` amounts;
  - synthetic test data only;
  - property tests for engine invariants;
  - owner-only API;
  - English docs, with README.md and README.zh-CN.md updated together;
  - signed commits.
- Heal performance. `contra.eligible_records` does one index lookup per record (a lateral aggregate) because a join turned quadratic. The new lookups keep that shape, and heal on 10,000 rows stays where it is (it was 0.1 s, M4 heal performance work).

## 4. Approaches

- **A, chosen: more evidence for "names the other account".** Today's three tiers stay unchanged; the matcher gets one new, weakest tier (sort code only). Everything after a link (In transit, placeholders, settlements, review, decisions) follows M4 as it is. Cost: the evidence lookups, a main-account setting, the Tembo coverage change and the LemFi interest fix.
- **B, rejected: mark rows as own transfers without pairing them.** It needs no other side, so it would also cover own accounts outside lararium. But the two sides wouldn't be linked: the Transfers page couldn't show them, and a manual account wouldn't move before its own statement arrives. It would also be a second mechanism next to M4 (an anti-goal).
- **Dropped: recognising the owner by name.** Names vary, and most of these rows print an account number anyway.

The owner chose A. The owner left the sort-code tier (goal 3) to the designer's recommendation, so it is kept, with the safeguard in §5.3.

## 5. Design

### 5.1 Evidence, in one definition

`contra.eligible_records` gives each record a `holder`: the own account its counterparty bank account is registered to. Today that is the registered account of the record's counterparty registry row. It becomes the first of these that applies:

1. **The full identifier, as now.** The registered account of the record's counterparty registry row (sort code and account number).
2. **An account number alone.** For an imported row with the other side's account number and no sort code: the own account whose registered UK bank account has that account number, when exactly one own account's does. When two do, it names nobody.
   - **Connector rows too (M6 ruling R28, 2026-11-23).** A connector whose record gives an account number without a sort code (eToro Money's bank transfers print `bankAccountNumber` alone) stores it on the transaction, `finance.transactions.counterparty_account_number` (8 digits; `CounterpartyFields.account_number`). It is the same evidence, counted with the import rows' numbers: one number names the own account registered under it, and two different ones (the transaction's and an import row's) name nobody. It makes the record `names_account`, so the weak tier never takes it. `transfer_contra` still reads only the full identifier at post time, so such a record is posted on Unassigned and matching moves it to In transit when it pairs. `preview_match` does not read it: only connectors give it, and they do not preview.
3. **A sub-account's main account.** For a bank transfer on a sub-account (§5.2) that names no bank account at all (none of items 1 and 2, and no sort code either): its main account.

M4's rule that a record naming its own account is no transfer (§5.3) holds for every item.

Separately, `weak_holder` is not part of `holder`. For an imported row with a sort code and no account number, it is the own account whose registered UK bank account has that sort code, when exactly one does. Only the weak tier (§5.3) reads it.

Both are derived when records are read, from what is already stored: the import rows' typed counterparty fields, the registry and `parent_account_id`. So registering or removing an identifier, or setting a main account, re-evaluates all history through the existing heal, and nothing has to be backfilled for these.

**Which import rows count.** Every import row that is this transaction: the committed row that posted it, and every row marked a duplicate of that row (`duplicate_of`), in any file and any format. M4's anti-goal of results that depend on arrival order requires it. If the Revolut CSV, which prints no account details, is imported before the Revolut PDF, the account number only the PDF's duplicate row prints must still count. When the rows disagree (two different account numbers, say), that item names nobody. A duplicate counts only when it is surely its original's row: matched by its key, or the one committed row of its account on that day with that instrument and amount. Otherwise M2 matched it by day and amount alone, in file order (Decision 16), and among alike rows it may be another's: a savings transfer's account number would turn a shop payment the same day into a transfer (PR #31 review). A known limit: a file whose rows have neither an id nor a running balance keys alike rows (same day, amount and description) by their order in the file (Decision 5), so if two such rows differ only in their account details, a file listing them in the other order matches them crosswise by key. None of the owner's formats has such rows: every row that prints account details also has an id or its own balance. Today `commit._counterparty` reads only the committed row; the derived lookup replaces that for these items.

Everything that asks which own account a posted record names reads it from this one definition:

- the pair relation;
- heal (`own.c.holder` in its arms);
- the matcher's lookup of an identified record's counterpart (`engine._registry_counterpart`).

Two readers keep what they read today (plan-time call, 2026-11-21):

- `transfer_contra`. Its callers are connector syncs, which none of the new evidence touches (it comes only from import rows and manual sub-accounts), and import commit. Commit runs matching right after posting, and matching re-points the contra (M4 §5.6).
- `preview_match`. Staging doesn't call it yet; it keeps reading full identifiers until the staging preview is built.

A record identified through items 2 or 3 behaves exactly like one identified today. If its counterpart is on a manual account and missing, it gets a placeholder after the account's last imported day, or `before_history` within the days the imports list in full (M4 §5.2, §5.8).

### 5.2 Main accounts

- **Where it's stored.** `finance.accounts.parent_account_id` already exists; the Monzo connector sets it on pots. The owner can now set or clear it on a manual account: "Main account" on the Accounts page, with an API to set or clear it.
- **Allowed values.** The main account must be:
  - an open own account that isn't a system account;
  - not the account itself;
  - an account without a main account of its own (one level, as pots have);
  - an account an identifier could be on: an asset or liability that isn't snapshots-only. Valuation ignores a snapshots-only account's postings, so a transfer naming it would dip net worth (PR #31 review, 2026-11-21).

  A connected account's main account stays the connector's to set.
- **Effect.** Setting or clearing it runs as a healing command, so history pairs again at once.
- **What counts.** Only bank transfers name the main account (§5.1, item 3). Interest is not a bank transfer (§5.5), so it stays income.
- **Pots.** Unaffected: none of their records can pair (§1).

### 5.3 The weak tier: a sort code alone

A new tier 4, below one-sided. Two records `r` and `c` are weak candidates when all of these hold:

- `r` has a `weak_holder` W and no `holder`;
- `c` is on W and names no bank account at all: no registry row and no partial details. This safeguard is the designer's call: a BOC payment to someone else's Monzo account can't pair with an unrelated Monzo receipt that names its sender's account;
- `c` is a bank transfer;
- the two are of opposite sign, the same instrument and amount, and `c` is booked in `r`'s window;
- no decision keeps them apart;
- M4's gate holds as for a one-sided pair: `c`'s account lists in full from the start of `r`'s window.

The existing rule then decides as it does for every tier. A record's candidates are those in its strongest tier, kept only where that tier is the candidate's strongest too. So a weak pair links only when neither record has a stronger candidate and each is the other's only one, or under the twins rule.

When the other side does name the account the sort code row is on, it isn't a weak case at all. Say a Revolut row prints the account number of the BOC account the BOC row is on: the Revolut row is identified (§5.1, item 2), and the two pair one-sided (tier 3), with the BOC row as the bank transfer that names nobody. This is how the 9 BOC → Revolut transfers pair. The weak tier is only for pairs where neither side names the other's account.

A record with only weak evidence is not identified:

- it never opens a transfer;
- it gets no placeholder and never waits as `awaiting`;
- it never gets a review reason or an alert.

A linked weak pair is a `matched` transfer like any other, with both contras on In transit. If a stronger candidate turns up later, heal dissolves the pair and matches again, as it does for any change of evidence.

On the Transfers page, a weak pair offers Reject, with Undo, and not Unpair (designer's call). Weak evidence alone never makes a transfer, so a wrong weak pair means neither record is one. Unpair keeps each record's own transfer, and here there is none to keep: after it, both records would leave the page with no way back, because `pair` and the candidate lists need an open transfer. Reject stores the decision for both records, and its rejected transfer stays under Recent with Undo.

Since PR #29 (merged into this work on 2026-11-21), `pair_records` pairs records that have no transfer, through the API. So the API's Unpair takes a weak pair too: both records leave the page, and either can be paired with its right counterpart, or the two again. Reject would mark both records as no transfer, which `pair_records` refuses, so without this a weak pair with one wrong side could never be put right (PR #31 review). The page keeps Reject only.

### 5.4 Tembo's coverage

- **What the layout reports.** The Tembo layout reports that a statement lists the account from its opening date (`account_opened_on`) to its valuation date (`document_date`). Coverage is then:
  - covered: from opening to valuation date;
  - listed in full: from opening to the day before the valuation date. The valuation day is only partly listed, like the day a Revolut statement is made.
- **The statement's period** becomes opening to valuation date (plan-time call, 2026-11-21). The printed "Transactions: first – last" period equals the first and last rows' dates, which the layout checks, so it can be derived and nothing is lost. The closing balance (the Plan value) stays undated.
- **A statement without an opening or valuation date** keeps today's rule (only the days between its first and last row in full). Both real statements print both dates.
- **Nothing exists before an account opens** (designer's call). When a committed statement lists an account in full from the opening date it prints, M4's gate (`pairs.Known.holds_from`) treats the account as known before that date too: it had no rows then. Without this, a transfer sent on the opening day would have a window starting the day before, the gate would refuse the one-sided pair, and the deposit would stay income. The real Monzo → Tembo transfer of 11 November is exactly that case. Decision 16's coverage (duplicates and covered days) still starts at the opening date.
- **Existing Tembo batches** get the new coverage on deploy, from their `import_statements` row (a migration).
- **After deploy, the owner registers Tembo's account** (the TEMBO suggestion that Monzo's records name). Monzo's transfer then pairs one-sided with the imported deposit (the gate passes), and no placeholder is posted.

### 5.5 LemFi's interest

- **Both formats.** LemFi's interest payouts ("Savings Interest Payout") are not bank transfers, in the PDF and in the CSV. The PDF layout already flags these rows for its interest total.
- **Already-imported rows.** Their transactions' `bank_transfer` is corrected on deploy (3 rows in production), and heal follows.

### 5.6 What doesn't change

- tiers 1 to 3;
- the mutual singleton rule and twins;
- the windows and the gates;
- placeholders and `before_history`;
- decisions;
- suggestions (full identifiers only);
- review reasons and notifications.

## 6. Expected effect on production (data of 2026-11-21)

- **Paired through account numbers: about 17.**
  - BOC 9, HSBC 2 and Lloyds 1, each to Revolut;
  - about 5 between LemFi and Revolut.
- **Paired through main accounts:**
  - **Exactly one candidate on the main account:** 53 of Revolut Savings' 59 rows and 19 of LemFi Savings' 22.
  - **Same-day repeats:** pair as twins.
  - **The rest:** go to review, at most 6 and 3. The 4 on 1 August keep the first-day edge (§2).
- **Paired through sort codes:** BOC → HSBC, 6.
- **Tembo:** Monzo → Tembo, once the owner registers Tembo's account.
- **New review items:** a few, at most about 9. They include LemFi Savings' £1 withdrawal, whose candidates are the £1 from LemFi Savings and the £1 from Zopa (the same-minute £1 is already the owner's pair).

After deploy, rerun the read-only counts used for this design and report them:

- unpaired own transfers, by pair of accounts;
- Needs attention;
- each statement's closing balance against the ledger.

## 7. Left to the builder

- How `holder`'s new sources and `weak_holder` are computed: in `eligible_records`, or a stored column kept up to date, as long as it is one definition, keeps the per-record lookup shape and keeps heal's timings.
- How LemFi's interest is marked in each format, without inventing a type code LemFi doesn't print (for instance a typed "pays interest" flag on the row, which the PDF layout already computes).
- The main-account API's path and payload, following the finance API's conventions, and its place on the Accounts page.
- The migrations for Tembo's coverage and LemFi's interest.
- Whether `preview_match` and the Transfers page say which evidence paired a transfer.
- Updating README.md, README.zh-CN.md and the M4 design (§5.3, §5.7) for the new evidence.

## 8. Verification

Synthetic tests:

- **Account number alone:**
  - A row naming a registered account's number pairs one-sided with that account's row.
  - Two registered accounts with that number: it names nobody.
  - Registering the identifier later pairs history; removing it reverts.
  - A row naming its own account's number is no transfer.
  - The same transaction imported as an export without account details and then as a statement with them pairs exactly as in the other order.
  - Two of its import rows naming different account numbers: it names nobody.
- **Main account:**
  - A deposit on a sub-account pairs with the main account's outgoing row.
  - With the main account's row missing, M4's rules apply: a placeholder after the last imported day, `before_history` within the days listed in full.
  - Clearing the main account reverts.
  - Interest stays income.
  - Monzo pots are unchanged.
- **Sort code alone:**
  - Pairs only when both rows are there and each is the other's only candidate.
  - No pair with a receipt that names its sender's account.
  - Two candidates: no pair, no review item.
  - Never a placeholder, and never a transfer of its own.
  - Reject on a weak pair puts both records back on Unassigned; Undo pairs them again.
- **Tembo:**
  - A statement lists in full the days from opening to the day before valuation.
  - Monzo → Tembo pairs with the imported deposit, also when sent on the account's opening day.
  - Registering Tembo's account after the import doesn't count it twice.
- **LemFi interest:** not a bank transfer, never paired.

Property tests. Extend the existing transfer property tests with sub-accounts and rows with partial account details, and check:

- the result doesn't depend on arrival order;
- a second heal changes nothing;
- net worth equals the true value;
- decisions survive;
- a record with only weak evidence never has a placeholder or a transfer of its own.

Checks:

- `make check` on docker-test;
- `make real-check` on the owner's real statements, results unchanged.

Production, after deploy: the counts in §6.
