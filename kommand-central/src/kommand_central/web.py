"""FastAPI ingestion API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
import re
from uuid import uuid4
import warnings

from fastapi import FastAPI, File, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError

from kommand_central.bandwidth import ReceiveBandwidth, _ReceiveBandwidthMiddleware
from kommand_central.state import ChannelStore
from kommand_central.layout import LayoutStore
from kommand_central.video import VideoConflict, VideoDecodeError, VideoStreams
from kommand_central.windows import DisplayLayouts
from kommand_common import ASSETS


MAX_FRAME_BYTES = 25 * 1024 * 1024
MAX_FRAME_WIDTH = 8192
MAX_FRAME_HEIGHT = 8192
MAX_FRAME_PIXELS = 33_554_432
ALLOWED_MEDIA_TYPES = {"image/webp"}


@dataclass(frozen=True, slots=True)
class _VideoPacket:
    content: bytes
    keyframe: bool
    stream: str
    width: int
    height: int
    sequence: int
    revision: int


@dataclass(eq=False)
class _VideoSubscriber:
    queue: asyncio.Queue[_VideoPacket | None] = field(default_factory=lambda: asyncio.Queue(maxsize=4))
    waiting: bool = True

    def offer(self, packet: _VideoPacket | None) -> bool:
        if self.queue.full():
            while not self.queue.empty():
                self.queue.get_nowait()
            self.waiting = True
        if packet is not None and self.waiting and not packet.keyframe:
            return True
        self.queue.put_nowait(packet)
        self.waiting = False
        return False


def _h264_keyframe(content: bytes) -> bool:
    return any(
        match.end() < len(content) and content[match.end()] & 0x1f == 5
        for match in re.finditer(b"\x00\x00(?:\x00)?\x01", content)
    )


def _validate_image(
    content: bytes,
    media_type: str,
    declared_width: int,
    declared_height: int,
) -> Image.Image:
    if not content:
        raise HTTPException(status_code=400, detail="Image is empty")
    if (
        declared_width > MAX_FRAME_WIDTH
        or declared_height > MAX_FRAME_HEIGHT
        or declared_width * declared_height > MAX_FRAME_PIXELS
    ):
        raise HTTPException(status_code=413, detail="Image dimensions exceed limits")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                actual_media_type = "image/webp" if image.format == "WEBP" else None
                if actual_media_type is None or actual_media_type != media_type:
                    raise HTTPException(
                        status_code=415,
                        detail="Image content does not match its type",
                    )
                image_width, image_height = image.size
                if (
                    image_width > MAX_FRAME_WIDTH
                    or image_height > MAX_FRAME_HEIGHT
                    or image_width * image_height > MAX_FRAME_PIXELS
                ):
                    raise HTTPException(status_code=413, detail="Image dimensions exceed limits")
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
                decoded_width, decoded_height = image.size
                decoded_image = image.convert("RGB")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=413, detail="Image exceeds pixel limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError) as exc:
        raise HTTPException(status_code=400, detail="Invalid or corrupt image") from exc

    if (
        decoded_width > MAX_FRAME_WIDTH
        or decoded_height > MAX_FRAME_HEIGHT
        or decoded_width * decoded_height > MAX_FRAME_PIXELS
    ):
        raise HTTPException(status_code=413, detail="Image dimensions exceed limits")
    if (decoded_width, decoded_height) != (declared_width, declared_height):
        raise HTTPException(status_code=400, detail="Declared dimensions do not match image")
    return decoded_image


def create_app(
    store: ChannelStore | None = None,
    layout_store: LayoutStore | None = None,
    receive_bandwidth: ReceiveBandwidth | None = None,
    display_layouts: DisplayLayouts | None = None,
) -> FastAPI:
    channel_store = store or ChannelStore()
    layouts = layout_store or LayoutStore()
    bandwidth = receive_bandwidth or ReceiveBandwidth()
    generation = uuid4().hex
    video_streams = VideoStreams(channel_store, MAX_FRAME_PIXELS)
    subscribers: dict[tuple[str, str], set[_VideoSubscriber]] = {}
    keyframe_requests: set[tuple[str, str]] = set()

    def publish(source: str, channel: str, packet: _VideoPacket | None) -> None:
        key = (source, channel)
        for subscriber in tuple(subscribers.get(key, ())):
            if subscriber.offer(packet):
                keyframe_requests.add(key)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def expire_video_streams() -> None:
            while True:
                await asyncio.sleep(30)
                video_streams.expire()

        task = asyncio.create_task(expire_video_streams())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            video_streams.clear()

    app = FastAPI(title="Kommand Central", version="0.1.0", lifespan=lifespan)
    app.state.channel_store = channel_store
    app.state.layout_store = layouts
    app.state.receive_bandwidth = bandwidth
    app.add_middleware(_ReceiveBandwidthMiddleware, bandwidth=bandwidth)

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon_svg() -> FileResponse:
        return FileResponse(ASSETS / "icon.svg", media_type="image/svg+xml")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon_ico() -> FileResponse:
        return FileResponse(ASSETS / "icon.ico", media_type="image/vnd.microsoft.icon")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "channels": len(channel_store.list())}

    @app.get("/api/v1/channels")
    def channels() -> dict[str, object]:
        return {
            "channels": [
                {
                    "source": frame.source,
                    "channel": frame.channel,
                    "width": frame.width,
                    "height": frame.height,
                    "captured_at": frame.captured_at,
                    "received_at": frame.received_at,
                    "revision": frame.revision,
                    "streaming": frame.streaming,
                    "image_url": f"/api/v1/sources/{frame.source}/channels/{frame.channel}/frame",
                }
                for frame in channel_store.list()
            ]
        }

    @app.get("/api/v1/layout")
    def layout() -> dict[str, object]:
        return {"layout": layouts.snapshot()}

    @app.get("/display/{window_id}")
    def browser_display(window_id: str) -> FileResponse:
        if display_layouts is None or window_id not in display_layouts.layouts:
            raise HTTPException(status_code=404, detail="Display window not found")
        return FileResponse(Path(__file__).with_name("browser_display.html"), media_type="text/html")

    @app.get("/api/v1/display/{window_id}")
    def browser_layout(window_id: str) -> dict[str, object]:
        selected = display_layouts.layouts.get(window_id) if display_layouts else None
        if selected is None:
            raise HTTPException(status_code=404, detail="Display window not found")
        snapshot, revision, name = selected.display_snapshot()
        return {"layout": snapshot, "revision": revision, "name": name, "generation": generation}

    @app.websocket("/api/v1/sources/{source}/channels/{channel}/video-stream")
    async def browser_video(websocket: WebSocket, source: str, channel: str) -> None:
        await websocket.accept()
        key = (source, channel)
        subscriber = _VideoSubscriber()
        subscribers.setdefault(key, set()).add(subscriber)
        keyframe_requests.add(key)

        async def receive_requests() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") == "keyframe":
                    keyframe_requests.add(key)
                    await websocket.send_json({"type": "keyframe-requested"})

        disconnect = asyncio.create_task(receive_requests())
        pending: asyncio.Task[_VideoPacket | None] | None = None
        try:
            while True:
                pending = asyncio.create_task(subscriber.queue.get())
                done, _ = await asyncio.wait((disconnect, pending), return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done:
                    disconnect.result()
                    break
                packet = pending.result()
                pending = None
                if packet is None:
                    break
                await websocket.send_json({
                    "keyframe": packet.keyframe, "stream": packet.stream,
                    "width": packet.width, "height": packet.height,
                    "sequence": packet.sequence, "revision": packet.revision,
                })
                await websocket.send_bytes(packet.content)
        except WebSocketDisconnect:
            pass
        finally:
            disconnect.cancel()
            if pending is not None:
                pending.cancel()
            subscribers[key].discard(subscriber)
            if not subscribers[key]:
                del subscribers[key]
                keyframe_requests.discard(key)
            for task in (disconnect, pending):
                if task is not None:
                    with suppress(asyncio.CancelledError, WebSocketDisconnect):
                        await task
            try:
                await websocket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass

    @app.get("/api/v1/sources/{source}/channels/{channel}/frame")
    def latest_frame(source: str, channel: str) -> Response:
        frame = channel_store.get(source, channel)
        if frame is None:
            raise HTTPException(status_code=404, detail="Channel has no frame")
        return Response(
            content=frame.content,
            media_type=frame.media_type,
            headers={"Cache-Control": "no-store", "X-Kommand-Revision": str(frame.revision)},
        )

    @app.post("/api/v1/sources/{source}/channels/{channel}/frames", status_code=202)
    async def upload_frame(
        source: str,
        channel: str,
        frame: UploadFile = File(...),
        protocol: str = Header("", alias="X-Kommand-Protocol"),
        captured_at: str = Header("", alias="X-Kommand-Captured-At"),
        width: int = Header(0, alias="X-Kommand-Width"),
        height: int = Header(0, alias="X-Kommand-Height"),
    ) -> dict[str, object]:
        if protocol != "1":
            raise HTTPException(status_code=400, detail="Unsupported Kommand protocol")
        if frame.content_type not in ALLOWED_MEDIA_TYPES:
            raise HTTPException(status_code=415, detail="Unsupported image type")
        if width <= 0 or height <= 0:
            raise HTTPException(status_code=400, detail="Invalid image dimensions")
        try:
            datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid capture timestamp") from exc
        content = await frame.read(MAX_FRAME_BYTES + 1)
        if len(content) > MAX_FRAME_BYTES:
            raise HTTPException(status_code=413, detail="Frame exceeds 25 MiB")
        image = _validate_image(content, frame.content_type, width, height)
        video_streams.discard(source, channel)
        publish(source, channel, None)
        stored = channel_store.update(
            source, channel, content, frame.content_type, width, height, captured_at, image
        )
        return {"accepted": True, "revision": stored.revision, "generation": generation}

    @app.post("/api/v1/sources/{source}/channels/{channel}/patches", status_code=202)
    async def upload_patch(
        source: str,
        channel: str,
        frame: UploadFile = File(...),
        protocol: str = Header("", alias="X-Kommand-Protocol"),
        captured_at: str = Header("", alias="X-Kommand-Captured-At"),
        width: int = Header(0, alias="X-Kommand-Width"),
        height: int = Header(0, alias="X-Kommand-Height"),
        x: int = Header(-1, alias="X-Kommand-X"),
        y: int = Header(-1, alias="X-Kommand-Y"),
        base_revision: int = Header(0, alias="X-Kommand-Base-Revision"),
        base_generation: str = Header("", alias="X-Kommand-Generation"),
    ) -> dict[str, object]:
        if protocol != "1":
            raise HTTPException(status_code=400, detail="Unsupported Kommand protocol")
        if frame.content_type != "image/webp":
            raise HTTPException(status_code=415, detail="Unsupported image type")
        if width <= 0 or height <= 0 or x < 0 or y < 0 or base_revision <= 0:
            raise HTTPException(status_code=400, detail="Invalid patch metadata")
        if base_generation != generation:
            raise HTTPException(status_code=409, detail="Server restarted; send a full frame")
        try:
            datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid capture timestamp") from exc
        content = await frame.read(MAX_FRAME_BYTES + 1)
        if len(content) > MAX_FRAME_BYTES:
            raise HTTPException(status_code=413, detail="Frame exceeds 25 MiB")
        image = _validate_image(content, frame.content_type, width, height)
        try:
            stored = channel_store.apply_patch(
                source, channel, base_revision, x, y, image, captured_at
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        video_streams.discard(source, channel)
        publish(source, channel, None)
        return {"accepted": True, "revision": stored.revision, "generation": generation}

    @app.post("/api/v1/sources/{source}/channels/{channel}/video", status_code=202)
    async def upload_video(
        source: str,
        channel: str,
        frame: UploadFile = File(...),
        protocol: str = Header("", alias="X-Kommand-Protocol"),
        captured_at: str = Header("", alias="X-Kommand-Captured-At"),
        width: int = Header(0, alias="X-Kommand-Width"),
        height: int = Header(0, alias="X-Kommand-Height"),
        stream_id: str = Header("", alias="X-Kommand-Video-Stream"),
        sequence: int = Header(-1, alias="X-Kommand-Video-Sequence"),
        base_generation: str = Header("", alias="X-Kommand-Generation"),
    ) -> dict[str, object]:
        if protocol != "1":
            raise HTTPException(status_code=400, detail="Unsupported Kommand protocol")
        if frame.content_type != "video/h264":
            raise HTTPException(status_code=415, detail="Unsupported video type")
        if width <= 0 or height <= 0 or sequence < 0 or not re.fullmatch(r"[0-9a-f]{32}", stream_id):
            raise HTTPException(status_code=400, detail="Invalid video metadata")
        if (
            width > MAX_FRAME_WIDTH or height > MAX_FRAME_HEIGHT
            or width * height > MAX_FRAME_PIXELS
            or (width + width % 2) * (height + height % 2) > MAX_FRAME_PIXELS
        ):
            raise HTTPException(status_code=413, detail="Video dimensions exceed limits")
        if sequence != 0 and base_generation != generation:
            raise HTTPException(status_code=409, detail="Server restarted; send a new keyframe")
        try:
            datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid capture timestamp") from exc
        content = await frame.read(MAX_FRAME_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail="Video frame is empty")
        if len(content) > MAX_FRAME_BYTES:
            raise HTTPException(status_code=413, detail="Video frame exceeds 25 MiB")
        video_streams.expire()
        try:
            stored = video_streams.receive(
                source, channel, stream_id, sequence, width, height, content, captured_at
            )
        except VideoConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VideoDecodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        keyframe = _h264_keyframe(content)
        publish(source, channel, _VideoPacket(content, keyframe, stream_id, width, height, sequence, stored.revision))
        request_keyframe = (source, channel) in keyframe_requests and not keyframe
        keyframe_requests.discard((source, channel))
        return {
            "accepted": True, "revision": stored.revision, "generation": generation,
            "request_keyframe": request_keyframe,
        }

    @app.post("/api/v1/sources/{source}/channels/{channel}/heartbeat")
    def heartbeat(
        source: str,
        channel: str,
        protocol: str = Header("", alias="X-Kommand-Protocol"),
        captured_at: str = Header("", alias="X-Kommand-Captured-At"),
        base_revision: int = Header(0, alias="X-Kommand-Base-Revision"),
        base_generation: str = Header("", alias="X-Kommand-Generation"),
    ) -> dict[str, object]:
        if protocol != "1":
            raise HTTPException(status_code=400, detail="Unsupported Kommand protocol")
        try:
            datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid capture timestamp") from exc
        if base_generation != generation:
            raise HTTPException(status_code=409, detail="Server restarted; send a full frame")
        try:
            current = channel_store.heartbeat(source, channel, base_revision, captured_at)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"accepted": True, "revision": current.revision, "generation": generation}

    return app
