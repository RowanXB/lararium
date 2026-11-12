"""Finance module: instruments, accounts and the double-entry ledger."""

from lararium.modules import ModuleSpec
from lararium.modules.finance.api import install

MODULE = ModuleSpec(name="finance", install=install)
