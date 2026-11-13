"""Run the worker: `python -m lararium.worker`."""

import logging

from lararium.config import get_settings
from lararium.worker.app import procrastinate_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Fails with a clear message when the environment is incomplete.
    get_settings()
    # run_worker imports the app's import_paths (lararium.worker.jobs), which registers the tasks.
    procrastinate_app.run_worker()


if __name__ == "__main__":
    main()
