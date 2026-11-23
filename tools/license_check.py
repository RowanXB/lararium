"""Dependency licence check: `make license-check`, which runs it in the backend's environment.

Every production dependency must carry a licence compatible with Lararium's
AGPL-3.0-or-later, or have a reviewed exception, both in tools/licenses.toml. Production
means what ships: the backend's uv.lock without the dev group (what the Docker image
installs) and the web app's dependencies without devDependencies (`pnpm licenses --prod`).

A licence counts as declared when the package's metadata is an SPDX expression: for Python,
`License-Expression` (PEP 639), else the `License` field; for npm, package.json's `license`.
An expression passes when it holds with the allowed identifiers true, so `MIT OR GPL-2.0-only`
passes and `MIT AND GPL-2.0-only` doesn't. Anything else, an unknown or free-text licence
included, fails unless an exception names that package with exactly that licence string: when
upstream changes its licence, the exception stops matching and the package is reviewed again.
An exception that matches nothing fails too, so the file never vouches for what isn't there.

Standard library only; uv and pnpm do the dependency resolution.
"""

import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "tools" / "licenses.toml"

type Node = str | tuple[str, list["Node"]]  # an identifier, or ("AND" | "OR", operands)

_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.+:-]+)")


def _tokens(expression: str) -> list[str] | None:
    tokens: list[str] = []
    position = 0
    expression = expression.rstrip()
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        if not match:
            return None
        tokens.append(match.group(1))
        position = match.end()
    return tokens


def parse(expression: str) -> Node | None:
    """An SPDX licence expression as a tree, or None when it isn't one. `X WITH exception`
    becomes X: an exception only grants more."""
    tokens = _tokens(expression)
    if not tokens:
        return None
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def operation(operator: str, operand: Callable[[], Node | None]) -> Node | None:
        first = operand()
        if first is None:
            return None
        operands = [first]
        while peek() == operator:
            take()
            following = operand()
            if following is None:
                return None
            operands.append(following)
        return operands[0] if len(operands) == 1 else (operator, operands)

    def factor() -> Node | None:
        token = peek()
        if token is None or token in {")", "AND", "OR", "WITH"}:
            return None
        take()
        if token == "(":
            inner = either()
            if inner is None or peek() != ")":
                return None
            take()
            return inner
        if peek() == "WITH":
            take()
            if peek() in {None, "(", ")", "AND", "OR", "WITH"}:
                return None
            take()
        return token

    def both() -> Node | None:
        return operation("AND", factor)

    def either() -> Node | None:
        return operation("OR", both)

    tree = either()
    return tree if tree is not None and position == len(tokens) else None


def is_allowed(expression: str, allowed: frozenset[str]) -> bool:
    def holds(node: Node) -> bool:
        if isinstance(node, str):
            return node in allowed
        operator, operands = node
        results = [holds(operand) for operand in operands]
        return all(results) if operator == "AND" else any(results)

    tree = parse(expression)
    return tree is not None and holds(tree)


@dataclass(frozen=True)
class Dependency:
    ecosystem: str  # "python" | "npm"
    name: str
    version: str
    license: str  # as the metadata declares it; "" when it declares none


@dataclass(frozen=True)
class Reviewed:
    ecosystem: str
    package: str
    license: str  # the declared string this review applies to, exactly
    concluded: str  # the SPDX expression the review found
    reason: str


@dataclass(frozen=True)
class Policy:
    allowed: frozenset[str]
    exceptions: tuple[Reviewed, ...]


