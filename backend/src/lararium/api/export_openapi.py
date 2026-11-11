"""Print the OpenAPI document: `python -m lararium.api.export_openapi`."""

import json

from lararium.api.app import create_app
from lararium.config import Settings


def render_openapi() -> str:
    # Building the app doesn't connect: the engine is lazy and nothing queries it.
    settings = Settings(
        database_url="postgresql+psycopg://openapi@localhost/unused", auth_mode="dev"
    )
    return json.dumps(create_app(settings).openapi(), sort_keys=True, indent=2)


def main() -> None:
    print(render_openapi())


if __name__ == "__main__":
    main()
