from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI


# Defined here rather than in registry.py: each module's __init__ builds its spec, and
# registry.py imports the modules, so defining it there would be an import cycle.
@dataclass(frozen=True)
class ModuleSpec:
    name: str
    install: Callable[[FastAPI], None]
