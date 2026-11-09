"""Главный цикл: экономный мейкер с контролем риска."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from .config import Config
from .exchange import Exchange
from .ledger import Ledger
from .notify import Notifier
from .strategy import FlowFilter, FlowParams, QuoteParams, VolatilityGuard, compute_quote, needs_requote

log = logging.getLogger("nadobot")
SNAPSHOT_EVERY_S = 300
FUNDING_REFRESH_S = 600
REJECTS_BEFORE_PAUSE = 3


class OrderBudget:
    """Ограничитель частоты executes: лимит Nado 10/с и 600/мин на кошелёк — держимся ниже."""

    def __init__(self, per_10s: int = 60, per_min: int = 450):
        self.per_10s, self.per_min = per_10s, per_min
        self.ts: list[float] = []

    def take(self, n: int) -> bool:
        now = time.time()
        self.ts = [t for t in self.ts if now - t < 60]
        last10 = sum(1 for t in self.ts if now - t < 10)
        if last10 + n > self.per_10s or len(self.ts) + n > self.per_min:
            return False
        self.ts.extend([now] * n)
        return True


class Bot:
    def __init__(self, cfg: Config, tg: Notifier | None = None, telegram_control: bool = True,
                 ex=None, ledger: Ledger | None = None):
        """telegram_control=False — командами управляет внешний Telegram-сервис (nado tg)."""
        self.cfg = cfg
        self.telegram_control = telegram_control
        self.paper = bool(getattr(ex, "is_paper", False))
        if self.paper:
            cfg.dry_run = False  # в тесте «ордера» идут в симулятор, а не на биржу
        if not self.paper and cfg.mode == "mainnet" and not cfg.dry_run and not cfg.mainnet_live_confirm:
            raise SystemExit(
                "mainnet + dry_run: false требует mainnet_live_confirm: true в config.yaml.\n"
                "Сначала прогоняй бота на testnet и в dry_run."
            )
        self.ex = ex or Exchange(cfg.mode, cfg.bot_key, cfg.owner, cfg.subaccount_name)
        self.ledger = ledger or Ledger()
        self.tg = tg or Notifier(cfg.tg_token, cfg.tg_chat)
        self.specs = self.ex.market_specs([m.symbol for m in cfg.markets])
        self.params = {
            m.symbol: QuoteParams(m.order_size_usd, m.offset_bps, m.max_position_usd,
                                  m.hard_position_usd, m.skew_bps)
            for m in cfg.markets
        }
        for m in cfg.markets:
            spec = self.specs[m.symbol]
            if m.order_size_usd < float(spec.min_size):
                log.warning("%s: order_size_usd=%s меньше минимума биржи %s$ — буду ставить по %s$",
                            m.symbol, m.order_size_usd, spec.min_size, spec.min_size)
            if m.max_position_usd < float(spec.min_size):
                raise SystemExit(f"{m.symbol}: max_position_usd должен быть ≥ минимального ордера {spec.min_size}$")
        self.guards = {m.symbol: VolatilityGuard(cfg.vol_window_s, cfg.vol_max_move_bps, cfg.vol_pause_s)
                       for m in cfg.markets}
        self.last_mid: dict[str, float | None] = {m.symbol: None for m in cfg.markets}
        fp = FlowParams(cfg.flow_window_s, cfg.flow_imbalance, cfg.flow_min_notional_usd,
                        cfg.momentum_window_s, cfg.momentum_bps)
        self.flows = {m.symbol: FlowFilter(fp) for m in cfg.markets}
        self.last_sides: dict[str, tuple] = {m.symbol: () for m in cfg.markets}
        self.budget = OrderBudget()
        self.flow_blocks = 0
        self.rejects: dict[str, int] = {m.symbol: 0 for m in cfg.markets}
        self.paused_until: dict[str, float] = {m.symbol: 0.0 for m in cfg.markets}
        self.funding: dict[str, Decimal] = {}
        self.funding_ts = 0.0
        self.fills_ts = 0.0
        self.day = None
        self.day_start_equity: float | None = None
        self.last_snapshot = 0.0
        self.stopped = False
        self.user_paused = False
        self._positions: dict = {}
        self._last_equity: float | None = None
        self._lock = threading.Lock()  # Telegram-поток vs главный цикл

    # ---------- проверки перед стартом ----------
    def preflight(self):
        acct = self.ex.account()
        if not acct["exists"]:
            raise SystemExit("Субаккаунт не найден: сначала внеси депозит на app.nado.xyz с этого MetaMask.")
        if not self.cfg.dry_run and not self.ex.is_bot_linked():
            raise SystemExit("Бот-ключ не привязан. Выполни: nado link")
        pos = {k: str(v) for k, v in acct["positions"].items() if v != 0}
        # Бот считает ВСЕ позиции на своих рынках своими. Чужую крупную позицию не закрываем молча.
        for sym, spec in self.specs.items():
            amt = acct["positions"].get(spec.product_id, Decimal(0))
            if amt == 0:
                continue
            bid, ask = self.ex.best_bid_ask(spec.product_id)
            usd = float(amt * (bid + ask) / 2)
            if abs(usd) >= self.params[sym].hard_position_usd:
                raise SystemExit(
                    f"На {sym} уже открыта позиция {usd:,.0f}$ — больше hard_position_usd. "
                    f"Бот бы её закрыл. Закрой вручную, убери рынок из config.yaml "
                    f"или используй отдельный субаккаунт (subaccount_name)."
                )
            log.warning("%s: уже есть позиция %.0f$ — бот будет считать её своей и сокращать", sym, usd)
        log.info("Equity %.2f$, позиции %s", acct["equity"], pos or "нет")
        mode = ("🧪 ТЕСТ без кошелька (виртуальные деньги, живые цены)" if self.paper
                else "DRY-RUN (ордера не отправляются)" if self.cfg.dry_run else "LIVE")
        self.tg.send(f"Старт {self.cfg.mode} {mode}. Equity {acct['equity']:.2f}$. "
                     f"Рынки: {', '.join(self.specs)}")
        if self.telegram_control:
            self._setup_telegram()

    def snapshot_now(self):
        try:
            self.ledger.snapshot(float(self.ex.account()["equity"]))
            self.last_snapshot = time.time()
        except Exception as e:  # noqa: BLE001
            log.debug("snapshot: %s", e)

    def report_text(self) -> str:
        self.snapshot_now()
        vol = None
        if hasattr(self.ex, "protocol_volume_24h"):
            try:
                vol = self.ex.protocol_volume_24h()
            except Exception:  # noqa: BLE001
                vol = None
        return self.ledger.report(protocol_volume_24h=vol)["text"]

    def status_text(self) -> str:
        eq = self._last_equity
        pos = {s: str(self._positions.get(sp.product_id, 0)) for s, sp in self.specs.items()}
        st = "ПАУЗА" if self.user_paused else ("остановлен" if self.stopped else "работает")
        fr = {s: f"{float(v) * 100:.3f}%/сут" for s, v in self.funding.items()}
        if eq is None:
            return st
        return f"{st}. Equity {eq:.2f}$\nПозиции: {pos}\nФандинг: {fr}"

    def _setup_telegram(self):
        status = self.status_text

        def pause():
            with self._lock:
                self.user_paused = True
                self.cancel_only()
            return "Пауза: ордера сняты, позиции не трогаю. /resume — продолжить."

        def resume():
            self.user_paused = False
            for s in self.last_mid:
                self.last_mid[s] = None
            return "Продолжаю."

        def stop():
            self.stopped = True
            return "Останавливаюсь: сниму ордера и выйду."

        self.tg.on("/status", status)
        self.tg.on("/report", self.report_text)
        self.tg.on("/pause", pause)
        self.tg.on("/resume", resume)
        self.tg.on("/stop", stop)
        self.tg.start_polling()

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

    # ---------- периодические задачи ----------
    def refresh_funding(self):
        if time.time() - self.funding_ts < FUNDING_REFRESH_S:
            return
        for sym, spec in self.specs.items():
            try:
                self.funding[sym] = self.ex.funding_rate_daily(spec.product_id)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: фандинг недоступен: %s", sym, e)
        self.funding_ts = time.time()

    def sync_fills(self):
        if self.cfg.dry_run or time.time() - self.fills_ts < self.cfg.fills_sync_s:
            return
        self.fills_ts = time.time()
        try:
            n = self.ledger.add_fills(self.ex.recent_fills(100))
            if n:
                log.info("Новых исполнений: %d", n)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось получить сделки: %s", e)

    # ---------- риск на уровне аккаунта ----------
    def check_account(self) -> bool:
        acct = self.ex.account()
        eq = float(acct["equity"])
        self._last_equity = eq
        self._positions = acct["positions"]
        now = time.time()
        today = datetime.now(timezone.utc).date()
        if self.day != today:
            if self.day is not None:  # сутки прошли — отчёт
                self.tg.send("Суточный отчёт:\n" + self.report_text())
            self.day, self.day_start_equity = today, eq
            self.ledger.event("day", f"start equity {eq:.2f}")
        if now - self.last_snapshot > SNAPSHOT_EVERY_S:
            self.ledger.snapshot(eq)
            self.last_snapshot = now

        loss = (self.day_start_equity or eq) - eq
        if loss >= self.cfg.max_daily_loss_usd:
            self.flatten(f"дневной убыток {loss:.2f}$ ≥ лимита {self.cfg.max_daily_loss_usd}$. Бот остановлен. "
                         f"Если это был вывод средств — запиши его: nado flow -<сумма>, и запусти снова.")
            self.stopped = True
            return False
        if acct["health_ratio"] < Decimal(str(self.cfg.min_health_ratio)):
            self.flatten(f"health ratio {float(acct['health_ratio']):.2f} ниже {self.cfg.min_health_ratio}")
            return False
        return True

    # ---------- котирование одного рынка ----------
    def quote_market(self, sym: str):
        spec, p = self.specs[sym], self.params[sym]
        now = time.time()
        if now < self.paused_until[sym]:
            return
        bid, ask = self.ex.best_bid_ask(spec.product_id)
        mid = float((bid + ask) / 2)

        if self.guards[sym].update(now, mid):
            if self.last_mid[sym] is not None:
                log.info("%s: высокая волатильность — снимаю ордера и жду", sym)
                if not self.cfg.dry_run:
                    self.ex.cancel_all([spec.product_id])
                self.last_mid[sym] = None
            return

        fr = self.funding.get(sym, Decimal(0))
        thr = Decimal(str(self.cfg.funding_max_daily))
        allow_long, allow_short = fr <= thr, fr >= -thr

        # общий лимит риска на все рынки: дальше только сокращаем позиции
        if self.cfg.max_total_position_usd > 0:
            total = 0.0
            for s2, sp2 in self.specs.items():
                amt = self._positions.get(sp2.product_id, Decimal(0))
                if amt:
                    b2, a2 = self.ex.best_bid_ask(sp2.product_id) if s2 != sym else (bid, ask)
                    total += abs(float(amt * (b2 + a2) / 2))
            if total >= self.cfg.max_total_position_usd:
                allow_long = allow_short = False

        pos = self._positions.get(spec.product_id, Decimal(0))
        q = compute_quote(spec, bid, ask, pos, p, allow_long, allow_short)

        if self.cfg.flow_filter and not q.close_position:
            feed = getattr(self.ex, "feed", None)
            trades = feed.trades(spec.product_id, now - self.cfg.flow_window_s) if feed else []
            block_bid, block_ask, why = self.flows[sym].signal(now, mid, trades)
            if block_bid and q.bid_price is not None:
                q.bid_price, q.bid_size = None, Decimal(0)
                q.reason = f"{q.reason}; bid снят: {why}"
                self.flow_blocks += 1
            if block_ask and q.ask_price is not None:
                q.ask_price, q.ask_size = None, Decimal(0)
                q.reason = f"{q.reason}; ask снят: {why}"
                self.flow_blocks += 1

        sides = (q.bid_price is not None, q.ask_price is not None)
        open_orders = [] if self.cfg.dry_run else self.ex.open_orders(spec.product_id)
        expected = sum(sides)
        if (not needs_requote(self.last_mid[sym], mid, self.cfg.requote_bps)
                and len(open_orders) >= expected and sides == self.last_sides[sym]
                and not q.close_position):
            return
        if not self.cfg.dry_run and not self.budget.take(1 + expected):
            log.debug("%s: лимит частоты ордеров — пропускаю перестановку", sym)
            return
        self.last_sides[sym] = sides

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

        tried = ok = 0
        if q.bid_price is not None and q.bid_size > 0:
            tried += 1
            ok += self._place(spec, q.bid_price, q.bid_size)
        if q.ask_price is not None and q.ask_size > 0:
            tried += 1
            ok += self._place(spec, q.ask_price, -q.ask_size)
        self.last_mid[sym] = mid

        if tried and not ok:
            self.rejects[sym] += 1
            if self.rejects[sym] >= REJECTS_BEFORE_PAUSE:
                self.paused_until[sym] = now + self.cfg.reject_pause_s
                self.rejects[sym] = 0
                self.last_mid[sym] = None
                msg = (f"{sym}: биржа {REJECTS_BEFORE_PAUSE} раза подряд отклонила ордера "
                       f"(рынок в reduce-only/post-only режиме или мало маржи?). Пауза {self.cfg.reject_pause_s}с.")
                self.tg.send(msg)
        else:
            self.rejects[sym] = 0

    def _place(self, spec, price, size) -> int:
        try:
            self.ex.place_post_only(spec, price, size, self.cfg.order_ttl_s)
            return 1
        except Exception as e:  # noqa: BLE001  post-only отказ при пересечении — норма
            log.warning("%s: ордер %s @ %s не принят: %s", spec.symbol, size, price, e)
            return 0

    # ---------- цикл ----------
    def tick(self):
        feed = getattr(self.ex, "feed", None)
        if feed is not None and feed.pids:
            try:
                feed.refresh()  # не чаще min_interval_s: в сканере на всех ботов один запрос
            except Exception as e:  # noqa: BLE001
                log.warning("feed: %s", e)
        if self.user_paused:
            self.check_account()
            return
        if self.check_account():
            self.refresh_funding()
            for sym in self.specs:
                self.quote_market(sym)
        self.sync_fills()

    def run(self):
        self.preflight()
        errors = 0
        try:
            while not self.stopped:
                try:
                    with self._lock:
                        self.tick()
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
            self.fills_ts = 0
            self.sync_fills()
            self.snapshot_now()
            self.tg.send("Бот остановлен, ордера сняты (позиции не трогал).")
