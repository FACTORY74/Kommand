"""Local, fullscreen-capable display of Central's channel layout."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
import tkinter as tk
from tkinter import messagebox
from typing import Any

from PIL import Image, ImageOps, ImageTk

from kommand_central.layout import LayoutStore, format_panel_label
from kommand_central.state import ChannelStore
from kommand_common.ui import COLORS, action_button


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("monitor", wintypes.RECT),
        ("work", wintypes.RECT),
        ("flags", wintypes.DWORD),
    ]


if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HANDLE
    _user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL


def _windows_handle(window: tk.Misc) -> int:
    handle = _user32.GetAncestor(window.winfo_id(), 2)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _windows_window_and_monitor(window: tk.Misc) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    handle = _windows_handle(window)
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(handle, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    monitor = _user32.MonitorFromWindow(handle, 2)
    if not monitor:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _MonitorInfo()
    info.size = ctypes.sizeof(info)
    if not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (
        (rect.left, rect.top, rect.right, rect.bottom),
        (info.monitor.left, info.monitor.top, info.monitor.right, info.monitor.bottom),
    )


def _windows_place_window(window: tk.Misc, bounds: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = bounds
    if not _user32.SetWindowPos(
        _windows_handle(window), None, left, top, right - left, bottom - top, 0x0014
    ):
        raise ctypes.WinError(ctypes.get_last_error())


@dataclass(slots=True)
class _Slot:
    path: tuple[int, ...]
    channel: str | None
    tile: tk.Frame
    label: tk.Label
    name: tk.Label
    revision: int | None = None
    size: tuple[int, int] | None = None
    photo: ImageTk.PhotoImage | None = None


class DisplayWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc, store: ChannelStore, layouts: LayoutStore) -> None:
        super().__init__(master)
        self.store = store
        self.layouts = layouts
        self._layout_revision = -1
        self._slots: list[_Slot] = []
        self._slot_by_widget: dict[tk.Misc, _Slot] = {}
        self._drag_source: tuple[tuple[int, ...], int] | None = None
        self._drag_target: _Slot | None = None
        self._refresh_job: str | None = None
        self._windowed_bounds: tuple[int, int, int, int] | None = None
        self._windowed_state = "normal"
        self.title("Kommand Central display")
        self.geometry("1280x720")
        self.configure(background=COLORS["background"])
        self.bind("<F11>", self._toggle_fullscreen)
        self.bind("<Escape>", self._exit_fullscreen)
        header = tk.Frame(self, background=COLORS["background"])
        header.pack(fill="x")
        tk.Label(
            header, text="Kommand Central display", foreground=COLORS["text"],
            background=COLORS["background"], font=("Segoe UI", 14),
        ).pack(side="left", padx=12, pady=8)
        self.fullscreen_button = action_button(header, "Fullscreen", self._toggle_fullscreen)
        self.fullscreen_button.pack(side="right", padx=12, pady=8)
        self.content = tk.Frame(self, background=COLORS["background"])
        self.content.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._refresh()

    def _toggle_fullscreen(self, _event: tk.Event | None = None) -> None:
        if self.attributes("-fullscreen"):
            self._exit_fullscreen()
            return
        try:
            if sys.platform == "win32":
                self.update_idletasks()
                self._windowed_bounds, monitor_bounds = _windows_window_and_monitor(self)
                self._windowed_state = self.state()
            self.attributes("-fullscreen", True)
            if sys.platform == "win32":
                # Tk recreates the native window when fullscreen changes.
                self.update_idletasks()
                _windows_place_window(self, monitor_bounds)
            self.fullscreen_button.configure(text="EXIT FULLSCREEN")
        except OSError as exc:
            if self.attributes("-fullscreen"):
                self._exit_fullscreen()
            messagebox.showerror("Fullscreen error", str(exc), parent=self)

    def _exit_fullscreen(self, _event: tk.Event | None = None) -> None:
        if not self.attributes("-fullscreen"):
            return
        try:
            self.attributes("-fullscreen", False)
            if sys.platform == "win32" and self._windowed_bounds is not None:
                self.update_idletasks()
                if self._windowed_state == "zoomed":
                    self.state("zoomed")
                else:
                    _windows_place_window(self, self._windowed_bounds)
                self._windowed_bounds = None
        except OSError as exc:
            messagebox.showerror("Fullscreen error", str(exc), parent=self)
        finally:
            self.fullscreen_button.configure(text="FULLSCREEN")

    def _build(
        self, node: dict[str, Any], parent: tk.Misc, path: tuple[int, ...]
    ) -> tk.Widget:
        if node["type"] == "grid":
            grid = tk.Frame(parent, background=COLORS["background"])
            for row in range(node["rows"]):
                grid.rowconfigure(row, weight=1, uniform="row")
            for column in range(node["columns"]):
                grid.columnconfigure(column, weight=1, uniform="column")
            for index, child in enumerate(node["children"]):
                widget = self._build(child, grid, path + (index,))
                widget.grid(
                    row=index // node["columns"], column=index % node["columns"],
                    sticky="nsew", padx=4, pady=4,
                )
            return grid
        tile = tk.Frame(
            parent, background=COLORS["surface"], highlightthickness=2,
            highlightbackground=COLORS["surface"],
        )
        tile.grid_propagate(False)
        tile.rowconfigure(0, weight=1)
        tile.columnconfigure(0, weight=1)
        label = tk.Label(
            tile, background=COLORS["surface"], foreground=COLORS["muted"],
            text="Unassigned panel",
        )
        label.grid(row=0, column=0, sticky="nsew")
        channel = node.get("channel")
        name = tk.Label(
            tile, background=COLORS["surface"], foreground=COLORS["text"],
            text=f"{format_panel_label(path)} / {channel or 'Unassigned'}", anchor="center",
        )
        name.grid(row=1, column=0, sticky="ew")
        slot = _Slot(path, channel, tile, label, name)
        self._slots.append(slot)
        for widget in (tile, label, name):
            self._slot_by_widget[widget] = slot
            if channel:
                widget.configure(cursor="hand2")
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_motion)
            widget.bind("<ButtonRelease-1>", self._drag_release)
        return tile

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_source = None
        self._set_drag_target(None)
        slot = self._slot_by_widget.get(event.widget)
        if slot and slot.channel:
            self._drag_source = (slot.path, self._layout_revision)

    def _slot_at(self, x: int, y: int) -> _Slot | None:
        widget = self.winfo_containing(x, y)
        return self._slot_by_widget.get(widget) if widget else None

    def _set_drag_target(self, slot: _Slot | None) -> None:
        if self._drag_target is slot:
            return
        if self._drag_target is not None:
            self._drag_target.tile.configure(highlightbackground=COLORS["surface"])
        self._drag_target = slot
        if slot is not None:
            slot.tile.configure(highlightbackground=COLORS["primary"])

    def _drag_motion(self, event: tk.Event) -> None:
        if self._drag_source is None:
            return
        slot = self._slot_at(event.x_root, event.y_root)
        self._set_drag_target(
            slot if slot is not None and slot.path != self._drag_source[0] else None
        )

    def _drag_release(self, event: tk.Event) -> None:
        source = self._drag_source
        self._drag_source = None
        self._set_drag_target(None)
        if source is None:
            return
        target = self._slot_at(event.x_root, event.y_root)
        if target is None or target.path == source[0]:
            return
        try:
            self.layouts.move_or_swap(source[0], target.path, source[1])
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not move channel", str(exc), parent=self)
            return
        self._refresh()

    def _refresh(self) -> None:
        if self._refresh_job is not None:
            self.after_cancel(self._refresh_job)
        layout_revision = self.layouts.revision
        if layout_revision != self._layout_revision:
            self._drag_source = None
            self._set_drag_target(None)
            for child in self.content.winfo_children():
                child.destroy()
            self._slots.clear()
            self._slot_by_widget.clear()
            self._build(self.layouts.snapshot(), self.content, ()).pack(fill="both", expand=True)
            self._layout_revision = layout_revision
        frames = {f"{frame.source}/{frame.channel}": frame for frame in self.store.list()}
        for slot in self._slots:
            channel = slot.channel
            frame = frames.get(channel) if channel is not None else None
            label = slot.label
            if frame is None:
                label.configure(text=f"Waiting for {channel}" if channel else "Unassigned panel", image="")
                slot.revision = None
                slot.photo = None
                continue
            size = (label.winfo_width(), label.winfo_height())
            if size[0] <= 1 or size[1] <= 1:
                continue
            if frame.revision == slot.revision and size == slot.size:
                continue
            scaled = ImageOps.contain(frame.image, size, Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(scaled, master=self)
            label.configure(image=photo, text="")
            slot.photo = photo
            slot.revision = frame.revision
            slot.size = size
        streaming = any(
            slot.channel is not None
            and (frame := frames.get(slot.channel)) is not None
            and frame.streaming
            for slot in self._slots
        )
        self._refresh_job = self.after(16 if streaming else 50, self._refresh)

    def destroy(self) -> None:
        if self._refresh_job is not None:
            self.after_cancel(self._refresh_job)
            self._refresh_job = None
        super().destroy()
