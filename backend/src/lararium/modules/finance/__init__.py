"""Finance module: instruments, accounts and the double-entry ledger."""

from fastapi import FastAPI

from lararium.modules import ModuleSpec
from lararium.modules.finance import api, connections_api
from lararium.modules.finance.monzo.sync import MonzoConnector


def _install(app: FastAPI) -> None:
    api.install(app)
    connections_api.install(app)


MODULE = ModuleSpec(name="finance", install=_install, connectors={"monzo": MonzoConnector()})
