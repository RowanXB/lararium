"""The two forms a Trading 212 History CSV's Notes take on a deposit or withdrawal that left
the account's family (account family identifier design §5.9): Trading 212's transaction id,
and the bank account a withdrawal was sent to. One definition for the Cash ISA profile (its
extract and its family_notes) and for the export merge that reads evidence from it. The id is
the whole text after the label. The bank account's groups are named as the import's typed
fields, so a CSV extract fills them from the same pattern.
"""

TRANSACTION_ID_NOTES = r"Transaction ID: (\S+)"
SENT_TO_BANK_NOTES = (
    r"Sent to Bank Account (?P<counterparty_sort_code>\d{2}-\d{2}-\d{2})"
    r" / (?P<counterparty_account_number>\d{8})"
)
