from decimal import Decimal as D

from nadobot.strategy import MarketSpec, QuoteParams, VolatilityGuard, compute_quote, needs_requote

SPEC = MarketSpec("BTC-PERP", 2, D("1"), D("0.001"), D("0.001"))
P = QuoteParams(order_size_usd=100, offset_bps=5, max_position_usd=300, hard_position_usd=600, skew_bps=4)


def test_flat_symmetric_quotes():
    q = compute_quote(SPEC, D(99999), D(100001), D(0), P)
    assert q.bid_price == D(99950) and q.ask_price == D(100050)
    assert q.bid_size == q.ask_size == D("0.001")
    assert q.bid_price < D(100001) and q.ask_price > D(99999)


def test_long_inventory_skews_down():
    flat = compute_quote(SPEC, D(99999), D(100001), D(0), P)
    longq = compute_quote(SPEC, D(99999), D(100001), D("0.002"), P)  # 200$ лонг
    assert longq.bid_price < flat.bid_price and longq.ask_price < flat.ask_price


def test_soft_limit_only_reduces():
    q = compute_quote(SPEC, D(99999), D(100001), D("0.004"), P)  # 400$ ≥ 300
    assert q.bid_price is None and q.ask_price is not None
    q = compute_quote(SPEC, D(99999), D(100001), D("-0.004"), P)
    assert q.ask_price is None and q.bid_price is not None


def test_hard_limit_closes():
    assert compute_quote(SPEC, D(99999), D(100001), D("0.007"), P).close_position


def test_never_crosses_book():
    tight = QuoteParams(100, 0, 300, 600, 0)
    q = compute_quote(SPEC, D(100000), D(100001), D(0), tight)
    assert q.bid_price < D(100001) and q.ask_price > D(100000)


def test_bad_book():
    q = compute_quote(SPEC, D(0), D(0), D(0), P)
    assert q.bid_price is None and q.ask_price is None and not q.close_position


def test_vol_guard_and_requote():
    g = VolatilityGuard(60, 40, 120)
    assert not g.update(0, 100.0)
    assert g.update(10, 100.5)  # 50 б.п. за 10 с → пауза
    assert g.update(100, 100.5)
    assert not g.update(200, 100.5)
    assert needs_requote(None, 1, 2) and needs_requote(100, 100.03, 2) and not needs_requote(100, 100.01, 2)
