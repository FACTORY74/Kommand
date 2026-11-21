"""FastAPI ingestion API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
import re
from uuid import uuid4
import warnings

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError

from kommand_central.bandwidth import ReceiveBandwidth, _ReceiveBandwidthMiddleware
from kommand_central.state import ChannelStore
from kommand_central.layout import LayoutStore
from kommand_central.video import VideoConflict, VideoDecodeError, VideoStreams


MAX_FRAME_BYTES = 25 * 1024 * 1024
MAX_FRAME_WIDTH = 8192
MAX_FRAME_HEIGHT = 8192
MAX_FRAME_PIXELS = 33_554_432
ALLOWED_MEDIA_TYPES = {"image/webp"}


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
) -> FastAPI:
    channel_store = store or ChannelStore()
    layouts = layout_store or LayoutStore()
    bandwidth = receive_bandwidth or ReceiveBandwidth()
    generation = uuid4().hex
    video_streams = VideoStreams(channel_store, MAX_FRAME_PIXELS)

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
                    "image_url": f"/api/v1/sources/{frame.source}/channels/{frame.channel}/frame",
                }
                for frame in channel_store.list()
            ]
        }

    @app.get("/api/v1/layout")
    def layout() -> dict[str, object]:
        return {"layout": layouts.snapshot()}

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
        return {"accepted": True, "revision": stored.revision, "generation": generation}

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
