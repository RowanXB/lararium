import hashlib

import pytest
from embit.descriptor.checksum import add_checksum

from lararium.modules.finance.bitcoin.derive import (
    Source,
    build_wallet,
    first_receiving,
)
from lararium.modules.finance.bitcoin.keys import WalletInput, WalletInputError, parse_input

# BIP84's account key (m/84'/0'/0' of "abandon ... about"), also Electrum's
# test_bip39_seed_bip84_native_segwit.
_ZPUB = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDt"
    "KsAYz2oz2AGutZYs"
)
# The same key with the xpub version bytes.
_ZPUB_AS_XPUB = (
    "xpub6CatWdiZiodmUeTDp8LT5or8nmbKNcuyvz7WyksVFkKB4RHwCD3XyuvPEbvqAQY3rAPshWcMLoP2fMFMKHPJ4ZeZXY"
    "VUhLv1VMrjPC7PW6V"
)
_BIP84_RECEIVING_0 = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
_BIP84_CHANGE_0 = "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"


def _electrum(kind: str, *keys: str, threshold: int | None = None) -> WalletInput:
    return parse_input(WalletInput("electrum", kind, keys, threshold))  # pyright: ignore[reportArgumentType]


def _descriptors(*descriptors: str) -> WalletInput:
    return parse_input(WalletInput("descriptor", descriptors=descriptors))


def test_electrum_zpub_is_p2wpkh() -> None:
    """BIP84 vector; Electrum's test_bip39_seed_bip84_native_segwit."""
    wallet = build_wallet(_electrum("single", _ZPUB))
    assert wallet.sources == (Source("d0", {0: 20, 1: 10}),)
    assert wallet.address("d0", 0, 0) == (_BIP84_RECEIVING_0, "p2wpkh")
    assert wallet.address("d0", 1, 0) == (_BIP84_CHANGE_0, "p2wpkh")


def test_electrum_ypub_is_p2sh_p2wpkh() -> None:
    """Electrum's test_bip39_seed_bip49_p2sh_segwit."""
    ypub = (
        "ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke7icmizBMXrzBx9979FfaHxHcrA"
        "rf3zbeJJJUZPf663zsP"
    )
    wallet = build_wallet(_electrum("single", ypub))
    assert wallet.address("d0", 0, 0) == ("37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf", "p2sh-p2wpkh")


def test_electrum_single_xpub_is_p2pkh() -> None:
    """An xpub standard wallet is p2pkh, never p2sh (Review Focus 2); BIP44 vector."""
    xpub = (
        "xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3DSSSSoekku"
        "dhUd9yLb6qx39T9nMdj"
    )
    wallet = build_wallet(_electrum("single", xpub))
    assert wallet.address("d0", 0, 0) == ("1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA", "p2pkh")


def test_taproot_descriptor() -> None:
    """BIP86 vector: the first receiving address of m/86'/0'/0'."""
    wallet = build_wallet(
        _descriptors(
            "tr(xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVky7D3UKDP9QyrLprQ3VCE"
            "CoY49yfdDEHGCtMMj92pReUsQ/<0;1>/*)"
        )
    )
    assert wallet.sources == (Source("d0", {0: 20, 1: 20}),)
    assert wallet.address("d0", 0, 0) == (
        "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr",
        "p2tr",
    )


def test_split_descriptors_equal_multipath() -> None:
    split = build_wallet(_descriptors(f"wpkh({_ZPUB}/0/*)", f"wpkh({_ZPUB}/1/*)"))
    multipath = build_wallet(_descriptors(f"wpkh({_ZPUB}/<0;1>/*)"))
    assert split.sources == multipath.sources == (Source("d0", {0: 20, 1: 20}),)
    for index in range(3):
        for branch in (0, 1):
            assert split.address("d0", branch, index) == multipath.address("d0", branch, index)
    assert split.address("d0", 1, 0)[0] == _BIP84_CHANGE_0
    assert split.descriptors == multipath.descriptors
    assert split.fingerprint == multipath.fingerprint


def test_bad_checksum_is_refused() -> None:
    good = add_checksum(f"wpkh({_ZPUB_AS_XPUB}/<0;1>/*)")
    assert build_wallet(_descriptors(good)).address("d0", 0, 0)[0] == _BIP84_RECEIVING_0
    wrong = good[:-1] + ("q" if good[-1] != "q" else "p")
    with pytest.raises(WalletInputError) as caught:
        build_wallet(_descriptors(wrong))
    assert str(caught.value) == "The descriptor could not be read."


