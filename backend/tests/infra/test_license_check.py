"""tools/license_check.py: the licence policy for production dependencies."""

import importlib.util
import sys
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).parents[3] / "tools"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("license_check", TOOLS / "license_check.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["license_check"] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


lc = _load()
ALLOWED = frozenset({"MIT", "Apache-2.0", "BSD-3-Clause", "GPL-2.0-or-later"})


@pytest.mark.parametrize(
    ("expression", "allowed"),
    [
        ("MIT", True),
        ("GPL-2.0-only", False),
        ("MIT OR GPL-2.0-only", True),
        ("MIT AND GPL-2.0-only", False),
        ("(MIT OR GPL-2.0-only) AND Apache-2.0", True),
        ("MIT OR GPL-2.0-only AND Apache-2.0", True),  # AND binds tighter
        ("GPL-2.0-only OR GPL-2.0-only AND MIT", False),
        ("GPL-2.0-or-later WITH Classpath-exception-2.0", True),
        ("(MIT)", True),
        # Not SPDX expressions, so unknown: they fail.
        ("", False),
        ("BSD", False),
        ("Apache 2.0", False),
        ("MIT License", False),
        ("MIT or Apache-2.0", False),
        ("MIT OR", False),
        ("(MIT", False),
        ("MIT)", False),
        ("MIT WITH", False),
        ("BSD-3-Clause, Apache-2.0", False),
        ("Unknown", False),
        ("LicenseRef-Proprietary", False),
    ],
)
def test_expressions(expression: str, allowed: bool) -> None:
    assert lc.is_allowed(expression, ALLOWED) is allowed


def _policy(*exceptions: Any) -> Any:
    return lc.Policy(ALLOWED, tuple(exceptions))


def _exception(package: str, license: str, ecosystem: str = "python") -> Any:
    return lc.Reviewed(ecosystem, lc.normalize(package), license, "MIT", "checked by hand")


def test_an_unknown_licence_fails_without_an_exception() -> None:
    [problem] = lc.check([lc.Dependency("python", "foo", "1.0", "BSD")], _policy())
    assert problem == "python foo 1.0: licence 'BSD' is not allowed and has no reviewed exception"
    [problem] = lc.check([lc.Dependency("npm", "bar", "2.0", "")], _policy())
    assert problem == "npm bar 2.0: no licence is not allowed and has no reviewed exception"


def test_an_exception_covers_its_package_and_licence_only() -> None:
    policy = _policy(_exception("Foo_Bar", "BSD"))
    assert lc.check([lc.Dependency("python", "foo-bar", "1.0", "BSD")], policy) == []
    # The licence string changed upstream: reviewed again.
    problems = lc.check([lc.Dependency("python", "foo-bar", "1.1", "BSD-4-Clause")], policy)
    assert problems[0].startswith("python foo-bar 1.1: licence 'BSD-4-Clause' is not allowed")
    # Another ecosystem's package of the same name isn't covered.
    problems = lc.check([lc.Dependency("npm", "foo-bar", "1.0", "BSD")], policy)
    assert problems[0].startswith("npm foo-bar 1.0:")


def test_an_unused_exception_fails() -> None:
    policy = _policy(_exception("gone", "BSD"))
    assert lc.check([lc.Dependency("python", "foo", "1.0", "MIT")], policy) == [
        "exception for python gone ('BSD') matches no production dependency: remove it,"
        " or review the package again"
    ]


def test_an_allowed_licence_needs_no_exception() -> None:
    assert lc.check([lc.Dependency("npm", "x", "1", "(MIT OR CC0-1.0)")], _policy()) == []


def test_python_licence_prefers_the_expression() -> None:
    metadata = Message()
    metadata["License"] = "MIT License"
    metadata["Classifier"] = "License :: OSI Approved :: MIT License"
    assert lc.python_license(metadata) == "MIT License"
    metadata["License-Expression"] = "MIT"
    assert lc.python_license(metadata) == "MIT"
    only_classifier = Message()
    only_classifier["Classifier"] = "Programming Language :: Python"
    only_classifier["Classifier"] = "License :: OSI Approved :: MIT License"
    assert lc.python_license(only_classifier) == "classifiers: OSI Approved :: MIT License"
    assert lc.python_license(Message()) == ""


def test_the_policy_file_loads() -> None:
    policy = lc.load_policy(TOOLS / "licenses.toml")
    assert "MIT" in policy.allowed and "GPL-2.0-only" not in policy.allowed
    assert all(e.reason for e in policy.exceptions)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ('ecosystem = "go"\nconcluded = "MIT"\nreason = "x"', "ecosystem"),
        ('ecosystem = "npm"\nconcluded = "MIT License"\nreason = "x"', "concluded"),
        ('ecosystem = "npm"\nconcluded = "MIT"\nreason = " "', "reason"),
    ],
)
def test_a_malformed_exception_is_refused(tmp_path: Path, entry: str, message: str) -> None:
    path = tmp_path / "licenses.toml"
    path.write_text(f'allowed = ["MIT"]\n[[exception]]\npackage = "p"\nlicense = "x"\n{entry}\n')
    with pytest.raises(ValueError, match=message):
        lc.load_policy(path)
