"""Desktop control shell for Kommand Central."""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path
import webbrowser

import uvicorn
from PIL import Image, ImageOps, ImageTk

from kommand_central.bandwidth import ReceiveBandwidth
from kommand_central.display import DisplayWindow, _render_view
from kommand_central.state import ChannelStore
from kommand_central.layout import LayoutStore, Viewport, format_panel_label
from kommand_central.web import create_app
from kommand_central.windows import DisplayLayouts
from kommand_common.ui import NumberStepper, action_button, app_header, apply_theme, card


class CentralApp(ttk.Frame):
    def __init__(self, master: tk.Tk):
        apply_theme(master)
        super().__init__(master, padding=24, style="App.TFrame")
        self.master = master
        self.store = ChannelStore()
        self.layouts = LayoutStore(Path(__file__).resolve().parents[2] / "layout.json")
        self.window_layouts = DisplayLayouts(self.layouts)
        self._active_window = next(iter(self.window_layouts.layouts), None)
        if self._active_window is not None:
            self.layouts = self.window_layouts.layouts[self._active_window]
        self._display_windows: dict[str, DisplayWindow] = {}
        self.receive_bandwidth = ReceiveBandwidth()
        self.server: uvicorn.Server | None = None
        self.server_thread: threading.Thread | None = None
        self._server_starting = False
        self.display_window: DisplayWindow | None = None
        self.host = tk.StringVar(value="0.0.0.0")
        self.port = tk.StringVar(value="8000")
        self.status = tk.StringVar(value="Stopped")
        root_layout = self.layouts.snapshot()
        self.layout_rows = tk.StringVar(value=str(root_layout["rows"]))
        self.layout_columns = tk.StringVar(value=str(root_layout["columns"]))
        self.nested_rows = tk.StringVar(value="2")
        self.nested_columns = tk.StringVar(value="2")
        self.assignment = tk.StringVar()
        self.window_name = tk.StringVar(value=self.layouts.window_name)
        self._seen_layout_revision = -1
        self._known_channels: frozenset[str] = frozenset()
        self._preview_images: dict[str, ImageTk.PhotoImage] = {}
        self._preview_revisions: dict[str, int] = {}
        self._last_preview_refresh = float("-inf")
        self._selected_panel: tuple[int, ...] | None = None
        self._panel_boxes: dict[tuple[int, ...], tuple[float, float, float, float]] = {}
        self._layout_images: list[ImageTk.PhotoImage] = []
        self._drag_channel: str | None = None
        self._drag_revision = -1
        self._drop_panel: tuple[int, ...] | None = None
        self._source_menu: tk.Menu | None = None
        self._space_down = False
        self._pan_start = None
        self._pan_view: Viewport | None = None
        self._panel_click = None
        self._pan_moved = False
        self._mini_moving = False

        master.title("Kommand Central")
        master.geometry("1040x820")
        master.minsize(900, 700)
        master.protocol("WM_DELETE_WINDOW", self._close)
        self.pack(fill="both", expand=True)
        self._build()
        self._render_window_tabs()
        self.after(500, self._refresh)

    def _build(self) -> None:
        app_header(self, "Kommand-Central: Arrange sources and control the display").pack(
            fill="x", pady=(0, 14)
        )
        server_card = card(self, "Server")
        server_card.pack(fill="x")
        ttk.Label(server_card, text="Listen address").grid(row=0, column=0, sticky="w")
        ttk.Entry(server_card, textvariable=self.host, width=22).grid(row=1, column=0, padx=(0, 12))
        ttk.Label(server_card, text="Port").grid(row=0, column=1, sticky="w")
        ttk.Entry(server_card, textvariable=self.port, width=10).grid(row=1, column=1, padx=(0, 12))
        self.start_button = action_button(server_card, "Start server", self._start, "primary")
        self.start_button.grid(row=1, column=2, padx=(0, 8))
        self.stop_button = action_button(server_card, "Stop", self._stop, "danger")
        self.stop_button.grid(row=1, column=3, padx=(0, 8))
        self.stop_button.configure(state="disabled")
        ttk.Label(server_card, text="Receive bandwidth").grid(row=0, column=5, sticky="w")
        self.receive_bandwidth_value = tk.StringVar(value="0.00 Mbps")
        ttk.Label(
            server_card,
            textvariable=self.receive_bandwidth_value,
            style="Status.TLabel",
        ).grid(row=1, column=5, sticky="w")

        self.view_splitter = tk.PanedWindow(
            self, orient="vertical", background="#101318", borderwidth=0,
            sashwidth=3, sashpad=12, sashrelief="raised", showhandle=False,
            opaqueresize=True,
        )
        layout_card = card(self.view_splitter, "Display layout")
        self.view_splitter.add(layout_card, minsize=270, stretch="never")
        tab_style = ttk.Style(self.master)
        tab_style.configure("Display.TNotebook", background="#191D24", borderwidth=1, tabmargins=(0, 0, 0, 0))
        tab_style.configure("Display.TNotebook.Tab", background="#222832", foreground="#9AA6B2", padding=(14, 9))
        tab_style.map("Display.TNotebook.Tab",
                      background=[("selected", "#191D24"), ("active", "#303844")],
                      foreground=[("selected", "#5AA7FF"), ("active", "#F2F5F8")],
                      padding=[("selected", (14, 9)), ("!selected", (14, 9))],
                      expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))])
        self.window_tabs = ttk.Notebook(layout_card, style="Display.TNotebook")
        self.window_tabs.pack(fill="both", expand=True)
        self._tab_pages: dict[str, ttk.Frame] = {}
        self._plus_page = ttk.Frame(self.window_tabs, style="Card.TFrame")
        self.window_tabs.add(self._plus_page, text="+")
        self.window_tabs.bind("<<NotebookTabChanged>>", self._window_tab_changed)
        self.window_tabs.bind("<ButtonPress-1>", self._window_tab_pressed)
        self.layout_editor = ttk.Frame(self.window_tabs, style="Card.TFrame", padding=(12, 12))
        layout_card = self.layout_editor
        controls = ttk.Frame(layout_card, style="Card.TFrame")
        controls.pack(fill="x", pady=(0, 10))
        ttk.Label(controls, text="Window name").pack(side="left")
        self.window_name_input = ttk.Entry(controls, textvariable=self.window_name, width=24)
        self.window_name_input.pack(side="left", padx=(6, 12))
        self.window_name_input.bind("<Return>", self._save_window_name)
        self.window_name_input.bind("<FocusOut>", self._save_window_name)
        style = ttk.Style(self.master)
        style.configure("OpenDisplay.TButton", background="#F59A45", foreground="#17110B")
        style.map("OpenDisplay.TButton", background=[("pressed", "#D87928"), ("active", "#FFB46B")])
        self.open_display_button = action_button(controls, "Open display", self._open_display)
        self.open_display_button.configure(style="OpenDisplay.TButton")
        self.open_display_button.pack(side="left", padx=(12, 0))
        style.configure("BrowserDisplay.TButton", background="#EDAD57", foreground="#17110B")
        style.map("BrowserDisplay.TButton", background=[("pressed", "#CE8938"), ("active", "#FFC477")])
        browser_display_button = action_button(controls, "Browser display", self._open_browser_display)
        browser_display_button.configure(style="BrowserDisplay.TButton")
        browser_display_button.pack(side="left", padx=(8, 0))
        grid_controls = ttk.Frame(layout_card, style="Card.TFrame")
        grid_controls.pack(fill="x", pady=(0, 10))
        ttk.Label(grid_controls, text="Root rows").pack(side="left")
        NumberStepper(grid_controls, self.layout_rows, minimum=1, maximum=12, width=3, zero_pad=2).pack(side="left", padx=(6, 12))
        ttk.Label(grid_controls, text="Columns").pack(side="left")
        NumberStepper(grid_controls, self.layout_columns, minimum=1, maximum=12, width=3, zero_pad=2).pack(side="left", padx=(6, 12))
        action_button(grid_controls, "Apply root grid", self._resize_layout, "primary").pack(side="left")

        self.layout_canvas = tk.Canvas(
            layout_card, height=240, background="#101318", highlightthickness=0,
        )
        self.layout_canvas.bind("<Configure>", self._draw_layout)
        self.layout_canvas.bind("<Button-1>", self._select_panel)
        self.layout_canvas.bind("<Enter>", lambda _event: self.layout_canvas.focus_set())
        self.layout_canvas.bind("<KeyPress-space>", self._space_press)
        self.layout_canvas.bind("<KeyRelease-space>", self._space_release)
        self.layout_canvas.bind("<FocusOut>", self._space_release)
        self.layout_canvas.bind("<B1-Motion>", self._pan_motion)
        self.layout_canvas.bind("<ButtonRelease-1>", self._pan_release)
        self.layout_canvas.bind("<MouseWheel>", self._mini_wheel)
        self.layout_canvas.bind("<Button-4>", lambda event: self._mini_zoom(event, 1))
        self.layout_canvas.bind("<Button-5>", lambda event: self._mini_zoom(event, -1))
        assignment_controls = ttk.Frame(layout_card, style="Card.TFrame", padding=(0, 10, 0, 0))
        assignment_controls.pack(side="bottom", fill="x")
        ttk.Label(assignment_controls, text="Source").pack(side="left")
        self.assignment_input = ttk.Combobox(
            assignment_controls, textvariable=self.assignment, state="readonly", width=34
        )
        self.assignment_input.pack(side="left", padx=(6, 8))
        self.assignment_input.bind("<<ComboboxSelected>>", lambda _event: self._assign_panel())
        action_button(assignment_controls, "Assign", self._assign_panel, "primary").pack(side="left")
        action_button(assignment_controls, "Clear", self._clear_assignment).pack(side="left", padx=(8, 18))
        ttk.Label(assignment_controls, text="Subdivide").pack(side="left")
        NumberStepper(assignment_controls, self.nested_rows, minimum=1, maximum=12, width=3, zero_pad=2).pack(side="left", padx=(6, 4))
        ttk.Label(assignment_controls, text="×").pack(side="left")
        NumberStepper(assignment_controls, self.nested_columns, minimum=1, maximum=12, width=3, zero_pad=2).pack(side="left", padx=(4, 8))
        action_button(assignment_controls, "Nest panel", self._nest_panel).pack(side="left")
        action_button(assignment_controls, "Remove nesting", self._flatten_panel, "danger").pack(side="left", padx=8)
        action_button(assignment_controls, "Delete window", self._delete_active_window, "danger").pack(side="left")
        ttk.Label(layout_card, text="Click to assign · Drag a live channel onto a panel · Wheel to zoom · Drag preview to pan").pack(side="bottom", anchor="w", pady=(6, 0))
        self.layout_canvas.pack(fill="both", expand=True)

        channels_card = card(self.view_splitter, "Live channels")
        self.view_splitter.add(channels_card, minsize=120, stretch="always")
        self.channels_tree = ttk.Treeview(
            channels_card,
            columns=("channel", "size", "revision", "received"),
            show="tree headings",
            style="LiveChannels.Treeview",
            height=3,
        )
        ttk.Style(self.master).configure("LiveChannels.Treeview", rowheight=64)
        self.channels_tree.heading("#0", text="Preview")
        self.channels_tree.column("#0", width=76, minwidth=76, stretch=False)
        for key, text, width in (
            ("channel", "Channel", 180),
            ("size", "Frame size", 110),
            ("revision", "Revision", 80),
            ("received", "Last received (UTC)", 210),
        ):
            self.channels_tree.heading(key, text=text)
            self.channels_tree.column(key, width=width, minwidth=width, anchor="center")
        scrollbar_style = ttk.Style(self.master)
        scrollbar_style.configure(
            "LiveChannels.Vertical.TScrollbar",
            troughcolor="#101318", background="#303844", bordercolor="#222832",
            lightcolor="#303844", darkcolor="#303844", arrowcolor="#9AA6B2",
        )
        scrollbar_style.map(
            "LiveChannels.Vertical.TScrollbar",
            background=[("pressed", "#5AA7FF"), ("active", "#465568")],
            arrowcolor=[("active", "#F2F5F8")],
        )
        self.channels_scrollbar = ttk.Scrollbar(
            channels_card, orient="vertical", command=self.channels_tree.yview,
            style="LiveChannels.Vertical.TScrollbar",
        )
        self.channels_scrollbar.pack(side="right", fill="y")
        self.channels_tree.configure(yscrollcommand=self.channels_scrollbar.set)
        self.channels_tree.pack(side="left", fill="both", expand=True)
        self.channels_tree.bind("<ButtonPress-1>", self._channel_drag_start, add="+")
        self.channels_tree.bind("<B1-Motion>", self._channel_drag_motion, add="+")
        self.channels_tree.bind("<ButtonRelease-1>", self._channel_drag_release, add="+")
        self.master.bind("<Escape>", self._cancel_channel_drag, add="+")
        self.view_splitter.pack(fill="both", expand=True, pady=(16, 0))

    def _start(self) -> None:
        if self.server_thread and self.server_thread.is_alive():
            return
        try:
            port = int(self.port.get())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid port", "Port must be between 1 and 65535.")
            return
        config = uvicorn.Config(
            create_app(self.store, self.layouts, self.receive_bandwidth, self.window_layouts),
            host=self.host.get().strip(),
            port=port,
            log_level="info",
            log_config=None,
        )
        self.server = uvicorn.Server(config)
        self.server_thread = threading.Thread(target=self._run_server, name="central-server", daemon=True)
        self._server_starting = True
        self.server_thread.start()
        self.status.set(f"Starting server on {self.host.get()}:{port}")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def _run_server(self) -> None:
        assert self.server is not None
        try:
            self.server.run()
        except Exception:
            logging.getLogger(__name__).exception("Central server stopped unexpectedly")

    def _stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.server_thread:
            self.server_thread.join(timeout=5)
        self.server = None
        self.server_thread = None
        self._server_starting = False
        self.status.set("Stopped")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _open_display(self) -> None:
        if self._active_window is None:
            return
        if not self._save_window_name():
            return
        if self.display_window is None or not self.display_window.winfo_exists():
            self.display_window = DisplayWindow(self.master, self.store, self.layouts)
            self._display_windows[self._active_window] = self.display_window
        else:
            self.display_window.lift()
            self.display_window.focus_force()

    def _open_browser_display(self) -> None:
        if self._active_window is None or not self._save_window_name():
            return
        if not self.server or not self.server_thread or not self.server_thread.is_alive() or not self.server.started:
            messagebox.showerror("Browser display", "Start the server before opening a browser display.", parent=self.master)
            return
        host = self.server.config.host
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        elif host == "::":
            host = "[::1]"
        elif ":" in host:
            host = f"[{host}]"
        url = f"http://{host}:{self.server.config.port}/display/{self._active_window}"
        if not webbrowser.open_new(url):
            messagebox.showerror("Browser display", f"Could not open browser. Open {url} manually.", parent=self.master)

    def _render_window_tabs(self) -> None:
        signature = (self._active_window, tuple((key, layout.window_name) for key, layout in self.window_layouts.layouts.items()))
        if getattr(self, "_tab_signature", None) == signature:
            return
        self._tab_signature = signature
        self.layout_editor.pack_forget()
        for key in list(self._tab_pages):
            if key not in self.window_layouts.layouts:
                self.window_tabs.forget(self._tab_pages[key])
                self._tab_pages.pop(key).destroy()
        for key, layout in self.window_layouts.layouts.items():
            if key not in self._tab_pages:
                page = self._tab_pages[key] = ttk.Frame(self.window_tabs, style="Card.TFrame")
                self.window_tabs.insert(self._plus_page, page)
            self.window_tabs.tab(self._tab_pages[key], text=layout.window_name)
        if self._active_window is not None:
            page = self._tab_pages[self._active_window]
            self.layout_editor.pack(in_=page, fill="both", expand=True)
            self.window_tabs.select(page)
            # The shared editor is a sibling of the notebook pages. New/selected
            # pages are raised by ttk, so keep the editor above their backgrounds.
            self.layout_editor.lift()
        else:
            self.window_tabs.select(self._plus_page)

    def _window_tab_changed(self, _event: object = None) -> None:
        self.layout_editor.lift()
        selected = self.window_tabs.select()
        for key, page in self._tab_pages.items():
            if str(page) == selected and key != self._active_window:
                self._switch_window(key)
                return

    def _window_tab_pressed(self, event: tk.Event) -> str | None:
        try:
            index = self.window_tabs.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None
        page = self.window_tabs.tabs()[index]
        if page == str(self._plus_page):
            self._add_window()
            return "break"
        return None

    def _delete_active_window(self) -> None:
        if self._active_window is not None:
            self._remove_window(self._active_window)

    def _switch_window(self, key: str) -> None:
        if key == self._active_window:
            return
        if self._active_window is not None and not self._save_window_name():
            return
        self._activate_window(key)

    def _activate_window(self, key: str | None) -> None:
        self._active_window = key
        self._pan_start = None
        self._pan_view = None
        self._panel_click = None
        self._drag_channel = None
        self._drop_panel = None
        self._selected_panel = None
        self._space_release()
        if self._source_menu is not None:
            self._source_menu.destroy()
            self._source_menu = None
        self.display_window = self._display_windows.get(key)
        self.assignment.set("")
        if key is not None:
            self.layouts = self.window_layouts.layouts[key]
            root = self.layouts.snapshot()
            self.layout_rows.set(str(root["rows"]))
            self.layout_columns.set(str(root["columns"]))
            self.window_name.set(self.layouts.window_name)
            self._refresh_layout_tree()
        self._render_window_tabs()

    def _add_window(self) -> None:
        if self._active_window is not None and not self._save_window_name():
            return
        try:
            key = self.window_layouts.add()
        except OSError as exc:
            messagebox.showerror("Add display window", str(exc), parent=self.master)
            return
        self._activate_window(key)

    def _remove_window(self, key: str) -> None:
        name = self.window_layouts.layouts[key].window_name
        if not messagebox.askyesno("Remove display window", f'Remove "{name}" and close its display window?', parent=self.master):
            return
        try:
            self.window_layouts.remove(key)
        except OSError as exc:
            messagebox.showerror("Remove display window", str(exc), parent=self.master)
            return
        display = self._display_windows.pop(key, None)
        if display is not None and display.winfo_exists():
            display.destroy()
        if self._active_window == key:
            self._activate_window(next(iter(self.window_layouts.layouts), None))
        else:
            self._render_window_tabs()

    def _save_window_name(self, _event: object = None) -> bool:
        if self._active_window is None:
            return True
        try:
            self.layouts.set_window_name(self.window_name.get())
        except OSError as exc:
            messagebox.showerror("Window name", str(exc), parent=self.master)
            return False
        self.window_name.set(self.layouts.window_name)
        self._render_window_tabs()
        if self.display_window is not None and self.display_window.winfo_exists():
            self.display_window.title(self.layouts.window_name)
            self.display_window.title_label.configure(text=self.layouts.window_name)
        return True

    def _refresh(self) -> None:
        if self.server and self.server_thread:
            if not self.server_thread.is_alive():
                self.status.set("Server stopped unexpectedly; see central.log")
                self.server = None
                self.server_thread = None
                self._server_starting = False
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
            elif self._server_starting and self.server.started:
                self.status.set(f"Listening on {self.server.config.host}:{self.server.config.port}")
                self._server_starting = False
        receive_mbps = self.receive_bandwidth.bytes_per_second * 8 / 1_000_000
        self.receive_bandwidth_value.set(f"{receive_mbps:.2f} Mbps")
        frames = self.store.list()
        frames.sort(key=lambda frame: (f"{frame.source}/{frame.channel}".casefold(), frame.source, frame.channel))
        now = time.monotonic()
        refresh_previews = now - self._last_preview_refresh >= 1
        if refresh_previews:
            self._last_preview_refresh = now
        existing = set(self.channels_tree.get_children())
        current: set[str] = set()
        for index, frame in enumerate(frames):
            key = f"{frame.source}/{frame.channel}"
            current.add(key)
            values = (
                key,
                f"{frame.width} × {frame.height}",
                frame.revision,
                frame.received_at.replace("T", " ")[:19],
            )
            if self.channels_tree.exists(key):
                self.channels_tree.item(key, values=values)
            else:
                self.channels_tree.insert("", "end", iid=key, values=values)
            self.channels_tree.move(key, "", index)
            if refresh_previews and self._preview_revisions.get(key) != frame.revision:
                thumbnail = ImageOps.pad(
                    frame.image, (50, 50), Image.Resampling.LANCZOS, color="#222832",
                )
                self._preview_images[key] = ImageTk.PhotoImage(thumbnail, master=self.master)
                self._preview_revisions[key] = frame.revision
                self.channels_tree.item(key, image=self._preview_images[key])
        for key in existing - current:
            self.channels_tree.delete(key)
            self._preview_images.pop(key, None)
            self._preview_revisions.pop(key, None)
        channel_set = frozenset(current)
        if channel_set != self._known_channels:
            self._known_channels = channel_set
            self.assignment_input.configure(values=[""] + sorted(channel_set))
        if self.layouts.revision != self._seen_layout_revision:
            self._refresh_layout_tree()
        elif refresh_previews:
            self._draw_layout()
        self.after(500, self._refresh)

    def _refresh_layout_tree(self) -> None:
        paths = {path for path, _ in self.layouts.leaves()} | {path for path, _ in self.layouts.grids()}
        if self._selected_panel not in paths:
            self._selected_panel = None
        self._seen_layout_revision = self.layouts.revision
        self._draw_layout()

    def _draw_layout(self, _event: object = None) -> None:
        if self._active_window is None:
            return
        canvas = self.layout_canvas
        canvas.delete("all")
        self._layout_images.clear()
        self._panel_boxes.clear()
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        display = self.display_window
        if display is not None and display.winfo_exists():
            ratio = max(1, display.winfo_width()) / max(1, display.winfo_height())
        else:
            ratio = self.master.winfo_screenwidth() / self.master.winfo_screenheight()
        w = min(max(1, width - 12), max(1, height - 12) * ratio)
        h = w / ratio
        left, top = (width - w) / 2, (height - h) / 2

        def draw(node: dict, path: tuple[int, ...], x: float, y: float, w: float, h: float) -> None:
            selected = path == self._selected_panel or path == self._drop_panel
            canvas.create_rectangle(x, y, x + w, y + h, fill="#222832", outline="#5AA7FF" if selected else "#596574", width=3 if selected else 1)
            self._panel_boxes[path] = (x, y, x + w, y + h)
            if node["type"] == "grid":
                # A nested grid's header remains selectable for removing nesting.
                header = min(20, h / 4) if path else 0
                if path:
                    canvas.create_text(x + 4, y + header / 2, anchor="w", text=format_panel_label(path), fill="#9AA6B2", width=max(1, w - 8))
                cw, ch = w / node["columns"], (h - header) / node["rows"]
                for index, child in enumerate(node["children"]):
                    draw(child, path + (index,), x + index % node["columns"] * cw + 2, y + header + index // node["columns"] * ch + 2, max(1, cw - 4), max(1, ch - 4))
                return
            channel = node.get("channel")
            frame = self.store.get(*channel.split("/", 1)) if channel and "/" in channel else None
            if frame is not None and w > 12 and h > 36:
                view = Viewport(**node.get("view", {}))
                if self._pan_start is not None and self._pan_start[0] == path and self._pan_view is not None:
                    view = self._pan_view
                rendered = _render_view(frame.image, (max(1, int(w - 8)), max(1, int(h - 30))), view)
                photo = ImageTk.PhotoImage(rendered, master=self.master)
                self._layout_images.append(photo)
                canvas.create_image(x + w / 2, y + (h - 24) / 2, image=photo)
            label = f"{format_panel_label(path)} · {channel or 'Unassigned'}"
            canvas.create_text(x + w / 2, y + h - min(14, h / 2), text=label, fill="#F2F5F8", width=max(1, w - 8), justify="center")

        draw(self.layouts.snapshot(), (), left, top, w, h)

    def _panel_at(self, x_root: int, y_root: int) -> tuple[int, ...] | None:
        x = x_root - self.layout_canvas.winfo_rootx()
        y = y_root - self.layout_canvas.winfo_rooty()
        for path, (left, top, right, bottom) in reversed(list(self._panel_boxes.items())):
            if left <= x <= right and top <= y <= bottom:
                return path
        return None

    def _select_panel(self, event: tk.Event) -> None:
        self.layout_canvas.focus_set()
        self._pan_start = None
        self._pan_view = None
        self._panel_click = None
        self._pan_moved = False
        self._mini_moving = False
        self._selected_panel = self._panel_at(event.x_root, event.y_root)
        self._draw_layout()
        slots = dict(self.layouts.leaves())
        slot = slots.get(self._selected_panel)
        if slot is None:
            return
        self.assignment.set(slot.channel or "")
        self._panel_click = (self._selected_panel, event.x_root, event.y_root, self._space_down)
        if slot.channel:
            self._pan_start = (self._selected_panel, event.x_root, event.y_root, slot.view, self.layouts.revision)
            self._pan_view = slot.view

    def _show_source_menu(self, event: tk.Event) -> None:
        if self._source_menu is not None:
            self._source_menu.destroy()
        menu = self._source_menu = tk.Menu(self, tearoff=False)
        def assign(channel: str) -> None:
            self.assignment.set(channel)
            self._assign_panel()
        menu.add_command(label="Unassigned", command=lambda: assign(""))
        for channel in sorted(self._known_channels, key=str.casefold):
            menu.add_command(label=channel, command=lambda value=channel: assign(value))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _channel_drag_start(self, event: tk.Event) -> None:
        self._drag_channel = self.channels_tree.identify_row(event.y)
        self._drag_revision = self.layouts.revision

    def _space_press(self, _event: object = None) -> str:
        self._space_down = True
        self.layout_canvas.configure(cursor="fleur")
        return "break"

    def _space_release(self, _event: object = None) -> str:
        self._space_down = False
        self.layout_canvas.configure(cursor="")
        return "break"

    def _pan_motion(self, event: tk.Event) -> None:
        if self._panel_click is not None:
            _, x, y, _space = self._panel_click
            self._pan_moved = self._pan_moved or max(abs(event.x_root - x), abs(event.y_root - y)) >= 4
        if not self._pan_moved:
            return
        if self._pan_start is None:
            return
        path, x, y, initial, _revision = self._pan_start
        box = self._panel_boxes.get(path)
        if box is None:
            return
        target = self._panel_at(event.x_root, event.y_root)
        if target != path:
            self._mini_moving = True
        if self._mini_moving:
            self._pan_view = initial
            self._drop_panel = target if target != path and target in dict(self.layouts.leaves()) else None
            self._draw_layout()
            return
        width, height = max(1, box[2] - box[0] - 8), max(1, box[3] - box[1] - 30)
        self._pan_view = Viewport(initial.zoom,
            max(-100, min(100, initial.pan_x + (event.x_root - x) / width)),
            max(-100, min(100, initial.pan_y + (event.y_root - y) / height)))
        self._draw_layout()

    def _pan_release(self, _event: object = None) -> None:
        start, view = self._pan_start, self._pan_view
        click, moved = self._panel_click, self._pan_moved
        moving = self._mini_moving
        self._mini_moving = False
        self._drop_panel = None
        self._panel_click = None
        self._pan_moved = False
        self._pan_start = None
        self._pan_view = None
        if start is not None and _event is not None and (moving or self._panel_at(_event.x_root, _event.y_root) != start[0]):
            target = self._panel_at(_event.x_root, _event.y_root)
            if target != start[0] and target in dict(self.layouts.leaves()):
                try:
                    self.layouts.move_or_swap(start[0], target, start[4])
                    self._selected_panel = target
                    self.assignment.set(dict(self.layouts.leaves())[target].channel or "")
                except (OSError, ValueError) as exc:
                    messagebox.showerror("Could not move channel", str(exc), parent=self.master)
            self._refresh_layout_tree()
        elif moved and start is not None and view is not None:
            self._save_mini_view(start[0], view, start[4])
        elif click is not None and not click[3] and _event is not None:
            if self._panel_at(_event.x_root, _event.y_root) == click[0]:
                self._show_source_menu(_event)

    def _save_mini_view(self, path: tuple[int, ...], view: Viewport, revision: int) -> None:
        try:
            self.layouts.set_view(path, view, revision)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not save panel view", str(exc), parent=self.master)
        self._refresh_layout_tree()

    def _mini_wheel(self, event: tk.Event) -> None:
        self._mini_zoom(event, event.delta / 120 if abs(event.delta) >= 120 else event.delta)

    def _mini_zoom(self, event: tk.Event, steps: float) -> None:
        path = self._panel_at(event.x_root, event.y_root)
        slot = dict(self.layouts.leaves()).get(path)
        if slot is None or not slot.channel or self._pan_start is not None or not steps:
            return
        left, top, right, bottom = self._panel_boxes[path]
        x = (event.x_root - self.layout_canvas.winfo_rootx() - left - 4) / max(1, right - left - 8) - 0.5
        y = (event.y_root - self.layout_canvas.winfo_rooty() - top - 3) / max(1, bottom - top - 30) - 0.5
        old = slot.view
        zoom = max(1, min(16, old.zoom * 1.2 ** max(-20, min(20, steps))))
        ratio = zoom / old.zoom
        view = Viewport(zoom, max(-100, min(100, x - ratio * (x - old.pan_x))),
                        max(-100, min(100, y - ratio * (y - old.pan_y))))
        self._save_mini_view(path, view, self.layouts.revision)

    def _channel_drag_motion(self, event: tk.Event) -> None:
        if not self._drag_channel:
            return
        path = self._panel_at(event.x_root, event.y_root)
        target = path if path in dict(self.layouts.leaves()) else None
        if target != self._drop_panel:
            self._drop_panel = target
            self._draw_layout()
        self.channels_tree.configure(cursor="hand2" if target is not None else "fleur")

    def _channel_drag_release(self, event: tk.Event) -> None:
        channel = self._drag_channel
        path = self._panel_at(event.x_root, event.y_root)
        revision = self._drag_revision
        self._cancel_channel_drag()
        if channel and path in dict(self.layouts.leaves()):
            if revision != self.layouts.revision:
                messagebox.showerror("Assignment", "The layout changed during the drag. Please try again.")
                return
            self._selected_panel = path
            self.assignment.set(channel)
            self._assign_panel()

    def _cancel_channel_drag(self, _event: object = None) -> None:
        self._drag_channel = None
        self._drop_panel = None
        self.channels_tree.configure(cursor="")
        self._draw_layout()

    def _selected_layout_path(self, allow_root: bool = False) -> tuple[int, ...]:
        if self._selected_panel is None:
            raise ValueError("Select a panel first.")
        if not self._selected_panel:
            if allow_root:
                return ()
            raise ValueError("Select a panel below the root grid.")
        return self._selected_panel

    def _resize_layout(self) -> None:
        try:
            self.layouts.resize(int(self.layout_rows.get()), int(self.layout_columns.get()))
            self._refresh_layout_tree()
        except ValueError as exc:
            messagebox.showerror("Layout", str(exc))

    def _assign_panel(self) -> None:
        try:
            self.layouts.assign(self._selected_layout_path(), self.assignment.get())
            self._refresh_layout_tree()
        except ValueError as exc:
            messagebox.showerror("Assignment", str(exc))

    def _clear_assignment(self) -> None:
        try:
            self.layouts.assign(self._selected_layout_path(), None)
            self._refresh_layout_tree()
        except ValueError as exc:
            messagebox.showerror("Assignment", str(exc))

    def _nest_panel(self) -> None:
        try:
            self.layouts.nest(
                self._selected_layout_path(), int(self.nested_rows.get()), int(self.nested_columns.get())
            )
            self._refresh_layout_tree()
        except ValueError as exc:
            messagebox.showerror("Nested layout", str(exc))

    def _flatten_panel(self) -> None:
        try:
            self.layouts.flatten(self._selected_layout_path())
            self._refresh_layout_tree()
        except ValueError as exc:
            messagebox.showerror("Nested layout", str(exc))

    def _close(self) -> None:
        if not self._save_window_name():
            return
        self._stop()
        self.master.destroy()