# Electrum's 2-of-2 multisig cases of tests/test_wallet_vertical.py (tag 4.8.4), one per
# key kind: xpub test_electrum_multisig_seed_standard, Ypub
# test_bip39_multisig_seed_p2sh_segwit, Zpub test_electrum_multisig_seed_segwit.
_MULTISIG = [
    pytest.param(
        "sh",
        "p2sh",
        "xpub661MyMwAqRbcGNEPu3aJQqXTydqR9t49Tkwb4Esrj112kw8xLthv8uybxvaki4Ygt9xiwZUQGeFTG7T2TUzR3eA4"
        "Zp3aq5RXsABHFBUrq4c",
        "xpub661MyMwAqRbcGfCPEkkyo5WmcrhTq8mi3xuBS7VEZ3LYvsgY1cCFDbenT33bdD12axvrmXhuX3xkAbKci3yZY9ZE"
        "k8vhLic7KNhLjqdh5ec",
        "32ji3QkAgXNz6oFoRfakyD3ys1XXiERQYN",
        "36XWwEHrrVCLnhjK5MrVVGmUHghr9oWTN1",
        id="xpub",
    ),
    pytest.param(
        "sh(wsh",
        "p2sh-p2wsh",
        "Ypub6hWbqA2p47QgsLt5J4nxrR3ngu8xsPGb7PdV8CDh48KyNngNqPKSqertAqYhQ4umELu1UsZUCYfj9XPA6AdSMZWD"
        "ZQobwF7EJ8uNrECaZg1",
        "Ypub6iNDhL4WWq5kFZcdFqHHwX4YTH4rYGp8xbndpRrY7WNZFFRfogSrL7wRTajmVHgR46AT1cqUG1mrcRd7h1WXwBsg"
        "X2QvT3zFbBCDiSDLkau",
        "35LeC45QgCVeRor1tJD6LiDgPbybBXisns",
        "39RhtDchc6igmx5tyoimhojFL1ZbQBrXa6",
        id="Ypub",
    ),
    pytest.param(
        "wsh",
        "p2wsh",
        "Zpub6xwgqLvc42wXB1wEELTdALD9iXwStMUkGqBgxkJFYumaL2dWgNvUkjEDWyDFZD3fZuDWDzd1KQJ4NwVHS7hs6H6Q"
        "kpNYSShfNiUZsgMdtNg",
        "Zpub6y4oYeETXAbzLNg45wcFDGwEG3vpgsyMJybiAfi2pJtNF3i3fJVxK2BeZJaw7VeKZm192QHvXP3uHDNpNmNDbQft"
        "9FiMzkKUhNXQafUMYUY",
        "bc1qvzezdcv6vs5h45ugkavp896e0nde5c5lg5h0fwe2xyfhnpkxq6gq7pnwlc",
        "bc1qxqf840dqswcmu7a8v82fj6ej0msx08flvuy6kngr7axstjcaq6us9hrehd",
        id="Zpub",
    ),
]


@pytest.mark.parametrize(("wrapper", "script", "key1", "key2", "receiving", "change"), _MULTISIG)
def test_multisig_sorts_keys(
    wrapper: str, script: str, key1: str, key2: str, receiving: str, change: str
) -> None:
    """Electrum's tests/test_wallet_vertical.py (4.8.4): see `_MULTISIG` for the case names.

    The test file has no mainnet 2-of-3 multisig built from public keys alone, so the
    sorting is pinned on its 2-of-2 cases.
    """
    forward = build_wallet(_electrum("multisig", key1, key2, threshold=2))
    backward = build_wallet(_electrum("multisig", key2, key1, threshold=2))
    closing = ")" * wrapper.count("(")
    explicit = build_wallet(
        _descriptors(f"{wrapper}(sortedmulti(2,{key1}/<0;1>/*,{key2}/<0;1>/*)){closing}")
    )
    assert forward.address("d0", 0, 0) == (receiving, script)
    assert forward.address("d0", 1, 0) == (change, script)
    for index in range(3):
        for branch in (0, 1):
            expected = explicit.address("d0", branch, index)
            assert forward.address("d0", branch, index) == expected
            assert backward.address("d0", branch, index) == expected
    assert forward.descriptors == backward.descriptors == explicit.descriptors
    assert forward.fingerprint == backward.fingerprint == explicit.fingerprint
    assert forward.sources == (Source("d0", {0: 20, 1: 10}),)


