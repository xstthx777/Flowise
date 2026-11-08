"""Telegram: уведомления и простые команды управления.

Команды принимаются ТОЛЬКО из чата TELEGRAM_CHAT_ID:
  /status  /report  /pause  /resume  /stop
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token, self.chat_id = token, str(chat_id)
        self._handlers: dict[str, Callable[[], str]] = {}
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str):
        log.info("NOTIFY: %s", text)
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": f"[NadoBot] {text}"[:4000]},
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram недоступен: %s", e)

    # ---------- команды ----------
    def on(self, command: str, handler: Callable[[], str]):
        self._handlers[command] = handler

    def start_polling(self):
        if not self.enabled or not self._handlers:
            return
        threading.Thread(target=self._poll, daemon=True, name="telegram").start()

    def _poll(self):
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        # пропускаем старые сообщения, накопившиеся до запуска
        try:
            r = requests.get(url, params={"offset": -1, "timeout": 0}, timeout=15).json()
            if r.get("result"):
                self._offset = r["result"][-1]["update_id"] + 1
        except Exception:  # noqa: BLE001
            pass
        while True:
            try:
                r = requests.get(url, params={"offset": self._offset, "timeout": 25}, timeout=35).json()
                for upd in r.get("result", []):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue  # чужие чаты игнорируем
                    cmd = (msg.get("text") or "").split()[0].split("@")[0].lower() if msg.get("text") else ""
                    h = self._handlers.get(cmd)
                    if h:
                        try:
                            self.send(h())
                        except Exception as e:  # noqa: BLE001
                            self.send(f"Ошибка команды {cmd}: {e}")
                    elif cmd.startswith("/"):
                        self.send("Команды: " + " ".join(sorted(self._handlers)))
            except Exception as e:  # noqa: BLE001
                log.debug("telegram poll: %s", e)
                time.sleep(10)
