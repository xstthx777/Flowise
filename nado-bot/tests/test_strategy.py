from decimal import Decimal as D

from nadobot.strategy import MarketSpec, QuoteParams, VolatilityGuard, compute_quote, needs_requote

# Реальные параметры BTC-PERP на Nado (ноябрь 2026): тик $1, шаг 0.00005 BTC, минимум $100
SPEC = MarketSpec("BTC-PERP", 2, D("1"), D("0.00005"), D("100"))
P = QuoteParams(order_size_usd=120, offset_bps=5, max_position_usd=300, hard_position_usd=600, skew_bps=4)
BID, ASK = D(99999), D(100001)


def notional(px, sz):
    return px * sz


def test_flat_symmetric_quotes():
    q = compute_quote(SPEC, BID, ASK, D(0), P)
    assert q.bid_price == D(99950) and q.ask_price == D(100050)
    assert q.bid_size == q.ask_size == D("0.0012")
    assert q.bid_price < ASK and q.ask_price > BID


def test_min_size_is_notional_not_base():
    """Регрессия: min_size=100 — это $100, а не 100 BTC."""
    small = QuoteParams(50, 5, 300, 600, 4)  # просим $50 — меньше минимума биржи
    q = compute_quote(SPEC, BID, ASK, D(0), small)
    assert q.bid_size < D("0.01")  # ни в коем случае не 100 BTC
    assert notional(q.bid_price, q.bid_size) >= D(100)
    assert notional(q.ask_price, q.ask_size) >= D(100)
    assert (q.bid_size / SPEC.size_increment) % 1 == 0


def test_long_inventory_skews_down():
    flat = compute_quote(SPEC, BID, ASK, D(0), P)
    longq = compute_quote(SPEC, BID, ASK, D("0.002"), P)  # 200$ лонг
    assert longq.bid_price < flat.bid_price and longq.ask_price < flat.ask_price


def test_soft_limit_only_reduces():
    q = compute_quote(SPEC, BID, ASK, D("0.004"), P)  # 400$ ≥ 300
    assert q.bid_price is None and q.ask_price is not None
    q = compute_quote(SPEC, BID, ASK, D("-0.004"), P)
    assert q.ask_price is None and q.bid_price is not None


def test_hard_limit_closes():
    assert compute_quote(SPEC, BID, ASK, D("0.007"), P).close_position


def test_never_crosses_book():
    tight = QuoteParams(120, 0, 300, 600, 0)
    q = compute_quote(SPEC, D(100000), D(100001), D(0), tight)
    assert q.bid_price < D(100001) and q.ask_price > D(100000)


def test_bad_book():
    q = compute_quote(SPEC, D(0), D(0), D(0), P)
    assert q.bid_price is None and q.ask_price is None and not q.close_position


def test_funding_blocks_new_longs_when_flat():
    q = compute_quote(SPEC, BID, ASK, D(0), P, allow_long=False)
    assert q.bid_price is None and q.ask_price is not None


def test_funding_allows_closing_short_only_up_to_position():
    q = compute_quote(SPEC, BID, ASK, D("-0.002"), P, allow_long=False)
    assert q.bid_price is not None and q.bid_size <= D("0.002")
    q = compute_quote(SPEC, BID, ASK, D("-0.0005"), P, allow_long=False)  # шорт $50 < минимума
    assert q.bid_price is None


def test_funding_blocks_new_shorts():
    q = compute_quote(SPEC, BID, ASK, D(0), P, allow_short=False)
    assert q.ask_price is None and q.bid_price is not None


def test_eth_like_market():
    eth = MarketSpec("ETH-PERP", 4, D("0.01"), D("0.001"), D("100"))
    q = compute_quote(eth, D("3000.00"), D("3000.01"), D(0), P)
    assert notional(q.bid_price, q.bid_size) >= D(100)
    assert q.bid_size == D("0.039")  # 120$/3000 = 0.03999 → вниз до шага


def test_vol_guard_and_requote():
    g = VolatilityGuard(60, 40, 120)
    assert not g.update(0, 100.0)
    assert g.update(10, 100.5)  # 50 б.п. за 10 с → пауза
    assert g.update(100, 100.5)
    assert not g.update(200, 100.5)
    assert needs_requote(None, 1, 2) and needs_requote(100, 100.03, 2) and not needs_requote(100, 100.01, 2)
