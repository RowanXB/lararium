"""The addresses of a Bitcoin wallet, from whichever public form the owner gave.

Electrum keys and descriptors become canonical descriptors (keys as `xpub`, no key
origins, sorted-multisig keys sorted, a receiving and change pair merged into one
`<0;1>` descriptor), so one wallet has one fingerprint however it was entered. Electrum's
old-seed and two-factor wallets cannot be written as descriptors from what the owner
pastes, so they derive through their own code, pinned by Electrum's own test vectors.
"""

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from embit import bip32, ec, script  # pyright: ignore[reportMissingTypeStubs]  # embit ships no stubs
from embit.base import EmbitError  # pyright: ignore[reportMissingTypeStubs]
from embit.descriptor import checksum, descriptor, miniscript  # pyright: ignore[reportMissingTypeStubs]
from embit.misc import secp256k1  # pyright: ignore[reportMissingTypeStubs]

from lararium.modules.finance.bitcoin.keys import WalletInput, WalletInputError

# embit is untyped, so its objects are handled as `Any` past these names.
_Descriptor: Any = descriptor.Descriptor
_Multi: Any = miniscript.Multi
_Sortedmulti: Any = miniscript.Sortedmulti
_HDKey: Any = bip32.HDKey
_PublicKey: Any = ec.PublicKey
_secp: Any = secp256k1
_script: Any = script
_checksum: Any = checksum.checksum

Branch = Literal[0, 1]
ScriptType = Literal["p2pkh", "p2sh-p2wpkh", "p2wpkh", "p2tr", "p2sh", "p2wsh", "p2sh-p2wsh"]
Gaps = Mapping[str, Mapping[Branch, int]]


@dataclass(frozen=True)
class Source:
    """One derivation source of a wallet: `d0`, `d1`, … in descriptor order, or `list`."""

    name: str
    gaps: dict[Branch, int]


class Wallet(Protocol):
    @property
    def sources(self) -> tuple[Source, ...]: ...

    @property
    def descriptors(self) -> tuple[str, ...]:
        """Canonical descriptors with checksums; empty for old, two-factor and lists."""
        ...

    @property
    def fingerprint(self) -> str:
        """SHA-256 hex that is the same for the same wallet however it was entered."""
        ...

    def address(self, source: str, branch: Branch, index: int) -> tuple[str, str]:
        """The address and its script type."""
        ...

    def listed(self) -> tuple[str, ...]:
        """The owner's address list; empty for a derived wallet."""
        ...


_UNREADABLE = "The descriptor could not be read."
_KEY_UNREADABLE = "The master public key could not be read."
_OLD_KEY_UNREADABLE = "An old Electrum master public key could not be read."
_NO_BRANCH = "A gap limit names a branch the wallet does not have."
_ELECTRUM_GAPS: dict[Branch, int] = {0: 20, 1: 10}
_DESCRIPTOR_GAPS: dict[Branch, int] = {0: 20, 1: 20}
_XPUB_VERSION = bytes.fromhex("0488b21e")  # BIP32 mainnet public key version
_SPLIT_STEP = re.compile(r"/([01])/\*")
_MULTIPATH_STEP = "/<0;1>/*"

# (sh, wsh, wpkh, taproot, multisig) of a descriptor → its wrapper and script type.
_SHAPES: dict[tuple[bool, bool, bool, bool, bool], tuple[str, ScriptType]] = {
    (False, False, False, False, False): ("pkh({})", "p2pkh"),
    (True, False, True, False, False): ("sh(wpkh({}))", "p2sh-p2wpkh"),
    (False, False, True, False, False): ("wpkh({})", "p2wpkh"),
    (False, False, False, True, False): ("tr({})", "p2tr"),
    (True, False, False, False, True): ("sh({})", "p2sh"),
    (False, True, False, False, True): ("wsh({})", "p2wsh"),
    (True, True, False, False, True): ("sh(wsh({}))", "p2sh-p2wsh"),
}
_ELECTRUM_SINGLE = {"xpub": "pkh({})", "ypub": "sh(wpkh({}))", "zpub": "wpkh({})"}
_ELECTRUM_MULTISIG = {"xpub": "sh({})", "Ypub": "sh(wsh({}))", "Zpub": "wsh({})"}
# Electrum only makes two-factor wallets of these key kinds (`get_signing_xpub`).
_TWO_FACTOR = {"xpub": "sh({})", "Zpub": "wsh({})"}
# TrustedCoin's mainnet signing key, from Electrum 4.8.4's
# electrum/plugins/trustedcoin/trustedcoin.py (`get_signing_xpub`).
_TRUSTEDCOIN_XPUB = (
    "xpub661MyMwAqRbcGnMkaTx2594P9EDuiEqMq25PM2aeG6UmwzaohgA6uDmNsvSUV8ubqwA3Wpste1hg69XHgjUuCD5H"
    "LcEp2QPzyV1HMrPppsL"
)


