"""Run the webhook receiver: `python -m lararium.webhook_in`.

The access log is off: every request line would carry the secret in its path.
"""

import uvicorn

from lararium.webhook_in.app import from_env

PORT = 8000


def main() -> None:
    # Fails with a clear message when the environment is incomplete.
    app = from_env()
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False, server_header=False)


if __name__ == "__main__":
    main()
