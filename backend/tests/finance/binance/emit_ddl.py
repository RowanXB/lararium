"""Prints the `CREATE TABLE` statements of labels, from `layout`, for a migration.

A migration is frozen SQL and never imports the catalogue or `layout`: run this when writing
one, and paste the output in. `uv run python tests/finance/binance/emit_ddl.py deposits ...`
(no argument: every label in `TABLE_LABELS`).
"""

import sys

from lararium.modules.finance.binance.layout import INDEXES, TABLE_LABELS, Table, layout

_NON_FINITE = "('NaN', 'Infinity', '-Infinity')"


def _column(table: Table, name: str) -> str:
    column = table.column(name)
    line = f'"{name}" {column.sql}'
    if not column.nullable:
        line += " NOT NULL"
    if column.name == "connection_id" and table.parent is None:
        line += " REFERENCES core.connections (id)"
    if column.name == "transaction_id":
        line += " REFERENCES finance.transactions (id) ON DELETE SET NULL"
    if column.sql == "numeric":
        line += f'\n        CHECK ("{name}" NOT IN {_NON_FINITE})'
    if column.sql == "numeric[]":
        line += (
            f'\n        CHECK (NOT ("{name}"'
            "\n          && ARRAY['NaN', 'Infinity', '-Infinity']::numeric[]))"
        )
    return line


def _quoted(names: tuple[str, ...]) -> str:
    return ", ".join(f'"{n}"' for n in names)


def create_table(table: Table) -> str:
    lines = [_column(table, c.name) for c in table.columns]
    lines.append(f"PRIMARY KEY ({_quoted(table.key)})")
    if table.parent is not None:
        parent = table.parent
        lines.append(
            f"FOREIGN KEY ({_quoted(parent.key)})\n        REFERENCES finance.{parent.name}"
            f"\n          ({_quoted(parent.key)})\n        ON DELETE CASCADE"
        )
    body = ",\n      ".join(lines)
    return f"CREATE TABLE finance.{table.name} (\n      {body}\n    )"


def indexes(table: Table) -> list[str]:
    found: list[str] = []
    if table.record:
        found.append(
            f"CREATE INDEX {table.name}_transaction_idx"
            f"\n      ON finance.{table.name} (transaction_id)"
            "\n      WHERE transaction_id IS NOT NULL"
        )
    for columns in INDEXES.get(table.label, ()) if table.parent is None else ():
        found.append(
            f"CREATE INDEX {table.name}_{'_'.join(columns)}_idx"
            f"\n      ON finance.{table.name} ({_quoted(columns)})"
        )
    return found


def statements(labels: list[str]) -> list[str]:
    out: list[str] = []
    for label in labels:
        for table in layout(label):
            out.append(create_table(table))
            out.extend(indexes(table))
    return out


if __name__ == "__main__":
    for statement in statements(sys.argv[1:] or list(TABLE_LABELS)):
        print(f'    """\n    {statement}\n    """,')
