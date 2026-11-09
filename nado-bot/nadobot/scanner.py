"""Сканер рынков: `nado scan 60`.

Один поток живых данных Nado + сетка виртуальных ботов (рынок × отступ × фильтр).
Каждый виртуальный бот — тот же алгоритм, что и боевой, со своими $ на балансе.
Итог — рейтинг: сколько стоит $1000 объёма и (грубо) один поинт на каждом варианте.
"""
from __future__ import annotations

import copy
import csv
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from eth_account import Account

from nado_protocol.client import NadoClientMode, create_nado_client

from .config import ROOT, Config, MarketCfg
from .feed import MarketFeed
from .ledger import Ledger
from .paper import PaperExchange

log = logging.getLogger("nadobot.scan")
SCAN_DIR = ROOT / "data" / "scans"


class _Quiet:
    """Notifier-заглушка: виртуальные боты сканера ничего не шлют в Telegram."""

    enabled = False

    def send(self, *a, **k):
        pass


@dataclass
class Variant:
    symbol: str
    offset_bps: float
    flow: bool
    bot: object
    ex: PaperExchange
    ledger: Ledger
    min_equity: float = 0.0


def pick_markets(client, top: int, min_volume_usd: float, only: list[str] | None) -> list[str]:
    syms = client.context.engine_client.get_symbols(product_type="perp").symbols
    if only:
        return [s for s in only if s in syms]
    tick = client.context.indexer_client.get_tickers()
    ranked = sorted(((float(v.get("quote_volume") or 0), v.get("base_currency")) for v in tick.values()
                     if str(v.get("base_currency", "")).endswith("-PERP")), reverse=True)
    return [s for vol, s in ranked if s in syms and vol >= min_volume_usd][:top]


def run_scan(cfg: Config, minutes: float = 60, top: int = 12, offsets=(2.0, 3.0, 5.0),
             compare_flow: bool = False, only: list[str] | None = None, min_volume_usd: float = 300_000,
             loop_s: float = 2.0, progress=None, stop_flag=None) -> dict:
    # виртуальные боты не должны засорять лог каждой перестановкой
    for name in ("nadobot", "nadobot.paper", "nadobot.feed"):
        logging.getLogger(name).setLevel(logging.WARNING)
    client = create_nado_client(NadoClientMode(cfg.mode), Account.create())
    symbols_data = client.context.engine_client.get_symbols(product_type="perp").symbols
    markets = pick_markets(client, top, min_volume_usd, only)
    if not markets:
        raise SystemExit("Нет подходящих рынков")
    feed = MarketFeed(client, [], min_interval_s=loop_s * 0.8)
    base_m = cfg.markets[0] if cfg.markets else MarketCfg("BTC-PERP")
    flows = (True, False) if compare_flow else (cfg.flow_filter,)

    from .bot import Bot

    variants: list[Variant] = []
    for sym in markets:
        for off in offsets:
            for fl in flows:
                c = copy.deepcopy(cfg)
                c.paper, c.dry_run = True, False
                c.loop_interval_s = loop_s
                c.flow_filter = fl
                c.max_daily_loss_usd = 1e9  # в сканере не останавливаемся — нам нужна статистика
                c.markets = [MarketCfg(sym, base_m.order_size_usd, off, base_m.max_position_usd,
                                       base_m.hard_position_usd, base_m.skew_bps)]
                ex = PaperExchange(cfg.mode, cfg.paper_balance, feed=feed, client=client,
                                   state_path=False, symbols=symbols_data)
                led = Ledger(Path(":memory:"))
                b = Bot(c, tg=_Quiet(), telegram_control=False, ex=ex, ledger=led)
                variants.append(Variant(sym, off, fl, b, ex, led, cfg.paper_balance))
    log.setLevel(logging.INFO)
    log.info("Сканирую %d рынков × %d вариантов = %d виртуальных ботов, %s мин",
             len(markets), len(offsets) * len(flows), len(variants), minutes)

    start = time.time()
    end = start + minutes * 60
    next_progress = start + 600
    while time.time() < end and not (stop_flag and stop_flag()):
        t0 = time.time()
        try:
            feed.refresh(force=True)
        except Exception as e:  # noqa: BLE001
            log.warning("feed: %s", e)
            time.sleep(5)
            continue
        for v in variants:
            try:
                v.bot.tick()
                eq = float(v.ex.account()["equity"])
                v.min_equity = min(v.min_equity, eq)
            except Exception as e:  # noqa: BLE001
                log.debug("%s %s: %s", v.symbol, v.offset_bps, e)
        if progress and time.time() >= next_progress:
            next_progress += 600
            progress(f"Сканер: прошло {int((time.time() - start) / 60)} из {int(minutes)} мин…")
        time.sleep(max(0.0, loop_s - (time.time() - t0)))

    elapsed_days = max((time.time() - start) / 86400, 1e-6)
    try:
        proto_vol = sum(float(v.get("quote_volume") or 0) for v in client.context.indexer_client.get_tickers().values())
    except Exception:  # noqa: BLE001
        proto_vol = 0.0

    rows = []
    for v in variants:
        v.ex.cancel_all(list(v.ex.specs))
        acct = v.ex.account()
        eq = float(acct["equity"])
        fills = v.ex.fills
        vol = sum(abs(f["quote"]) for f in fills)
        fees = sum(f["fee"] for f in fills)
        pnl = eq - cfg.paper_balance
        cost = -pnl
        vol_day = vol / elapsed_days
        cost_day = cost / elapsed_days
        share = vol_day / proto_vol if proto_vol else 0
        pts_lo, pts_hi = 300_000 / 7 * share, 950_000 / 7 * share
        open_pos = sum(abs(float(a) * float(v.ex.mid.get(pid, 0))) for pid, a in v.ex.pos.items())
        rows.append({
            "market": v.symbol, "offset_bps": v.offset_bps, "flow": "on" if v.flow else "off",
            "fills": len(fills), "volume_usd": round(vol, 2), "fees_usd": round(fees, 4),
            "pnl_usd": round(pnl, 4), "max_drawdown_usd": round(cfg.paper_balance - v.min_equity, 4),
            "open_pos_usd": round(open_pos, 2),
            "cost_per_1k_usd": round(cost / vol * 1000, 4) if vol else None,
            "volume_per_day_usd": round(vol_day), "cost_per_day_usd": round(cost_day, 2),
            "points_per_day_lo": round(pts_lo, 1), "points_per_day_hi": round(pts_hi, 1),
            "usd_per_point_mid": round(cost_day / ((pts_lo + pts_hi) / 2), 4) if pts_lo > 0 and cost_day > 0
            else (0.0 if pts_lo > 0 else None),
            "flow_blocks": v.bot.flow_blocks,
        })

    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    path = SCAN_DIR / f"scan_{datetime.now():%Y%m%d_%H%M}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return {"rows": rows, "csv": path, "minutes": (time.time() - start) / 60, "protocol_volume_24h": proto_vol,
            "markets": markets}


