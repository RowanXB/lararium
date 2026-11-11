# Deliberately no binary-fraction numbers: convert a Decimal to a string before it goes in.
type JSONValue = bool | int | str | list[JSONValue] | dict[str, JSONValue] | None
