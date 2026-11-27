import json
from decimal import Decimal

import pytest

from lararium.modules.finance.binance.catalogue import ENDPOINTS, FIELDS
from lararium.modules.finance.binance.parse import (
    OTHER_VALUE,
    ApiValue,
    BinanceDataError,
    parse,
)
from tests.finance.binance.fixtures import build_response, build_row

LABELS = sorted(ENDPOINTS)
LIST_LABELS = [label for label in LABELS if ENDPOINTS[label].rows != ""]


def has_path(value: ApiValue, path: str) -> bool:
    """Whether the body holds something at the path (`[]`: in any element, `<x>`: any key)."""
    if path == "":
        return True
    head, _, rest = path.partition(".")
    name = head.split("[", 1)[0]
    steps = head[len(name) :].replace("[", " [").split()
    candidates: list[ApiValue] = [value]
    if name:
        if name.startswith("<"):
            candidates = [v for c in candidates if isinstance(c, dict) for v in c.values()]
        else:
            candidates = [c[name] for c in candidates if isinstance(c, dict) and name in c]
    for step in steps:
        if step == "[]":
            candidates = [item for c in candidates if isinstance(c, list) for item in c]
        else:
            index = int(step[1:-1])
            candidates = [c[index] for c in candidates if isinstance(c, list) and len(c) > index]
    return any(has_path(c, rest) for c in candidates)


@pytest.mark.parametrize("label", LABELS)
def test_parse_every_label_round_trips(label: str) -> None:
    row = build_row(label)
    body = build_response(label, [row])
    parsed = parse(label, body)

    assert len(parsed.rows) == 1
    assert dict(parsed.rows[0]) == row
    assert parsed.unknown_fields == set()
    assert parsed.unknown_enums == set()
    assert parsed.body == body
    # Every field the catalogue lists is in the body built from it.
    missing = [spec.path for spec in FIELDS[label] if not has_path(body, spec.path)]
    assert missing == []


@pytest.mark.parametrize("label", LIST_LABELS)
def test_a_second_record_is_a_second_row(label: str) -> None:
    rows = [build_row(label, seq) for seq in range(3)]
    assert [dict(r) for r in parse(label, build_response(label, rows)).rows] == rows
    assert parse(label, build_response(label, [])).rows == []


def test_unknown_field_is_reported_by_name_only() -> None:
    row = build_row("deposits")
    row["memoText"] = "value-that-must-not-leak"
    body = build_response("deposits", [row])

    parsed = parse("deposits", body)
    assert parsed.unknown_fields == {"[].memoText"}
    assert "value-that-must-not-leak" not in repr(parsed.unknown_fields)
    # The record is kept whole, so nothing Binance sent is dropped.
    assert parsed.rows[0]["memoText"] == "value-that-must-not-leak"


def test_unknown_fields_are_found_at_every_level() -> None:
    body = build_response("earn-flexible-subscriptions", [build_row("earn-flexible-subscriptions")])
    assert isinstance(body, dict)
    body["newTop"] = 1
    rows = body["rows"]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    rows[0]["newInRow"] = 2
    assert parse("earn-flexible-subscriptions", body).unknown_fields == {
        "newTop",
        "rows[].newInRow",
    }

    nested = build_response("um-account", [build_row("um-account")])
    assert isinstance(nested, dict)
    positions = nested["positions"]
    assert isinstance(positions, list) and isinstance(positions[0], dict)
    positions[0]["extra"] = "x"
    assert parse("um-account", nested).unknown_fields == {"positions[].extra"}


def test_a_map_keyed_by_anything_is_not_unknown() -> None:
    row = build_row("earn-flexible-position")
    row["tierAnnualPercentageRate"] = {"0-5AAA": "0.05", "5-10AAA": "0.04"}
    parsed = parse("earn-flexible-position", build_response("earn-flexible-position", [row]))
    assert parsed.unknown_fields == set()

    detail = {"AAA": build_row("asset-detail")["AAA"], "BBB": build_row("asset-detail")["AAA"]}
    assert parse("asset-detail", detail).unknown_fields == set()


