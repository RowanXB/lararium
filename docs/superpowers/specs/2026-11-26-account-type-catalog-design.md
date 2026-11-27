# Design: an account type catalog, with the Trading 212 Cash ISA as its first new type

- Date: 2026-11-26
- Status: approved 2026-11-26; implemented
- Builds on: `main` at `75c78ff`; the M2 import design (the format catalog); the M6 design (`2026-11-22-etoro-trading212-design.md`, §5.12); the transfer evidence design (`2026-11-21-transfer-evidence-design.md`)
- Replaces: the uncommitted draft `2026-11-26-trading212-cash-isa-design.md` of another session, whose findings and owner decisions are folded in here (§7 to §9)

## 1. Intent

The owner opened a Trading 212 Cash ISA. Its balance must count in net worth, its cash movements must be in the ledger, and a Monzo top-up into it must pair with the Monzo payment as a transfer. Lararium is open source, so what is built must work for any Trading 212 customer.

Adding it showed a gap in how Lararium is organised. Today the owner chooses a *mechanism* first: "Connect" (Monzo, IBKR, Trading 212, eToro) or "Upload statements" (HSBC, Lloyds, Revolut and the rest). Behind those sit two unrelated code paths, each with its own upload routes, and an account doesn't remember which files it takes. The Cash ISA belongs to a connector's institution but has no API, so it fits neither group.

The owner wants the choice to be the *account type* ("Trading 212 Cash ISA"), with everything else following from it: whether it syncs, which files it takes, which parser reads each, and where the upload goes. The owner chose to unify the catalog and the entry points now and to keep the two engines underneath (2026-11-26). Merging the engines is a later, separate project that this design must not block.

## 2. What the sources allow (checked 2026-11-26)

- **No API.** Trading 212's API docs and its API-key help article say the API serves Invest and Stocks ISA accounts only. The owner confirmed the Cash ISA shows no API setting in the app.
- **No card top-ups.** Trading 212's payment methods article says a Cash ISA takes bank transfers and instant bank transfers only. Every Cash ISA deposit is a bank transfer.
- **CSV export.** The Cash ISA's History page exports a CSV with the header `Action,Time (UTC),Notes,ID,Total,Currency (Total)`, the same columns as the Invest and Stocks ISA export. The owner's first export (`private/t212-cash-isa/`, not committed) has one `Deposit` row: `Time (UTC)` to the second (`YYYY-MM-DD HH:MM:SS+00:00`), `Notes` = `Transaction ID: <uuid>`, `Total` signed (the connector's tests show a withdrawal as negative), `Currency (Total)` quoted.
- **The Monzo side.** In production, Monzo's record of that deposit is a Faster Payment naming a `uk_bank_account` (Trading 212's payee account) with a payment reference, seconds before the export's time. The owner confirmed Invest, Stocks ISA and Cash ISA show the same sort code and account number for bank transfers and differ only by reference. Trading 212's help says to use "your account number" as the reference; it does not say whether every customer pays the same payee account.
- **Not yet seen:** an interest row, a withdrawal, a transfer between Trading 212 accounts, a statement PDF.

## 3. Goals, non-goals, anti-goals

**Goals**

1. One catalog in code lists the account types the owner can add and what is true of each: whether it has an API, which files it exports, how money gets into it, which accounts it moves money with.
2. The Add account page is one list of account types. The owner never chooses between "connect" and "upload".
3. An account whose type a rule or a page reads remembers that type. Uploading a file to such an account needs no profile choice.
4. "Trading 212 Cash ISA" is an account type. Picking it creates the account; its History CSV imports with every column stored.
5. A Monzo bank transfer to a payee account shared by several own accounts pairs with the deposit on the account its payment reference names, for the Cash ISA and equally for Invest and Stocks ISA.
6. The Invest and Stocks ISA inference about moves between them stops only when the owner has a Cash ISA.
7. Existing accounts keep working with no change to their data.

**Non-goals**

