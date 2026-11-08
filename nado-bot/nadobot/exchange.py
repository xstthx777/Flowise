"""Обёртка над официальным nado-protocol SDK.

Бот подписывает ордера ключом бот-кошелька (linked signer), а торгует
от имени субаккаунта твоего MetaMask-адреса (owner). Ключ MetaMask бот не видит.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from eth_account import Account
from web3 import Web3

from nado_protocol.client import NadoClientMode, create_nado_client
from nado_protocol.engine_client.types.execute import (
    CancelProductOrdersParams,
    LinkSignerParams,
    OrderParams,
    PlaceOrderParams,
)
from nado_protocol.utils.bytes32 import subaccount_to_hex
from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
from nado_protocol.utils.nonce import gen_order_nonce
from nado_protocol.utils.order import build_appendix

from .strategy import MarketSpec

log = logging.getLogger(__name__)
X18 = Decimal(10) ** 18
ZERO_SIGNER = "0x" + "00" * 32


def to_x18(x: Decimal) -> int:
    return int(x * X18)


def from_x18(x) -> Decimal:
    return Decimal(int(x)) / X18


def signer_bytes32(address: str) -> str:
    """Адрес бот-кошелька в формате bytes32: 20 байт адреса + 12 нулевых."""
    return subaccount_to_hex(Web3.to_checksum_address(address), "")


class Exchange:
    def __init__(self, mode: str, bot_key: str, owner: str, subaccount_name: str = "default"):
        if not bot_key:
            raise ValueError("NADO_BOT_KEY не задан — сначала `python -m nadobot keygen`")
        if not owner:
            raise ValueError("NADO_OWNER (адрес MetaMask) не задан в .env")
        self.bot = Account.from_key(bot_key)
        self.owner = Web3.to_checksum_address(owner)
        if self.owner == self.bot.address:
            raise ValueError("NADO_OWNER должен быть адресом MetaMask, а не бот-кошелька")
        self.client = create_nado_client(NadoClientMode(mode), self.bot)
        self.engine = self.client.context.engine_client
        self.sub_hex = subaccount_to_hex(self.owner, subaccount_name)

    # ---------- рынки ----------
    def market_specs(self, symbols: list[str]) -> dict[str, MarketSpec]:
        data = self.engine.get_symbols(product_type="perp").symbols
        out = {}
        for sym in symbols:
            s = data.get(sym)
            if s is None:
                raise ValueError(f"рынок {sym} не найден. Доступны: {', '.join(sorted(data))}")
            out[sym] = MarketSpec(
                symbol=sym,
                product_id=int(s.product_id),
                price_increment=from_x18(s.price_increment_x18),
                size_increment=from_x18(s.size_increment),
                min_size=from_x18(s.min_size),
            )
        return out

    def best_bid_ask(self, product_id: int) -> tuple[Decimal, Decimal]:
        p = self.engine.get_market_price(product_id)
        return from_x18(p.bid_x18), from_x18(p.ask_x18)

    # ---------- аккаунт ----------
    def account(self) -> dict:
        """equity = нетто стоимость аккаунта; health_ratio = maintenance health / equity."""
        info = self.engine.get_subaccount_info(self.sub_hex)
        healths = [from_x18(h.health) for h in info.healths]  # [initial, maintenance, unweighted]
        equity = healths[2] if len(healths) > 2 else healths[-1]
        maint = healths[1] if len(healths) > 1 else equity
        positions = {pb.product_id: from_x18(pb.balance.amount) for pb in info.perp_balances}
        return {
            "exists": info.exists,
            "equity": equity,
            "maint_health": maint,
            "health_ratio": (maint / equity) if equity > 0 else Decimal(0),
            "positions": positions,
        }

    def linked_signer(self) -> str:
        return self.engine.get_linked_signer(self.sub_hex).linked_signer

    def is_bot_linked(self) -> bool:
        linked = self.linked_signer().lower()
        return linked[:42] == self.bot.address.lower()

    def fee_rates(self):
        return self.client.subaccount.get_subaccount_fee_rates(self.sub_hex)

    # ---------- торговля ----------
    def place_post_only(self, spec: MarketSpec, price: Decimal, size: Decimal, ttl_s: int):
        """size > 0 — покупка, size < 0 — продажа."""
        order = OrderParams(
            sender=self.sub_hex,
            priceX18=to_x18(price),
            amount=to_x18(size),
            expiration=get_expiration_timestamp(ttl_s),
            nonce=gen_order_nonce(),
            appendix=build_appendix(order_type=OrderType.POST_ONLY),
        )
        return self.engine.place_order(PlaceOrderParams(product_id=spec.product_id, order=order))

    def cancel_all(self, product_ids: list[int]):
        return self.engine.cancel_product_orders(
            CancelProductOrdersParams(sender=self.sub_hex, productIds=product_ids)
        )

    def open_orders(self, product_id: int):
        return self.engine.get_subaccount_open_orders(product_id, self.sub_hex).orders

    def close_position(self, product_id: int):
        return self.engine.close_position(self.sub_hex, product_id)

    # ---------- привязка бот-ключа через MetaMask ----------
    def link_signer_typed_data(self, unlink: bool = False) -> dict:
        """EIP-712 данные, которые подпишет MetaMask (eth_signTypedData_v4)."""
        from nado_protocol.contracts.eip712.sign import build_eip712_typed_data
        from nado_protocol.contracts.types import NadoTxType

        nonce = self.engine.tx_nonce(self.sub_hex)
        msg = {
            "sender": self.sub_hex,
            "signer": ZERO_SIGNER if unlink else signer_bytes32(self.bot.address),
            "nonce": int(nonce),  # MetaMask: number (tx nonce мал, безопасно для JS)
        }
        td = build_eip712_typed_data(
            NadoTxType.LINK_SIGNER, msg, self.engine.endpoint_addr, self.engine.chain_id
        )
        return td.model_dump()

    def submit_link_signer(self, typed_message: dict, signature: str):
        params = LinkSignerParams(
            sender=typed_message["sender"],
            signer=typed_message["signer"],
            nonce=int(typed_message["nonce"]),
            signature=signature,
        )
        return self.engine.link_signer(params)
