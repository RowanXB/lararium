# Design: a Spending page with the owner's categories

- Date: 2026-11-28
- Status: approved by the owner in chat (2026-11-28)
- Builds on: `main` at `ebe562f`; the income-categories design (`2026-11-27-income-categories-design.md`), whose store, API and effective category this reuses unchanged

## 1. Intent

The owner's category already overrides the source's on spending: `PUT /finance/transaction-categories` takes any transaction, and the effective category (`cashflow.flows`) puts the owner's first, so the Overview's "Spending by category" honours it. What is missing is a page that lists single spending entries so the owner can set one. Today only Monzo gives spending a category; spending from every other source is "Uncategorised".

## 2. Goals and non-goals

**Goals**

1. A Spending page lists every spending entry of a range of days, filtered by the account it left, with totals by category and by period.
2. The owner marks one or many entries with a category of their own wording, changes it, or removes it, exactly as on the Income page.
3. The page and the Overview agree: for the same range and no account filter, the page's total equals the Overview's spending (§3.5).
4. The source's category is shown beside the owner's and never changes.

**Non-goals**

- Rules that categorise future spending automatically.
- A new store, migration or change to the effective category.
- Honouring a transaction's category splits on this page (as on the Income page, §4.5 of the income design): an entry has one effective category. For a Monzo split payment the owner has not categorised, the page's per-category numbers can differ from the Overview's while the totals agree.

## 3. Design

### 3.1 What a spending entry is

The same flows as the cash-flow report, the ones it counts as spending: a negative flow, or a positive flow the source marks as a refund, and in both cases not one the source marks as not spending (`include_in_spending` false). An entry is those flows of one transaction and instrument added up, signed as spending: what went out is positive, a refund is negative. `quantity` is in the source's instrument with the same sign. An entry without a price or FX rate is listed with no amount and left out of every total (`missing_count`).

### 3.2 API

`GET /finance/spending?start&end&bucket&account_id&category` — same defaults and rules as `/finance/income`. Returns `start`, `end`, `bucket`, `total`, `missing_count`, `periods`, `categories` (largest first, as on the Income page; of equal ones, none last), `entries` (newest first; each with the Income entry's fields less `income_kind`, plus `refund`), and `known_categories`. `category` narrows the entries only; `uncategorised` picks those with none.

A category's total can be negative when its refunds outweigh its payments in the range.

### 3.3 Known categories per side

The picker of each page offers the owner categories in use on that side, so "salary" is not offered for a shop nor "groceries" for a salary. `known_owner_categories` takes a side: a category is in use on the spending side when a non-void transaction carrying it posts a spending flow (a posting to an income, expense or equity account with positive quantity, or any such posting of a refund), and on the income side when it posts an income flow (negative quantity, not a refund). A transaction with both is on both. The Income page switches to its side (my call, open to correction).

On the Spending page the picker also offers, after those, the source categories of the entries listed that are not already offered, so the owner can reuse the source's words (`eating_out`) for other sources' spending.

### 3.4 The page

A third tab in the Overview section, at `/spending`, after Income. The same layout as the Income page: range picker, account filter, total and uncategorised, by category (clicking one filters the list) and by period, then the entries table with checkboxes, a bulk bar and a per-row picker. Columns: Date, Account, To / description, Amount (a refund shows as a negative amount and is marked "Refund"), Source (the source's category), Category (the owner's). What the two pages share is one component, not a copy.

The Overview's "Spending by category" card links to the Spending page on the same dates, as "Where it came from" links to Income.

### 3.5 Moves to and from hidden accounts are entries too (owner, 2026-11-28)

A hidden account counts as someone else's, so money moved between it and an account still in the reports is income or spending (cash-flow report). Until now such a move was in the totals and periods only, apart from categories and payees. The owner asked for it to be an entry like any other, categorised by them, with the category kept when the account is shown again:

- There is one definition of a flow, which `cashflow.flows` returns and every report reads (`flow_postings.py`): a posting on an account outside the reports, negated, on a transaction not wholly on the hidden side. Outside the reports is an income, expense or equity account (Opening balances excepted), a hidden account, or In transit on a side (its own record, or a placeholder) of a transfer to or from a hidden account that is not rejected. A transaction is wholly on the hidden side, and no flow, when it posts to a hidden account and to no shown one (asset or liability, not hidden, not In transit): a hidden account's own payment, or its side of a transfer; so is the settlement of a transfer to or from a hidden account. A move is then the posting on the hidden account (a Monzo pot move) or on In transit (a transfer's side still shown), negated: positive when the money arrived. Its account is the first shown account its transaction posts to (a transfer's settlement posts to none, so its fee is the sending account's); it names the hidden account it crossed (`hidden_account_id`, `hidden_account_name`), for In transit the transfer's hidden end. A fee booked on the same transaction as such a side is an ordinary flow beside the move, so the shown account's whole fall is counted once.
- Limits kept (my call, open to correction), since no connector or import posts such a transaction and only one posted by hand through the API can: a move is valued from the posting outside the reports, so one transaction that converts between a shown account and a hidden one (USD out, GBP in) is valued at the hidden side, not at what the shown side lost; and an entry is one transaction and instrument, so one that pays from two shown accounts, or moves to two hidden ones, is listed under the first.
- Its category follows the same rule as any flow (the owner's, else the source's, else none), and so does its income kind (a pot move has none). It is never a refund, and the source's `include_in_spending` does not apply to it: a move to a hidden pot that Monzo leaves out of spending is still money leaving the reports, and leaving it out would break the agreement of net worth's change with income less spending.
- The cash-flow report counts it in categories and payees as well as the totals (the owner asked for "Where it went" to follow the same rule), and still totals it by hidden account for the Overview's card, now read from these flows.
- The Income and Spending pages list it as an entry, marked "From <account>" or "To <account>", so their totals equal the Overview's income and spending; `hidden_moved_in` and `hidden_moved_out` go.
- The owner's category is keyed by that transaction's source reference. When the account is shown again, the move is a transfer between own accounts, no flow, and the stored category is unused; hidden again, it applies again. A transaction the ledger made itself (a placeholder for a transfer's missing side) is keyed by its id and can lose its category when re-created, the limit the income design §4.1 accepts.
- An owner category used on such a move counts as known on its side (income when the money arrived); the known categories read the same definition of a flow's posting, so one used only wholly on the hidden side is not offered while its account is hidden.

## 4. Tests

- The categories sum to the total; the periods sum to the total.
- Goal 3, as a property test beside the Income page's.
- A refund is negative spending on the page and never income; `include_in_spending` false is not listed.
- The owner's category beats the source's, and setting then clearing it leaves the source's as it was (already pinned by the income work; one case here through the Spending report).
- Known categories per side.
- Frontend: the page lists, filters, and sets and clears a category in bulk and per row.

Amounts and names in tests are invented. Container tests run on the remote Docker host.