def test_opaque_elements_may_carry_any_keys() -> None:
    row = build_row("exchange-info")
    row["filters"] = [{"filterType": "PRICE_FILTER", "tickSize": "0.01", "anythingNew": 3}]
    parsed = parse("exchange-info", build_response("exchange-info", [row]))
    assert parsed.unknown_fields == set()


def test_unknown_enum_value_is_reported() -> None:
    row = build_row("earn-flexible-redemptions", status="NEW_STATE")
    parsed = parse("earn-flexible-redemptions", build_response("earn-flexible-redemptions", [row]))
    assert parsed.unknown_enums == {("rows[].status", "NEW_STATE")}
    assert parsed.unknown_fields == set()


def test_an_enum_value_is_named_only_when_it_is_a_short_upper_case_code() -> None:
    for value in ("Some free text, maybe an address", "lower", "A" * 40, ""):
        row = build_row("earn-flexible-redemptions", status=value)
        label = "earn-flexible-redemptions"
        parsed = parse(label, build_response(label, [row]))
        assert parsed.unknown_enums == {("rows[].status", OTHER_VALUE)}


def test_a_numeric_code_outside_its_values_is_reported() -> None:
    row = build_row("deposits", status=9)
    assert parse("deposits", build_response("deposits", [row])).unknown_enums == {
        ("[].status", "9")
    }


def test_missing_required_field_fails_naming_it() -> None:
    row = build_row("deposits")
    del row["amount"]
    with pytest.raises(BinanceDataError) as caught:
        parse("deposits", build_response("deposits", [row]))
    assert caught.value.label == "deposits"
    assert caught.value.field == "[].amount"
    assert "deposits" in str(caught.value) and "[].amount" in str(caught.value)


def test_a_null_required_field_is_missing() -> None:
    row = build_row("deposits", id=None)
    with pytest.raises(BinanceDataError) as caught:
        parse("deposits", build_response("deposits", [row]))
    assert caught.value.field == "[].id"


def test_a_missing_required_field_at_the_top_fails() -> None:
    with pytest.raises(BinanceDataError) as caught:
        parse("time", {})
    assert caught.value.field == "serverTime"


def test_an_optional_field_may_be_absent_or_null() -> None:
    row = build_row("deposits")
    del row["sourceAddress"]
    row["addressTag"] = None
    assert parse("deposits", build_response("deposits", [row])).rows[0] == row


@pytest.mark.parametrize(
    ("label", "field", "value"),
    [
        ("deposits", "amount", "twelve"),
        ("deposits", "amount", True),
        ("deposits", "amount", "NaN"),
        ("deposits", "amount", Decimal("Infinity")),
        ("deposits", "insertTime", "1700000000000"),
        ("deposits", "insertTime", Decimal("1.5")),
        ("deposits", "insertTime", True),
        ("deposits", "id", 12),
        ("deposits", "coin", 5),
        ("deposits", "address", ["x"]),
        ("api-restrictions", "enableReading", 1),
        ("dividends", "id", "7"),
    ],
)
def test_a_value_of_the_wrong_type_fails_naming_the_field(
    label: str, field: str, value: ApiValue
) -> None:
    row = build_row(label, **{field: value})
    with pytest.raises(BinanceDataError) as caught:
        parse(label, build_response(label, [row]))
    assert caught.value.field.endswith(field)
    assert str(value) not in str(caught.value)


def test_a_container_of_the_wrong_kind_fails() -> None:
    with pytest.raises(BinanceDataError):
        parse("deposits", {"id": "1"})
    with pytest.raises(BinanceDataError):
        parse("earn-flexible-subscriptions", {"rows": {"a": 1}, "total": 0})
    with pytest.raises(BinanceDataError):
        parse("spot-account", {"balances": [3]})


def test_decimals_are_accepted_as_strings_numbers_and_integers() -> None:
    for value in ("0.10", Decimal("0.1"), 3, "7"):
        row = build_row("deposits", amount=value)
        parsed = parse("deposits", build_response("deposits", [row]))
        assert parsed.rows[0]["amount"] == value


def test_integer_fields_accept_a_whole_decimal() -> None:
    row = build_row("deposits", status=Decimal("1.0"))
    assert parse("deposits", build_response("deposits", [row])).unknown_enums == set()


