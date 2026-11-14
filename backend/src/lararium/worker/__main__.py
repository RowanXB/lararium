"""Run the worker: `python -m lararium.worker`."""

import logging

from lararium.config import get_settings
from lararium.worker.app import procrastinate_app

# httpx logs every request's full URL at INFO, and httpcore its connections at DEBUG; the
# Feishu hook URL carries the bot's token, and Monzo URLs carry account ids.
QUIET_LOGGERS = ("httpx", "httpcore")
# Jobs run side by side, so one connection's approval wait (up to 4 minutes) and backfill
# can't use up another's approval window. The sync lock still keeps one connection's jobs
# from overlapping.
CONCURRENCY = 4


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    # Fails with a clear message when the environment is incomplete.
    get_settings()
    # run_worker imports the app's import_paths (lararium.worker.jobs), which registers the tasks.
    procrastinate_app.run_worker(concurrency=CONCURRENCY)


if __name__ == "__main__":
    main()
