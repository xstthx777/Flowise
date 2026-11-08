"""Telegram: уведомления, кнопки и приём команд.

Команды принимаются ТОЛЬКО из чата TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token, self.chat_id = token, str(chat_id or "")
        self._handlers: dict[str, Callable[[], str]] = {}
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, timeout: float = 15, **payload):
        r = requests.post(API.format(token=self.token, method=method), json=payload, timeout=timeout)
        return r.json()

    def send(self, text: str, buttons: Optional[list[list[dict]]] = None, prefix: bool = True):
        """buttons: [[{"text": "...", "callback_data": "..."} | {"text": "...", "url": "..."}], ...]"""
        log.info("NOTIFY: %s", text.replace("\n", " | ")[:300])
        if not self.enabled:
            return
        body = {"chat_id": self.chat_id, "text": (f"[NadoBot] {text}" if prefix else text)[:4000],
                "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": buttons}
        try:
            res = self._call("sendMessage", **body)
            if not res.get("ok"):
                log.warning("Telegram отклонил сообщение: %s", res.get("description"))
                if buttons:  # например, кривая ссылка в кнопке — шлём хотя бы текст
                    body.pop("reply_markup", None)
                    self._call("sendMessage", **body)
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram недоступен: %s", e)

    def answer_callback(self, callback_id: str, text: str = ""):
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except Exception:  # noqa: BLE001
            pass

    # ---------- получение апдейтов ----------
    def skip_backlog(self):
        try:
            r = self._call("getUpdates", offset=-1, timeout=0)
            if r.get("result"):
                self._offset = r["result"][-1]["update_id"] + 1
        except Exception:  # noqa: BLE001
            pass

    def get_updates(self, timeout: int = 25) -> list[dict]:
        r = requests.get(API.format(token=self.token, method="getUpdates"),
                         params={"offset": self._offset, "timeout": timeout},
                         timeout=timeout + 10).json()
        ups = r.get("result", [])
        if ups:
            self._offset = ups[-1]["update_id"] + 1
        return ups

    def start_inbox(self, inbox: "queue.Queue[dict]", on_foreign: Optional[Callable[[dict], None]] = None):
        """Фоновый поток: кладёт сообщения/нажатия из нашего чата в очередь."""

        def loop():
            self.skip_backlog()
            while True:
                try:
                    for upd in self.get_updates():
                        if "callback_query" in upd:
                            cq = upd["callback_query"]
                            chat = str(cq.get("message", {}).get("chat", {}).get("id"))
                            item = {"kind": "button", "data": cq.get("data", ""), "cb_id": cq["id"], "chat": chat}
                        elif "message" in upd:
                            m = upd["message"]
                            chat = str(m.get("chat", {}).get("id"))
                            item = {"kind": "text", "text": (m.get("text") or "").strip(), "chat": chat,
                                    "from": m.get("from", {})}
                        else:
                            continue
                        if chat == self.chat_id:
                            inbox.put(item)
                        elif on_foreign:
                            on_foreign(item)
                except Exception as e:  # noqa: BLE001
                    log.debug("telegram poll: %s", e)
                    time.sleep(10)

        threading.Thread(target=loop, daemon=True, name="telegram").start()

    # ---------- простой режим для `nado run` ----------
    def on(self, command: str, handler: Callable[[], str]):
        self._handlers[command] = handler

    def start_polling(self):
        if not self.enabled or not self._handlers:
            return
        inbox: "queue.Queue[dict]" = queue.Queue()
        self.start_inbox(inbox)

        def worker():
            while True:
                item = inbox.get()
                cmd = item.get("text", "").split()[0].split("@")[0].lower() if item.get("text") else ""
                h = self._handlers.get(cmd)
                if h:
                    try:
                        self.send(h())
                    except Exception as e:  # noqa: BLE001
                        self.send(f"Ошибка команды {cmd}: {e}")
                elif cmd.startswith("/"):
                    self.send("Команды: " + " ".join(sorted(self._handlers)))

        threading.Thread(target=worker, daemon=True, name="telegram-cmd").start()
