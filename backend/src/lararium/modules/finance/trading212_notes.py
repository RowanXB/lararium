"""The two forms a Trading 212 export's Notes take on a deposit or withdrawal that left the
account's family (account family identifier design §5.9): the card's transaction id, and the
bank account a withdrawal was sent to. One definition for the catalog profile that classifies
the cell and the merge that reads evidence from it. The id is the whole text after the label.
"""

TRANSACTION_ID_NOTES = r"Transaction ID: (\S+)"
SENT_TO_BANK_NOTES = r"Sent to Bank Account (\d{2})-(\d{2})-(\d{2}) / (\d{8})"
