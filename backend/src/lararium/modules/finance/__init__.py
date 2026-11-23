"""Finance module: instruments, accounts and the double-entry ledger."""

from fastapi import FastAPI

from lararium.modules import ModuleSpec
from lararium.modules.finance import api, connections_api, transfers_api
from lararium.modules.finance.ibkr.sync import IbkrConnector
from lararium.modules.finance.monzo.sync import MonzoConnector
from lararium.modules.finance.trading212.sync import Trading212Connector


def _install(app: FastAPI) -> None:
    api.install(app)
    connections_api.install(app)
    transfers_api.install(app)


MODULE = ModuleSpec(
    name="finance",
    install=_install,
    connectors={
        "monzo": MonzoConnector(),
        "ibkr": IbkrConnector(),
        "trading212": Trading212Connector(),
    },
)
