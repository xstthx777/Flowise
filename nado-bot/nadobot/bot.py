"""Главный цикл: экономный мейкер с контролем риска."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from .config import Config
from .exchange import Exchange
from .ledger import Ledger
from .notify import Notifier
from .strategy import QuoteParams, VolatilityGuard, compute_quote, needs_requote

log = logging.getLogger("nadobot")
SNAPSHOT_EVERY_S = 300


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ex = Exchange(cfg.mode, cfg.bot_key, cfg.owner, cfg.subaccount_name)
        self.ledger = Ledger()
        self.tg = Notifier(cfg.tg_token, cfg.tg_chat)
        self.specs = self.ex.market_specs([m.symbol for m in cfg.markets])
        self.params = {
            m.symbol: QuoteParams(m.order_size_usd, m.offset_bps, m.max_position_usd,
                                  m.hard_position_usd, m.skew_bps)
            for m in cfg.markets
        }
        self.guards = {m.symbol: VolatilityGuard(cfg.vol_window_s, cfg.vol_max_move_bps, cfg.vol_pause_s)
                       for m in cfg.markets}
        self.last_mid: dict[str, float | None] = {m.symbol: None for m in cfg.markets}
        self.day = None
        self.day_start_equity: float | None = None
        self.last_snapshot = 0.0
        self.stopped = False
        self._positions: dict = {}

    # ---------- проверки перед стартом ----------
    def preflight(self):
        acct = self.ex.account()
        if not acct["exists"]:
            raise SystemExit("Субаккаунт не найден: сначала внеси депозит на app.nado.xyz с этого MetaMask.")
        if not self.cfg.dry_run and not self.ex.is_bot_linked():
            raise SystemExit("Бот-ключ не привязан. Выполни: python -m nadobot link")
        log.info("Equity %.2f$, позиции %s", acct["equity"], acct["positions"])
        mode = "DRY-RUN (ордера не отправляются)" if self.cfg.dry_run else "LIVE"
        self.tg.send(f"Старт {self.cfg.mode} {mode}. Equity {acct['equity']:.2f}$")

    # ---------- аварийные действия ----------
    def flatten(self, reason: str):
        pids = [s.product_id for s in self.specs.values()]
        self.tg.send(f"FLATTEN: {reason}")
        self.ledger.event("flatten", reason)
        if self.cfg.dry_run:
            return
        try:
            self.ex.cancel_all(pids)
        except Exception as e:  # noqa: BLE001
            log.error("cancel_all: %s", e)
        positions = self.ex.account()["positions"]
        for pid in pids:
            if positions.get(pid, Decimal(0)) != 0:
                try:
                    self.ex.close_position(pid)
                except Exception as e:  # noqa: BLE001
                    log.error("close_position %s: %s", pid, e)

    def cancel_only(self):
        if self.cfg.dry_run:
            return
        try:
            self.ex.cancel_all([s.product_id for s in self.specs.values()])
        except Exception as e:  # noqa: BLE001
            log.error("cancel_all: %s", e)

    # ---------- риск на уровне аккаунта ----------
    def check_account(self) -> bool:
        acct = self.ex.account()
        eq = float(acct["equity"])
        now = time.time()
        today = datetime.now(timezone.utc).date()
        if self.day != today:
            self.day, self.day_start_equity = today, eq
            self.ledger.event("day", f"start equity {eq:.2f}")
        if now - self.last_snapshot > SNAPSHOT_EVERY_S:
            self.ledger.snapshot(eq)
            self.last_snapshot = now

        loss = (self.day_start_equity or eq) - eq
        if loss >= self.cfg.max_daily_loss_usd:
            self.flatten(f"дневной убыток {loss:.2f}$ ≥ лимита {self.cfg.max_daily_loss_usd}$. Бот остановлен.")
            self.stopped = True
            return False
        if acct["health_ratio"] < Decimal(str(self.cfg.min_health_ratio)):
            self.flatten(f"health ratio {float(acct['health_ratio']):.2f} ниже {self.cfg.min_health_ratio}")
            return False
        self._positions = acct["positions"]
        return True

    # ---------- котирование одного рынка ----------
    def quote_market(self, sym: str):
        spec, p = self.specs[sym], self.params[sym]
        bid, ask = self.ex.best_bid_ask(spec.product_id)
        mid = float((bid + ask) / 2)

        if self.guards[sym].update(time.time(), mid):
            if self.last_mid[sym] is not None:
                log.info("%s: высокая волатильность — снимаю ордера и жду", sym)
                if not self.cfg.dry_run:
                    self.ex.cancel_all([spec.product_id])
                self.last_mid[sym] = None
            return

        pos = self._positions.get(spec.product_id, Decimal(0))
        open_n = 0 if self.cfg.dry_run else len(self.ex.open_orders(spec.product_id))
        expected = 2 if abs(float(pos) * mid) < p.max_position_usd else 1
        if not needs_requote(self.last_mid[sym], mid, self.cfg.requote_bps) and open_n >= expected:
            return

        q = compute_quote(spec, bid, ask, pos, p)
        log.info("%s mid=%.2f pos=%s → bid %s x %s | ask %s x %s (%s)",
                 sym, mid, pos, q.bid_price, q.bid_size, q.ask_price, q.ask_size, q.reason)

        if self.cfg.dry_run:
            self.last_mid[sym] = mid
            return

        self.ex.cancel_all([spec.product_id])
        if q.close_position:
            self.tg.send(f"{sym}: {q.reason} — закрываю позицию")
            self.ex.close_position(spec.product_id)
            self.last_mid[sym] = None
            return
        if q.bid_price is not None and q.bid_size > 0:
            self._place(spec, q.bid_price, q.bid_size)
        if q.ask_price is not None and q.ask_size > 0:
            self._place(spec, q.ask_price, -q.ask_size)
        self.last_mid[sym] = mid

    def _place(self, spec, price, size):
        try:
            self.ex.place_post_only(spec, price, size, self.cfg.order_ttl_s)
        except Exception as e:  # noqa: BLE001  post-only отказ при пересечении — норма
            log.warning("%s: ордер %s @ %s не принят: %s", spec.symbol, size, price, e)

    # ---------- цикл ----------
    def run(self):
        self.preflight()
        errors = 0
        try:
            while not self.stopped:
                try:
                    if self.check_account():
                        for sym in self.specs:
                            self.quote_market(sym)
                    errors = 0
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    log.exception("Ошибка цикла (%d подряд): %s", errors, e)
                    if errors >= 5:
                        self.cancel_only()
                        self.tg.send(f"5 ошибок подряд, ордера сняты, пауза 60с: {e}")
                        time.sleep(60)
                        errors = 0
                time.sleep(self.cfg.loop_interval_s)
        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C")
        finally:
            self.cancel_only()
            self.tg.send("Бот остановлен, ордера сняты (позиции не трогал).")
