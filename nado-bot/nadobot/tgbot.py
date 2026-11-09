"""Telegram-сервис: `nado tg`.

Процесс работает постоянно и слушает Telegram. Торговля включается и выключается
кнопками, MetaMask подключается по ссылке из бота. Команды принимаются только
из чата TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import time
from decimal import Decimal
from urllib.parse import urlencode

from .config import ROOT, Config, load_config, set_config_value, set_first_market
from .exchange import Exchange
from .ledger import Ledger
from .link import is_link_done, link_url_params, start_link_server, submit_signature
from .notify import Notifier

log = logging.getLogger("nadobot.tg")
STATE = ROOT / "data" / "state.json"
LINK_TIMEOUT_S = 15 * 60
SIG_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")

MENU = [
    [{"text": "📊 Статус", "callback_data": "status"}, {"text": "📈 Отчёт", "callback_data": "report"}],
    [{"text": "▶️ Запустить", "callback_data": "run"}, {"text": "⏹ Остановить", "callback_data": "stop"}],
    [{"text": "🦊 Подключить MetaMask", "callback_data": "link"}, {"text": "⚙️ Режим", "callback_data": "mode"}],
    [{"text": "🔎 Сканер 60 мин", "callback_data": "scan"}, {"text": "🚨 Закрыть всё", "callback_data": "panic"}],
    [{"text": "❓ Помощь", "callback_data": "help"}],
]

HELP = """Команды:
/menu — кнопки
/status — состояние аккаунта и бота
/report — объём, комиссии, затраты, цена поинта
/run — запустить торговлю, /stop — остановить (ордера снимаются, позиции остаются)
/link — подключить MetaMask, /unlink — отключить
/points 12345 — записать текущее число поинтов (из app.nado.xyz)
/flow 100 или /flow -50 — записать депозит/вывод
/paper — тест без кошелька (живые цены, виртуальные деньги), /paper reset — сбросить, /paper off — реальный режим
/dryrun on|off — режим «только считать» / реальные ордера
/scan 60 — сканер рынков на 60 мин (какой рынок/отступ даёт самые дешёвые поинты), /scanstop
/panic — снять ордера и закрыть позиции (с подтверждением)"""


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(st: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st), encoding="utf-8")


class TgApp:
    def __init__(self, cfg: Config):
        if not (cfg.tg_token and cfg.tg_chat):
            raise SystemExit("Нужны TELEGRAM_TOKEN и TELEGRAM_CHAT_ID в .env. Запусти: nado tgsetup")
        self.cfg = cfg
        self.tg = Notifier(cfg.tg_token, cfg.tg_chat)
        self._init_exchange()
        self.bot = None  # nadobot.bot.Bot, пока торговля включена
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.link_job: dict | None = None  # {"unlink", "params", "until", "srv"}
        self.panic_armed_until = 0.0
        self.scan_thread = None
        self.scan_stop = False
        self.scan_best = None
        self.last_tick = 0.0

    def _init_exchange(self, reset_paper: bool = False):
        self.paper = self.cfg.paper
        if self.paper:
            from .paper import make_paper
            self.ex, self.ledger = make_paper(self.cfg, reset=reset_paper)
        else:
            self.ex = Exchange(self.cfg.mode, self.cfg.bot_key, self.cfg.owner, self.cfg.subaccount_name)
            self.ledger = Ledger()
        if hasattr(self, "_specs_cache"):
            del self._specs_cache

    def report(self) -> str:
        if self.bot is not None:
            return self.bot.report_text()
        vol = None
        if hasattr(self.ex, "protocol_volume_24h"):
            try:
                vol = self.ex.protocol_volume_24h()
            except Exception:  # noqa: BLE001
                pass
        return self.ledger.report(protocol_volume_24h=vol)["text"]

    def set_paper(self, on: bool, reset: bool = False):
        if self.bot is not None:
            self.stop_trading("смена режима", save=False)
        set_config_value("paper", "true" if on else "false")
        self.cfg = load_config()
        try:
            self._init_exchange(reset_paper=reset)
        except Exception as e:  # noqa: BLE001
            set_config_value("paper", "true")
            self.cfg = load_config()
            self._init_exchange()
            return self.say(f"Не получилось включить реальный режим: {e}\n"
                            "Проверь NADO_OWNER и NADO_BOT_KEY в .env. Остаюсь в тесте.")
        if on:
            self.menu(f"🧪 Тестовый режим{' (сброшен)' if reset else ''}: живые цены Nado, виртуальный баланс "
                      f"{self.cfg.paper_balance}$, кошелёк не нужен. Жми ▶️ Запустить.")
        else:
            self.menu("💼 Реальный режим. Подключи MetaMask (🦊) и начни с DRY-RUN.")

    # ---------- ответы ----------
    def say(self, text: str, buttons=None):
        self.tg.send(text, buttons=buttons, prefix=False)

    def menu(self, text: str = "Меню:"):
        self.say(text, MENU)

    # ---------- торговля ----------
    def start_trading(self, quiet: bool = False):
        if self.bot is not None:
            return self.say("Уже работает.")
        from .bot import Bot

        self.cfg = load_config()  # подхватить правки config.yaml
        try:
            bot = Bot(self.cfg, tg=self.tg, telegram_control=False,
                  ex=self.ex if self.paper else None, ledger=self.ledger)
            bot.preflight()
        except SystemExit as e:
            return self.say(f"Не запустил:\n{e}")
        except Exception as e:  # noqa: BLE001
            return self.say(f"Не запустил, ошибка: {e}")
        self.bot = bot
        _save_state({"trading": True})
        if not quiet:
            self.say("▶️ Торговля запущена." + (" 🧪 ТЕСТ: виртуальные деньги, живые цены." if self.paper else
                                          " Режим DRY-RUN: ордера не отправляются." if self.cfg.dry_run else ""))

    def stop_trading(self, reason: str = "по команде", save: bool = True):
        if self.bot is None:
            if save:
                _save_state({"trading": False})
            return self.say("Торговля и так остановлена.")
        self.bot.cancel_only()
        self.bot.fills_ts = 0
        self.bot.sync_fills()
        self.bot.snapshot_now()
        self.bot = None
        if save:
            _save_state({"trading": False})
        self.say(f"⏹ Остановлено ({reason}). Ордера сняты, позиции не трогал.")

    def tick_trading(self):
        if self.bot is None or time.time() - self.last_tick < self.cfg.loop_interval_s:
            return
        self.last_tick = time.time()
        try:
            self.bot.tick()
            self.bot._errors = 0
        except Exception as e:  # noqa: BLE001
            self.bot._errors = getattr(self.bot, "_errors", 0) + 1
            log.exception("Ошибка цикла: %s", e)
            if self.bot._errors >= 5:
                self.bot.cancel_only()
                self.say(f"5 ошибок подряд, ордера сняты, пауза 60с: {e}")
                self.last_tick = time.time() + 60
                self.bot._errors = 0
        if self.bot is not None and self.bot.stopped:  # kill-switch внутри Bot
            self.bot = None
            _save_state({"trading": False})
            self.say("Бот остановился сам (kill-switch). Разберись в причине и нажми ▶️, когда будешь готов.")

    # ---------- MetaMask ----------
    def start_link(self, unlink: bool = False):
        if self.paper:
            return self.say("Сейчас тестовый режим — кошелёк не нужен. Чтобы подключить MetaMask, "
                            "переключись: ⚙️ Режим → 💼 Реальный.", MENU)
        if self.bot is not None and unlink:
            self.stop_trading("перед отвязкой")
        try:
            acct = self.ex.account()
        except Exception as e:  # noqa: BLE001
            return self.say(f"Nado недоступен: {e}")
        net = "mainnet (Ink)" if self.cfg.mode == "mainnet" else "testnet (Ink Sepolia)"
        if not acct["exists"] or acct["equity"] < Decimal(5):
            return self.say(
                f"Сначала внеси депозит минимум 5 USDT0 на app.nado.xyz ({net}) с кошелька {self.ex.owner}.\n"
                f"Сейчас equity: {acct['equity']:.2f}$. Потом снова /link.")
        if not unlink and self.ex.is_bot_linked():
            return self.say("✅ Бот уже подключён к твоему аккаунту. Можно запускать: /run", MENU)

        params = link_url_params(self.ex, unlink=unlink)
        page = self.cfg.sign_page_url + "?" + urlencode(params)
        mm = "https://metamask.app.link/dapp/" + page.split("://", 1)[1]
        if self.link_job and self.link_job.get("srv"):
            self.link_job["srv"].shutdown()
        srv, local = None, None
        try:
            srv, local = start_link_server(self.ex, unlink=unlink)
        except OSError:
            pass  # порт занят — обойдёмся без локальной страницы
        self.link_job = {"unlink": unlink, "params": params, "until": time.time() + LINK_TIMEOUT_S, "srv": srv}
        action = "отключения" if unlink else "подключения"
        text = (
            f"🦊 Ссылка для {action} MetaMask ({net})\n\n"
            f"Твой MetaMask: {self.ex.owner}\nБот-кошелёк: {self.ex.bot.address}\n\n"
            f"📱 С телефона: кнопка «Открыть в MetaMask». Если приложение не открылось, скопируй ссылку ниже "
            f"и вставь её в браузер внутри MetaMask.\n"
            f"💻 С компьютера: «Открыть в браузере» (нужно расширение MetaMask)"
            + (f" или локальная страница {local}" if local else "") + ".\n\n"
            "На странице проверь адреса и нажми «Подписать». Газ не нужен. Я сам увижу подключение "
            f"и напишу (ссылка действует {LINK_TIMEOUT_S // 60} мин).\n\n{page}"
        )
        self.say(text, [[{"text": "📱 Открыть в MetaMask", "url": mm}],
                        [{"text": "💻 Открыть в браузере", "url": page}]])

    def check_link(self):
        job = self.link_job
        if not job or time.time() < job.get("next", 0):
            return
        job["next"] = time.time() + 5
        try:
            done = is_link_done(self.ex, job["unlink"])
        except Exception:  # noqa: BLE001
            return
        if done:
            self._finish_link(True)
        elif time.time() > job["until"]:
            self._finish_link(False)

    def _finish_link(self, ok: bool):
        job, self.link_job = self.link_job, None
        if job and job.get("srv"):
            job["srv"].shutdown()
        if not ok:
            return self.say("⌛ Время ссылки вышло, подключения не было. Нажми 🦊, чтобы получить новую.")
        if job["unlink"]:
            self.say("🔓 Бот-ключ отвязан. Бот больше не может торговать от твоего имени.")
        else:
            self.menu("✅ MetaMask подключён! Бот-кошелёк может торговать от имени твоего аккаунта.\n"
                      "Начни с DRY-RUN (/dryrun on), потом ▶️ Запустить.")

    def try_signature(self, text: str) -> bool:
        if not (self.link_job and SIG_RE.match(text)):
            return False
        try:
            submit_signature(self.ex, self.link_job["params"], text)
            self.say("Подпись отправлена в Nado, проверяю…")
            self.link_job["next"] = 0
        except Exception as e:  # noqa: BLE001
            self.say(f"Nado отклонил подпись: {e}\nПопробуй новую ссылку: /link")
        return True

    # ---------- информация ----------
    def status(self):
        lines = []
        try:
            a = self.ex.account()
            pos = {s: str(a["positions"].get(sp.product_id, 0)) for s, sp in self._specs().items()}
            regime = "🧪 ТЕСТ (виртуальные деньги)" if self.paper else ("DRY-RUN" if self.cfg.dry_run else "LIVE")
            lines += [
                f"Сеть: {self.cfg.mode} | {regime}",
                f"Торговля: {'▶️ включена' if self.bot else '⏹ выключена'}",
                f"Equity: {a['equity']:.2f}$ | health ratio {float(a['health_ratio']):.2f}",
                f"Позиции: {pos}",
            ]
            if self.paper:
                pnl = a["equity"] - self.ex.start_balance
                lines.append(f"Старт теста: {self.ex.start_balance:.2f}$ → сейчас {a['equity']:.2f}$ ({pnl:+.2f}$)")
                lines.append(f"Ордеров в симуляторе: {len(self.ex.orders)}, исполнений: {len(self.ex.fills)}")
            else:
                lines.append(f"MetaMask: {'✅ подключён' if self.ex.is_bot_linked() else '❌ не подключён (/link)'}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"Nado недоступен: {e}")
        if self.bot:
            lines.append("Фандинг: " + ", ".join(f"{k} {float(v) * 100:.3f}%/сут" for k, v in self.bot.funding.items()))
        self.say("\n".join(lines), MENU)

    def _specs(self):
        if not hasattr(self, "_specs_cache"):
            self._specs_cache = self.ex.market_specs([m.symbol for m in self.cfg.markets])
        return self._specs_cache

    def mode(self):
        c = self.cfg
        mk = "\n".join(f"• {m.symbol}: ордер {m.order_size_usd}$, отступ {m.offset_bps} б.п., "
                       f"лимит {m.max_position_usd}/{m.hard_position_usd}$" for m in c.markets)
        regime = ("🧪 ТЕСТ без кошелька (виртуальные деньги)" if self.paper
                  else "DRY-RUN (только расчёт)" if c.dry_run else "LIVE (реальные ордера)")
        self.say(
            f"Сеть: {c.mode}\nРежим: {regime}\n"
            f"Kill-switch: −{c.max_daily_loss_usd}$/сутки\nРынки:\n{mk}\n\n"
            "Сеть и рынки меняются в config.yaml. Здесь можно переключить режим:",
            [[{"text": "🧪 Тест без кошелька", "callback_data": "paper_on"},
              {"text": "💼 Реальный", "callback_data": "paper_off"}],
             [{"text": "🔄 Сбросить тест", "callback_data": "paper_reset"}],
             [{"text": "DRY-RUN (реальный)", "callback_data": "dryrun_on"},
              {"text": "💸 LIVE (реальный)", "callback_data": "dryrun_off"}]])

    def set_dryrun(self, on: bool):
        was_running = self.bot is not None
        if was_running:
            self.stop_trading("смена режима", save=False)
        set_config_value("dry_run", "true" if on else "false")
        self.cfg = load_config()
        msg = "🧪 Режим DRY-RUN: бот только считает, ордера не отправляет." if on else \
            "💸 Режим LIVE: реальные ордера."
        if not on and self.cfg.mode == "mainnet" and not self.cfg.mainnet_live_confirm:
            msg += ("\n⚠️ Это mainnet: торговля не запустится, пока в config.yaml не стоит "
                    "mainnet_live_confirm: true (защита от случайного запуска).")
        self.say(msg)
        if was_running:
            self.start_trading()

    def panic(self, confirmed: bool):
        if not confirmed:
            self.panic_armed_until = time.time() + 60
            return self.say("🚨 Снять все ордера и ЗАКРЫТЬ ВСЕ ПОЗИЦИИ бота по рынку?",
                            [[{"text": "Да, закрыть всё", "callback_data": "panic_yes"},
                              {"text": "Отмена", "callback_data": "menu"}]])
        if time.time() > self.panic_armed_until:
            return self.say("Подтверждение устарело, нажми 🚨 ещё раз.")
        from .bot import Bot

        self.bot = None
        _save_state({"trading": False})
        cfg = load_config()
        cfg.dry_run = True  # чтобы конструктор не упёрся в mainnet_live_confirm
        b = Bot(cfg, tg=self.tg, telegram_control=False, ex=self.ex if self.paper else None, ledger=self.ledger)
        b.cfg.dry_run = False
        b.flatten("ручной panic из Telegram")
        self.say("Готово: ордера сняты, позиции закрыты (если были). Торговля выключена.", MENU)

    # ---------- сканер ----------
    def start_scan(self, minutes: float):
        import threading

        from .scanner import best_rows, run_scan, summarize

        if self.scan_thread and self.scan_thread.is_alive():
            return self.say("Сканер уже работает. /scanstop — остановить досрочно.")
        self.scan_stop = False
        cfg = load_config()

        def job():
            try:
                res = run_scan(cfg, minutes, progress=self.say, stop_flag=lambda: self.scan_stop)
                text = summarize(res)
                ok = best_rows(res)
                self.scan_best = (ok[0]["market"], ok[0]["offset_bps"], ok[0]["flow"]) if ok else None
                btn = None
                if self.scan_best:
                    m, o, fl = self.scan_best
                    btn = [[{"text": f"✅ Применить: {m} {o:g} б.п.", "callback_data": "scan_apply"}]]
                self.say(text, btn)
            except Exception as e:  # noqa: BLE001
                log.exception("scan")
                self.say(f"Сканер упал: {e}")

        self.scan_thread = threading.Thread(target=job, daemon=True, name="scan")
        self.scan_thread.start()
        self.say(f"🔎 Сканер запущен на {minutes:g} мин: топ рынков Nado × отступы 2/3/5 б.п., виртуально. "
                 "Торговлю не трогает. Пришлю рейтинг по окончании.")

    def apply_scan(self):
        if not self.scan_best:
            return self.say("Нет результата сканера. Запусти /scan 60")
        m, o, fl = self.scan_best
        was = self.bot is not None
        if was:
            self.stop_trading("применяю результат сканера", save=False)
        set_first_market(m, o)
        set_config_value("flow_filter", "true" if fl == "on" else "false")
        self.cfg = load_config()
        if hasattr(self, "_specs_cache"):
            del self._specs_cache
        self.say(f"Записал в config.yaml: рынок {m}, отступ {o:g} б.п., фильтр потока {fl}.")
        if was:
            self.start_trading()

    # ---------- разбор команд ----------
    def handle(self, item: dict):
        if item["kind"] == "button":
            self.tg.answer_callback(item["cb_id"])
            cmd, args = item["data"], []
        else:
            text = item["text"]
            if self.try_signature(text):
                return
            if not text.startswith("/"):
                return self.menu("Не понял. Вот меню:")
            parts = text.split()
            cmd, args = parts[0][1:].split("@")[0].lower(), parts[1:]

        try:
            if cmd in ("start", "menu"):
                self.menu("Nado Bot на связи. Что делаем?")
            elif cmd == "help":
                self.say(HELP)
            elif cmd == "status":
                self.status()
            elif cmd == "report":
                self.say(self.report())
            elif cmd == "paper_on" or (cmd == "paper" and args[:1] in ([], ["on"])):
                self.set_paper(True)
            elif cmd == "paper_off" or (cmd == "paper" and args[:1] == ["off"]):
                self.set_paper(False)
            elif cmd == "paper_reset" or (cmd == "paper" and args[:1] == ["reset"]):
                self.set_paper(True, reset=True)
            elif cmd == "run":
                if not self.paper and not self.cfg.dry_run and not self.ex.is_bot_linked():
                    return self.say("MetaMask не подключён. Сначала 🦊 /link", MENU)
                self.start_trading()
            elif cmd == "stop":
                self.stop_trading()
            elif cmd == "link":
                self.start_link(False)
            elif cmd == "unlink":
                self.start_link(True)
            elif cmd == "mode":
                self.mode()
            elif cmd == "dryrun_on" or (cmd == "dryrun" and args[:1] == ["on"]):
                self.set_dryrun(True)
            elif cmd == "dryrun_off" or (cmd == "dryrun" and args[:1] == ["off"]):
                self.set_dryrun(False)
            elif cmd == "points" and args:
                self.ledger.set_points(float(args[0].replace(",", ".")))
                self.say("Записал.\n" + self.ledger.report()["text"])
            elif cmd == "flow" and args:
                self.ledger.flow(float(args[0].replace(",", ".")), "telegram")
                if self.bot is not None:  # не считать депозит/вывод дневной прибылью/убытком
                    self.bot.day_start_equity = (self.bot.day_start_equity or 0) + float(args[0])
                self.say("Записал.")
            elif cmd == "scan":
                self.start_scan(float(args[0]) if args else 60)
            elif cmd == "scanstop":
                self.scan_stop = True
                self.say("Останавливаю сканер, пришлю то, что успел набрать.")
            elif cmd == "scan_apply":
                self.apply_scan()
            elif cmd == "panic":
                self.panic(False)
            elif cmd == "panic_yes":
                self.panic(True)
            else:
                self.say(HELP)
        except Exception as e:  # noqa: BLE001
            log.exception("команда %s", cmd)
            self.say(f"Ошибка: {e}")

    # ---------- главный цикл ----------
    def run(self):
        def foreign(item):
            log.warning("Сообщение из чужого чата %s проигнорировано", item.get("chat"))

        self.tg.start_inbox(self.inbox, on_foreign=foreign)
        resume = _load_state().get("trading")
        regime = "🧪 ТЕСТ без кошелька" if self.paper else ("DRY-RUN" if self.cfg.dry_run else "LIVE")
        self.menu(f"🟢 Nado Bot запущен ({self.cfg.mode}, {regime})."
                  + (" Возобновляю торговлю после перезапуска…" if resume else ""))
        if resume:
            self.start_trading(quiet=False)
        try:
            while True:
                try:
                    item = self.inbox.get(timeout=0.5)
                    self.handle(item)
                except queue.Empty:
                    pass
                self.check_link()
                self.tick_trading()
        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C")
        finally:
            if self.bot is not None:
                self.bot.cancel_only()
            self.tg.send("🔴 Сервис остановлен (окно закрыто/Ctrl+C). Ордера сняты.", prefix=False)