- Merging the two engines, or changing what either stores (§5).
- A new backend upload route. The existing ones stay; the web sends the owner to the right one (§6.4).
- A generic "interface" abstraction over APIs and files, or a handler layer over the existing connectors. Considered and dropped (§5).
- Types for the accounts Monzo, eToro and IBKR create. No rule reads one yet.
- Changing how any existing connector syncs, normalises or reconciles, apart from the family rule (§9).
- Changing the card pairing rule (`card_tier`).
- Interest rules beyond the profile's default (§7.2), until a real interest row shows its `Action`.
- Statement PDFs for the Cash ISA, until a real one is seen.
- Reconciling the Cash ISA against a provider balance (there is none).

**Anti-goals**

- A catalog that is only a second list beside the existing ones. The import format catalog and the web's provider list must be *derived from* it.
- A catalog that speaks of "connectors" and "imports". A type is configured by what is true of the account, and rules read that configuration, never "which code path made this record". Nothing in a type's configuration would change if the engines were merged later.
- Configuration nothing reads. A field or an entry is added when a rule or a page needs it.
- Changing the rule that an imported row's `time_unit` never takes part in matching.
- A transfer identified as going to an account by a guess.
- Owner data (account numbers, references, amounts, dates) in the repository.
- Any change to what existing accounts post.

## 4. Constraints

- Docs and code in English. Every source field is stored typed. Messages name a column or a rule, never a value.
- Tests run on CI and on the remote `docker-test` host; engine invariants get Hypothesis property tests. Test files are synthetic, never the owner's.
- Production has real data for Monzo, IBKR, eToro, Trading 212 Invest and Stocks ISA, and imported bank statements. Nothing may need re-importing.

## 5. Approaches

**How deep to unify.**

1. *Catalog only, holding just the Cash ISA.* Smallest, but two ways to add an account would coexist. Rejected: the "second list" anti-goal.
2. *Catalog of account-type facts, one Add account page, engines kept.* **Chosen by the owner (2026-11-26).**
3. *One engine.* Rejected for now: it rewrites four connectors and the importer (about 24,000 lines, 48,000 of tests) and re-verifies real ledgers, with one new requirement to shape it. The engines also differ for real reasons (a parsed PDF needs the owner's review; a daily API sync can't have one).

**How much to put in the catalog.** A wider version was drafted and dropped by the owner on my recommendation: each type listing uniform "interfaces" (an API and a file as the same kind of entry) with a handler registry, a unified backend upload route, and a type for every account a provider creates. An API and a file differ in what they need (credentials, scheduling and health against upload, format detection and review), so one shape for both leaves fields that apply to only one; the connectors already have their own interface and registry; the unified route would still answer in two shapes; and nothing reads a Monzo pot's type. What the owner sees is the same either way.

**What reads the Cash ISA's CSV.**

- *The statement parser, with a CSV profile.* **Chosen.** A Cash ISA takes no card payments, so every row is a bank transfer, and bank transfers pair through the existing registry tiers on imported rows. No connector change.
- *A keyless Trading 212 connection.* Its one advantage was the card pairing rule, which only connector records join. With no card top-ups it buys nothing, and it needs `export.py` and `accounts.py` to learn a connection with no API. Dropped.
- *Letting imported rows with a time to the second into the card rule.* Not needed, and it reverses an owner rule. Rejected by the owner.

## 6. The catalog and the Add account page

### 6.1 The catalog

A new module, `lararium/modules/finance/account_types.py`, holds a tuple of entries. Each is a tile on the Add account page.

