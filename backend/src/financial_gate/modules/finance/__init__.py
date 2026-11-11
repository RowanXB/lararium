"""Finance module: instruments, accounts and the double-entry ledger."""

from financial_gate.modules import ModuleSpec
from financial_gate.modules.finance.api import install

MODULE = ModuleSpec(name="finance", install=install)
