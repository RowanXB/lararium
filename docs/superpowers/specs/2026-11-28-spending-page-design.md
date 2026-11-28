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
3. The page and the Overview agree: for the same range and no account filter, the page's total plus money moved out to hidden accounts equals the Overview's spending.
4. The source's category is shown beside the owner's and never changes.

**Non-goals**

- Rules that categorise future spending automatically.
- A new store, migration or change to the effective category.
- Honouring a transaction's category splits on this page (as on the Income page, §4.5 of the income design): an entry has one effective category. For a Monzo split payment the owner has not categorised, the page's per-category numbers can differ from the Overview's while the totals agree.

## 3. Design

### 3.1 What a spending entry is

The same flows as the cash-flow report, the ones it counts as spending: a negative flow, or a positive flow the source marks as a refund, and in both cases not one the source marks as not spending (`include_in_spending` false). An entry is those flows of one transaction and instrument added up, signed as spending: what went out is positive, a refund is negative. `quantity` is in the source's instrument with the same sign. An entry without a price or FX rate is listed with no amount and left out of every total (`missing_count`).

### 3.2 API

`GET /finance/spending?start&end&bucket&account_id&category` — same defaults and rules as `/finance/income`. Returns `start`, `end`, `bucket`, `total`, `missing_count`, `periods`, `categories` (largest first, none last), `entries` (newest first; each with the Income entry's fields less `income_kind`, plus `refund`), `known_categories`, and `hidden_moved_out` (money moved out to hidden accounts over the range, zero with an account filter). `category` narrows the entries only; `uncategorised` picks those with none.

A category's total can be negative when its refunds outweigh its payments in the range.

### 3.3 Known categories per side

The picker of each page offers the owner categories in use on that side, so "salary" is not offered for a shop nor "groceries" for a salary. `known_owner_categories` takes a side: a category is in use on the spending side when a non-void transaction carrying it posts a spending flow (a posting to an income, expense or equity account with positive quantity, or any such posting of a refund), and on the income side when it posts an income flow (negative quantity, not a refund). A transaction with both is on both. The Income page switches to its side (my call, open to correction).

On the Spending page the picker also offers, after those, the source categories of the entries listed that are not already offered, so the owner can reuse the source's words (`eating_out`) for other sources' spending.

### 3.4 The page

A third tab in the Overview section, at `/spending`, after Income. The same layout as the Income page: range picker, account filter, total and uncategorised, by category (clicking one filters the list) and by period, then the entries table with checkboxes, a bulk bar and a per-row picker. Columns: Date, Account, To / description, Amount (a refund shows as a negative amount and is marked "Refund"), Source (the source's category), Category (the owner's). What the two pages share is one component, not a copy.

The Overview's "Spending by category" card links to the Spending page on the same dates, as "Where it came from" links to Income.

## 4. Tests

- The categories sum to the total; the periods sum to the total.
- Goal 3, as a property test beside the Income page's.
- A refund is negative spending on the page and never income; `include_in_spending` false is not listed.
- The owner's category beats the source's, and setting then clearing it leaves the source's as it was (already pinned by the income work; one case here through the Spending report).
- Known categories per side.
- Frontend: the page lists, filters, and sets and clears a category in bulk and per row.

Amounts and names in tests are invented. Container tests run on the remote Docker host.
