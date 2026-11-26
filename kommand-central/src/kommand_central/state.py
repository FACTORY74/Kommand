"""Thread-safe latest-frame channel state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from io import BytesIO
import threading

from PIL import Image
from av import VideoFrame


@dataclass(frozen=True, slots=True)
class ChannelFrame:
    source: str
    channel: str
    _content: bytes | None
    _image: Image.Image | None
    media_type: str
    width: int
    height: int
    captured_at: str
    received_at: str
    revision: int
    streaming: bool = False
    _decoded: VideoFrame | None = field(default=None, repr=False, compare=False)
    _image_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @property
    def image(self) -> Image.Image:
        with self._image_lock:
            if self._image is None:
                if self._decoded is None:
                    raise RuntimeError("Channel has no decoded image.")
                image = self._decoded.to_image()
                if image.size != (self.width, self.height):
                    image = image.crop((0, 0, self.width, self.height))
                object.__setattr__(self, "_image", image)
            assert self._image is not None
            return self._image

    @property
    def content(self) -> bytes:
        if self._content is not None:
            return self._content
        with self._image_lock:
            if self._content is None:
                encoded = BytesIO()
                self.image.save(encoded, format="WEBP", lossless=True)
                object.__setattr__(self, "_content", encoded.getvalue())
            assert self._content is not None
            return self._content


class ChannelStore:
    def __init__(self) -> None:
        self._frames: dict[tuple[str, str], ChannelFrame] = {}
        self._revision = 0
        self._lock = threading.RLock()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def update(
        self,
        source: str,
        channel: str,
        content: bytes | None,
        media_type: str,
        width: int,
        height: int,
        captured_at: str,
        image: Image.Image | None,
        decoded: VideoFrame | None = None,
    ) -> ChannelFrame:
        if image is None and decoded is None:
            raise ValueError("A channel update must contain a decoded frame.")
        with self._lock:
            self._revision += 1
            frame = ChannelFrame(
                source=source,
                channel=channel,
                _content=content,
                _image=image,
                _decoded=decoded,
                media_type=media_type,
                width=width,
                height=height,
                captured_at=captured_at,
                received_at=datetime.now(timezone.utc).isoformat(),
                revision=self._revision,
            )
            self._frames[(source, channel)] = frame
            return frame

    def apply_patch(
        self,
        source: str,
        channel: str,
        base_revision: int,
        x: int,
        y: int,
        image: Image.Image,
        captured_at: str,
    ) -> ChannelFrame:
        with self._lock:
            frame = self._frames.get((source, channel))
            if frame is None or frame.revision != base_revision:
                raise ValueError("Frame revision mismatch; send a full frame")
            if x < 0 or y < 0 or x + image.width > frame.width or y + image.height > frame.height:
                raise ValueError("Patch is outside the frame")
            combined = frame.image.copy()
            combined.paste(image, (x, y))
            return self.update(
                source, channel, None, "image/webp",
                frame.width, frame.height, captured_at, combined,
            )

    def update_if_revision(
        self,
        source: str,
        channel: str,
        content: bytes | None,
        media_type: str,
        width: int,
        height: int,
        captured_at: str,
        image: Image.Image | None,
        expected_revision: int | None,
        decoded: VideoFrame | None = None,
    ) -> ChannelFrame:
        with self._lock:
            current = self._frames.get((source, channel))
            if expected_revision is not None and (
                current is None or current.revision != expected_revision
            ):
                raise ValueError("Frame revision mismatch; send a full frame")
            frame = replace(
                self.update(source, channel, content, media_type, width, height, captured_at, image, decoded),
                streaming=True,
            )
            self._frames[(source, channel)] = frame
            return frame

    def get(self, source: str, channel: str) -> ChannelFrame | None:
        with self._lock:
            return self._frames.get((source, channel))

    def heartbeat(
        self, source: str, channel: str, base_revision: int, captured_at: str
    ) -> ChannelFrame:
        with self._lock:
            frame = self._frames.get((source, channel))
            if frame is None or frame.revision != base_revision:
                raise ValueError("Frame revision mismatch; send a full frame")
            refreshed = replace(
                frame, captured_at=captured_at, received_at=datetime.now(timezone.utc).isoformat()
            )
            self._frames[(source, channel)] = refreshed
            return refreshed

    def list(self) -> list[ChannelFrame]:
        with self._lock:
            return sorted(self._frames.values(), key=lambda item: (item.source, item.channel))
