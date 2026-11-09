"""Общий поток рыночных данных: одним запросом цены всех рынков, одним — их сделки.

Экономит лимиты Nado (2400 «весов»/мин на IP): вместо запроса на каждый рынок
каждый цикл — 2 запроса на все рынки сразу. Используется и реальным ботом,
и тестовым режимом, и сканером.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from decimal import Decimal

from nado_protocol.indexer_client.types.query import IndexerMatchesParams

from .exchange import from_x18

log = logging.getLogger("nadobot.feed")
KEEP_S = 180  # сколько секунд истории сделок держать
FUNDING_REFRESH_S = 600


class Trade(tuple):
    """(idx, ts, price, side, notional): side +1 — тейкер купил, −1 — тейкер продал."""

    __slots__ = ()

    def __new__(cls, idx, ts, price, side, notional):
        return super().__new__(cls, (idx, ts, price, side, notional))

    idx = property(lambda s: s[0])
    ts = property(lambda s: s[1])
    price = property(lambda s: s[2])
    side = property(lambda s: s[3])
    notional = property(lambda s: s[4])


class MarketFeed:
    def __init__(self, client, product_ids: list[int], min_interval_s: float = 0.8):
        self.engine = client.context.engine_client
        self.indexer = client.context.indexer_client
        self.pids = sorted(set(product_ids))
        self.min_interval_s = min_interval_s
        self._bbo: dict[int, tuple[Decimal, Decimal]] = {}
        self._trades: dict[int, deque] = {pid: deque() for pid in self.pids}
        self._last_idx = 0
        self._funding: dict[int, Decimal] = {}
        self._funding_ts = 0.0
        self.last_refresh = 0.0

    def add(self, product_ids: list[int]):
        for pid in product_ids:
            if pid not in self._trades:
                self.pids.append(pid)
                self._trades[pid] = deque()
        self.pids.sort()

    def refresh(self, force: bool = False):
        now = time.time()
        if not force and now - self.last_refresh < self.min_interval_s:
            return
        self.last_refresh = now
        mp = self.engine.get_market_prices(self.pids)  # вес = число рынков
        for p in mp.market_prices:
            self._bbo[int(p.product_id)] = (from_x18(p.bid_x18), from_x18(p.ask_x18))
        try:
            self._pull_trades()
        except Exception as e:  # noqa: BLE001
            log.debug("trades: %s", e)
        if now - self._funding_ts > FUNDING_REFRESH_S:
            try:
                fr = self.indexer.get_perp_funding_rates(self.pids)
                for k, v in fr.items():
                    self._funding[int(k)] = from_x18(v.funding_rate_x18)
                self._funding_ts = now
            except Exception as e:  # noqa: BLE001
                log.debug("funding: %s", e)

    def _pull_trades(self):
        limit = min(500, max(100, 25 * len(self.pids)))  # вес запроса = 2 + limit/10
        res = self.indexer.get_matches(IndexerMatchesParams(product_ids=self.pids, limit=limit))
        new = []
        for t in res.txs:
            idx = int(t.submission_idx)
            if idx <= self._last_idx or not t.timestamp:
                continue
            tx = t.tx.model_dump() if hasattr(t.tx, "model_dump") else t.tx
            mo = tx.get("match_orders") if isinstance(tx, dict) else None
            if not mo:
                continue
            pid = int(mo["product_id"])
            price = from_x18(mo["maker"]["order"]["price_x18"])
            taker_amt = from_x18(mo["taker"]["order"]["amount"])
            # объём сделки берём из match (base_filled) по тому же submission_idx
            new.append((idx, pid, int(t.timestamp), price, 1 if taker_amt > 0 else -1))
        size_by_idx: dict[int, Decimal] = {}
        for m in res.matches:
            size_by_idx[int(m.submission_idx)] = max(size_by_idx.get(int(m.submission_idx), Decimal(0)),
                                                     abs(from_x18(m.base_filled)))
        for idx, pid, ts, price, side in sorted(new):
            if pid in self._trades:
                notional = price * size_by_idx.get(idx, Decimal(0))
                self._trades[pid].append(Trade(idx, ts, price, side, notional))
        if res.txs:
            self._last_idx = max(self._last_idx, max(int(t.submission_idx) for t in res.txs))
        cutoff = time.time() - KEEP_S
        for dq in self._trades.values():
            while dq and dq[0].ts < cutoff:
                dq.popleft()

    # ---------- чтение ----------
    def bbo(self, pid: int) -> tuple[Decimal, Decimal]:
        return self._bbo.get(pid, (Decimal(0), Decimal(0)))

    def trades(self, pid: int, since_ts: float = 0) -> list[Trade]:
        return [t for t in self._trades.get(pid, ()) if t.ts >= since_ts]

    def funding(self, pid: int) -> Decimal | None:
        return self._funding.get(pid)