def min_fills_for(minutes: float) -> int:
    """Чем дольше скан, тем больше исполнений нужно, чтобы вариант попал в рейтинг (меньше случайности)."""
    return max(5, int(minutes / 3))


def best_rows(res: dict) -> list[dict]:
    mf = min_fills_for(res["minutes"])
    rows = [r for r in res["rows"] if r["fills"] >= mf and r["cost_per_1k_usd"] is not None]
    rows.sort(key=lambda r: (r["cost_per_1k_usd"], -r["volume_usd"]))
    return rows


def summarize(res: dict, limit: int = 12) -> str:
    min_fills = min_fills_for(res["minutes"])
    rows = best_rows(res)
    head = (f"Сканер: {res['minutes']:.0f} мин, рынков {len(res['markets'])}, вариантов {len(res['rows'])}.\n"
            f"Рейтинг (чем меньше $ на 1000$ объёма, тем дешевле поинты; минус = в плюсе):\n")
    if not rows:
        return head + f"Ни у одного варианта нет ≥{min_fills} исполнений — сканируй дольше."
    lines = []
    for i, r in enumerate(rows[:limit], 1):
        ppt = r["usd_per_point_mid"]
        ppt_s = "в плюсе" if ppt == 0.0 else (f"~{ppt:.2f}$/поинт" if ppt is not None else "—")
        lines.append(
            f"{i}. {r['market']} {r['offset_bps']:g} б.п. фильтр {r['flow']}: "
            f"{r['cost_per_1k_usd']:+.3f}$/1k, объём {r['volume_per_day_usd']:,}$/сут, "
            f"итог {r['cost_per_day_usd'] * -1:+.1f}$/сут, просадка {r['max_drawdown_usd']:.2f}$, {ppt_s}")
    best = rows[0]
    tail = (f"\n\nЛучший: {best['market']} с отступом {best['offset_bps']:g} б.п. "
            f"Подставь в config.yaml (markets → symbol/offset_bps) и проверь в тесте ещё сутки.\n"
            f"Таблица: {res['csv']}\n"
            "⚠️ Короткий скан = шум: результат в разное время суток разный. Надёжнее — несколько часов.")
    return head + "\n".join(lines) + tail
