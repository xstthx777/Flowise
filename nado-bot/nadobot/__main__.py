"""CLI: python -m nadobot <команда>

  keygen          создать бот-кошелёк и записать ключ в .env
  link            привязать бот-ключ к твоему аккаунту Nado через MetaMask
  unlink          отвязать бот-ключ (через MetaMask)
  status          equity, позиции, linked signer, комиссии
  run             запустить бота
  panic           снять все ордера и закрыть позиции
  report          затраты и цена поинта
  points <N>      записать текущее общее число поинтов (смотри в app.nado.xyz)
  flow <+/-$>     записать депозит (+) или вывод (−), чтобы отчёт был честным
"""
from __future__ import annotations

import logging
import os
import sys

from .config import ENV_PATH, load_config, load_env


def _setup_logging():
    from .config import ROOT
    (ROOT / "data").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "data" / "bot.log", encoding="utf-8")],
    )


def keygen():
    from eth_account import Account

    load_env()
    if os.environ.get("NADO_BOT_KEY"):
        print("NADO_BOT_KEY уже есть в .env — новый не создаю (иначе потеряешь привязку).")
        print("Адрес бот-кошелька:", Account.from_key(os.environ["NADO_BOT_KEY"]).address)
        return
    acct = Account.create()
    lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines() if ENV_PATH.exists() else []
    lines = [line for line in lines if not line.startswith("NADO_BOT_KEY=")]
    lines.append(f"NADO_BOT_KEY={acct.key.hex()}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":  # на Windows chmod не ограничивает доступ — см. README
        os.chmod(ENV_PATH, 0o600)
    print("Бот-кошелёк создан:", acct.address)
    print("Ключ записан в .env (права 600). На этот адрес деньги НЕ отправляй — он только подписывает ордера.")


def _utf8_console():
    """Windows-консоль по умолчанию в cp866/cp1251 и падает на «≥ → ⚠»."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str]):
    _utf8_console()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "keygen":
        return keygen()

    _setup_logging()
    if cmd in ("points", "flow", "report"):
        from .ledger import Ledger
        led = Ledger()
        if cmd == "points":
            led.set_points(float(args[0]))
        elif cmd == "flow":
            led.flow(float(args[0]), " ".join(args[1:]))
        print(led.report()["text"])
        return

    cfg = load_config()
    from .exchange import Exchange

    if cmd in ("link", "unlink"):
        from .link import run_link_server
        ex = Exchange(cfg.mode, cfg.bot_key, cfg.owner, cfg.subaccount_name)
        ok = run_link_server(ex, unlink=(cmd == "unlink"))
        print("Успешно." if ok else "Не получилось — смотри сообщение на странице.")
    elif cmd == "status":
        ex = Exchange(cfg.mode, cfg.bot_key, cfg.owner, cfg.subaccount_name)
        a = ex.account()
        print(f"Сеть: {cfg.mode}\nMetaMask: {ex.owner}\nБот-кошелёк: {ex.bot.address}")
        print(f"Субаккаунт существует: {a['exists']}\nEquity: {a['equity']:.2f}$  "
              f"health ratio: {float(a['health_ratio']):.2f}")
        print("Позиции:", {k: str(v) for k, v in a["positions"].items() if v != 0} or "нет")
        print("Linked signer:", ex.linked_signer(), "(бот привязан)" if ex.is_bot_linked() else "(НЕ бот)")
        for sym, sp in ex.market_specs([m.symbol for m in cfg.markets]).items():
            bid, ask = ex.best_bid_ask(sp.product_id)
            try:
                fr = f"{float(ex.funding_rate_daily(sp.product_id)) * 100:.3f}%/сутки"
            except Exception:  # noqa: BLE001
                fr = "?"
            print(f"{sym}: bid {bid} / ask {ask}, мин. ордер {sp.min_size}$, фандинг {fr}")
        try:
            fees = ex.fee_rates()
            print("Твои комиссии (по product_id в config): ", {
                m: (f"maker {int(fees.maker_fee_rates_x18[sp.product_id]) / 1e14:.2f} б.п., "
                    f"taker {int(fees.taker_fee_rates_x18[sp.product_id]) / 1e14:.2f} б.п.")
                for m, sp in ex.market_specs([m.symbol for m in cfg.markets]).items()})
        except Exception as e:  # noqa: BLE001
            print("Комиссии: не удалось получить:", e)
    elif cmd == "run":
        from .bot import Bot
        Bot(cfg).run()
    elif cmd == "panic":
        from .bot import Bot
        cfg.dry_run = True  # обходим защиту mainnet_live_confirm: panic только закрывает
        b = Bot(cfg)
        b.cfg.dry_run = False
        b.flatten("ручной panic")
        print("Ордера сняты, позиции закрыты (если были).")
    else:
        print("Неизвестная команда.\n", __doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
