"""The modules the application installs, in order."""

from lararium.core.connectors import Connector
from lararium.modules import ModuleSpec, finance

__all__ = ["MODULES", "ModuleSpec", "connector"]

MODULES: tuple[ModuleSpec, ...] = (finance.MODULE,)


def connector(name: str) -> Connector:
    """The connector registered under this name by any module; KeyError if none is."""
    for module in MODULES:
        if name in module.connectors:
            return module.connectors[name]
    raise KeyError(name)
