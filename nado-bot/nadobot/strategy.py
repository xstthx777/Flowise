"""Чистая логика котирования (без сети) — легко тестировать.

Все цены и размеры здесь в «человеческих» единицах (float/Decimal),
перевод в x18 делает exchange.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Optional


@dataclass
class MarketSpec:
    symbol: str
    product_id: int
    price_increment: Decimal  # шаг цены
    size_increment: Decimal  # шаг размера
    min_size: Decimal  # минимальный размер ордера (в базовом активе)


@dataclass
class QuoteParams:
    order_size_usd: float  # размер одной котировки в $
    offset_bps: float  # отступ от mid в б.п. (1 б.п. = 0.01%)
    max_position_usd: float  # мягкий лимит позиции: дальше котируем только на сокращение
    hard_position_usd: float  # жёсткий лимит: закрываем позицию
    skew_bps: float  # насколько сдвигать котировки при полном инвентаре


@dataclass
class Quote:
    bid_price: Optional[Decimal]
    bid_size: Decimal
    ask_price: Optional[Decimal]
    ask_size: Decimal
    close_position: bool = False
    reason: str = ""


def round_down(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_up(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_CEILING) * step


def compute_quote(
    spec: MarketSpec,
    best_bid: Decimal,
    best_ask: Decimal,
    position: Decimal,
    p: QuoteParams,
) -> Quote:
    """Считает пару post-only котировок вокруг mid со сдвигом по инвентарю.

    position — текущая позиция в базовом активе (+ лонг, − шорт).
    """
    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        return Quote(None, Decimal(0), None, Decimal(0), reason="пустой/кривой стакан")

    mid = (best_bid + best_ask) / 2
    pos_usd = float(position * mid)

    if abs(pos_usd) >= p.hard_position_usd:
        return Quote(None, Decimal(0), None, Decimal(0), close_position=True,
                     reason=f"позиция {pos_usd:.0f}$ ≥ жёсткого лимита")

    # Инвентарь от -1 до 1: в лонге сдвигаем обе котировки вниз (охотнее продаём)
    inv = max(-1.0, min(1.0, pos_usd / p.max_position_usd)) if p.max_position_usd > 0 else 0.0
    shift = Decimal(str(-inv * p.skew_bps / 1e4))
    off = Decimal(str(p.offset_bps / 1e4))

    bid_px = round_down(mid * (1 - off + shift), spec.price_increment)
    ask_px = round_up(mid * (1 + off + shift), spec.price_increment)

    # post-only: не пересекаем стакан, иначе ордер отклонят
    if bid_px >= best_ask:
        bid_px = best_ask - spec.price_increment
    if ask_px <= best_bid:
        ask_px = best_bid + spec.price_increment

    size = round_down(Decimal(str(p.order_size_usd)) / mid, spec.size_increment)
    size = max(size, spec.min_size)

    bid_size, ask_size = size, size
    reason = "ok"
    if pos_usd >= p.max_position_usd:  # слишком длинные — только продаём
        bid_px, bid_size, reason = None, Decimal(0), "лимит лонга: только ask"
    elif pos_usd <= -p.max_position_usd:
        ask_px, ask_size, reason = None, Decimal(0), "лимит шорта: только bid"

    return Quote(bid_px, bid_size, ask_px, ask_size, reason=reason)


class VolatilityGuard:
    """Пауза, если цена за окно сходила дальше порога."""

    def __init__(self, window_s: float, max_move_bps: float, pause_s: float):
        self.window_s = window_s
        self.max_move_bps = max_move_bps
        self.pause_s = pause_s
        self._hist: list[tuple[float, float]] = []
        self.paused_until = 0.0

    def update(self, ts: float, mid: float) -> bool:
        """Возвращает True, если сейчас нужно стоять в стороне."""
        self._hist.append((ts, mid))
        self._hist = [(t, m) for t, m in self._hist if ts - t <= self.window_s]
        lo = min(m for _, m in self._hist)
        hi = max(m for _, m in self._hist)
        if lo > 0 and (hi - lo) / lo * 1e4 > self.max_move_bps:
            self.paused_until = ts + self.pause_s
        return ts < self.paused_until


def needs_requote(last_mid: Optional[float], mid: float, requote_bps: float) -> bool:
    if last_mid is None:
        return True
    return abs(mid - last_mid) / last_mid * 1e4 >= requote_bps
