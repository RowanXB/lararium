import re
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "lararium"


def test_no_float_in_source() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}"
        for path in SRC.rglob("*.py")
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"\bfloat\b", line)
    ]
    assert offenders == []
