"""平台无关的客服领域契约。

连接器只负责把平台消息翻译为 CanonicalEvent，客服核心不接触 Cookie、Token
或具体平台页面结构。真实连接器接入时，优先实现官方 API，再实现独立浏览器
Profile 的 listen/send/health 适配层。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterable, Literal, Protocol, TypedDict


Platform = Literal["fliggy", "douyin", "meituan", "qunar", "dianping", "ctrip", "tongcheng"]
MessageType = Literal["text", "image", "card", "red_packet", "system", "unsupported"]


class MessageContent(TypedDict, total=False):
    type: MessageType
    text: str
    url: str
    alt: str


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str
    platform: Platform
    platform_account_id: str
    external_conversation_id: str
    external_message_id: str
    direction: Literal["inbound", "outbound"]
    sender_id: str
    content: MessageContent
    occurred_at: datetime
    sender_name: str | None = None
    conversation_id_source: str = 'platform'
    message_id_source: str = 'platform'


@dataclass(frozen=True)
class OutboundMessageCommand:
    account_id: str
    conversation_id: str
    content: MessageContent
    idempotency_key: str


@dataclass(frozen=True)
class DeliveryReceipt:
    accepted: bool
    external_message_id: str | None
    status: Literal["sent", "pending", "failed"]
    error: str | None = None


@dataclass(frozen=True)
class AccountHealth:
    status: Literal["connected", "needs_login", "paused", "error"]
    detail: str | None = None


class PlatformAdapter(Protocol):
    platform: Platform

    async def connect(self, account_id: str) -> AccountHealth: ...

    async def disconnect(self, account_id: str) -> None: ...

    def listen(self, account_id: str, cursor: str | None = None) -> AsyncIterable[CanonicalEvent]: ...

    async def send_message(self, command: OutboundMessageCommand) -> DeliveryReceipt: ...

    async def health(self, account_id: str) -> AccountHealth: ...


class ProductProvider(Protocol):
    async def get_product_context(self, product_id: str) -> dict[str, object]: ...


class InventoryProvider(Protocol):
    async def query_availability(self, product_id: str, query: dict[str, object]) -> dict[str, object]: ...


class OrderProvider(Protocol):
    async def get_order(self, order_id: str) -> dict[str, object]: ...
