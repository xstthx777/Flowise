"""Учёт: снимки equity, депозиты/выводы, поинты → цена одного поинта."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import ROOT

DB = ROOT / "data" / "ledger.sqlite"


class Ledger:
    def __init__(self, path: Path = DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS equity(ts REAL, equity REAL);
            CREATE TABLE IF NOT EXISTS flows(ts REAL, amount REAL, note TEXT);
            CREATE TABLE IF NOT EXISTS points(ts REAL, total REAL);
            CREATE TABLE IF NOT EXISTS events(ts REAL, kind TEXT, text TEXT);
            """
        )

    def snapshot(self, equity: float):
        self.db.execute("INSERT INTO equity VALUES(?,?)", (time.time(), equity))
        self.db.commit()

    def flow(self, amount: float, note: str = ""):
        """+ депозит, − вывод (в $)."""
        self.db.execute("INSERT INTO flows VALUES(?,?,?)", (time.time(), amount, note))
        self.db.commit()

    def set_points(self, total: float):
        self.db.execute("INSERT INTO points VALUES(?,?)", (time.time(), total))
        self.db.commit()

    def event(self, kind: str, text: str):
        self.db.execute("INSERT INTO events VALUES(?,?,?)", (time.time(), kind, text))
        self.db.commit()

    def equity_at_or_after(self, ts: float):
        r = self.db.execute(
            "SELECT equity FROM equity WHERE ts>=? ORDER BY ts LIMIT 1", (ts,)).fetchone()
        return r[0] if r else None

    def report(self) -> dict:
        first = self.db.execute("SELECT ts, equity FROM equity ORDER BY ts LIMIT 1").fetchone()
        last = self.db.execute("SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        flows = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM flows").fetchone()[0]
        pts = self.db.execute("SELECT total FROM points ORDER BY ts DESC LIMIT 1").fetchone()
        if not first:
            return {"text": "Данных пока нет — запусти бота."}
        # затраты = (старт + чистые депозиты) − текущий equity
        cost = first[1] + flows - last[1]
        points = pts[0] if pts else 0
        days = max((last[0] - first[0]) / 86400, 1e-9)
        out = {
            "start_equity": first[1], "equity": last[1], "net_flows": flows,
            "cost_usd": cost, "days": days, "points": points,
            "cost_per_point": (cost / points) if points else None,
        }
        cpp = f"{out['cost_per_point']:.4f}$" if points else "— (введи поинты: points <число>)"
        out["text"] = (
            f"Период: {days:.1f} дн.\nEquity: {first[1]:.2f}$ → {last[1]:.2f}$ "
            f"(депозиты/выводы {flows:+.2f}$)\nЗатраты на фарм: {cost:.2f}$ "
            f"({cost / days:.2f}$/день)\nПоинты: {points:g}\nЦена поинта: {cpp}"
        )
        return out