```python
@dataclass(frozen=True)
class AccountShape:
    ledger_type: LedgerType | None   # None: the owner says "money I have" or "money I owe"
    subtype: AccountSubtype
    tax_wrapper: TaxWrapper
    balance_mode: BalanceMode

@dataclass(frozen=True)
class Api:
    connector: str                   # "monzo", "ibkr", "trading212", "etoro"
    config: Mapping[str, JSONValue]  # fixed connection config, e.g. {"account_kind": "isa"}

@dataclass(frozen=True)
class FileFormat:
    label: str                       # as the institution names the file
    format: str                      # "csv", "pdf", "ofx", "qif", "xlsx", "xml"
    # The statement profile that reads it. None: the type's API code reads it, through the
    # upload its connection already has.
    profile: ProfileConfigModel | None
    recommended: bool = False

Method = Literal["card", "bank_transfer"]

@dataclass(frozen=True)
class AccountType:
    key: str                         # stable, stored on accounts: "trading212.cash_isa"
    institution: str                 # "Trading 212"
    label: str                       # "Cash ISA"
    shape: AccountShape | None       # None: the API creates its accounts itself
    api: Api | None
    files: tuple[FileFormat, ...]
    # How money can be paid into the account, as the institution documents it. Empty: not
    # stated, and nothing is inferred (§8.2).
    funding: frozenset[Method] = frozenset()
    # Accounts of one family move money between themselves without the record naming the
    # other account (§9). None: no such moves.
    family: str | None = None
```

Rules the module checks at import time (a test asserts them too): keys are unique; a type has an API or at least one file; a file with no profile is on a type with an API; a `family` has at least two types.

**Entries**

| Key | API | Files | Funding | Family |
|---|---|---|---|---|
| `monzo` | monzo | none | | |
| `ibkr` | ibkr | Flex statement XML | | |
| `etoro` | etoro | Account statement xlsx | | |
| `trading212.invest` | trading212, `account_kind: invest` | History CSV | card, bank transfer | `trading212` |
| `trading212.stocks_isa` | trading212, `account_kind: isa` | History CSV | card, bank transfer | `trading212` |
| `trading212.cash_isa` | none | History CSV, with a profile (§7) | bank transfer | `trading212` |
| one per institution in today's `importing/catalog.py` (`hsbc_uk`, `lloyds`, `boc_uk`, `revolut.current`, `revolut.savings`, `tembo`, `lemfi`) | none | today's catalog entries, each with its profile | | |

`funding` and `family` are filled in only where a rule reads them (§8, §9) and the institution's own documentation states them. The Trading 212 values are from its payment methods article (§2). *(My call: the other providers' are left empty, not guessed.)*

Monzo, IBKR and eToro create several accounts from one API connection, so their entry is the provider. That is how they work today and it does not change.

The statement types that exist today keep the shape the upload form creates today: subtype `bank`, tax wrapper `none`, and the side (asset or liability) asked of the owner, since one bank's statement format serves both a current account and a card.

The statement profiles stay where they are defined today, in `importing/catalog.CATALOG`, which gains the Cash ISA's entry; a statement type's `files` are read from it by the type's name, so each profile is written once and `GET /finance/import-catalog` answers as today plus that entry. *(Planning call: the other direction, deriving `CATALOG` from the types, moves sixteen profile definitions for no gain and collides with open PR #30.)* The web's `PROVIDERS` list is removed; the page reads the catalog.

A type's display name is its institution and label together (`Trading 212 Cash ISA`); a type that is its institution's only one has an empty label (`HSBC UK`). The two Revolut types keep the names the import catalog gives them today (`Revolut Current`, `Revolut Savings`).

### 6.2 Accounts remember their type

Migration: `finance.accounts.type_key text NULL`. (The existing `product_type` column is the source's own product label, such as Monzo's, and stays as it is.)

