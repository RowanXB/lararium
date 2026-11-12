"""The modules the application installs, in order."""

from lararium.modules import ModuleSpec, finance

__all__ = ["MODULES", "ModuleSpec"]

MODULES: tuple[ModuleSpec, ...] = (finance.MODULE,)
