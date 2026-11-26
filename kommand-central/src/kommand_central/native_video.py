"""Native libVLC video surfaces embedded inside Tk panels."""

import importlib
import ctypes
from ctypes.util import find_library
import os
from pathlib import Path
import sys
import time
import tkinter as tk
from types import ModuleType

from kommand_central.layout import Viewport


_dll_directory = None


def prepare_native_video() -> None:
    if sys.platform.startswith("linux"):
        library = find_library("X11")
        if library is None:
            raise RuntimeError("Embedded video requires X11.")
        x11 = ctypes.CDLL(library)
        x11.XInitThreads.argtypes = []
        x11.XInitThreads.restype = ctypes.c_int
        if not x11.XInitThreads():
            raise RuntimeError("Could not initialize X11 threading for native video.")


def load_vlc() -> ModuleType:
    global _dll_directory
    if sys.platform == "win32" and "PYTHON_VLC_LIB_PATH" not in os.environ:
        candidates = [
            Path(sys.prefix) / "vlc-3.0.24",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "VideoLAN" / "VLC",
        ]
        for directory in candidates:
            if (directory / "libvlc.dll").is_file():
                _dll_directory = os.add_dll_directory(str(directory))
                os.environ["PYTHON_VLC_LIB_PATH"] = str(directory / "libvlc.dll")
                os.environ["PYTHON_VLC_MODULE_PATH"] = str(directory / "plugins")
                break
    try:
        return importlib.import_module("vlc")
    except (ImportError, OSError, NameError) as exc:
        raise RuntimeError(
            "Native video requires libVLC. Run setup_env.cmd on Windows, "
            "or install VLC and python-vlc on your platform."
        ) from exc


def video_bounds(
    size: tuple[int, int], source_size: tuple[int, int], view: Viewport,
) -> tuple[int, int, int, int]:
    width, height = size
    scale = min(width / source_size[0], height / source_size[1]) * view.zoom
    video_width, video_height = max(1, round(source_size[0] * scale)), max(1, round(source_size[1] * scale))
    return (
        round((width - video_width) / 2 + view.pan_x * width),
        round((height - video_height) / 2 + view.pan_y * height),
        video_width, video_height,
    )


class NativeVideoEngine:
    def __init__(self) -> None:
        self.vlc = load_vlc()
        self.instance = self.vlc.Instance(
            "--no-audio", "--no-video-title-show", "--no-osd",
            "--avcodec-hw=any", "--avcodec-threads=1", "--network-caching=150", "--quiet",
        )
        if self.instance is None:
            raise RuntimeError("Could not initialize native video playback.")

    def close(self) -> None:
        self.instance.release()


class NativeVideoPanel:
    def __init__(self, parent: tk.Misc, engine: NativeVideoEngine, url: str) -> None:
        self.surface = tk.Frame(parent, background="black")
        self.engine = engine
        self.url = url
        self.player = engine.instance.media_player_new()
        self.player.video_set_mouse_input(False)
        self.player.video_set_key_input(False)
        self._geometry: tuple[int, int, int, int] | None = None
        self._handle: int | None = None
        self._retry_at = 0.0
        self._started = False

    def update(self, size: tuple[int, int], source_size: tuple[int, int], view: Viewport) -> str | None:
        bounds = video_bounds(size, source_size, view)
        if bounds != self._geometry:
            self.surface.place(x=bounds[0], y=bounds[1], width=bounds[2], height=bounds[3])
            self._geometry = bounds
        self.surface.update_idletasks()
        handle = self.surface.winfo_id()
        if self._handle != handle:
            if sys.platform == "win32":
                self.player.set_hwnd(handle)
            elif sys.platform == "darwin":
                raise RuntimeError("Embedded Tk/libVLC playback is supported on Windows and Linux, not macOS.")
            else:
                self.player.set_xwindow(handle)
            self._handle = handle
        state = self.player.get_state()
        if not self._started or state in (
            self.engine.vlc.State.Error, self.engine.vlc.State.Ended, self.engine.vlc.State.Stopped,
        ):
            if time.monotonic() < self._retry_at:
                return "Video disconnected; reconnecting"
            self.player.stop()
            media = self.engine.instance.media_new(self.url)
            media.add_option(":demux=ts")
            media.add_option(":network-caching=150")
            media.add_option(":http-reconnect")
            self.player.set_media(media)
            media.release()
            if self.player.play() == -1:
                raise RuntimeError("Native video player could not start the stream.")
            self._started = True
            self._retry_at = time.monotonic() + 2
        return None

    def close(self) -> None:
        self.player.stop()
        self.player.release()
        self.surface.destroy()