def test_numbers_are_decimals() -> None:
    text = json.dumps(build_response("deposits", [build_row("deposits")]))
    # What the client does with a number that has a fraction.
    body = json.loads(text.replace('"1.5"', "1.5"), parse_float=Decimal)
    parsed = parse("deposits", body)

    def walk(value: object) -> list[object]:
        if isinstance(value, dict):
            return [x for v in value.values() for x in walk(v)]
        if isinstance(value, list):
            return [x for v in value for x in walk(v)]
        return [value]

    values = [x for row in parsed.rows for x in walk(dict(row))]
    assert Decimal("1.5") in values
    assert not any(type(x).__name__ == "fl" + "oat" for x in values)


def test_rows_come_from_the_endpoints_rows_path() -> None:
    nested = build_response("rebate", [build_row("rebate")])
    assert [dict(r) for r in parse("rebate", nested).rows] == [build_row("rebate")]
    # An empty array is an empty answer; an array of the wrong kind is an error.
    assert parse("deposits", []).rows == []
    assert parse("earn-flexible-subscriptions", {"rows": [], "total": 0}).rows == []
    with pytest.raises(BinanceDataError):
        parse("earn-flexible-subscriptions", {"rows": 5})


def test_a_record_list_that_is_null_is_never_an_empty_answer() -> None:
    # The body itself, for a label whose records are the body; a container present but null.
    for label, body, field in [
        ("deposits", None, "."),
        ("convert", {"list": None, "moreData": False}, "list"),
        ("rebate", {"status": "OK", "type": "GENERAL", "code": "0", "data": None}, "data.data"),
    ]:
        with pytest.raises(BinanceDataError) as caught:
            parse(label, body)
        assert (caught.value.label, caught.value.field) == (label, field)
    # A label whose body is its one record: a null body is no record either.
    with pytest.raises(BinanceDataError):
        parse("api-restrictions", None)


def test_a_record_list_left_out_is_an_empty_answer() -> None:
    # Whether Binance leaves the key out of an empty window is not known; the real sync works.
    assert parse("earn-flexible-subscriptions", {"total": 0}).rows == []
    assert parse("convert", {"moreData": False}).rows == []
    # Only the list itself: a parent of it left out is not an answer of that shape.
    envelope = {"status": "OK", "type": "GENERAL", "code": "0"}
    assert parse("rebate", {**envelope, "data": {"page": 1}}).rows == []
    with pytest.raises(BinanceDataError) as caught:
        parse("rebate", envelope)
    assert (caught.value.label, caught.value.field) == ("rebate", "data.data")


def test_a_record_list_left_out_of_an_answer_that_says_it_has_more_is_an_error() -> None:
    # The answer contradicts itself: it is not read as an empty window.
    with pytest.raises(BinanceDataError) as caught:
        parse("convert", {"moreData": True})
    assert (caught.value.label, caught.value.field) == ("convert", "list")
    # A `total` counts every page; the page asked past a full one has none, so it says nothing.
    assert parse("earn-flexible-rewards", {"total": 3}).rows == []
    assert parse("dividends", {"total": 1}).rows == []


def test_a_bar_is_keyed_by_position() -> None:
    bar = build_row("klines")
    parsed = parse("klines", build_response("klines", [bar]))
    assert list(parsed.rows[0]) == [str(i) for i in range(12)]
    body = build_response("klines", [bar])
    assert isinstance(body, list) and isinstance(body[0], list) and len(body[0]) == 12
    # A bar with an extra position, or too few, is caught.
    body[0].append("extra")
    assert parse("klines", body).unknown_fields == {"[][12]"}
    with pytest.raises(BinanceDataError) as caught:
        parse("klines", [body[0][:3]])
    assert caught.value.field == "[][3]"


def test_the_error_names_no_value() -> None:
    row = build_row("deposits", address="0x" + "9" * 40, amount="abc")
    with pytest.raises(BinanceDataError) as caught:
        parse("deposits", build_response("deposits", [row]))
    assert "9999" not in str(caught.value) and "abc" not in str(caught.value)
