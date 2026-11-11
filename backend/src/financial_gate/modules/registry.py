"""The modules the application installs, in order."""

from financial_gate.modules import ModuleSpec, finance

__all__ = ["MODULES", "ModuleSpec"]

MODULES: tuple[ModuleSpec, ...] = (finance.MODULE,)
