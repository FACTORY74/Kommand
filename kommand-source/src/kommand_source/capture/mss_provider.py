"""Cross-platform screen and region capture using MSS."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Mapping

from PIL import Image
import mss

from kommand_source.config import ChannelConfig, parse_region_target, parse_screen_target
from kommand_source.capture.wgc_provider import (
    WindowCaptureResized,
    WindowsCaptureSession,
    WindowsGraphicsCaptureError,
)
from kommand_source.capture.windows import WindowIdentity, find_window, get_window_identity


class CaptureError(RuntimeError):
    """A configured target could not be captured."""


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    content: bytes
    width: int
    height: int
    media_type: str
    extension: str
    alignment: tuple[object, ...] = ()
    image: Image.Image | None = None


class MssCaptureProvider:
    def __init__(self) -> None:
        self._legacy_windows: dict[tuple[str, str], WindowIdentity] = {}
        self._window_session: WindowsCaptureSession | None = None

    def capture(self, channel: ChannelConfig) -> CapturedFrame:
        try:
            if channel.capture_type == "window":
                return self._capture_window(channel)
            self.close()
            with mss.mss() as capturer:
                target = self._target(capturer.monitors, channel)
                shot = capturer.grab(target)
            image = Image.frombytes("RGB", shot.size, shot.rgb)
            return self._prepare(
                image, channel,
                (channel.capture_type, channel.capture_target, target["left"], target["top"],
                 target["width"], target["height"]),
            )
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(str(exc)) from exc

    def _capture_window(self, channel: ChannelConfig) -> CapturedFrame:
        try:
            if channel.window_handle is not None:
                if channel.window_process_id is None or channel.window_process_start is None:
                    raise CaptureError("Window identity is incomplete; select the window again.")
                identity = WindowIdentity(
                    channel.window_handle, channel.window_process_id, channel.window_process_start
                )
            else:
                key = (channel.name, channel.capture_target)
                identity = self._legacy_windows.get(key)
                if identity is None:
                    identity = get_window_identity(find_window(channel.capture_target).handle)
                    self._legacy_windows[key] = identity
            if self._window_session is not None and self._window_session.identity != identity:
                self.close()
            if self._window_session is None:
                self._window_session = WindowsCaptureSession(identity)
            try:
                try:
                    image, window = self._window_session.capture()
                except WindowCaptureResized:
                    self.close()
                    self._window_session = WindowsCaptureSession(identity)
                    image, window = self._window_session.capture()
            except Exception:
                self.close()
                raise
            if image.mode != "RGB":
                image = image.convert("RGB")
            return self._prepare(
                image, channel,
                ("window", identity.handle, identity.process_id, identity.process_start, window.left, window.top,
                 window.width, window.height),
            )
        except WindowsGraphicsCaptureError as exc:
            raise CaptureError(str(exc)) from exc

    def close(self) -> None:
        if self._window_session is not None:
            session = self._window_session
            self._window_session = None
            session.close()

    @staticmethod
    def _target(
        monitors: list[dict[str, int]], channel: ChannelConfig
    ) -> Mapping[str, int]:
        if channel.capture_type == "screen":
            try:
                index = parse_screen_target(channel.capture_target)
            except ValueError as exc:
                raise CaptureError(str(exc)) from exc
            if index >= len(monitors):
                available = max(0, len(monitors) - 1)
                raise CaptureError(
                    f"Monitor {index} is unavailable; {available} monitor(s) detected."
                )
            return monitors[index]

        if channel.capture_type == "region":
            try:
                left, top, width, height = parse_region_target(channel.capture_target)
            except ValueError as exc:
                raise CaptureError(str(exc)) from exc
            return {"left": left, "top": top, "width": width, "height": height}

        raise CaptureError(f"Unsupported capture type: {channel.capture_type}")

    @staticmethod
    def _prepare(
        image: Image.Image, channel: ChannelConfig, alignment: tuple[object, ...]
    ) -> CapturedFrame:
        if channel.format == "video":
            return CapturedFrame(
                content=b"", width=image.width, height=image.height,
                media_type="image/webp", extension="webp", alignment=alignment, image=image,
            )
        return MssCaptureProvider._encode(image, channel.quality, alignment)

    @staticmethod
    def _encode(
        image: Image.Image, quality: int, alignment: tuple[object, ...]
    ) -> CapturedFrame:
        output = BytesIO()
        image.save(output, format="WEBP", quality=quality)
        return CapturedFrame(
            content=output.getvalue(),
            width=image.width,
            height=image.height,
            media_type="image/webp",
            extension="webp",
            alignment=alignment,
            image=image,
        )

