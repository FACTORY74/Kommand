"""Rendered window capture using Windows Graphics Capture."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from PIL import Image

from kommand_source.capture.windows import WindowIdentity, WindowInfo, resolve_window

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


class WindowsGraphicsCaptureError(RuntimeError):
    pass


class WindowCaptureResized(WindowsGraphicsCaptureError):
    pass


def _bounds(window: WindowInfo) -> tuple[int, int, int, int]:
    return window.left, window.top, window.width, window.height


class WindowsCaptureSession:
    """Keep one native capture alive while a channel is running."""

    def __init__(self, identity: WindowIdentity, fps: int = 15) -> None:
        try:
            from windows_capture import Frame, InternalCaptureControl, WindowsCapture
        except ImportError as exc:
            raise WindowsGraphicsCaptureError(
                "The Windows capture backend is not installed; run setup_env.cmd."
            ) from exc

        window = resolve_window(identity)
        self.identity = identity
        self._window_size = (window.width, window.height)
        self._changed = threading.Condition()
        self._latest: NDArray[np.uint8] | None = None
        self._generation = 0
        self._image_generation = -1
        self._image: Image.Image | None = None
        self._next_copy = float("-inf")
        self._error: Exception | None = None
        self._closing = False
        self._capture = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            window_hwnd=identity.handle,
        )
        started = False
        try:
            now = time.monotonic()
            if now < self._next_copy:
                return
            @self._capture.event
            def on_frame_arrived(frame: Frame, control: InternalCaptureControl) -> None:
                try:
                    # The mapped BGRA buffer is only valid while the native frame is alive.
                    pixels = frame.frame_buffer.copy()
                    with self._changed:
                        self._latest = pixels
                        deadline = self._next_copy + 1 / fps
                        self._next_copy = deadline if deadline > now else now + 1 / fps
                        self._generation += 1
                        self._changed.notify_all()
                except Exception as exc:
                    with self._changed:
                        self._error = exc
                        self._changed.notify_all()
                    control.stop()

            @self._capture.event
            def on_closed() -> None:
                with self._changed:
                    if not self._closing:
                        self._error = RuntimeError("The selected window closed during capture.")
                    self._changed.notify_all()

            self._control = self._capture.start_free_threaded()
            started = True
        finally:
            if not started:
                capture = self._capture
                self._capture = None
                del capture.capture

    def capture(self, timeout_seconds: float = 5.0) -> tuple[Image.Image, WindowInfo]:
        window = resolve_window(self.identity)
        if (window.width, window.height) != self._window_size:
            raise WindowCaptureResized("Window size changed; restarting capture.")
        deadline = time.monotonic() + timeout_seconds
        with self._changed:
            while True:
                if self._error is not None:
                    raise WindowsGraphicsCaptureError(str(self._error)) from self._error
                if self._closing:
                    raise WindowsGraphicsCaptureError("Window capture has stopped.")
                latest = self._latest
                if latest is not None:
                    generation = self._generation
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WindowsGraphicsCaptureError(
                        f"Timed out waiting for a rendered frame from '{window.title}'."
                    )
                self._changed.wait(remaining)

        pixels = latest
        if generation != self._image_generation:
            self._image = Image.frombuffer(
                "RGB", (pixels.shape[1], pixels.shape[0]), pixels, "raw", "BGRX", 0, 1,
            )
            self._image_generation = generation
        assert self._image is not None
        image = self._image
        current = resolve_window(self.identity)
        if _bounds(current) != _bounds(window):
            raise WindowsGraphicsCaptureError("Window changed during capture; retrying.")
        return image, current

    def close(self) -> None:
        with self._changed:
            if self._closing:
                return
            self._closing = True
            self._changed.notify_all()
        try:
            if not self._control.is_finished():
                self._control.stop()
            self._control.wait()
        finally:
            # Native bound callbacks form a cycle invisible to Python's GC.
            capture = self._capture
            assert capture is not None
            self._capture = None
            del capture.capture


def capture_rendered_window(
    identity: WindowIdentity, timeout_seconds: float = 5.0
) -> tuple[Image.Image, WindowInfo]:
    session = WindowsCaptureSession(identity)
    try:
        return session.capture(timeout_seconds)
    finally:
        session.close()