def _sha256_hex(parts: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


def _parse(text: str) -> Any:
    try:
        return _Descriptor.from_string(text)
    # embit fails in many ways on malformed text (IndexError among them); every one of
    # them means the same to the owner.
    except Exception:
        raise WalletInputError(_UNREADABLE) from None


def _shape(parsed: Any) -> tuple[bool, bool, bool, bool, bool]:
    multisig = isinstance(parsed.miniscript, _Multi)
    return (parsed.sh, parsed.wsh, parsed.wpkh, parsed.taproot, multisig)


def _key_text(key: Any) -> str:
    # The origin says where a key came from, not which addresses it gives, so it is
    # dropped: the same wallet from Electrum and from Sparrow is then one fingerprint.
    if key.is_extended:
        return key.key.to_base58(_XPUB_VERSION) + key.suffix
    return key.sec().hex()


def _canonical_body(text: str) -> str:
    """The descriptor without checksum, origins or key version; refuses what it cannot scan."""
    body, mark, given = text.partition("#")
    try:
        right: str = _checksum(body)
    except EmbitError:
        raise WalletInputError(_UNREADABLE) from None
    if mark and given != right:
        raise WalletInputError(_UNREADABLE)
    parsed = _parse(body)
    inner = parsed.miniscript
    shape = _shape(parsed)
    if shape not in _SHAPES or (inner is not None and not shape[4]) or parsed.taptree:
        raise WalletInputError("This kind of descriptor is not supported.")
    if not parsed.is_wildcard or parsed.num_branches > 2:
        raise WalletInputError(
            "A descriptor must end in /* with at most a receiving and a change branch."
        )
    wrapper = _SHAPES[shape][0]
    if inner is None:
        return wrapper.format(_key_text(parsed.key))
    threshold, *keys = inner.args
    texts = [_key_text(key) for key in keys]
    if isinstance(inner, _Sortedmulti):
        texts.sort()
    return wrapper.format(f"{inner.NAME}({threshold},{','.join(texts)})")


def _merge_split(bodies: list[str]) -> list[str]:
    """Join a `/0/*` receiving and `/1/*` change descriptor pair into one `<0;1>`."""
    halves: dict[str, set[str]] = {}
    for body in bodies:
        steps: list[str] = _SPLIT_STEP.findall(body)
        if "<" not in body and len(steps) == body.count("/*") and len(set(steps)) == 1:
            halves.setdefault(_SPLIT_STEP.sub(_MULTIPATH_STEP, body), set()).add(steps[0])
    merged: list[str] = []
    for body in bodies:
        template = _SPLIT_STEP.sub(_MULTIPATH_STEP, body)
        if halves.get(template) == {"0", "1"}:
            body = template
        if body not in merged:
            merged.append(body)
    return merged


def _check_index(index: int) -> None:
    if index < 0:
        raise ValueError("An address index is never negative.")


def _position(sources: tuple[Source, ...], name: str, branch: Branch, index: int) -> int:
    _check_index(index)
    for position, source in enumerate(sources):
        if source.name == name and branch in source.gaps:
            return position
    raise ValueError("The wallet has no such source or branch.")


def _sources(defaults: Sequence[dict[Branch, int]], gaps: Gaps | None) -> tuple[Source, ...]:
    chosen = {f"d{position}": dict(branches) for position, branches in enumerate(defaults)}
    for name, override in (gaps or {}).items():
        branches = chosen.get(name)
        if branches is None or not set(override) <= set(branches):
            raise WalletInputError(_NO_BRANCH)
        if any(limit < 1 for limit in override.values()):
            raise WalletInputError("A gap limit must be at least 1.")
        branches.update(override)
    return tuple(Source(name, branches) for name, branches in chosen.items())


@dataclass(frozen=True)
class _Derived:
    sources: tuple[Source, ...]
    descriptors: tuple[str, ...]
    fingerprint: str
    parsed: tuple[Any, ...]

    def address(self, source: str, branch: Branch, index: int) -> tuple[str, str]:
        parsed = self.parsed[_position(self.sources, source, branch, index)]
        branch_index = branch if parsed.num_branches == 2 else None
        address: str = parsed.derive(index, branch_index=branch_index).address()
        return address, _SHAPES[_shape(parsed)][1]

    def listed(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class _OldElectrum:
    """Electrum's pre-2.0 wallet: key *i* of branch *c* is P + sha256d("i:c:" + P)·G."""

    sources: tuple[Source, ...]
    fingerprint: str
    mpk: bytes
    descriptors: tuple[str, ...] = ()

    def address(self, source: str, branch: Branch, index: int) -> tuple[str, str]:
        _position(self.sources, source, branch, index)
        # Electrum 4.8.4 electrum/keystore.py `Old_KeyStore.get_pubkey_from_mpk`.
        tweak = hashlib.sha256(
            hashlib.sha256(f"{index}:{branch}:".encode() + self.mpk).digest()
        ).digest()
        point = _secp.ec_pubkey_add(_secp.ec_pubkey_parse(b"\x04" + self.mpk), tweak)
        address: str = _script.p2pkh(_PublicKey(point, compressed=False)).address()
        return address, "p2pkh"

    def listed(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class _Listed:
    addresses: tuple[str, ...]
    sources: tuple[Source, ...] = (Source("list", {}),)
    descriptors: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        return _sha256_hex(self.addresses)

    def address(self, source: str, branch: Branch, index: int) -> tuple[str, str]:
        if source != "list" or branch != 0:
            raise ValueError("An address list has only the source 'list', branch 0.")
        _check_index(index)
        address = self.addresses[index]
        kind: str = _script.address_to_scriptpubkey(address).script_type()
        return address, kind

    def listed(self) -> tuple[str, ...]:
        return self.addresses


def _descriptor_wallet(
    texts: Sequence[str], defaults: dict[Branch, int], gaps: Gaps | None
) -> _Derived:
    bodies = _merge_split([_canonical_body(text) for text in texts])
    parsed = tuple(_parse(body) for body in bodies)
    branch_defaults: list[dict[Branch, int]] = [
        defaults if one.num_branches == 2 else {0: defaults[0]} for one in parsed
    ]
    descriptors = tuple(f"{body}#{_checksum(body)}" for body in bodies)
    return _Derived(_sources(branch_defaults, gaps), descriptors, _sha256_hex(descriptors), parsed)


def _trustedcoin_key(key1: str, key2: str) -> str:
    """TrustedCoin's cosigner key for the owner's two keys.

    Ported from Electrum 4.8.4 electrum/plugins/trustedcoin/trustedcoin.py
    (`get_user_id`, `make_xpub`): a BIP32 public child of TrustedCoin's key whose
    32-byte "index" is the SHA-256 of the owner's two keys, sorted and joined.
    """
    user_id = hashlib.sha256("".join(sorted((key1, key2))).encode()).digest()
    root = _HDKey.from_base58(_TRUSTEDCOIN_XPUB)
    parent: bytes = root.key.sec()
    mac = hmac.new(root.chain_code, parent + user_id, hashlib.sha512).digest()
    point = _secp.ec_pubkey_add(_secp.ec_pubkey_parse(parent), mac[:32])
    child: str = _HDKey(_PublicKey(point), mac[32:]).to_base58()
    return child


def _check_readable(keys: Sequence[str]) -> None:
    for key in keys:
        try:
            _HDKey.from_base58(key)
        except Exception:  # embit is untyped and fails in many ways
            raise WalletInputError(_KEY_UNREADABLE) from None


def _electrum_wallet(spec: WalletInput, gaps: Gaps | None) -> Wallet:
    keys = spec.keys
    prefix = keys[0][:4]
    if spec.electrum_type == "old":
        mpk = bytes.fromhex(keys[0])
        try:
            _secp.ec_pubkey_parse(b"\x04" + mpk)
        except Exception:  # not a point on the curve
            raise WalletInputError(_OLD_KEY_UNREADABLE) from None
        return _OldElectrum(_sources([_ELECTRUM_GAPS], gaps), _sha256_hex(keys), mpk)
    _check_readable(keys)
    if spec.electrum_type == "single":
        wrapper = _ELECTRUM_SINGLE.get(prefix)
        if wrapper is None:
            raise WalletInputError("A single-key Electrum wallet's key is an xpub, ypub or zpub.")
        return _descriptor_wallet([wrapper.format(keys[0] + _MULTIPATH_STEP)], _ELECTRUM_GAPS, gaps)
    if spec.electrum_type == "2fa":
        wrapper = _TWO_FACTOR.get(prefix)
        if wrapper is None or keys[1][:4] != prefix:
            raise WalletInputError("A two-factor wallet's keys are both xpub or both Zpub.")
        cosigners = (*keys, _trustedcoin_key(keys[0], keys[1]))
        inner = ",".join(key + _MULTIPATH_STEP for key in cosigners)
        derived = _descriptor_wallet(
            [wrapper.format(f"sortedmulti(2,{inner})")], _ELECTRUM_GAPS, gaps
        )
        # TrustedCoin's key is derived, not entered: the owner's two keys name the wallet.
        return replace(derived, descriptors=(), fingerprint=_sha256_hex(keys))
    wrapper = _ELECTRUM_MULTISIG.get(prefix)
    if wrapper is None:
        raise WalletInputError("A multisig Electrum wallet's keys are xpub, Ypub or Zpub.")
    inner = ",".join(key + _MULTIPATH_STEP for key in keys)
    text = wrapper.format(f"sortedmulti({spec.threshold},{inner})")
    return _descriptor_wallet([text], _ELECTRUM_GAPS, gaps)


def build_wallet(spec: WalletInput, gaps: Gaps | None = None) -> Wallet:
    """The wallet a `parse_input`-validated spec describes.

    `gaps` overrides the default gap limits of the named sources' branches. Raises
    `WalletInputError` for a wallet it cannot derive.
    """
    if spec.form == "electrum":
        return _electrum_wallet(spec, gaps)
    if spec.form == "descriptor":
        return _descriptor_wallet(spec.descriptors, _DESCRIPTOR_GAPS, gaps)
    if gaps:
        raise WalletInputError(_NO_BRANCH)
    return _Listed(spec.addresses)


def first_receiving(spec: WalletInput) -> str:
    """The address the owner compares with the wallet's own before saving it."""
    wallet = build_wallet(spec)
    return wallet.address(wallet.sources[0].name, 0, 0)[0]