def normalize(name: str) -> str:
    """PEP 503 for Python names; npm names are lower case already."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_policy(path: Path) -> Policy:
    data = tomllib.loads(path.read_text())
    exceptions: list[Reviewed] = []
    for entry in data.get("exception", []):
        exception = Reviewed(
            ecosystem=entry["ecosystem"],
            package=normalize(entry["package"]),
            license=entry["license"],
            concluded=entry["concluded"],
            reason=entry["reason"].strip(),
        )
        if exception.ecosystem not in {"python", "npm"}:
            raise ValueError(f"{exception.package}: ecosystem must be python or npm")
        if parse(exception.concluded) is None:
            raise ValueError(f"{exception.package}: concluded must be an SPDX expression")
        if not exception.reason:
            raise ValueError(f"{exception.package}: an exception needs its reason")
        exceptions.append(exception)
    return Policy(frozenset(data["allowed"]), tuple(exceptions))


def check(dependencies: Iterable[Dependency], policy: Policy) -> list[str]:
    """What fails the check: dependencies whose licence isn't allowed and has no exception,
    and exceptions no dependency uses."""
    by_key = {(e.ecosystem, e.package, e.license): e for e in policy.exceptions}
    used: set[Reviewed] = set()
    problems: list[str] = []
    for d in dependencies:
        if is_allowed(d.license, policy.allowed):
            continue
        exception = by_key.get((d.ecosystem, normalize(d.name), d.license))
        if exception is not None:
            used.add(exception)
            continue
        declared = f"licence {d.license!r}" if d.license else "no licence"
        problems.append(
            f"{d.ecosystem} {d.name} {d.version}: {declared} is not allowed and has no"
            " reviewed exception"
        )
    for e in policy.exceptions:
        if e not in used:
            problems.append(
                f"exception for {e.ecosystem} {e.package} ({e.license!r}) matches no"
                " production dependency: remove it, or review the package again"
            )
    return problems


def python_license(metadata: importlib.metadata.PackageMetadata) -> str:
    for field in ("License-Expression", "License"):
        if value := metadata.get(field):
            return " ".join(value.split())
    classifiers = [
        c.removeprefix("License :: ")
        for c in metadata.get_all("Classifier") or []
        if c.startswith("License :: ")
    ]
    return "classifiers: " + "; ".join(classifiers) if classifiers else ""


def python_dependencies(backend: Path) -> tuple[list[Dependency], list[str]]:
    """The backend's production dependencies, as installed here, and the problems found:
    one that should be installed and isn't, or at another version than uv.lock pins."""
    export = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements.txt",
            "--no-hashes",
            "--no-annotate",
            "--no-header",
            "--quiet",
        ],
        cwd=backend,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    dependencies: list[Dependency] = []
    problems: list[str] = []
    for line in export.splitlines():
        requirement, _, marker = line.partition(";")
        if not requirement.strip() or requirement.startswith(("#", "-")):
            continue
        name, _, version = requirement.strip().partition("==")
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            if not marker.strip():
                problems.append(f"python {name} {version} is not installed: run make backend-sync")
            # else it is only for other platforms (its marker excludes this one)
            continue
        if distribution.version != version:
            problems.append(
                f"python {name}: {distribution.version} is installed, uv.lock pins {version}:"
                " run make backend-sync"
            )
        dependencies.append(
            Dependency("python", name, version, python_license(distribution.metadata))
        )
    return dependencies, problems


def npm_dependencies(web: Path) -> list[Dependency]:
    listing = subprocess.run(
        ["pnpm", "licenses", "list", "--json", "--prod"],
        cwd=web,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    dependencies: list[Dependency] = []
    for packages in json.loads(listing).values():
        for package in packages:
            # pnpm reports a missing licence as "Unknown"; that is no SPDX identifier.
            license = package.get("license") or ""
            for version in package["versions"]:
                dependencies.append(Dependency("npm", package["name"], version, license))
    return dependencies


def main() -> int:
    policy = load_policy(POLICY)
    python, problems = python_dependencies(ROOT / "backend")
    npm = npm_dependencies(ROOT / "web")
    problems += check([*python, *npm], policy)
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        print(f"see {POLICY.relative_to(ROOT)} for the allowed licences and reviewed exceptions")
        return 1
    print(
        f"ok: {len(python)} python and {len(npm)} npm production dependencies,"
        f" {len(policy.exceptions)} by reviewed exception"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