def test_electrum_old_seed() -> None:
    """Electrum's tests/test_wallet_vertical.py (4.8.4) test_electrum_seed_old."""
    mpk = (
        "e9d4b7866dd1e91c862aebf62a49548c7dbf7bcc6e4b7b8c9da820c7737968df9c09d5a3e271dc814a29981f81b3"
        "faaf2737b551ef5dcc6189cf0f8252c442b3"
    )
    wallet = build_wallet(_electrum("old", mpk))
    assert wallet.sources == (Source("d0", {0: 20, 1: 10}),)
    assert wallet.address("d0", 0, 0) == ("1FJEEB8ihPMbzs2SkLmr37dHyRFzakqUmo", "p2pkh")
    assert wallet.address("d0", 1, 0) == ("1KRW8pH6HFHZh889VDq6fEKvmrsmApwNfe", "p2pkh")
    assert wallet.descriptors == ()
    assert wallet.fingerprint == hashlib.sha256(mpk.encode()).hexdigest()


# Electrum's 2FA cases of tests/test_wallet_vertical.py (tag 4.8.4): the owner's two keys
# (x1, x2) and the first receiving and change addresses.
_TWO_FACTOR = [
    pytest.param(
        "xpub661MyMwAqRbcEwwgkoFakyXCTDazT9WiS6CJx3VhNrRetv7vKJVARtyihwCVatSsUtVsEYcvdxhvDtkSk8qKV3VV"
        "tcL3csz6sQTbGzmEckd",
        "xpub661MyMwAqRbcGLAk7ig1pyMxkFxPAftvkmCA1hoX15BeLnhjktuA29oGb7bh9opQgNERu6iWhwcY6b5bZX57dYsG"
        "o7zYjwXTNCryfKuPfek",
        ("3Bw5jczNModhFAbvfwvUHbdGrC2Lh2qRQp", "p2sh"),
        ("3Ke6pKrmtSyyQaMob1ES4pk8siAAkRmst9", "p2sh"),
        id="test_electrum_seed_2fa_legacy_pre27_25words",
    ),
    pytest.param(
        "xpub661MyMwAqRbcFboJqRKsYtxrATXM5ZBZgbsHzw5j35b5r8eKdQNcZ87UeR24LDSn2RxspwL9s7yM3KqtPFq5dwP5"
        "csmQ2Xb1dgaQztrNGyP",
        "xpub661MyMwAqRbcFKNZyWzsSjUXQxwgnhYPpyaGitW9HVCLng5wWdCJ1Neq2DLV3717ED1RG3aTGLJVVBt5CJEXmCzM"
        "LBjqXtK4MEvRXiYSvnJ",
        ("39XK9VBGiK4bqNJYrajfKE8C1ky4gYA5Zy", "p2sh"),
        ("3PKtHrjiKdsZ73ULZ4Sf1vDBnrUoAEtLDe", "p2sh"),
        id="test_electrum_seed_2fa_legacy_pre27_24words",
    ),
    pytest.param(
        "xpub68qvwUg8sewQvcUgwxuTYr9rrgu5nfn6BwajQpYT9p8fXWxdCRHpN86UWruWJAD1ede8Sv8ERrTa22Gyc4SBfm7z"
        "FpcyoVWVBKCVwnw6s1J",
        "xpub68qvwUg8sewQxoXBXCTLrFKbHkx3QLY5M63EiejxTQRKSFPHjmWCwK8byvZMM2wZNYA3SmxXoma3M1zxhGESHZwt"
        "B7SwrxRgKXAG8dCD2eS",
        ("35L8XmCDoEBKeaWRjvmZvoZvhp8BXMMMPV", "p2sh"),
        ("3PeZEcumRqHSPNN43hd4yskGEBdzXgY8Cy", "p2sh"),
        id="test_electrum_seed_2fa_legacy_post27",
    ),
    pytest.param(
        "Zpub6yzmSw6Fh7zdkoe7x9wEZQkLgpzVpsdB36vV68b5L3Zx7vkrJuKYyPReXMSjBegmtUjFBxP2uZEdL87cYvtTtGaV"
        "uwtRRCTSFUsoAdKZMge",
        "Zpub6yzmSw6Fh7zdo59CbLzxGzjvEM5ACLVAWRLAodGNTmXokBv46TEQXpoUYUaoxPCeynysxg7APfScikCQ2jhCfM3N"
        "cNEk46BCVfSSrdrSkbR",
        (
            "bc1qpmufh0zjp5prfsrk2yskcy82sa26srqkd97j0457andc6m0gh5asw7kqd2",
            "p2wsh",
        ),
        (
            "bc1qd4q50nft7kxm9yglfnpup9ed2ukj3tkxp793y0zya8dc9m39jcwq308dxz",
            "p2wsh",
        ),
        id="test_electrum_seed_2fa_segwit",
    ),
]


