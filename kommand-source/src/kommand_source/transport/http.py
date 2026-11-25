"""HTTP client for publishing frames to Kommand Central."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from av.error import FFmpegError
import requests
from PIL import Image, ImageChops

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


class _MeasuredBody(BytesIO):
    def __init__(self, content: bytes, bandwidth: RollingBandwidth) -> None:
        super().__init__(content)
        self.bandwidth = bandwidth

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        self.bandwidth.record(len(chunk))
        return chunk


class _MeasuredSession(requests.Session):
    def __init__(self, bandwidth: RollingBandwidth) -> None:
        super().__init__()
        self.bandwidth = bandwidth

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        if isinstance(request.body, bytes):
            request.body = _MeasuredBody(request.body, self.bandwidth)
        return super().send(request, **kwargs)


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
        self.session = _MeasuredSession(bandwidth) if bandwidth is not None else requests.Session()
        self._previous: Image.Image | None = None
        self._channel_name: str | None = None
        self._alignment: tuple[object, ...] | None = None
        self._revision: int | None = None
        self._generation: str | None = None
        self._last_full = 0.0
        self._video: VideoEncoder | None = None
        self._video_key: tuple[object, ...] | None = None
        self._video_stream: str | None = None
        self._last_video_keyframe = 0.0

    def upload(self, channel: ChannelConfig, frame: CapturedFrame) -> None:
        if frame.image is not None:
            current = frame.image.copy()
        else:
            with Image.open(BytesIO(frame.content)) as encoded:
                current = encoded.convert("RGB")
        source = quote(self.config.source_name, safe="")
        channel_name = quote(channel.name, safe="")
        base_url = (
            f"{self.config.central_url.rstrip('/')}"
            f"/api/v1/sources/{source}/channels/{channel_name}"
        )
        if channel.format == "video":
            self._upload_video(channel, frame, current, base_url)
            return
        if self._video is not None:
            self._reset()
        now = time.monotonic()
        full = (
            channel.format == "webp"
            or self._previous is None
            or self._channel_name != channel.name
            or self._revision is None
            or self._generation is None
            or self._alignment != frame.alignment
            or self._previous.size != current.size
            or now - self._last_full >= channel.full_frame_interval_seconds
        )
        headers = {
            "X-Kommand-Protocol": "1",
            "X-Kommand-Captured-At": datetime.now(timezone.utc).isoformat(),
        }
        content = frame.content
        endpoint = "frames"
        if not full:
            assert self._previous is not None
            bbox = ImageChops.difference(self._previous, current).getbbox()
            headers["X-Kommand-Base-Revision"] = str(self._revision)
            headers["X-Kommand-Generation"] = str(self._generation)
            if bbox is None:
                endpoint = "heartbeat"
            else:
                patch = current.crop(bbox)
                output = BytesIO()
                patch.save(output, format="WEBP", lossless=True)
                if len(output.getvalue()) < len(content):
                    endpoint = "patches"
                    content = output.getvalue()
                    headers["X-Kommand-X"] = str(bbox[0])
                    headers["X-Kommand-Y"] = str(bbox[1])
                    headers["X-Kommand-Width"] = str(patch.width)
                    headers["X-Kommand-Height"] = str(patch.height)
                else:
                    full = True
        if full:
            headers["X-Kommand-Width"] = str(frame.width)
            headers["X-Kommand-Height"] = str(frame.height)
        files = None if endpoint == "heartbeat" else {
            "frame": (f"{channel.name}.webp", content, "image/webp")
        }
        result = self._send(f"{base_url}/{endpoint}", headers, files)
        revision = result["revision"]
        generation = result["generation"]
        self._previous = current
        self._channel_name = channel.name
        self._alignment = frame.alignment
        self._revision = revision
        self._generation = generation
        if full:
            self._last_full = now

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
            force_keyframe = sequence > 0 and now - self._last_video_keyframe >= channel.full_frame_interval_seconds
            content = self._video.encode(image, force_keyframe=force_keyframe)
            if sequence == 0 or force_keyframe:
                self._last_video_keyframe = now
        except (FFmpegError, VideoEncodingError, ValueError) as exc:
            self._reset()
            raise UploadError(f"Could not encode video: {exc}") from exc
        assert self._video_stream is not None
        headers = {
            "X-Kommand-Protocol": "1",
            "X-Kommand-Captured-At": datetime.now(timezone.utc).isoformat(),
            "X-Kommand-Width": str(image.width),
            "X-Kommand-Height": str(image.height),
            "X-Kommand-Video-Stream": self._video_stream,
            "X-Kommand-Video-Sequence": str(sequence),
            "X-Kommand-Generation": self._generation or "",
        }
        result = self._send(
            f"{base_url}/video", headers,
            {"frame": (f"{channel.name}.h264", content, "video/h264")},
        )
        self._revision = result["revision"]
        self._generation = result["generation"]

    def _send(
        self, url: str, headers: dict[str, str], files: dict[str, tuple[str, bytes, str]] | None
    ) -> dict[str, Any]:
        try:
            response = self.session.post(
                url,
                headers=headers,
                files=files,
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            result = response.json()
            revision = result["revision"]
            generation = result["generation"]
            if not isinstance(revision, int) or revision <= 0 or not isinstance(generation, str) or not generation:
                raise ValueError("Invalid Central revision response")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            self._reset()
            raise UploadError(_error_explanation(exc)) from exc
        return result

    def _reset(self) -> None:
        self._previous = None
        self._revision = None
        self._generation = None
        self._video = None
        self._video_key = None
        self._video_stream = None

    def close(self) -> None:
        self._reset()
        self.session.close()

