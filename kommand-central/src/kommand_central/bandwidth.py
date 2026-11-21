"""Incoming HTTP request-body bandwidth tracking."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from kommand_common.bandwidth import RollingBandwidth


class ReceiveBandwidth(RollingBandwidth):
    pass


class _ReceiveBandwidthMiddleware:
    def __init__(self, app: ASGIApp, bandwidth: ReceiveBandwidth) -> None:
        self.app = app
        self.bandwidth = bandwidth

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def receive_and_record() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                self.bandwidth.record(len(message.get("body", b"")))
            return message

        await self.app(scope, receive_and_record, send)
