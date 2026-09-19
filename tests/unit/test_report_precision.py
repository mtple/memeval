"""Report digits must not depend on the calling worker's decimal context."""
from decimal import Inexact, Rounded, localcontext
from fractions import Fraction
from types import SimpleNamespace

import pytest

from market_replay.domain.quantities import fraction_to_decimal_str
from market_replay.domain.status import OrderState
from market_replay.evaluation.debrief import attribution


@pytest.mark.parametrize("precision", [6, 28, 80])
def test_exact_fraction_formatting_across_contexts(precision):
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.traps[Inexact] = ctx.traps[Rounded] = True
        assert fraction_to_decimal_str(Fraction(10**78 - 1), 18) == "9" * 78 + "." + "0" * 18
        assert fraction_to_decimal_str(Fraction(10**100 - 1, 10**100), 18) == "0." + "9" * 18
        assert fraction_to_decimal_str(Fraction(-1, 3), 8) == "-0.33333333"
        assert fraction_to_decimal_str(Fraction(-1, 10**100), 2) == "-0.00"
        assert fraction_to_decimal_str(Fraction(9, 2), 0) == "4"
        assert ctx.prec == precision


def test_fifo_receipt_amounts_in_default_worker_context():
    # First confirmed round trip from FreeTurtle's credential-screened receipts.
    def order(order_id, asset_in, asset_out, amount_in, amount_out, confirmed):
        return SimpleNamespace(order_id=order_id, state=OrderState.CONFIRMED,
                               asset_in=asset_in, asset_out=asset_out, amount_in=amount_in,
                               amount_out=amount_out, gas_charged=784982759347,
                               confirm_time_ms=confirmed)
    session = SimpleNamespace(pack=SimpleNamespace(numeraire="NATIVE"),
                              alias=SimpleNamespace(asset=lambda asset: asset),
                              sim=SimpleNamespace(orders={
                                  "buy": order("buy", "NATIVE", "asset_test", 6001343388599709, 41101042446868066, 3606000),
                                  "sell": order("sell", "asset_test", "NATIVE", 41101042446868066, 5682008908882070, 5082000),
                              }))
    with localcontext() as ctx:
        ctx.prec = 28
        report = attribution(session)
    expected = 5682008908882070 - 6001343388599709 - 2 * 784982759347
    assert report["sales"][0]["contribution_raw"] == f"{expected}.000000000000000000"
