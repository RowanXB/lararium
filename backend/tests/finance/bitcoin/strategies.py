"""Random chain transactions over the wallet's and strangers' addresses (test_properties.py).

A transaction spends one to three inputs, each the wallet's or a stranger's, and pays one to
four outputs: the wallet's, a stranger's, or no address (with a value, or none: OP_RETURN).
Its inputs add up to its outputs plus a fee, so any mix of own and stranger inputs appears:
receipts, spends and shared transactions. Some wait in the mempool.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hypothesis import strategies as st

from .fake_esplora import TIP, make_tx


@dataclass(frozen=True)
class ChainTx:
    n: int
    # (address, value in satoshis)
    inputs: tuple[tuple[str, int], ...]
    outputs: tuple[tuple[str | None, int], ...]
    fee: int
    height: int | None  # None: in the mempool

    def build(self) -> dict[str, Any]:
        return make_tx(
            self.n, list(self.outputs), spends=list(self.inputs), height=self.height, fee=self.fee
        )

    def change(self, own: Sequence[str]) -> int:
        """The wallet's outputs less its inputs, in satoshis."""
        paid = sum(value for address, value in self.outputs if address in own)
        spent = sum(value for address, value in self.inputs if address in own)
        return paid - spent


@st.composite
def chain_txs(draw: st.DrawFn, n: int, own: Sequence[str], strangers: Sequence[str]) -> ChainTx:
    addresses = [*own, *strangers]
    outputs = draw(
        st.lists(
            st.tuples(
                st.one_of(st.sampled_from(addresses), st.none()),
                st.one_of(st.just(0), st.integers(1, 10**7)),
            ),
            min_size=1,
            max_size=4,
        )
    )
    fee = draw(st.integers(0, 5_000))
    total = max(sum(value for _, value in outputs) + fee, 1)
    fee = total - sum(value for _, value in outputs)
    spenders = draw(st.lists(st.sampled_from(addresses), min_size=1, max_size=3))
    weights = draw(st.lists(st.integers(1, 5), min_size=len(spenders), max_size=len(spenders)))
    values = [total * w // sum(weights) for w in weights]
    values[0] += total - sum(values)
    height = draw(st.one_of(st.none(), st.integers(TIP - 300, TIP - 10)))
    return ChainTx(n, tuple(zip(spenders, values, strict=True)), tuple(outputs), fee, height)


@st.composite
def chains(draw: st.DrawFn, own: Sequence[str], strangers: Sequence[str]) -> list[ChainTx]:
    count = draw(st.integers(1, 6))
    return [draw(chain_txs(n + 1, own, strangers), label=f"tx {n + 1}") for n in range(count)]
