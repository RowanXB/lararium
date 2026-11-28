"""Which postings are flows: the one definition `cashflow.flows` and the owner's known
categories both read (spending-page design §3.5).

A flow is a posting on an account outside the reports, negated, on a transaction that is
not wholly on the hidden side. Outside the reports is

- an income, expense or equity account (Opening balances excepted: where an account's
  history starts is not income), which is also where a connector puts the unknown side of
  what it reads (Unassigned);
- an account left out of reports (hidden), which counts as someone else's;
- In transit, on a side of a transfer to or from a hidden account (its own record, or the
  placeholder standing in for it): the money left for, or came from, the hidden account.
  A rejected transfer is no transfer.

A transaction is wholly on the hidden side, and so no flow at all, when it posts to a
hidden account and to no shown one (an asset or liability account, not hidden, not In
transit): a hidden account's own payment, or its side of a transfer. So is the settlement
of a transfer to or from a hidden account (its fee, or its conversion), which posts only to
In transit and Unassigned and goes with the transfer's accounts.

The fragments are spliced into a raw `text()` query that binds `:excluded` (the hidden
accounts' ids), `:skipped` (Opening balances, or none) and `:in_transit`, and in which the
posting, its account and its transaction are aliased `p`, `a` and `t`. HIDDEN_LEGS_SQL is a
common table expression to put after `with` (or a comma) before them.
"""

# Each side of a transfer to or from a hidden account, with that hidden account: one per
# transaction, should it be a side of several, the first by id.
HIDDEN_LEGS_SQL = """
    hidden_legs as (
      select distinct on (leg.id) leg.id as transaction_id,
        case when tr.from_account_id = any(:excluded)
             then tr.from_account_id else tr.to_account_id end as hidden_id
      from finance.transfers tr
      cross join lateral unnest(array[tr.out_transaction_id, tr.in_transaction_id,
                                      tr.placeholder_transaction_id]) as leg(id)
      where tr.status <> 'rejected' and leg.id is not null
        and (tr.from_account_id = any(:excluded) or tr.to_account_id = any(:excluded))
      order by leg.id, 2
    )
"""

# The posting `p` (on account `a`, of transaction `t`) is a flow's, before the instrument
# rule (cashflow.py).
FLOW_POSTING_SQL = """(
    t.status <> 'void'
    and (
      (a.ledger_type in ('income', 'expense', 'equity') and a.id <> all(:skipped))
      or a.id = any(:excluded)
      or (a.id = :in_transit
          and exists (select 1 from hidden_legs hl where hl.transaction_id = t.id))
    )
    and not (
      exists (
        select 1 from finance.postings hp
        where hp.transaction_id = t.id and hp.account_id = any(:excluded)
      )
      and not exists (
        select 1 from finance.postings sp
        join finance.accounts sa on sa.id = sp.account_id
        where sp.transaction_id = t.id and sa.ledger_type in ('asset', 'liability')
          and sa.id <> all(:excluded) and sa.id is distinct from :in_transit
      )
    )
    and not exists (
      select 1 from finance.transfers st
      where st.settlement_transaction_id = t.id
        and (st.from_account_id = any(:excluded) or st.to_account_id = any(:excluded))
    )
)"""

# Of a flow's posting: it is a move to or from a hidden account (on the hidden account, or
# on In transit, which is a flow's only when it crosses), not an ordinary flow. The source's
# include_in_spending and refund marks do not apply to a move.
MOVE_SQL = "(a.id = any(:excluded) or a.id is not distinct from :in_transit)"

# Of a move's posting: the hidden account it crossed to or from.
HIDDEN_ACCOUNT_SQL = """(case
    when a.id = any(:excluded) then a.id
    when a.id is not distinct from :in_transit
      then (select hl.hidden_id from hidden_legs hl where hl.transaction_id = t.id)
  end)"""
