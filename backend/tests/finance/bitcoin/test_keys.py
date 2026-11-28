import pytest

from lararium.modules.finance.bitcoin.keys import (
    PrivateMaterialError,
    UnsupportedNetworkError,
    WalletInput,
    WalletInputError,
    parse_input,
    refuse_private,
)

_BODY = "1" * 107  # a base58 body: prefix (4) + body = 111 characters
_ZPUB = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDt"
    "KsAYz2oz2AGutZYs"
)
_XPUB = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoCu1Rupje8YtGqsefD265TMg7"
    "usUDFdp6W1EGMcet8"
)
_PRIVATE = "This looks like a private key. Never enter it anywhere: "
_GUIDE = "Lararium needs the master public key from Wallet → Information."


def test_seed_phrase_is_refused() -> None:
    with pytest.raises(PrivateMaterialError) as caught:
        refuse_private("abandon " * 11 + "about")
    assert str(caught.value) == (
        "This looks like a seed phrase. Never enter it anywhere: "
        "Lararium needs the master public key from Wallet → Information."
    )
    assert "abandon" not in str(caught.value)


def test_address_list_is_not_a_seed() -> None:
    addresses = " ".join(["bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"] * 12)
    refuse_private(addresses)


@pytest.mark.parametrize("prefix", ["xprv", "yprv", "zprv", "Yprv", "Zprv", "tprv"])
def test_extended_private_keys_are_refused(prefix: str) -> None:
    with pytest.raises(PrivateMaterialError) as caught:
        refuse_private(prefix + _BODY)
    assert str(caught.value) == _PRIVATE + _GUIDE
    assert _BODY not in str(caught.value)


def test_private_key_inside_a_descriptor_is_refused() -> None:
    with pytest.raises(PrivateMaterialError):
        refuse_private(f"wpkh([d34db33f/84h/0h/0h]xprv{_BODY}/0/*)")


@pytest.mark.parametrize("wif", ["K" + "1" * 51, "5" + "1" * 50])
def test_wif_private_key_is_refused(wif: str) -> None:
    with pytest.raises(PrivateMaterialError) as caught:
        refuse_private(wif)
    assert str(caught.value) == _PRIVATE + _GUIDE


def test_64_hex_is_refused() -> None:
    with pytest.raises(PrivateMaterialError):
        refuse_private("ab" * 32)


@pytest.mark.parametrize("prefix", ["tpub", "upub", "vpub", "Upub", "Vpub"])
def test_test_network_keys_are_refused(prefix: str) -> None:
    with pytest.raises(UnsupportedNetworkError) as caught:
        refuse_private(prefix + _BODY)
    assert str(caught.value) == "Only Bitcoin mainnet wallets are supported."


def test_public_forms_pass() -> None:
    refuse_private(_ZPUB)
    refuse_private("ab" * 64)


def test_multisig_needs_threshold_within_keys() -> None:
    with pytest.raises(WalletInputError):
        parse_input(
            WalletInput("electrum", "multisig", (_XPUB, _XPUB, _XPUB), 4, (), ()),
        )
    with pytest.raises(WalletInputError):
        parse_input(WalletInput("electrum", "multisig", (_XPUB, "Zpub" + _BODY), 2, (), ()))
    ok = parse_input(WalletInput("electrum", "multisig", (f" {_XPUB} ", _XPUB), 2, (), ()))
    assert ok.keys == (_XPUB, _XPUB)


def test_2fa_takes_two_keys() -> None:
    for keys in ((_XPUB,), (_XPUB, _XPUB, _XPUB)):
        with pytest.raises(WalletInputError):
            parse_input(WalletInput("electrum", "2fa", keys, None, (), ()))
    parse_input(WalletInput("electrum", "2fa", (_XPUB, _XPUB), None, (), ()))


def test_addresses_are_checked() -> None:
    with pytest.raises(UnsupportedNetworkError):
        parse_input(
            WalletInput(
                "addresses", None, (), None, (), ("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",)
            )
        )
    with pytest.raises(WalletInputError):
        parse_input(WalletInput("addresses", None, (), None, (), ("bc1qnotanaddressatall0000000",)))
    ok = parse_input(
        WalletInput(
            "addresses",
            None,
            (),
            None,
            (),
            (" bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq ", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"),
        )
    )
    assert ok.addresses[0] == "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


def test_errors_never_echo_the_input() -> None:
    secret = "bc1qnotanaddressatall0000000"
    with pytest.raises(WalletInputError) as caught:
        parse_input(WalletInput("addresses", None, (), None, (), (secret,)))
    assert secret not in str(caught.value)
