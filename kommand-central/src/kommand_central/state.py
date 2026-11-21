"""Thread-safe latest-frame channel state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from io import BytesIO
import threading

from PIL import Image


@dataclass(frozen=True, slots=True)
class ChannelFrame:
    source: str
    channel: str
    _content: bytes | None
    image: Image.Image
    media_type: str
    width: int
    height: int
    captured_at: str
    received_at: str
    revision: int
    streaming: bool = False

    @property
    def content(self) -> bytes:
        if self._content is not None:
            return self._content
        encoded = BytesIO()
        self.image.save(encoded, format="WEBP", lossless=True)
        return encoded.getvalue()


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
        image: Image.Image,
    ) -> ChannelFrame:
        with self._lock:
            self._revision += 1
            frame = ChannelFrame(
                source=source,
                channel=channel,
                _content=content,
                image=image,
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
        image: Image.Image,
        expected_revision: int | None,
    ) -> ChannelFrame:
        with self._lock:
            current = self._frames.get((source, channel))
            if expected_revision is not None and (
                current is None or current.revision != expected_revision
            ):
                raise ValueError("Frame revision mismatch; send a full frame")
            frame = replace(
                self.update(source, channel, content, media_type, width, height, captured_at, image),
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