@pytest.mark.parametrize(("key1", "key2", "receiving", "change"), _TWO_FACTOR)
def test_electrum_2fa(
    key1: str, key2: str, receiving: tuple[str, str], change: tuple[str, str]
) -> None:
    """Electrum's tests/test_wallet_vertical.py (4.8.4): the case is the parameter id."""
    for keys in ((key1, key2), (key2, key1)):
        wallet = build_wallet(_electrum("2fa", *keys))
        assert wallet.sources == (Source("d0", {0: 20, 1: 10}),)
        assert wallet.address("d0", 0, 0) == receiving
        assert wallet.address("d0", 1, 0) == change
        assert wallet.descriptors == ()
        assert (
            wallet.fingerprint
            == hashlib.sha256("\n".join(sorted((key1, key2))).encode()).hexdigest()
        )


def test_fingerprint_ignores_key_version() -> None:
    electrum = build_wallet(_electrum("single", _ZPUB))
    as_zpub = build_wallet(_descriptors(f"wpkh({_ZPUB}/<0;1>/*)"))
    as_xpub = build_wallet(_descriptors(f"wpkh({_ZPUB_AS_XPUB}/<0;1>/*)"))
    # BIP84's master key fingerprint and account path, as Sparrow or Bitcoin Core write it.
    with_origin = build_wallet(_descriptors(f"wpkh([73c5da0a/84'/0'/0']{_ZPUB}/<0;1>/*)"))
    canonical = add_checksum(f"wpkh({_ZPUB_AS_XPUB}/<0;1>/*)")
    wallets = (electrum, as_zpub, as_xpub, with_origin)
    assert {wallet.descriptors for wallet in wallets} == {(canonical,)}
    assert len({wallet.fingerprint for wallet in wallets}) == 1
    assert electrum.fingerprint == hashlib.sha256(canonical.encode()).hexdigest()


def test_address_list() -> None:
    addresses = (_BIP84_RECEIVING_0, "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA")
    wallet = build_wallet(parse_input(WalletInput("addresses", addresses=addresses)))
    assert wallet.sources == (Source("list", {}),)
    assert wallet.listed() == addresses
    assert wallet.address("list", 0, 1) == ("1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA", "p2pkh")
    assert wallet.descriptors == ()
    assert wallet.fingerprint == hashlib.sha256("\n".join(sorted(addresses)).encode()).hexdigest()


def test_gap_override_is_applied() -> None:
    spec = _electrum("single", _ZPUB)
    assert build_wallet(spec, {"d0": {1: 50}}).sources == (Source("d0", {0: 20, 1: 50}),)
    with pytest.raises(WalletInputError):
        build_wallet(spec, {"d1": {0: 30}})


@pytest.mark.parametrize(
    "descriptor",
    [
        pytest.param(f"wpkh({_ZPUB}/0/0)", id="not-ranged"),
        pytest.param(f"wpkh({_ZPUB}/<0;1;2>/*)", id="three-branches"),
        pytest.param(f"wsh(pk({_ZPUB}/<0;1>/*))", id="not-multisig"),
        pytest.param(f"wpkh({_ZPUB}/0'/*)", id="hardened"),
    ],
)
def test_unsupported_descriptor_is_refused(descriptor: str) -> None:
    with pytest.raises(WalletInputError):
        build_wallet(_descriptors(descriptor))


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(WalletInput("electrum", "single", (_ZPUB,)), id="electrum"),
        pytest.param(
            WalletInput("descriptor", descriptors=(f"wpkh({_ZPUB}/1/*)", f"wpkh({_ZPUB}/0/*)")),
            id="descriptors",
        ),
        pytest.param(
            WalletInput("addresses", addresses=("1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA",)),
            id="addresses",
        ),
    ],
)
def test_first_receiving_matches_address(spec: WalletInput) -> None:
    spec = parse_input(spec)
    wallet = build_wallet(spec)
    assert first_receiving(spec) == wallet.address(wallet.sources[0].name, 0, 0)[0]
