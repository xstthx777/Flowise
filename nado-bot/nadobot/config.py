from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """Мини-загрузчик .env (KEY=VALUE), чтобы не тянуть лишних зависимостей."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class MarketCfg:
    symbol: str
    order_size_usd: float = 50
    offset_bps: float = 4
    max_position_usd: float = 150
    hard_position_usd: float = 300
    skew_bps: float = 3


@dataclass
class Config:
    mode: str = "testnet"  # testnet | mainnet
    dry_run: bool = True
    subaccount_name: str = "default"
    loop_interval_s: float = 3.0
    requote_bps: float = 2.0
    order_ttl_s: int = 120
    vol_window_s: float = 60
    vol_max_move_bps: float = 40
    vol_pause_s: float = 120
    max_daily_loss_usd: float = 20
    min_health_ratio: float = 0.5
    markets: list[MarketCfg] = field(default_factory=list)

    owner: str = ""  # адрес MetaMask (публичный)
    bot_key: str = ""  # приватный ключ ОТДЕЛЬНОГО бот-кошелька
    tg_token: str = ""
    tg_chat: str = ""


def load_config(path: str | Path = ROOT / "config.yaml") -> Config:
    load_env()
    raw = yaml.safe_load(Path(path).read_text()) or {}
    markets = [MarketCfg(**m) for m in raw.pop("markets", [])]
    cfg = Config(**raw, markets=markets)
    cfg.owner = os.environ.get("NADO_OWNER", "")
    cfg.bot_key = os.environ.get("NADO_BOT_KEY", "")
    cfg.tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    cfg.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if cfg.mode not in ("testnet", "mainnet"):
        raise ValueError("mode должен быть testnet или mainnet")
    if not cfg.markets:
        raise ValueError("в config.yaml не задан ни один рынок")
    for m in cfg.markets:
        if not (m.max_position_usd < m.hard_position_usd):
            raise ValueError(f"{m.symbol}: max_position_usd должен быть меньше hard_position_usd")
    return cfg
