from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token, self.chat_id = token, chat_id

    def send(self, text: str):
        log.info("NOTIFY: %s", text)
        if not (self.token and self.chat_id):
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": f"[NadoBot] {text}"},
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram недоступен: %s", e)
