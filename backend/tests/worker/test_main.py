"""python -m lararium.worker: how the worker is started."""

from typing import Any

import pytest

from lararium.worker import __main__ as worker_main
from lararium.worker.app import procrastinate_app


def test_the_worker_deletes_finished_jobs_and_keeps_failed_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Periodic jobs run every minute: kept, finished jobs would grow the database (and every
    # dump) without bound.
    seen: dict[str, Any] = {}
    monkeypatch.setattr(worker_main, "get_settings", lambda: None)
    monkeypatch.setattr(procrastinate_app, "run_worker", lambda **kwargs: seen.update(kwargs))

    worker_main.main()

    assert seen["delete_jobs"] == "successful"
    assert seen["concurrency"] == worker_main.CONCURRENCY
