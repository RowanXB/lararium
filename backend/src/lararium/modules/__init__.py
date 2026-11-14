from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from fastapi import FastAPI

from lararium.core.connectors import Connector


# Defined here rather than in registry.py: each module's __init__ builds its spec, and
# registry.py imports the modules, so defining it there would be an import cycle.
@dataclass(frozen=True)
class ModuleSpec:
    name: str
    install: Callable[[FastAPI], None]
    # Keyed by Connector.name.
    connectors: Mapping[str, Connector] = field(default_factory=dict[str, Connector])
