"""Логика симулятора без сети: исполнения, комиссии, фандинг, equity."""
import time
from decimal import Decimal as D

import pytest

from nadobot import paper as P
from nadobot.strategy import MarketSpec

SPEC = MarketSpec("BTC-PERP", 2, D(1), D("0.00005"), D(100))


@pytest.fixture
def ex(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "STATE", tmp_path / "state.json")
    e = object.__new__(P.PaperExchange)
    e.specs = {2: SPEC}
    e.fee_maker, e.fee_taker, e.maint = {2: D("0.0001")}, {2: D("0.00035")}, {2: D("0.01")}
    e.orders, e.fills, e.last_trade_idx = [], [], {}
    e.mid, e.funding, e.pos = {2: D(75000)}, {}, {}
    e._oid = e._fill_idx = 0
    e._last_funding_ts = time.time()
    e.start_balance = e.cash = D(200)
    e._trades = []
    e._recent_trades = lambda pid: e._trades
    e.engine_bbo = lambda pid: (D(74999), D(75001))
    return e


def test_post_only_rejects_crossing(ex):
    with pytest.raises(Exception):
        ex.place_post_only(SPEC, D(75001), D("0.0016"), 60)  # bid на уровне ask
    with pytest.raises(Exception):
        ex.place_post_only(SPEC, D(74000), D("0.001"), 60)  # 74$ < минимума 100$


def test_fill_only_when_trade_strictly_through(ex):
    ex.place_post_only(SPEC, D(74970), D("0.0016"), 60)
    ts = ex.orders[0]["ts"] + 1
    ex._trades = [(1, ts, D(74970), -1)]  # продажа ровно по нашей цене — мы в конце очереди, не исполнились
    ex._match(2, D(74980), D(74990))
    assert ex.orders and not ex.fills
    ex._trades = [(2, ts, D(74965), -1)]  # продали дешевле нашей цены → нас бы точно исполнили
    ex._match(2, D(74960), D(74975))
    assert not ex.orders and len(ex.fills) == 1
    assert ex.pos[2] == D("0.0016")
    fee = D(74970) * D("0.0016") * D("0.0001")
    assert ex.cash == D(200) - D(74970) * D("0.0016") - fee


def test_old_trades_dont_fill(ex):
    ex.place_post_only(SPEC, D(74970), D("0.0016"), 60)
    ex._trades = [(1, ex.orders[0]["ts"] - 5, D(74000), -1)]  # сделка ДО выставления ордера
    ex._match(2, D(74980), D(74990))
    assert ex.orders and not ex.fills


def test_book_crossing_fills_ask(ex):
    ex.place_post_only(SPEC, D(75030), D("-0.0016"), 60)
    ex._match(2, D(75030), D(75031))  # bid поднялся до нашего ask
    assert ex.pos[2] == D("-0.0016")


def test_round_trip_earns_spread_minus_fees(ex):
    ex._fill(2, D(74970), D("0.0016"), maker=True)
    ex._fill(2, D(75030), D("-0.0016"), maker=True)
    pnl = ex.account()["equity"] - D(200)
    gross = D(60) * D("0.0016")  # 0.096$
    fees = (D(74970) + D(75030)) * D("0.0016") * D("0.0001")  # ≈0.024$
    assert pnl == gross - fees and pnl > 0


def test_close_position_pays_taker(ex):
    ex._fill(2, D(75000), D("0.0016"), maker=True)
    ex.close_position(2)
    assert ex.pos[2] == 0 and ex.fills[-1]["fee"] == pytest.approx(float(D(74999) * D("0.0016") * D("0.00035")))


def test_funding_charges_longs(ex):
    ex.pos = {2: D("0.01")}
    ex.funding = {2: D("0.001")}  # 0.1%/сутки
    ex._last_funding_ts = time.time() - 86400
    before = ex.cash
    ex._accrue_funding()
    assert float(before - ex.cash) == pytest.approx(float(D("0.01") * D(75000) * D("0.001")), rel=1e-3)