- Set when the owner creates an account from a type with no API (the Cash ISA, a bank's statement type).
- Set by the Trading 212 connector on the account it creates (`trading212.invest` or `trading212.stocks_isa`, from the connection's `account_kind`), and backfilled for the existing ones. §8.2 and §9 read it.
- Not set on accounts Monzo, eToro and IBKR create: nothing reads it.
- Left NULL for existing manual accounts, which behave exactly as today (the owner picks a profile on the import page). The existing `PATCH` on an account (today it renames) also accepts `type_key`, limited to types with no API and to accounts that can take imports (`require_importable`), so the owner can set it once on an old statement account. *(My call: guessing a type from past imports could be wrong, and there are few such accounts.)*
- Unknown keys are refused when written. A key no longer in the catalog reads as NULL.

### 6.3 API

- `GET /finance/account-types`: the catalog, as the page needs it (key, institution, label, whether it has an API and which credentials form that needs, its files with label, format and `recommended`).
- The existing account create route accepts `type_key` for a type with no API. The institution is the type's; the body's shape must agree with the type's wherever the type states one (422 otherwise), and the side is the body's when the type leaves it open. `AccountOut` gains `type_key`.
- No other route changes. `POST /finance/imports` still takes a profile: the web finds or creates it from the type's format, as it does today from a catalog entry. The connection create and upload routes stay as they are.

### 6.4 Web

The Add account page becomes one list grouped by institution:

```
Add an account

Trading 212
  [ Invest        Syncs automatically · CSV export optional ]
  [ Stocks ISA    Syncs automatically · CSV export optional ]
  [ Cash ISA      Upload its History CSV                    ]
Monzo
  [ Monzo         Syncs automatically                       ]
HSBC UK
  [ HSBC UK       Upload statements · PDF recommended       ]
…
Something else
  [ Another bank's file ]  [ Type it in yourself ]
```

Picking a type shows what that type needs: the existing credentials form when it has an API; otherwise a name (prefilled) and the existing statement upload form, given that type's formats. "Something else" is unchanged.

On the Accounts page, an account whose type has files gets an "Upload file" action. For a statement type it opens the statement upload form in place, with the account and its formats already chosen (format detection as `lib/detectFormat` does today, asked only when the file can't tell). For a Trading 212 API account it links to the Connections page, where that connection's upload already is. The Connections page is unchanged, apart from its Trading 212 form taking the kind from the picked type.

## 7. The Trading 212 Cash ISA

### 7.1 The type

`trading212.cash_isa`: asset, subtype `savings_pot`, tax wrapper `isa`, balance mode `transactions`, institution Trading 212, default name `Trading 212 Cash ISA`, no API, funding by bank transfer only, family `trading212`. A manual account: nothing syncs it. (The subtype only sets the icon and label on the Accounts page; no calculation reads it.)

### 7.2 Its file: "History CSV"

Format `csv`, `recommended`, with this profile (staged for the owner's review, as every statement is):

| CSV column | Profile field |
|---|---|
| `Action` | `type`, and `description` |
| `Time (UTC)` | `date`, a `date_format` with seconds and offset, timezone UTC |
| `Notes` | `memo` |
| `ID` | `external_id` |
| `Total` | `amount` (signed, inflow positive) |
| `Currency (Total)` | `currency` |

- `card_types` is empty: every row is a bank transfer, as Trading 212 states for the Cash ISA. When a real interest row is seen, its `Action` goes in `card_types`, as Revolut's `Gross Interest` does, so it is never taken for a bank transfer. Until then an interest row imports as an ordinary row.
- `Notes` is stored whole in `memo`. Its `Transaction ID` is not split out: the import has no field for it and nothing matches on it. *(Carried over from the other session as its call. If the owner wants it as its own field, that is an `extract` pattern plus a new extract field.)*
- This profile is on the Cash ISA type only. An Invest or Stocks ISA export has the same header and is read by the Trading 212 connector's upload, as today; each goes to the right place because the upload starts from the account.
- The time is to the second, but per the owner's rule it is display only, except at the coverage gate (§8.3 item 4). Matching is by booking date, as the registry tiers do.
- Each export must start on the account's first day or on a day an earlier upload covers (imports already gate matching on the days they list in full). The README's Trading 212 section says so. The days an export lists in full are read from its file name (§8.3).

## 8. Bank top-ups to a payee account shared by several own accounts

Needed for goal 6. Two pieces of transfer evidence, both generic.

### 8.1 References on a shared payee account

- A `uk_bank_account` identifier registered to no account may carry **references**: pairs of (own account, reference), a reference unique within the identifier. An identifier carries either an account (as today) or references, never both.
- A record naming that identifier whose payment reference matches a registered reference names that reference's account, as if the identifier were registered to it. The existing tiers then apply. "The account a record's identifier names" is read in three places today (`contra.eligible_records` for pairing, `contra.transfer_contra` for which contra a new record posts to, `engine.preview_match` for import previews); all three resolve it the same way, through one definition.
- An identifier carrying references is not offered as a suggestion to register (`queries.suggestions`), and can't be dismissed while it carries any.
- Registering an account on an identifier that carries references is refused, and so is adding references to one that has an account.
- Matching is exact after trimming surrounding spaces and upper-casing both sides. No reference, or one matching none, leaves the record unidentified; it still names a bank account (`names_account`), so the weak sort-code tier never pairs it.
- Registering, changing or removing references re-runs matching, as registering a card merchant does.
- No provider's account number or reference is in code. The owner registers what their own app shows.

### 8.2 A deposit whose method the source doesn't state

Today the one-sided registry tier (`pairs.registry_tier`) pairs an identified record with an unidentified one only if the unidentified one is marked a bank transfer. A Cash ISA row is (every one is, §7.2). A Trading 212 Invest or Stocks ISA DEPOSIT is not: its API doesn't say how the money arrived, so those top-ups pair by hand even once §8.1 identifies the Monzo side.

The account type now says what the API doesn't. In `registry_tier`, the unidentified side passes if it is marked a bank transfer **or** all of these hold:

- its account's type lists `bank_transfer` in `funding`;
- it names nobody: no own account, no card merchant, no bank account (`_card_receiver`'s test without its "from a connection" clause);
- the other side is identified and names its account.

Such a deposit also has to be loaded for pairing at all: today `pairs._load_records` drops a record that is no bank transfer and names nothing unless its account is a card merchant's holder, so it gains the same condition.

Nothing else about the tier changes: equal amounts, opposite signs, the date window and uniqueness still decide, and the tier stays `ONE_SIDED`. The weak sort-code tier is not widened: it still needs both sides marked bank transfers.

A deposit that was a card payment is not caught by this. Its Monzo side names a card merchant, not a bank account, so it is identified by no reference and only `card_tier` can pair it, as today.

Effect on today's data: an identified bank transfer to a Trading 212 Invest or Stocks ISA account now pairs with that account's same-amount deposit in the window. No other account type lists a funding method, so no other pairing changes. *(My call: the transaction's stored `bank_transfer` flag is left as the source gave it; the type is read at pairing time, so a type's configuration can be corrected without rewriting records.)*

### 8.3 Which days a Cash ISA upload covers (added 2026-11-27, after review)

A review of PR #46 found the Cash ISA's first deposit counted twice. A History CSV states no period, so an export lists in full only the days strictly between its first and last rows (M2 Decision 16). A one-row file lists none. The Monzo transfer that §8.1 identifies is then refused its one-sided pair by the import-coverage gate (M4 §5.8): its window starts the day before the deposit, which no upload lists. So the transfer opens `inferred`, with a placeholder on the Cash ISA beside the imported row.

Three changes, decided by the owner 2026-11-27:

1. **The period from the file name.** Trading 212 names an export `from_<YYYY-MM-DD>_to_<YYYY-MM-DD>_…csv`, with the range the owner chose. A CSV profile may state a pattern for its file name (named groups `start` and `end`, ISO dates); the Cash ISA profile does. When the name matches, the upload's period is that range. It lists every day in full except its last: a file never starts partway through a day (owner, 2026-11-21), and an export may be taken partway through its last day (owner, 2026-11-27). An export's first day, with or without a stated period, is listed in full too (owner, 2026-11-27), unless its rows carry a second date: Revolut's CSV books a row on its completed date but may select rows by their started date, so a payment started the day before its range and completed on its first day may be missing (unproven either way). A profile that reads a started date (`started_at`) keeps its first day listed in part. So does an OFX file whose `DTSTART` gives a time of day other than midnight in its own offset (an incremental download starting at noon, say): the period is read by its dates, so the day it starts on is listed only from that time, and the listing starts the day after it. A `DTSTART` that is date-only or exactly 00:00:00 starts at the day's start (as the owner's HSBC export writes it), so its first day is listed in full. Closed PDF statements already list their whole period. Batches already committed keep the coverage stored when they were staged. A name that does not match, a range whose start is after its end, or rows outside the range: no period, and a warning on the batch saying the file name gives none. The upload is read as today.
2. **An account's own opening date.** A batch that lists in full from its account's opening already speaks for all time before it, but only a statement's `account_opened_on` says when that is. The account's `opened_on` now says so too: a batch whose first full day is on or before its account's `opened_on` speaks for all time before. The Add account upload form asks for an optional opening date when it creates an account. With it, a deposit on the day the account opened pairs.
4. **A receipt does not come before its payment** (owner, 2026-11-27, narrowing the 2026-11-21/24 rule that an imported row's time is display only). The coverage gate's window starts a day before the sending side's booking date, because a date-only statement can date a payment after the money left. When the identified record sends and is a connector's record (its time the instant the money left, as the card rule already assumes), and the candidate is an imported row timed to the second, no earlier than the sending instant minus `near_window`, the gate asks only from that instant's day in the candidate's booking frame: the time zone its file is read in (`occurred_tz`: its profile's, UTC for the Cash ISA; else the owner's), since the days imports list are booking dates. In the owner's zone, a payment between 00:00 and 01:00 in London in summer would be asked from the day after the UTC day its real receipt is booked on. An OFX row is booked on the date its `DTPOSTED` writes, in that offset; where its own booking date shows that frame a day behind its zone, the day is taken a day earlier (never later). Known limitation: an OFX row whose `DTPOSTED` offset (or the UTC it is read as without one) differs from its `occurred_tz` and whose booking date doesn't show it can still be asked from a day late after a payment just after midnight in summer; closing it needs the row's own offset stored. The weak sort-code tier's gate is relaxed the same way, from the record whose sort code names the other's account, when it sends and is a connector's record. An imported sender never counts, even one timed to the second: an imported row's time can be a posting time, as OFX's `DTPOSTED` is, which may come after the money left. This rule only ever lets the gate accept a pair it used to refuse: windows, tiers and every other use of time are unchanged, and an imported row's time still decides nothing else.
3. **A refused row is still a candidate; it just can't be taken by itself.** (Reworked 2026-11-27 at the owner's request: one rule instead of special cases.) The coverage gate answers only "may this pair be taken automatically?". It does not take the row out of the record's candidates. So the pair relation keeps every same-amount edge as a candidate and marks the ones the gate refused as not takeable. Everything that asks about candidates sees them: uniqueness, interchangeable rows, the cross-currency and card rules' "a record with another candidate is left for review", and the review's candidate list. A pair is taken automatically only when the two records are each other's only candidate and the edge between them is takeable. Review reason: `several_candidates` with more than one candidate, `amount_differs` as before, else `not_listed_in_full` when the one candidate is not takeable. A transfer to a manual account gets no placeholder while it has a candidate of the same amount on that account: the candidate may be its real row, and a placeholder beside it would count the money twice in the account's balance (net worth counts the amount in In transit until the owner pairs, as before). `before_history` still comes first. This also changes M4's `several_candidates` on a manual account, which used to post a placeholder beside the rows. A group of identical rows (interchangeable, same account, instrument, amount and booking date) with an untakeable pair among them pairs none of them. A transfer settled before history (`before_history`) gets no `not_listed_in_full`. When a later upload lists the days, the edge becomes takeable and the pair forms by itself if it is unique. The owner can pair by hand from the review. A transfer awaiting a manual account is never overdue: it waits for an upload, not a connection's report.

## 9. Moves between the owner's Trading 212 accounts

A Trading 212 TRANSFER names no other account. Today, when the owner has exactly one `invest` and one `isa` connection, the other is taken to be the counterpart (`trading212/accounts.sibling`). With a Cash ISA a TRANSFER could be to or from it, so the owner decided that once a Cash ISA exists, TRANSFERs name no counterpart, whatever their date (2026-11-26).

With the Cash ISA an account type, that is expressible exactly, and generically: **a record that moves money within a family names the other account only when the owner has exactly two accounts of that family.** `family` is configuration on the type (§6.1). Accounts are counted by their `type_key`. Trading 212's connector asks for "the only other account of my family" in place of counting its own connections:

- Invest and Stocks ISA only: each is the other's counterpart, as today.
- Invest, Stocks ISA and Cash ISA: no TRANSFER names a counterpart; they pair by the same-amount tiers or by hand.
- Accounts are counted whatever their status or opening date, so closing one doesn't bring the inference back.

When the set of the owner's accounts in the family changes (one is added, or an old account is given a family type), each Trading 212 connection replays its normalisation at its next sync, so every TRANSFER names what the rule now gives, including earlier ones that named the other account. The connection remembers the set it last normalised with, as Monzo's remembers its card merchants; no account route runs a normalisation itself. Production held no Trading 212 TRANSFER on 2026-11-26 (two DEPOSITs only), so no existing pair comes apart when the Cash ISA is added.

Known limitation, in the README: Lararium knows only the accounts added to it. An owner who holds a Cash ISA at Trading 212 but hasn't added it keeps the two-account inference, and a move to the Cash ISA would be named as the other account's.

## 10. Building it in two parts

- **Part A (§6, §7):** the catalog, `type_key`, the Add account page, the "Upload file" action, and the Cash ISA type with its profile. After it, the Cash ISA is added and imported; its top-ups are paired by hand.
- **Part B (§8, §9):** references, the widened one-sided tier and the family rule. It reads `funding`, `family` and `type_key`, so it comes after Part A. After it, bank top-ups to all three Trading 212 accounts pair on their own.

## 11. Left to the builder

- Module layout and helper names.
- The exact `date_format` string, checked against the real file by `make real-check`, which reads `private/t212-cash-isa/` when present.
- Storage for references (a table, or a column on the holders table), its migration, and the API route and form for registering them.
- Response and form models, and the generated web client.
- Tests. Expected coverage:
  - catalog invariants; the derived import catalog equal to today's plus the Cash ISA entry; the Trading 212 `type_key` backfill;
  - creating an account from a type; `type_key` refusals; the web's upload for a typed account asks for no profile;
  - the profile: a synthetic CSV with the real header stages and commits (signed amounts, UTC times, `memo`, `external_id`, currency; every row a bank transfer); re-importing an overlapping file adds nothing;
  - references (Hypothesis properties where the engine is involved): a transfer naming the shared payee with the Cash ISA's reference pairs with the Cash ISA row; with another account's reference, not with it; with none or an unknown one, with nothing; case and surrounding spaces don't matter; an identifier refuses both an account and references;
  - funding (Hypothesis properties): an identified bank transfer pairs with a same-amount unmarked deposit on an account whose type lists `bank_transfer`; not when the type doesn't, when the deposit names anybody, or when two deposits fit; the weak tier is unchanged; a card payment still pairs only by the card rule;
  - family: with Invest and Stocks ISA, a TRANSFER names the other; with a Cash ISA added, none does, and the earlier ones are re-posted without a counterpart; closing the Cash ISA changes nothing.

## 12. Open, for the owner

- Interest: once a real interest row exists, export from the account's first day; a follow-up sets its `Action` in `card_types` (and adds a statement PDF if one is offered).
- TODO (owner, 2026-11-27): a web form for a shared payee's references, to list, add and remove them, with a read-back of the current set. Part B registers them through the API only (the plan's Decision 2), and the only read-back is the registration response.
- TODO (2026-11-27): setting or changing an existing account's opening date (§8.3 item 2). It is set only when an account is created (the Add account upload form asks for it); no route sets or changes it on an account that exists.

## 13. After deploy

1. Check the two Trading 212 accounts carry their type, and that the Add account page lists every type.
2. Add the Cash ISA from the Add account page, giving its opening date, and upload its History CSV from the account's first day, keeping the file name Trading 212 gives it (§8.3).
3. Give accounts made before account types existed their type with `PATCH /api/v1/finance/accounts/{id}` and `{"type_key": ...}` (as the README's Importing statements section says). After deploy each Trading 212 connection replays once at its next sync (the normaliser version is bumped).
4. Register the shared payee account's references for the three Trading 212 accounts; check the first Cash ISA deposit pairs with its Monzo payment, and that the two earlier Invest and Stocks ISA deposits pair or stay as they are for a stated reason.
