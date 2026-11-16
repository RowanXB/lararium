"""Builders for synthetic Monzo API objects (no real Monzo data)."""

from typing import Any

ACCOUNT_ID = "acc_TEST1"


def make_account(**overrides: Any) -> dict[str, Any]:
    account: dict[str, Any] = {
        "id": ACCOUNT_ID,
        "closed": False,
        "created": "2024-01-15T09:00:00.000Z",
        "description": "uk_retail",
        "type": "uk_retail",
        "currency": "GBP",
        "country_code": "GB",
        # Synthetic bank details.
        "sort_code": "998877",
        "account_number": "87654321",
        "owners": [{"user_id": "user_TEST1", "preferred_name": "Test Owner"}],
        "product_type": "standard",
        "owner_type": "personal",
    }
    return account | overrides


def make_merchant(**overrides: Any) -> dict[str, Any]:
    """An expanded merchant, with every key Monzo's merchant object has that is mapped."""
    merchant: dict[str, Any] = {
        "id": "merch_TEST1",
        "group_id": "grp_TEST1",
        "name": "Test Grocer",
        "logo": "https://logos.example/test-grocer.png",
        "online": False,
        "category": "groceries",
        "metadata": {"website": "https://test-grocer.example"},
        "address": {
            "address": "1 Test Street",
            "city": "Testford",
            "region": "Testshire",
            "country": "GBR",
            "postcode": "TE1 1ST",
            "latitude": 51.5,
            "longitude": -0.1278,
            "formatted": "1 Test Street, Testford TE1 1ST",
            "short_formatted": "1 Test Street",
            "zoom_level": 17,
            "approximate": False,
        },
    }
    return merchant | overrides


def make_transaction(**overrides: Any) -> dict[str, Any]:
    transaction: dict[str, Any] = {
        "id": "tx_TEST0001",
        "account_id": ACCOUNT_ID,
        "amount": -1234,
        "currency": "GBP",
        "local_amount": -1234,
        "local_currency": "GBP",
        "created": "2026-03-01T12:00:00.000Z",
        "settled": "2026-03-02T06:00:00.000Z",
        "description": "TEST SHOP LONDON",
        "category": "groceries",
        "notes": "",
        "merchant": None,
        "metadata": {},
        "counterparty": {},
        "is_load": False,
    }
    return transaction | overrides


def make_pot(**overrides: Any) -> dict[str, Any]:
    pot: dict[str, Any] = {
        "id": "pot_TEST1",
        "name": "Savings",
        "balance": 5000,
        "currency": "GBP",
        "created": "2024-02-01T09:00:00.000Z",
        "updated": "2026-03-01T09:00:00.000Z",
        "deleted": False,
        "current_account_id": ACCOUNT_ID,
        "type": "flexible_savings",
        "isa_wrapper": "",
        "locked": False,
        "round_up": False,
        "round_up_multiplier": None,
        # goal_amount is only present when the pot has a goal: pass it as an override.
    }
    return pot | overrides


def make_balance(**overrides: Any) -> dict[str, Any]:
    balance: dict[str, Any] = {
        "balance": 25000,
        "total_balance": 30000,
        "currency": "GBP",
        "spend_today": -1234,
    }
    return balance | overrides
