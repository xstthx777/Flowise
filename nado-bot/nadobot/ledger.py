"""Учёт: снимки equity, сделки (объём/комиссии), депозиты/выводы, поинты → цена поинта."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import ROOT

DB = ROOT / "data" / "ledger.sqlite"


class Ledger:
    def __init__(self, path: Path = DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)  # читает и Telegram-поток
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS equity(ts REAL, equity REAL);
            CREATE TABLE IF NOT EXISTS flows(ts REAL, amount REAL, note TEXT);
            CREATE TABLE IF NOT EXISTS points(ts REAL, total REAL);
            CREATE TABLE IF NOT EXISTS events(ts REAL, kind TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS fills(
                idx INTEGER, digest TEXT, ts REAL, product_id INTEGER,
                base REAL, quote REAL, fee REAL, PRIMARY KEY(idx, digest));
            """
        )

    # ---------- запись ----------
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

    def add_fills(self, fills: list[dict]) -> int:
        """Сохраняет новые исполнения, возвращает число новых."""
        before = self.db.total_changes
        self.db.executemany(
            "INSERT OR IGNORE INTO fills VALUES(:idx,:digest,:ts,:product_id,:base,:quote,:fee)",
            [{**f, "ts": f["ts"] or time.time()} for f in fills],
        )
        self.db.commit()
        return self.db.total_changes - before

    # ---------- чтение ----------
    def trading_stats(self, since: float = 0) -> dict:
        r = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(ABS(quote)),0), COALESCE(SUM(fee),0) FROM fills WHERE ts>=?",
            (since,),
        ).fetchone()
        return {"fills": r[0], "volume": r[1], "fees": r[2]}

    def report(self, protocol_volume_24h: float | None = None) -> dict:
        first = self.db.execute("SELECT ts, equity FROM equity ORDER BY ts LIMIT 1").fetchone()
        last = self.db.execute("SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        if not first:
            return {"text": "Данных пока нет — запусти бота."}
        flows = self.db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM flows WHERE ts>=?", (first[0],)).fetchone()[0]
        pts = self.db.execute("SELECT total FROM points ORDER BY ts LIMIT 1").fetchone()
        pts_last = self.db.execute("SELECT total FROM points ORDER BY ts DESC LIMIT 1").fetchone()
        all_t = self.trading_stats(first[0])
        day = self.trading_stats(time.time() - 86400)

        # затраты = (старт + чистые депозиты) − текущий equity
        cost = first[1] + flows - last[1]
        days = max((last[0] - first[0]) / 86400, 1e-9)
        # поинты = прирост между первой и последней записью (первая — база на старте)
        n_pts = self.db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        points = (pts_last[0] - pts[0]) if n_pts >= 2 else 0.0
        out = {
            "start_equity": first[1], "equity": last[1], "net_flows": flows,
            "cost_usd": cost, "days": days, "points": points,
            "cost_per_point": (cost / points) if points else None,
            "volume": all_t["volume"], "fees": all_t["fees"],
            "cost_per_1k_volume": (cost / all_t["volume"] * 1000) if all_t["volume"] else None,
        }
        if points:
            cpp = f"{out['cost_per_point']:.4f}$"
        elif n_pts == 1:
            cpp = f"— база {pts[0]:g} записана, после следующего начисления введи `points` ещё раз"
        else:
            cpp = "— введи текущие поинты: points <число> (сейчас и после каждого начисления)"
        per_day = f"{cost / days:.2f}$/день" if days >= 1 else "<1 дня данных"
        c1k = f"{out['cost_per_1k_volume']:.3f}$" if all_t["volume"] else "—"
        est = ""
        # объём и затраты в сутки: за последние 24ч, а если данных меньше суток — пересчёт темпа на сутки
        span_days = max((last[0] - first[0]) / 86400, 1 / 1440)
        extrap = span_days < 1
        vol_day = all_t["volume"] / span_days if extrap else day["volume"]
        cost_day = cost / span_days if extrap else cost / days
        if extrap and all_t["volume"]:
            est += (f"\nТемп на сутки (экстраполяция {span_days * 1440:.0f} мин данных): "
                    f"объём ≈{vol_day:,.0f}$, итог ≈{-cost_day:+,.2f}$/сутки")
        if protocol_volume_24h and vol_day:
            share = vol_day / protocol_volume_24h
            lo, hi = 300_000 / 7 * share, 950_000 / 7 * share
            est += (f"\nГрубая оценка поинтов: {lo:,.0f}–{hi:,.0f} в день "
                    f"(доля {share * 100:.3f}% от {protocol_volume_24h / 1e6:,.0f}M$ объёма Nado за 24ч; "
                    f"без учёта fee tier, токсичности и рефералок — реальное число может сильно отличаться)")
            if lo > 0 and cost_day > 0:
                est += f"\nОценка цены поинта: {cost_day / hi:.4f}–{cost_day / lo:.4f}$"
        out["text"] = (
            f"Период: {days:.1f} дн.\n"
            f"Equity: {first[1]:.2f}$ → {last[1]:.2f}$ (депозиты/выводы {flows:+.2f}$)\n"
            f"Объём: {all_t['volume']:,.0f}$ ({all_t['fills']} сделок), за 24ч {day['volume']:,.0f}$\n"
            f"Комиссии: {all_t['fees']:+.2f}$ (минус = ребейт), за 24ч {day['fees']:+.2f}$\n"
            f"Затраты на фарм: {cost:.2f}$ ({per_day}, {c1k} на 1000$ объёма)\n"
            f"Поинты за период: {points:g}\nЦена поинта: {cpp}"
        ) + est
        return out
