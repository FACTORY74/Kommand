"""Persistent H.264 publishing and HTTP health checks."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from av.error import FFmpegError
import requests
from PIL import Image, ImageChops
from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from kommand_source.capture import CapturedFrame
from kommand_source.config import ChannelConfig, SourceConfig
from kommand_common.bandwidth import RollingBandwidth
from kommand_source.transport.video import VideoEncoder, VideoEncodingError


class UploadError(RuntimeError):
    """Central rejected a frame or could not be reached."""


class CentralConnectionError(RuntimeError):
    """Central's health endpoint could not be reached successfully."""


def _error_explanation(error: Exception) -> str:
    """Keep transport wrappers and request URLs out of the status table."""
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, requests.HTTPError) and error.response is not None:
            return f"HTTP {error.response.status_code}: {error.response.reason}"
        nested = (
            error.__cause__
            or getattr(error, "reason", None)
            or next((arg for arg in error.args if isinstance(arg, Exception)), None)
            or error.__context__
        )
        if not isinstance(nested, Exception) or id(nested) in seen:
            break
        error = nested
    message = getattr(error, "strerror", None) or str(error)
    message = " ".join(message.split()) or type(error).__name__
    return message if len(message) <= 200 else message[:197] + "..."


def check_central_connection(central_url: str, timeout: float = 3.0) -> None:
    url = f"{central_url.rstrip('/')}/health"
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CentralConnectionError(_error_explanation(exc)) from exc


class CentralClient:
    def __init__(self, config: SourceConfig, bandwidth: RollingBandwidth | None = None):
        self.config = config
        self._previous: Image.Image | None = None
        self._revision: int | None = None
        self._generation: str | None = None
        self._video: VideoEncoder | None = None
        self._video_key: tuple[object, ...] | None = None
        self._video_stream: str | None = None
        self._last_video_keyframe = 0.0
        self._force_video_keyframe = False
        self._socket: ClientConnection | None = None
        self._bandwidth = bandwidth
        self._last_heartbeat = float("-inf")

    def upload(self, channel: ChannelConfig, frame: CapturedFrame) -> None:
        if frame.image is not None:
            current = frame.image
        else:
            with Image.open(BytesIO(frame.content)) as encoded:
                current = encoded.convert("RGB")
        source = quote(self.config.source_name, safe="")
        channel_name = quote(channel.name, safe="")
        base_url = (
            f"{self.config.central_url.rstrip('/')}"
            f"/api/v1/sources/{source}/channels/{channel_name}"
        )
        self._upload_video(channel, frame, current, base_url)

    def _upload_video(
        self, channel: ChannelConfig, frame: CapturedFrame, image: Image.Image, base_url: str
    ) -> None:
        key = (
            channel.name, image.size, frame.alignment, channel.video_fps,
            channel.quality, channel.full_frame_interval_seconds,
        )
        try:
            if self._video is None or self._video_key != key:
                self._reset()
                self._video = VideoEncoder(
                    image.width, image.height, channel.video_fps,
                    channel.quality, channel.full_frame_interval_seconds,
                )
                self._video_key = key
                self._video_stream = uuid4().hex
            sequence = self._video.sequence
            now = time.monotonic()
            force_keyframe = sequence > 0 and (
                self._force_video_keyframe or now - self._last_video_keyframe >= channel.full_frame_interval_seconds
            )
            unchanged = (
                self._previous is not None and self._previous.size == image.size
                and ImageChops.difference(self._previous, image).getbbox() is None
            )
            if unchanged and not force_keyframe:
                if now - self._last_heartbeat < 1:
                    return
                result = self._send_video(base_url, {
                    "type": "heartbeat", "revision": self._revision,
                    "generation": self._generation,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                })
                self._force_video_keyframe = result.get("request_keyframe", False) is True
                self._last_heartbeat = now
                return
            content = self._video.encode(image, force_keyframe=force_keyframe)
            if sequence == 0 or force_keyframe:
                self._last_video_keyframe = now
        except (FFmpegError, VideoEncodingError, ValueError) as exc:
            self._reset()
            raise UploadError(f"Could not encode video: {exc}") from exc
        assert self._video_stream is not None
        result = self._send_video(base_url, {
            "type": "frame", "protocol": "1",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "width": image.width, "height": image.height,
            "stream": self._video_stream, "sequence": sequence,
            "generation": self._generation or "",
            "timestamp_us": time.monotonic_ns() // 1000,
        }, content)
        self._revision = result["revision"]
        self._generation = result["generation"]
        self._force_video_keyframe = result.get("request_keyframe", False) is True
        self._previous = image.copy()

    def _send_video(
        self, base_url: str, metadata: dict[str, object], content: bytes | None = None,
    ) -> dict[str, Any]:
        try:
            if self._socket is None:
                url = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                self._socket = connect(
                    f"{url}/video-upload", open_timeout=self.config.request_timeout_seconds,
                    close_timeout=1, max_size=4096, compression=None,
                )
            text = json.dumps(metadata)
            self._socket.send(text)
            if self._bandwidth is not None:
                self._bandwidth.record(len(text.encode("utf-8")))
            if content is not None:
                self._socket.send(content)
                if self._bandwidth is not None:
                    self._bandwidth.record(len(content))
            result = json.loads(self._socket.recv(timeout=self.config.request_timeout_seconds))
            if not isinstance(result, dict):
                raise ValueError("Invalid Central response")
            if result.get("accepted") is not True:
                raise ValueError(str(result.get("detail", "Central rejected video")))
            revision, generation = result["revision"], result["generation"]
            if (
                not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0
                or not isinstance(generation, str) or not generation
            ):
                raise ValueError("Invalid Central revision response")
            return result
        except (OSError, TimeoutError, WebSocketException, ValueError, KeyError, TypeError) as exc:
            self._reset()
            raise UploadError(_error_explanation(exc)) from exc

    def _reset(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._previous = None
        self._revision = None
        self._generation = None
        self._video = None
        self._video_key = None
        self._video_stream = None
        self._force_video_keyframe = False
        self._last_heartbeat = float("-inf")

    def close(self) -> None:
        self._reset()

