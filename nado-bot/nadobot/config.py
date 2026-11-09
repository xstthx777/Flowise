from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config.yaml"
# Публичная страница подписи (web/sign.html), закреплённая на коммите — её нельзя подменить незаметно
DEFAULT_SIGN_PAGE = "https://rawcdn.githack.com/xstthx777/Flowise/d496dcccd3dd07d811a3e9113081f0daeb519966/nado-bot/web/sign.html"


def load_env(path: Path = ENV_PATH) -> None:
    """Мини-загрузчик .env (KEY=VALUE), чтобы не тянуть лишних зависимостей."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class MarketCfg:
    symbol: str
    order_size_usd: float = 120
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
    funding_max_daily: float = 0.0005  # 0.05%/сутки: дороже — не набираем позицию в платящую сторону
    fills_sync_s: float = 60
    reject_pause_s: float = 300
    mainnet_live_confirm: bool = False
    sign_page_url: str = DEFAULT_SIGN_PAGE
    paper: bool = False  # тестовый режим без кошелька
    paper_balance: float = 200
    markets: list[MarketCfg] = field(default_factory=list)

    owner: str = ""  # адрес MetaMask (публичный)
    bot_key: str = ""  # приватный ключ ОТДЕЛЬНОГО бот-кошелька
    tg_token: str = ""
    tg_chat: str = ""


def set_env_value(key: str, value: str, path: Path = ENV_PATH) -> None:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    lines = [line for line in lines if not line.strip().startswith(key + "=")]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def set_config_value(key: str, value: str, path: Path = CONFIG_PATH) -> None:
    """Меняет «key: value» верхнего уровня в config.yaml, сохраняя комментарии."""
    import re

    text = path.read_text(encoding="utf-8-sig")
    pat = re.compile(rf"^{re.escape(key)}:[^\n#]*", re.M)
    text = pat.sub(f"{key}: {value}", text, count=1) if pat.search(text) else text + f"\n{key}: {value}\n"
    path.write_text(text, encoding="utf-8")


def load_config(path: str | Path = CONFIG_PATH) -> Config:
    load_env()
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {}
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
