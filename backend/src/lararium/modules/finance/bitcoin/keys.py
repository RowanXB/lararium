"""What the owner pastes to add a Bitcoin wallet, and the refusal of anything secret.

Lararium only ever reads a wallet, so it needs public material: a master public key (or
descriptor, or a list of addresses). Anything that could spend funds is refused on sight,
and no message here repeats the input.
"""

import re
from dataclasses import dataclass
from typing import Literal

from embit.base import EmbitError  # pyright: ignore[reportMissingTypeStubs]  # embit ships no stubs
from embit.script import (  # pyright: ignore[reportMissingTypeStubs]
    address_to_scriptpubkey,  # pyright: ignore[reportUnknownVariableType]
)

from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.identifiers import normalize_identifier

WalletForm = Literal["electrum", "descriptor", "addresses"]
ElectrumType = Literal["single", "multisig", "2fa", "old"]


class PrivateMaterialError(FinanceValidationError):
    """The input is a seed phrase or a private key."""


class UnsupportedNetworkError(FinanceValidationError):
    """The input belongs to a Bitcoin test network."""


class WalletInputError(FinanceValidationError):
    """The input is not a well-formed wallet of the chosen form."""


@dataclass(frozen=True)
class WalletInput:
    form: WalletForm
    electrum_type: ElectrumType | None = None
    keys: tuple[str, ...] = ()
    threshold: int | None = None
    descriptors: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()


_GUIDE = "Lararium needs the master public key from Wallet → Information."
_SEED_MESSAGE = f"This looks like a seed phrase. Never enter it anywhere: {_GUIDE}"
PRIVATE_MESSAGE = f"This looks like a private key. Never enter it anywhere: {_GUIDE}"
NETWORK_MESSAGE = "Only Bitcoin mainnet wallets are supported."

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_RUN = re.compile(f"[{_BASE58}]{{51,}}")
_HEX_RUN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
_SEED_WORD = re.compile(r"[a-z]{3,8}")
_SEED_MIN_WORDS = 12
# Test-network private keys are refused as private, not as a network mismatch: the
# more serious problem is named first.
_PRIVATE_PREFIXES = ("xprv", "yprv", "zprv", "Yprv", "Zprv", "tprv", "uprv", "vprv", "Uprv", "Vprv")
_TESTNET_PUBLIC_PREFIXES = ("tpub", "upub", "vpub", "Upub", "Vpub")
_MAINNET_PUBLIC_PREFIXES = ("xpub", "ypub", "zpub", "Ypub", "Zpub")
_EXTENDED_KEY_LENGTH = (100, 120)
_OLD_KEY = re.compile(r"[0-9a-f]{128}")


def _looks_like_seed(text: str) -> bool:
    words = text.split()
    return len(words) >= _SEED_MIN_WORDS and all(_SEED_WORD.fullmatch(w) for w in words)


def _is_wif(run: str) -> bool:
    return (len(run) == 51 and run[0] == "5") or (len(run) == 52 and run[0] in "KL")


def refuse_private(text: str) -> None:
    """Raise `PrivateMaterialError` for a seed or private key, `UnsupportedNetworkError`
    for a test-network public key; return for anything else."""
    if _looks_like_seed(text):
        raise PrivateMaterialError(_SEED_MESSAGE)
    if _HEX_RUN.search(text) is not None:
        raise PrivateMaterialError(PRIVATE_MESSAGE)
    runs = _BASE58_RUN.findall(text)
    for run in runs:
        if run.startswith(_PRIVATE_PREFIXES) or _is_wif(run):
            raise PrivateMaterialError(PRIVATE_MESSAGE)
    if any(run.startswith(_TESTNET_PUBLIC_PREFIXES) for run in runs):
        raise UnsupportedNetworkError(NETWORK_MESSAGE)


def _is_extended_public_key(key: str) -> bool:
    low, high = _EXTENDED_KEY_LENGTH
    return (
        key.startswith(_MAINNET_PUBLIC_PREFIXES)
        and low <= len(key) <= high
        and all(c in _BASE58 for c in key)
    )


def _clean_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(key.strip() for key in keys)
    for key in cleaned:
        refuse_private(key)
    return cleaned


def _check_electrum(raw: WalletInput) -> WalletInput:
    kind = raw.electrum_type
    if kind is None:
        raise WalletInputError("Choose the Electrum wallet type.")
    keys = _clean_keys(raw.keys)
    threshold = None
    if kind in ("single", "old") and len(keys) != 1:
        raise WalletInputError("This wallet type takes exactly one master public key.")
    if kind == "2fa" and len(keys) != 2:
        raise WalletInputError("A two-factor wallet takes exactly two master public keys.")
    if kind == "multisig":
        if len(keys) < 2:
            raise WalletInputError("A multisig wallet takes at least two master public keys.")
        threshold = raw.threshold
        if threshold is None or not 1 <= threshold <= len(keys):
            raise WalletInputError("The signature threshold must be between 1 and the key count.")
        if len({key[:4] for key in keys}) != 1:
            raise WalletInputError("All the keys of a multisig wallet must be the same kind.")
    if kind == "old":
        if _OLD_KEY.fullmatch(keys[0]) is None:
            raise WalletInputError("An old Electrum master public key is 128 hex digits.")
    elif not all(_is_extended_public_key(key) for key in keys):
        raise WalletInputError("That is not a master public key (xpub, ypub, zpub, Ypub or Zpub).")
    return WalletInput("electrum", kind, keys, threshold, (), ())


def _check_descriptors(raw: WalletInput) -> WalletInput:
    descriptors = tuple(d.strip() for d in raw.descriptors)
    if not descriptors or any(not d for d in descriptors):
        raise WalletInputError("Enter at least one output descriptor.")
    for descriptor in descriptors:
        refuse_private(descriptor)
    return WalletInput("descriptor", None, (), None, descriptors, ())


def _check_address(address: str) -> str:
    refuse_private(address)
    lowered = address.lower()
    if lowered.startswith(("tb1", "bcrt1")) or address[:1] in "mn2":
        raise UnsupportedNetworkError(NETWORK_MESSAGE)
    if lowered.startswith("bc1"):
        address = lowered
    elif address[:1] not in "13":
        raise WalletInputError("That is not a Bitcoin address.")
    try:
        address = normalize_identifier("chain_address", address)
        decoded: object = address_to_scriptpubkey(address)
    except (FinanceValidationError, EmbitError, ValueError):
        raise WalletInputError("That is not a Bitcoin address.") from None
    if decoded is None:
        raise WalletInputError("That is not a Bitcoin address.")
    return address


def _check_addresses(raw: WalletInput) -> WalletInput:
    addresses = tuple(dict.fromkeys(_check_address(a.strip()) for a in raw.addresses))
    if not addresses:
        raise WalletInputError("Enter at least one address.")
    return WalletInput("addresses", None, (), None, (), addresses)


def parse_input(raw: WalletInput) -> WalletInput:
    """The validated, normalised wallet input; raises a `FinanceValidationError` otherwise."""
    if raw.form == "electrum":
        return _check_electrum(raw)
    if raw.form == "descriptor":
        return _check_descriptors(raw)
    return _check_addresses(raw)
