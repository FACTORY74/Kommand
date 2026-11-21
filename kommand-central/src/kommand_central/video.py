"""Bounded-lifetime H.264 decoder state for incoming channels."""

from dataclasses import dataclass
import threading
import time
from typing import Callable

import av
from av.error import FFmpegError

from kommand_central.state import ChannelFrame, ChannelStore


class VideoConflict(ValueError):
    pass


class VideoDecodeError(ValueError):
    pass


@dataclass(slots=True)
class _Stream:
    stream_id: str
    decoder: av.VideoCodecContext
    next_sequence: int
    width: int
    height: int
    revision: int
    last_seen: float


class VideoStreams:
    def __init__(
        self, store: ChannelStore, max_pixels: int,
        clock: Callable[[], float] = time.monotonic, idle_seconds: float = 300,
    ) -> None:
        self._store = store
        self._max_pixels = max_pixels
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._streams: dict[tuple[str, str], _Stream] = {}
        self._lock = threading.Lock()

    def discard(self, source: str, channel: str) -> None:
        with self._lock:
            self._streams.pop((source, channel), None)

    def clear(self) -> None:
        with self._lock:
            self._streams.clear()

    def expire(self) -> None:
        with self._lock:
            cutoff = self._clock() - self._idle_seconds
            for key, stream in list(self._streams.items()):
                if stream.last_seen <= cutoff:
                    del self._streams[key]

    def receive(
        self, source: str, channel: str, stream_id: str, sequence: int,
        width: int, height: int, content: bytes, captured_at: str,
    ) -> ChannelFrame:
        key = (source, channel)
        with self._lock:
            previous = self._streams.get(key)
            if sequence == 0:
                decoder = av.CodecContext.create("h264", "r")
                decoder.options = {"max_pixels": str(self._max_pixels), "threads": "1"}
                decoder.open()
                if "max_pixels" in decoder.options:
                    raise RuntimeError("H.264 decoder cannot enforce the image pixel limit.")
            else:
                if (
                    previous is None or previous.stream_id != stream_id
                    or previous.next_sequence != sequence
                    or (previous.width, previous.height) != (width, height)
                ):
                    raise VideoConflict("Video stream is out of sync; send a new keyframe.")
                decoder = previous.decoder
            try:
                frames = decoder.decode(av.Packet(content))
                if len(frames) != 1 or (sequence == 0 and not frames[0].key_frame):
                    raise VideoDecodeError("Invalid or incomplete H.264 frame.")
                frame = frames[0]
                if (frame.width, frame.height) != (width + width % 2, height + height % 2):
                    raise VideoDecodeError("Video dimensions do not match the declared size.")
                image = frame.to_image().crop((0, 0, width, height))
                stored = self._store.update_if_revision(
                    source, channel, None, "image/webp", width, height, captured_at,
                    image, previous.revision if sequence != 0 and previous is not None else None,
                )
            except (FFmpegError, VideoDecodeError, ValueError) as exc:
                if sequence != 0:
                    self._streams.pop(key, None)
                if isinstance(exc, VideoDecodeError):
                    raise
                if isinstance(exc, FFmpegError):
                    raise VideoDecodeError("Invalid or corrupt H.264 frame.") from exc
                raise VideoConflict("Channel changed; send a new keyframe.") from exc
            self._streams[key] = _Stream(
                stream_id, decoder, sequence + 1, width, height, stored.revision, self._clock()
            )
            return stored
