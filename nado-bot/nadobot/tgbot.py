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

from .config import ROOT, Config, load_config, set_config_value
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
    [{"text": "🚨 Закрыть всё", "callback_data": "panic"}, {"text": "❓ Помощь", "callback_data": "help"}],
]

HELP = """Команды:
/menu — кнопки
/status — состояние аккаунта и бота
/report — объём, комиссии, затраты, цена поинта
/run — запустить торговлю, /stop — остановить (ордера снимаются, позиции остаются)
/link — подключить MetaMask, /unlink — отключить
/points 12345 — записать текущее число поинтов (из app.nado.xyz)
/flow 100 или /flow -50 — записать депозит/вывод
/dryrun on|off — режим «только считать» / реальные ордера
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
        self.ex = Exchange(cfg.mode, cfg.bot_key, cfg.owner, cfg.subaccount_name)
        self.ledger = Ledger()
        self.bot = None  # nadobot.bot.Bot, пока торговля включена
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.link_job: dict | None = None  # {"unlink", "params", "until", "srv"}
        self.panic_armed_until = 0.0
        self.last_tick = 0.0

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
            bot = Bot(self.cfg, tg=self.tg, telegram_control=False)
            bot.preflight()
        except SystemExit as e:
            return self.say(f"Не запустил:\n{e}")
        except Exception as e:  # noqa: BLE001
            return self.say(f"Не запустил, ошибка: {e}")
        self.bot = bot
        _save_state({"trading": True})
        if not quiet:
            self.say("▶️ Торговля запущена." + (" Режим DRY-RUN: ордера не отправляются." if self.cfg.dry_run else ""))

    def stop_trading(self, reason: str = "по команде", save: bool = True):
        if self.bot is None:
            if save:
                _save_state({"trading": False})
            return self.say("Торговля и так остановлена.")
        self.bot.cancel_only()
        self.bot.sync_fills()
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
            lines += [
                f"Сеть: {self.cfg.mode} | {'DRY-RUN' if self.cfg.dry_run else 'LIVE'}",
                f"Торговля: {'▶️ включена' if self.bot else '⏹ выключена'}",
                f"Equity: {a['equity']:.2f}$ | health ratio {float(a['health_ratio']):.2f}",
                f"Позиции: {pos}",
                f"MetaMask: {'✅ подключён' if self.ex.is_bot_linked() else '❌ не подключён (/link)'}",
            ]
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
        self.say(
            f"Сеть: {c.mode}\nРежим: {'DRY-RUN (только расчёт)' if c.dry_run else 'LIVE (реальные ордера)'}\n"
            f"Kill-switch: −{c.max_daily_loss_usd}$/сутки\nРынки:\n{mk}\n\n"
            "Сеть и рынки меняются в config.yaml. Здесь можно переключить режим:",
            [[{"text": "🧪 DRY-RUN", "callback_data": "dryrun_on"},
              {"text": "💸 LIVE", "callback_data": "dryrun_off"}]])

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
        b = Bot(cfg, tg=self.tg, telegram_control=False)
        b.cfg.dry_run = False
        b.flatten("ручной panic из Telegram")
        self.say("Готово: ордера сняты, позиции закрыты (если были). Торговля выключена.", MENU)

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
                self.say(self.ledger.report()["text"])
            elif cmd == "run":
                if not self.cfg.dry_run and not self.ex.is_bot_linked():
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
        self.menu(f"🟢 Nado Bot запущен ({self.cfg.mode}, {'DRY-RUN' if self.cfg.dry_run else 'LIVE'})."
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
