"""Тестовый режим (paper trading): живые данные Nado, виртуальные деньги.

Кошелёк не нужен, ордера на биржу не отправляются. Бот работает по тому же
алгоритму, а этот класс имитирует биржу:

- цены, стакан, фандинг и правила рынков (тик, шаг, минимум $100) берутся с Nado;
- ордер «исполняется», только если по рынку прошла реальная сделка СТРОГО ЛУЧШЕ
  нашей цены (или стакан перешёл через нашу цену). Это консервативно: в реальности
  исполнений будет столько же или больше, а здесь мы всегда в конце очереди;
- комиссии — по ставкам Nado для новичка (maker 1 б.п., taker 3.5 б.п.);
- фандинг начисляется непрерывно по текущей 24ч ставке.
"""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal

from eth_account import Account

from nado_protocol.client import NadoClientMode, create_nado_client

from .config import ROOT
from .exchange import from_x18
from .strategy import MarketSpec

log = logging.getLogger("nadobot.paper")
STATE = ROOT / "data" / "paper_state.json"
ZERO = Decimal(0)


class PaperExchange:
    is_paper = True

    def __init__(self, mode: str = "mainnet", balance: float = 200.0, reset: bool = False,
                 feed=None, client=None, state_path=None, symbols=None):
        from .feed import MarketFeed

        # Случайный ключ только чтобы создать клиент: им ничего не подписывается
        self.client = client or create_nado_client(NadoClientMode(mode), Account.create())
        self.engine = self.client.context.engine_client
        self.indexer = self.client.context.indexer_client
        self.feed = feed or MarketFeed(self.client, [])
        self._symbols = symbols  # кэш symbols для сканера (чтобы не дёргать API на каждый экземпляр)
        self.state_path = STATE if state_path is None else state_path
        self.owner = "paper"
        self.bot = Account.create()
        self.specs: dict[int, MarketSpec] = {}
        self.fee_maker: dict[int, Decimal] = {}
        self.fee_taker: dict[int, Decimal] = {}
        self.maint: dict[int, Decimal] = {}
        self.orders: list[dict] = []  # {id, pid, price, size(+buy/-sell), ts}
        self.fills: list[dict] = []
        self.last_trade_idx: dict[int, int] = {}
        self.mid: dict[int, Decimal] = {}
        self.funding: dict[int, Decimal] = {}
        self._oid = 0
        self._fill_idx = 0
        self._last_funding_ts = time.time()
        st = {} if reset else self._load()
        self.start_balance = Decimal(str(st.get("start_balance", balance)))
        self.cash = Decimal(str(st.get("cash", balance)))
        self.pos: dict[int, Decimal] = {int(k): Decimal(v) for k, v in st.get("pos", {}).items()}
        self._save()

    # ---------- состояние ----------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save(self):
        if self.state_path is False:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "start_balance": str(self.start_balance), "cash": str(self.cash),
            "pos": {str(k): str(v) for k, v in self.pos.items() if v != 0},
        }), encoding="utf-8")

    # ---------- рынки (живые данные) ----------
    def market_specs(self, symbols: list[str]) -> dict[str, MarketSpec]:
        data = self._symbols or self.engine.get_symbols(product_type="perp").symbols
        out = {}
        for sym in symbols:
            s = data.get(sym)
            if s is None:
                raise ValueError(f"рынок {sym} не найден. Доступны: {', '.join(sorted(data))}")
            pid = int(s.product_id)
            spec = MarketSpec(sym, pid, from_x18(s.price_increment_x18), from_x18(s.size_increment),
                              from_x18(s.min_size))
            self.specs[pid] = spec
            self.fee_maker[pid] = from_x18(s.maker_fee_rate_x18)
            self.fee_taker[pid] = from_x18(s.taker_fee_rate_x18)
            self.maint[pid] = 1 - from_x18(s.long_weight_maintenance_x18)
            out[sym] = spec
        self.feed.add(list(self.specs))
        return out

    def best_bid_ask(self, product_id: int) -> tuple[Decimal, Decimal]:
        self.feed.refresh()
        bid, ask = self.feed.bbo(product_id)
        if bid > 0 and ask > 0:
            self.mid[product_id] = (bid + ask) / 2
        self._accrue_funding()
        self._match(product_id, bid, ask)
        return bid, ask

    def funding_rate_daily(self, product_id: int) -> Decimal:
        fr = self.feed.funding(product_id)
        if fr is None:
            fr = from_x18(self.indexer.get_perp_funding_rate(product_id).funding_rate_x18)
        self.funding[product_id] = fr
        return fr

    def protocol_volume_24h(self) -> float:
        t = self.indexer.get_tickers()
        return sum(float(v.get("quote_volume") or 0) for v in t.values())

    # ---------- имитация исполнения ----------
    def _recent_trades(self, pid: int) -> list[tuple[int, int, Decimal, int]]:
        """(submission_idx, ts, цена, сторона тейкера: +1 покупка, −1 продажа) новых сделок рынка."""
        last = self.last_trade_idx.get(pid)
        trades = self.feed.trades(pid)
        out = [(t.idx, t.ts, t.price, t.side) for t in trades if last is None or t.idx > last]
        if trades:
            self.last_trade_idx[pid] = max(t.idx for t in trades)
        return out

    def _match(self, pid: int, bid: Decimal, ask: Decimal):
        mine = [o for o in self.orders if o["pid"] == pid]
        if not mine:
            # всё равно двигаем курсор сделок, чтобы не засчитать старые
            if pid not in self.last_trade_idx:
                try:
                    self._recent_trades(pid)
                except Exception:  # noqa: BLE001
                    pass
            return
        try:
            trades = self._recent_trades(pid)
        except Exception as e:  # noqa: BLE001
            log.debug("trades: %s", e)
            trades = []
        for o in list(mine):
            hit = False
            if o["size"] > 0:  # наш bid: продавец прошёл НИЖЕ нашей цены, или ask опустился до неё
                hit = (ask > 0 and ask <= o["price"]) or any(
                    s < 0 and px < o["price"] and ts > o["ts"] for _, ts, px, s in trades)
            else:  # наш ask
                hit = (bid > 0 and bid >= o["price"]) or any(
                    s > 0 and px > o["price"] and ts > o["ts"] for _, ts, px, s in trades)
            if hit:
                self.orders.remove(o)
                self._fill(pid, o["price"], o["size"], maker=True)

    def _fill(self, pid: int, price: Decimal, size: Decimal, maker: bool):
        notional = price * abs(size)
        fee = notional * (self.fee_maker[pid] if maker else self.fee_taker[pid])
        self.cash -= price * size + fee
        self.pos[pid] = self.pos.get(pid, ZERO) + size
        self._fill_idx += 1
        self.fills.append({"idx": self._fill_idx, "digest": f"paper-{self._fill_idx}", "ts": time.time(),
                           "product_id": pid, "base": float(size), "quote": float(-price * size),
                           "fee": float(fee)})
        sym = self.specs[pid].symbol if pid in self.specs else pid
        log.info("🧪 ИСПОЛНЕНИЕ %s %s %s @ %s (%s, комиссия %.4f$)", sym, "BUY" if size > 0 else "SELL",
                 abs(size), price, "maker" if maker else "taker", fee)
        self._save()

    def _accrue_funding(self):
        now = time.time()
        dt = now - self._last_funding_ts
        self._last_funding_ts = now
        for pid, amt in self.pos.items():
            if amt == 0 or pid not in self.mid:
                continue
            rate = self.funding.get(pid, ZERO)
            # rate > 0: лонги платят шортам
            self.cash -= amt * self.mid[pid] * rate * Decimal(str(dt / 86400))

    # ---------- аккаунт ----------
    def account(self) -> dict:
        upnl_value = sum((amt * self.mid.get(pid, ZERO) for pid, amt in self.pos.items()), ZERO)
        equity = self.cash + upnl_value
        maint_req = sum((abs(amt) * self.mid.get(pid, ZERO) * self.maint.get(pid, Decimal("0.01"))
                         for pid, amt in self.pos.items()), ZERO)
        maint = equity - maint_req
        return {"exists": True, "equity": equity, "maint_health": maint,
                "health_ratio": (maint / equity) if equity > 0 else ZERO,
                "positions": dict(self.pos)}

    def is_bot_linked(self) -> bool:
        return True

    def linked_signer(self) -> str:
        return "paper"

    def fee_rates(self):
        return None

    # ---------- ордера ----------
    def place_post_only(self, spec: MarketSpec, price: Decimal, size: Decimal, ttl_s: int):
        bid, ask = self.engine_bbo(spec.product_id)
        if (size > 0 and ask > 0 and price >= ask) or (size < 0 and bid > 0 and price <= bid):
            raise Exception("post-only: ордер пересёк бы стакан")  # как на настоящей бирже
        if abs(size) * price < spec.min_size:
            raise Exception(f"меньше минимума {spec.min_size}$")
        self._oid += 1
        self.orders.append({"id": self._oid, "pid": spec.product_id, "price": price, "size": size,
                            "ts": int(time.time())})

    def engine_bbo(self, pid: int):
        return self.feed.bbo(pid)

    def cancel_all(self, product_ids: list[int]):
        self.orders = [o for o in self.orders if o["pid"] not in product_ids]

    def open_orders(self, product_id: int):
        return [o for o in self.orders if o["pid"] == product_id]

    def close_position(self, product_id: int):
        amt = self.pos.get(product_id, ZERO)
        if amt == 0:
            return
        bid, ask = self.engine_bbo(product_id)
        price = bid if amt > 0 else ask  # рыночное закрытие по лучшей цене стакана
        self.orders = [o for o in self.orders if o["pid"] != product_id]
        self._fill(product_id, price, -amt, maker=False)

    def recent_fills(self, limit: int = 100) -> list[dict]:
        return self.fills[-limit:]

    def reset(self, balance: float):
        self.start_balance = self.cash = Decimal(str(balance))
        self.pos, self.orders, self.fills = {}, [], []
        self._save()


PAPER_DB = ROOT / "data" / "paper.sqlite"


def make_paper(cfg, reset: bool = False):
    """(PaperExchange, Ledger) для тестового режима — отдельная база, не смешивается с реальной."""
    from .ledger import Ledger

    if reset:
        for f in (PAPER_DB, STATE):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
    return PaperExchange(cfg.mode, cfg.paper_balance, reset=reset), Ledger(PAPER_DB)
