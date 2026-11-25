"""Tk desktop configuration and status UI."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import logging
import queue
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageOps, ImageTk
import mss

from kommand_source.config import (
    CHANNEL_NAME_PATTERN, ChannelConfig, SourceConfig, parse_region_target, parse_screen_target, save_config,
)
from kommand_source.capture.windows import (
    WindowDiscoveryError,
    WindowIdentity,
    WindowInfo,
    find_window,
    get_window_identity,
    list_visible_windows,
    resolve_window,
)
from kommand_source.debug_store import DebugStore
from kommand_source.runner import SourceRunner
from kommand_source.transport import CentralConnectionError, check_central_connection
from kommand_common.bandwidth import RollingBandwidth
from kommand_common.ui import NumberStepper, action_button, app_header, apply_theme, card

FORMAT_LABELS = {"WEBP": "webp", "WEBP + PATCH": "webp_patch", "VIDEO": "video"}


class SourceApp(ttk.Frame):
    COLORS = {
        "background": "#101318",
        "surface": "#191D24",
        "surface_high": "#222832",
        "border": "#303844",
        "text": "#F2F5F8",
        "muted": "#9AA6B2",
        "primary": "#5AA7FF",
        "primary_hover": "#80BAFF",
        "primary_pressed": "#3689E6",
        "danger": "#FF6B7A",
        "selection": "#244B73",
    }

    def __init__(
        self,
        master: tk.Tk,
        config: SourceConfig,
        config_path: Path,
        debug_store: DebugStore,
    ):
        apply_theme(master)
        super().__init__(master, padding=24, style="App.TFrame")
        self.master = master
        self.config = config
        self.config_path = config_path
        self.debug_store = debug_store
        self.events: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self.send_bandwidth = RollingBandwidth()
        self.runner = SourceRunner(debug_store, self._post_status, self.send_bandwidth)

        master.title("Kommand Source")
        master.geometry("1080x700")
        master.minsize(940, 620)
        master.protocol("WM_DELETE_WINDOW", self._close)
        self.pack(fill="both", expand=True)

        self.source_name = tk.StringVar(value=config.source_name)
        self.central_url = tk.StringVar(value=config.central_url)
        self.name = tk.StringVar()
        self.capture_type = tk.StringVar(value="screen")
        self.capture_format = tk.StringVar(value="WEBP + PATCH")
        self.capture_target = tk.StringVar(value="1")
        self.interval = tk.StringVar(value="5000")
        self.video_fps = tk.StringVar(value="15")
        self.full_frame_interval = tk.StringVar(value="60")
        self.quality = tk.StringVar(value="80")
        self.enabled = tk.BooleanVar(value=True)
        self.overall_status = tk.StringVar(value="Stopped")
        self.connection_status = tk.StringVar(value="Checking…")
        self.send_bandwidth_value = tk.StringVar(value="0.00 Mbps")
        self.connection_events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.connection_check_running = False
        self.window_events: queue.Queue[
            list[tuple[str, WindowIdentity, str | None, str | None]]
        ] = queue.Queue()
        self.window_check_thread: threading.Thread | None = None
        self._window_choices: dict[str, WindowInfo] = {}
        self._screen_choices: dict[str, str] = {}
        self._result_details: dict[str, str] = {}
        self._editor_window_identity: WindowIdentity | None = None
        self._editor_window_title = ""
        self._editor_capture_type = "screen"
        self._loading_editor = False
        self._editor_baseline: tuple[object, ...] = ()
        self._stopping = False
        self._closing = False
        self._preview_images: dict[str, ImageTk.PhotoImage] = {}
        self._preview_files: dict[str, tuple[Path, int, int]] = {}
        self._broken_preview: ImageTk.PhotoImage | None = None

        self._build()
        self._refresh_channels()
        self._editor_baseline = self._editor_values()
        for variable in (
            self.name, self.capture_type, self.capture_format, self.capture_target,
            self.interval, self.full_frame_interval, self.quality, self.video_fps, self.enabled,
        ):
            variable.trace_add("write", self._editor_changed)
        self.source_name.trace_add("write", self._connection_changed)
        self.central_url.trace_add("write", self._connection_changed)
        rows = self.tree.get_children()
        if rows:
            self.tree.selection_set(rows[0])
            self.tree.focus(rows[0])
            self.tree.see(rows[0])
            self._select_channel()
        self._editor_changed()
        self._connection_changed()
        self.after(100, self._drain_events)
        self.after(0, self._refresh_previews)
        self.after(0, self._check_central_connection)
        self.after(0, self._check_window_titles)

    def _build(self) -> None:
        app_header(self, "Kommand-Source: Define screenshots and destination").pack(
            fill="x", pady=(0, 14)
        )

        connection = card(self, "Central connection")
        connection.pack(fill="x")
        self.source_name_input = self._entry(connection, "Machine Name", self.source_name, 0, 0)
        self.central_url_input = self._entry(connection, "Central URL", self.central_url, 0, 2, width=38)
        ttk.Label(connection, text="Connection status").grid(row=0, column=4, sticky="w")
        self.connection_status_label = ttk.Label(
            connection,
            textvariable=self.connection_status,
            style="ConnectionNeutral.Status.TLabel",
            width=len("Not contactable"),
            anchor="center",
        )
        self.connection_status_label.grid(row=1, column=4, sticky="w")
        ttk.Label(connection, text="Send bandwidth").grid(row=0, column=5, sticky="w", padx=(12, 0))
        ttk.Label(
            connection,
            textvariable=self.send_bandwidth_value,
            style="Status.TLabel",
        ).grid(row=1, column=5, sticky="w", padx=(12, 0))

        channels = card(self, "Channels")
        channels.pack(fill="both", expand=True, pady=(16, 0))
        channels.columnconfigure(0, weight=1)
        channels.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            channels,
            columns=("channel", "type", "target", "status", "detail"),
            show="tree headings",
            height=8,
            style="Source.Treeview",
        )
        # A 64-pixel row leaves a comfortable gutter around the 50-pixel preview.
        ttk.Style(self.master).configure("Source.Treeview", rowheight=64)
        self.tree.heading("#0", text="Preview")
        for key, label, width, minimum, stretch in (
            ("channel", "Channel", 120, 100, False),
            ("type", "Type", 80, 70, False),
            ("target", "Target", 230, 140, True),
            ("status", "Status", 95, 85, False),
            ("detail", "Last result", 190, 140, True),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=minimum, stretch=stretch, anchor="w")
        self.tree.column("channel", anchor="center")
        self.tree.column("status", anchor="center")
        self.tree.column("detail", anchor="center")
        self.tree.column("#0", width=76, minwidth=76, stretch=False, anchor="center")
        self.tree.grid(row=0, column=0, columnspan=6, sticky="nsew")
        scrollbar_style = ttk.Style(self.master)
        scrollbar_style.configure(
            "Source.Vertical.TScrollbar",
            troughcolor=self.COLORS["background"],
            background=self.COLORS["border"],
            bordercolor=self.COLORS["surface_high"],
            lightcolor=self.COLORS["border"],
            darkcolor=self.COLORS["border"],
            arrowcolor=self.COLORS["muted"],
        )
        scrollbar_style.map(
            "Source.Vertical.TScrollbar",
            background=[("pressed", self.COLORS["primary"]), ("active", "#465568")],
            arrowcolor=[("active", self.COLORS["text"])],
        )
        self.channels_scrollbar = ttk.Scrollbar(
            channels, orient="vertical", command=self.tree.yview, style="Source.Vertical.TScrollbar",
        )
        self.channels_scrollbar.grid(row=0, column=6, sticky="ns")
        self.tree.configure(yscrollcommand=self.channels_scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._select_channel)
        self.tree.bind("<Configure>", self._wrap_results)
        self.tree.bind("<ButtonRelease-1>", self._wrap_results, add="+")

        editor = ttk.Frame(channels, padding=(0, 10, 0, 0))
        editor.grid(row=1, column=0, columnspan=6, sticky="ew")
        ttk.Label(editor, text="Channel name").grid(row=0, column=0, sticky="w")
        self.name_input = ttk.Entry(editor, textvariable=self.name, width=22, style="Source.TEntry")
        self.name_input.grid(row=1, column=0, sticky="ew", padx=(0, 12))
        ttk.Label(editor, text="Type").grid(row=0, column=1, sticky="w")
        self.capture_type_input = ttk.Combobox(
            editor,
            textvariable=self.capture_type,
            values=("screen", "window"),
            state="readonly",
            style="Source.TCombobox",
            width=14,
        )
        self.capture_type_input.grid(row=1, column=1, sticky="ew", padx=(0, 12))
        self.capture_type_input.bind("<<ComboboxSelected>>", self._capture_type_selected)
        ttk.Label(editor, text="Format").grid(row=0, column=2, sticky="w")
        self.format_input = ttk.Combobox(
            editor, textvariable=self.capture_format, values=tuple(FORMAT_LABELS),
            state="readonly", width=14, style="Source.TCombobox",
        )
        self.format_input.grid(row=1, column=2, sticky="ew", padx=(0, 12))
        self.format_input.bind("<<ComboboxSelected>>", self._format_changed)
        ttk.Label(editor, text="Target").grid(row=0, column=3, sticky="w")
        self.target_input = ttk.Combobox(
            editor, textvariable=self.capture_target, width=24,
            postcommand=self._refresh_window_choices, style="Source.TCombobox",
        )
        self.target_input.grid(row=1, column=3, sticky="ew", padx=(0, 8))
        self.target_input.bind("<<ComboboxSelected>>", self._window_selected)
        self.target_input.bind("<FocusOut>", lambda _event: self.target_input.selection_clear(), add="+")
        self.refresh_windows_button = ttk.Button(
            editor, text="REFRESH WINDOWS", command=self._refresh_windows
        )
        self.refresh_windows_button.grid(row=1, column=4, sticky="ew")

        self.interval_label = ttk.Label(editor, text="Interval (ms)")
        self.interval_input = NumberStepper(
            editor, self.interval, minimum=250, maximum=86_400_000, step=250, width=10, allow_invalid=True
        )
        self.full_frame_label = ttk.Label(editor, text="Full/keyframe (sec)")
        self.full_frame_input = NumberStepper(
            editor, self.full_frame_interval, minimum=1, maximum=86_400, width=8, allow_invalid=True
        )
        self.quality_label = ttk.Label(editor, text="Quality")
        self.quality_input = NumberStepper(
            editor, self.quality, minimum=1, maximum=100, width=5, zero_pad=2, allow_invalid=True
        )
        self.video_fps_label = ttk.Label(editor, text="Video FPS")
        self.video_fps_input = NumberStepper(
            editor, self.video_fps, minimum=1, maximum=60, width=5, allow_invalid=True
        )
        for stepper in (self.interval_input, self.full_frame_input, self.quality_input, self.video_fps_input):
            stepper.entry.configure(style="Source.Stepper.TEntry")
        ttk.Checkbutton(editor, text="Enabled", variable=self.enabled).grid(row=3, column=4, sticky="w")
        editor.columnconfigure(3, weight=1)
        self._capture_type_changed()
        self._format_changed()

        edit_buttons = ttk.Frame(channels, style="Card.TFrame")
        edit_buttons.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        self.new_button = action_button(edit_buttons, "New", self._clear_editor)
        self.new_button.pack(side="left")
        self.update_button = action_button(edit_buttons, "Update", self._upsert, "primary")
        self.update_button.pack(side="left", padx=8)
        action_button(edit_buttons, "Remove", self._remove, "danger").pack(side="left")
        action_button(edit_buttons, "Capture once", self._capture_once).pack(side="left", padx=8)

        self.actions = ttk.Frame(self, padding=(0, 16, 0, 0), style="App.TFrame")
        # Reserve the footer before the expandable channel card. Under height
        # pressure the middle card shrinks; primary actions remain reachable.
        self.actions.pack(fill="x", side="bottom", before=channels)
        action_button(self.actions, "Save config", self._save).pack(side="left")
        self.start_button = action_button(self.actions, "Start capture", self._start, "primary")
        self.start_button.pack(side="left", padx=8)
        self.stop_button = action_button(self.actions, "Stop", self._stop, "danger")
        self.stop_button.configure(state="disabled")
        self.stop_button.pack(side="left")
        ttk.Label(self.actions, textvariable=self.overall_status, style="Status.TLabel").pack(side="right")
        self._configure_validation_styles()

    def _configure_styles(self, master: tk.Tk) -> None:
        colors = self.COLORS
        master.configure(background=colors["background"])
        style = ttk.Style(master)
        style.theme_use("clam")

        style.configure(".", font=("Segoe UI", 10), foreground=colors["text"])
        style.configure("TFrame", background=colors["surface"])
        style.configure("App.TFrame", background=colors["background"])
        style.configure("Card.TFrame", background=colors["surface"])
        style.configure(
            "TLabel", background=colors["surface"], foreground=colors["muted"]
        )
        style.configure(
            "Title.TLabel",
            background=colors["background"],
            foreground=colors["text"],
            font=("Segoe UI Semibold", 24),
        )
        style.configure(
            "Subtitle.TLabel",
            background=colors["background"],
            foreground=colors["muted"],
            font=("Segoe UI", 10),
        )
        style.configure(
            "Eyebrow.TLabel",
            background=colors["background"],
            foreground=colors["primary"],
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "Status.TLabel",
            background=colors["surface_high"],
            foreground=colors["text"],
            padding=(12, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "Card.TLabelframe",
            background=colors["surface"],
            bordercolor=colors["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=colors["surface"],
            foreground=colors["primary"],
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "TEntry",
            fieldbackground=colors["surface_high"],
            foreground=colors["text"],
            insertcolor=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            padding=(9, 7),
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", colors["primary"])],
            lightcolor=[("focus", colors["primary"])],
            darkcolor=[("focus", colors["primary"])],
        )
        style.configure(
            "TCombobox",
            fieldbackground=colors["surface_high"],
            background=colors["surface_high"],
            foreground=colors["text"],
            arrowcolor=colors["primary"],
            bordercolor=colors["border"],
            padding=(7, 6),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", colors["surface_high"])],
            foreground=[("readonly", colors["text"])],
            bordercolor=[("focus", colors["primary"])],
        )
        master.option_add("*TCombobox*Listbox.background", colors["surface_high"])
        master.option_add("*TCombobox*Listbox.foreground", colors["text"])
        master.option_add("*TCombobox*Listbox.selectBackground", colors["selection"])

        style.configure(
            "TButton",
            background=colors["surface_high"],
            foreground=colors["text"],
            borderwidth=0,
            padding=(14, 9),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "TButton",
            background=[("active", colors["border"]), ("pressed", colors["selection"])],
            foreground=[("disabled", colors["muted"])],
        )
        style.configure(
            "Accent.TButton", background=colors["primary"], foreground="#07111E"
        )
        style.map(
            "Accent.TButton",
            background=[
                ("active", colors["primary_hover"]),
                ("pressed", colors["primary_pressed"]),
                ("disabled", colors["border"]),
            ],
            foreground=[("disabled", colors["muted"])],
        )
        style.configure(
            "Danger.TButton", background=colors["surface_high"], foreground=colors["danger"]
        )
        style.map("Danger.TButton", background=[("active", "#3A2830")])
        style.configure(
            "TCheckbutton",
            background=colors["surface"],
            foreground=colors["text"],
            indicatorcolor=colors["surface_high"],
            padding=(4, 6),
        )
        style.map(
            "TCheckbutton",
            indicatorcolor=[("selected", colors["primary"])],
            background=[("active", colors["surface"])],
        )
        style.configure(
            "Treeview",
            background=colors["surface_high"],
            fieldbackground=colors["surface_high"],
            foreground=colors["text"],
            bordercolor=colors["border"],
            rowheight=32,
            font=("Segoe UI", 9),
        )
        style.map(
            "Treeview",
            background=[("selected", colors["selection"])],
            foreground=[("selected", colors["text"])],
        )
        style.configure(
            "Treeview.Heading",
            background=colors["surface"],
            foreground=colors["muted"],
            relief="flat",
            padding=(8, 9),
            font=("Segoe UI Semibold", 9),
        )
        style.map("Treeview.Heading", background=[("active", colors["surface_high"])])

    @staticmethod
    def _entry(
        parent: tk.Misc,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        width: int = 18,
        show: str = "",
    ) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row * 2, column=column, sticky="w", padx=(0, 8))
        entry = ttk.Entry(parent, textvariable=variable, width=width, show=show, style="Source.TEntry")
        entry.grid(row=row * 2 + 1, column=column, columnspan=2, sticky="ew", padx=(0, 12))
        return entry

    def _configure_validation_styles(self) -> None:
        style = ttk.Style(self.master)
        red_background = "#512730"
        red = self.COLORS["danger"]
        style.map(
            "Source.TEntry", fieldbackground=[("invalid", red_background)],
            bordercolor=[("invalid", red), ("focus", self.COLORS["primary"])],
            lightcolor=[("invalid", red), ("focus", self.COLORS["primary"])],
            darkcolor=[("invalid", red), ("focus", self.COLORS["primary"])],
        )
        style.map(
            "Source.TCombobox",
            fieldbackground=[("invalid", red_background), ("readonly", self.COLORS["surface_high"])],
            bordercolor=[("invalid", red), ("focus", self.COLORS["primary"])],
        )
        style.map(
            "Source.Stepper.TEntry",
            fieldbackground=[("invalid", red_background), ("focus", self.COLORS["surface_high"])],
        )
        status_options = {
            "padding": (12, 7),
            "font": ("Segoe UI Semibold", 9),
        }
        style.configure(
            "ConnectionNeutral.Status.TLabel",
            background=self.COLORS["surface_high"], foreground=self.COLORS["muted"], **status_options,
        )
        style.configure(
            "ConnectionGood.Status.TLabel",
            background="#183A2B", foreground="#63D995", **status_options,
        )
        style.configure(
            "ConnectionBad.Status.TLabel",
            background="#512730", foreground=self.COLORS["danger"], **status_options,
        )

    def _set_connection_status(self, status: str) -> None:
        normalized = status.strip().casefold()
        if normalized == "not contactable":
            style = "ConnectionBad.Status.TLabel"
        elif normalized == "contactable" or normalized.startswith("running"):
            style = "ConnectionGood.Status.TLabel"
        else:
            style = "ConnectionNeutral.Status.TLabel"
        self.connection_status.set(status)
        self.connection_status_label.configure(style=style)

    def _editor_values(self) -> tuple[object, ...]:
        target: object = self.capture_target.get().strip()
        if self.capture_type.get() == "window" and self._editor_window_identity is not None:
            target = self._editor_window_identity

        def number(variable: tk.StringVar) -> int | str:
            value = variable.get()
            try:
                return int(value)
            except ValueError:
                return value

        return (
            self.name.get().strip(), self.capture_type.get(), self.capture_format.get(), target,
            number(self.interval), number(self.full_frame_interval), number(self.quality),
            number(self.video_fps), self.enabled.get(),
        )

    @staticmethod
    def _mark_invalid(widget: ttk.Widget, invalid: bool) -> None:
        widget.state(["invalid" if invalid else "!invalid"])

    def _connection_changed(self, *_args: str) -> None:
        self._mark_invalid(
            self.source_name_input, not bool(CHANNEL_NAME_PATTERN.fullmatch(self.source_name.get().strip()))
        )
        self._mark_invalid(
            self.central_url_input, not self.central_url.get().strip().startswith(("http://", "https://"))
        )

    def _editor_changed(self, *_args: str) -> None:
        if self._loading_editor:
            return
        selected = self.tree.selection()
        name = self.name.get().strip()
        duplicate = any(
            channel.name == name and (not selected or channel.name != selected[0])
            for channel in self.config.channels
        )
        self._mark_invalid(self.name_input, not CHANNEL_NAME_PATTERN.fullmatch(name) or duplicate)
        capture_type = self.capture_type.get()
        self._mark_invalid(self.capture_type_input, capture_type not in {"screen", "window", "region"})
        target_invalid = False
        if capture_type == "screen":
            try:
                parse_screen_target(self.capture_target.get().strip())
            except ValueError:
                target_invalid = True
        elif capture_type == "window":
            identity = self._editor_window_identity
            target_invalid = identity is None or self.capture_target.get() != self._window_label(
                self._editor_window_title, identity.handle
            )
        elif capture_type == "region":
            try:
                parse_region_target(self.capture_target.get())
            except ValueError:
                target_invalid = True
        self._mark_invalid(self.target_input, target_invalid)
        hidden_invalid = False
        for variable, stepper in (
            (self.interval, self.interval_input), (self.full_frame_interval, self.full_frame_input),
            (self.quality, self.quality_input), (self.video_fps, self.video_fps_input),
        ):
            try:
                invalid = not stepper.minimum <= int(variable.get()) <= stepper.maximum
            except ValueError:
                invalid = True
            self._mark_invalid(stepper.entry, invalid)
            hidden_invalid = hidden_invalid or (invalid and not stepper.grid_info())
        self._mark_invalid(self.format_input, self.capture_format.get() not in FORMAT_LABELS or hidden_invalid)
        self.update_button.configure(
            state="normal" if self._editor_values() != self._editor_baseline else "disabled"
        )

    def _remember_editor(self) -> None:
        self._editor_baseline = self._editor_values()
        self._editor_changed()

    def _configuration_from_ui(self) -> SourceConfig:
        return SourceConfig(
            source_name=self.source_name.get().strip(),
            central_url=self.central_url.get().strip(),
            request_timeout_seconds=self.config.request_timeout_seconds,
            channels=list(self.config.channels),
        )

    def _channel_from_editor(self) -> ChannelConfig:
        identity = self._editor_window_identity if self.capture_type.get() == "window" else None
        if self.capture_type.get() == "window" and identity is None:
            raise ValueError("Select a window from the window list.")
        capture_format = FORMAT_LABELS.get(self.capture_format.get())
        if capture_format is None:
            raise ValueError("Select a valid format.")
        if self.capture_type.get() == "screen":
            parse_screen_target(self.capture_target.get().strip())
        elif self.capture_type.get() == "region":
            parse_region_target(self.capture_target.get().strip())

        def number(variable: tk.StringVar, stepper: NumberStepper, label: str) -> int:
            try:
                value = int(variable.get())
            except ValueError as exc:
                raise ValueError(
                    f"{label} must be a whole number between {stepper.minimum} and {stepper.maximum}."
                ) from exc
            if not stepper.minimum <= value <= stepper.maximum:
                raise ValueError(f"{label} must be between {stepper.minimum} and {stepper.maximum}.")
            return value

        channel = ChannelConfig(
            name=self.name.get().strip(),
            capture_type=self.capture_type.get(),
            capture_target=self._editor_window_title if identity else self.capture_target.get().strip(),
            interval_ms=number(self.interval, self.interval_input, "Capture interval"),
            full_frame_interval_seconds=number(
                self.full_frame_interval, self.full_frame_input, "Full/keyframe interval"
            ),
            quality=number(self.quality, self.quality_input, "Quality"),
            format=capture_format,
            video_fps=number(self.video_fps, self.video_fps_input, "Video FPS"),
            enabled=self.enabled.get(),
            window_handle=identity.handle if identity else None,
            window_process_id=identity.process_id if identity else None,
            window_process_start=identity.process_start if identity else None,
        )
        channel.validate()
        return channel

    def _upsert(self) -> None:
        try:
            channel = self._channel_from_editor()
            selected = self.tree.selection()
            if not selected and any(item.name == channel.name for item in self.config.channels):
                raise ValueError(f"Duplicate channel name: {channel.name}")
            original_name = selected[0] if selected else channel.name
            updated = self._with_channel(channel, original_name)
            self._apply_running_configuration(updated)
        except (RuntimeError, ValueError) as exc:
            messagebox.showerror("Invalid channel", str(exc))
            return
        self.config = updated
        self._refresh_channels()
        self.tree.selection_set(channel.name)
        self._remember_editor()

    def _remove(self) -> None:
        selected = self.tree.selection()
        if selected:
            updated = replace(
                self.config,
                channels=[item for item in self.config.channels if item.name != selected[0]],
            )
            try:
                self._apply_running_configuration(updated)
            except (RuntimeError, ValueError) as exc:
                messagebox.showerror("Cannot remove channel", str(exc))
                return
            self.config = updated
            self._refresh_channels()
            self._clear_editor()

    def _with_channel(self, channel: ChannelConfig, original_name: str) -> SourceConfig:
        if channel.name != original_name and any(
            item.name == channel.name for item in self.config.channels
        ):
            raise ValueError(f"Duplicate channel name: {channel.name}")
        channels = [item for item in self.config.channels if item.name != original_name]
        channels.append(channel)
        updated = replace(self.config, channels=channels)
        updated.validate()
        return updated

    def _apply_running_configuration(self, config: SourceConfig) -> None:
        if self.runner.active:
            self.runner.reconfigure(config)
            count = sum(channel.enabled for channel in config.channels)
            self.overall_status.set(f"Running {count} channel(s)")

    def _select_channel(self, _event: object = None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        channel = next((item for item in self.config.channels if item.name == selected[0]), None)
        if channel is None:
            return
        self._loading_editor = True
        try:
            self.name.set(channel.name)
            self.capture_type.set(channel.capture_type)
            self.capture_format.set(next(label for label, value in FORMAT_LABELS.items() if value == channel.format))
            self._editor_window_identity = self._channel_window_identity(channel)
            self._editor_window_title = channel.capture_target
            self.capture_target.set(
                self._window_label(channel.capture_target, self._editor_window_identity.handle)
                if self._editor_window_identity else channel.capture_target
            )
            self.interval.set(str(channel.interval_ms))
            self.video_fps.set(str(channel.video_fps))
            self.full_frame_interval.set(str(channel.full_frame_interval_seconds))
            self.quality.set(str(channel.quality))
            self.enabled.set(channel.enabled)
            self._capture_type_changed()
            self._format_changed()
            self.target_input.selection_clear()
        finally:
            self._loading_editor = False
        self._remember_editor()

    def _clear_editor(self) -> None:
        self._loading_editor = True
        try:
            self.name.set("")
            self.capture_type.set("screen")
            self.capture_format.set("WEBP + PATCH")
            self.capture_target.set("1")
            self._editor_window_identity = None
            self._editor_window_title = ""
            self.interval.set("5000")
            self.video_fps.set("15")
            self.full_frame_interval.set("60")
            self.quality.set("80")
            self.enabled.set(True)
            self.tree.selection_remove(self.tree.selection())
            self._capture_type_changed()
            self._format_changed()
            self.target_input.selection_clear()
        finally:
            self._loading_editor = False
        self._remember_editor()

    def _format_changed(self, _event: object = None) -> None:
        capture_format = FORMAT_LABELS.get(self.capture_format.get())
        is_video = capture_format == "video"
        self.full_frame_label.configure(text="Keyframe (seconds)" if is_video else "Full frame (seconds)")
        fields = (
            (self.interval_label, self.interval_input, not is_video, 0, "ew"),
            (self.video_fps_label, self.video_fps_input, is_video, 0, "w"),
            (self.full_frame_label, self.full_frame_input, capture_format != "webp", 1, "ew"),
            (self.quality_label, self.quality_input, True, 1 if capture_format == "webp" else 2, "w"),
        )
        for label, widget, visible, column, sticky in fields:
            if visible:
                label.grid(row=2, column=column, sticky="w", pady=(10, 0))
                widget.grid(row=3, column=column, sticky=sticky, padx=(0, 12))
            else:
                label.grid_remove()
                widget.grid_remove()
        if hasattr(self, "update_button"):
            self._editor_changed()

    def _capture_type_selected(self, _event: object = None) -> None:
        capture_type = self.capture_type.get()
        if capture_type != self._editor_capture_type:
            self.capture_target.set("1" if capture_type == "screen" else "")
            self._editor_window_identity = None
            self._editor_window_title = ""
        self._capture_type_changed()
        self._editor_changed()

    def _capture_type_changed(self, _event: object = None) -> None:
        self._editor_capture_type = self.capture_type.get()
        is_window = self.capture_type.get() == "window"
        self.refresh_windows_button.configure(state="normal" if is_window else "disabled")
        self.target_input.configure(state="readonly" if is_window else "normal")
        if is_window:
            self._refresh_windows(show_errors=False)
        else:
            self._editor_window_identity = None
            self._editor_window_title = ""
            self.target_input.configure(values=())

    def _refresh_windows(self, show_errors: bool = True) -> None:
        try:
            windows = list_visible_windows()
            self._set_window_choices(windows)
            identity = self._editor_window_identity
            if identity is not None:
                matching = next((window for window in windows if window.handle == identity.handle), None)
                if matching is not None:
                    try:
                        actual_identity = get_window_identity(matching.handle)
                    except WindowDiscoveryError as exc:
                        if show_errors:
                            messagebox.showerror("Window discovery", str(exc))
                    else:
                        if actual_identity == identity:
                            self._editor_window_title = matching.title
                            self.capture_target.set(self._window_label(matching.title, matching.handle))
            else:
                query = self.capture_target.get().strip().casefold()
                selected = self.tree.selection()
                existing = next(
                    (channel for channel in self.config.channels
                     if selected and channel.name == selected[0] and channel.capture_type == "window"),
                    None,
                )
                matches = [
                    window for window in windows
                    if existing is not None and query == existing.capture_target.casefold()
                    and query and query in window.title.casefold()
                ]
                exact = [window for window in matches if window.title.casefold() == query]
                candidates = exact or matches
                if len(candidates) == 1:
                    window = candidates[0]
                    self._editor_window_identity = get_window_identity(window.handle)
                    self._editor_window_title = window.title
                    self.capture_target.set(self._window_label(window.title, window.handle))
            if not windows and show_errors:
                messagebox.showinfo("Windows", "No visible, non-minimized windows were found.")
        except WindowDiscoveryError as exc:
            self.target_input.configure(values=())
            if show_errors:
                messagebox.showerror("Window discovery", str(exc))

    def _refresh_window_choices(self) -> None:
        if self.capture_type.get() == "window":
            self._refresh_windows()
        elif self.capture_type.get() == "screen":
            try:
                with mss.mss() as capturer:
                    self._screen_choices = {
                        f"{index} — {'Combined desktop' if index == 0 else 'Screen ' + str(index)} "
                        f"({monitor['width']} × {monitor['height']})": str(index)
                        for index, monitor in enumerate(capturer.monitors)
                    }
                self.target_input.configure(values=list(self._screen_choices))
            except Exception as exc:
                self._screen_choices = {}
                self.target_input.configure(values=())
                messagebox.showerror("Screen discovery", str(exc))

    @staticmethod
    def _window_label(title: str, handle: int) -> str:
        return f"{title} [0x{handle:X}]"

    @staticmethod
    def _channel_window_identity(channel: ChannelConfig) -> WindowIdentity | None:
        if (
            channel.window_handle is None
            or channel.window_process_id is None
            or channel.window_process_start is None
        ):
            return None
        return WindowIdentity(channel.window_handle, channel.window_process_id, channel.window_process_start)

    def _set_window_choices(self, windows: list[WindowInfo]) -> None:
        self._window_choices = {
            self._window_label(window.title, window.handle): window for window in windows
        }
        self.target_input.configure(
            values=list(self._window_choices) if self.capture_type.get() == "window" else ()
        )

    def _window_selected(self, _event: object = None) -> None:
        self.target_input.selection_clear()
        if self.capture_type.get() == "screen":
            target = self._screen_choices.get(self.capture_target.get())
            if target is not None:
                self.capture_target.set(target)
            self._editor_changed()
            return
        if self.capture_type.get() != "window":
            return
        window = self._window_choices.get(self.capture_target.get())
        if window is None:
            self._editor_window_identity = None
            self._editor_window_title = ""
            self._editor_changed()
            messagebox.showerror("Window selection", "Refresh and select an available window.")
            return
        try:
            self._editor_window_identity = get_window_identity(window.handle)
        except WindowDiscoveryError as exc:
            self._editor_window_identity = None
            self._editor_window_title = ""
            self._editor_changed()
            messagebox.showerror("Window selection", str(exc))
            return
        self._editor_window_title = window.title
        self._editor_changed()

    def _resolve_legacy_windows(self, config: SourceConfig) -> SourceConfig:
        channels = []
        for channel in config.channels:
            if channel.capture_type == "window" and self._channel_window_identity(channel) is None:
                try:
                    window = find_window(channel.capture_target)
                    identity = get_window_identity(window.handle)
                    channel = replace(
                        channel, capture_target=window.title, window_handle=identity.handle,
                        window_process_id=identity.process_id, window_process_start=identity.process_start,
                    )
                except WindowDiscoveryError as exc:
                    self._post_status(channel.name, "Error", str(exc))
            channels.append(channel)
        return replace(config, channels=channels)

    def _refresh_channels(self) -> None:
        existing = {
            item: (self.tree.set(item, "status"), self.tree.set(item, "detail"))
            for item in self.tree.get_children()
        }
        self.tree.delete(*self.tree.get_children())
        for channel in sorted(self.config.channels, key=lambda channel: (channel.name.casefold(), channel.name)):
            previous = existing.get(channel.name, ())
            status = previous[0] if previous else ("Ready" if channel.enabled else "Disabled")
            detail = previous[1] if previous else ""
            self.tree.insert(
                "",
                "end",
                iid=channel.name,
                text="",
                image=self._preview_images.get(channel.name, ""),
                values=(
                    channel.name,
                    channel.capture_type,
                    channel.capture_target,
                    status,
                    detail,
                ),
            )

    def _wrap_results(self, _event: object = None) -> None:
        style = ttk.Style(self.master)
        font = tkfont.Font(root=self.master, font=style.lookup("Source.Treeview", "font"))
        available = max(20, int(self.tree.column("detail", "width")) - 16)
        row_height = 64
        for name in self.tree.get_children():
            text = self._result_details.get(name, self.tree.set(name, "detail"))
            lines = []
            for paragraph in text.splitlines() or [""]:
                line = ""
                for character in paragraph:
                    if line and font.measure(line + character) > available:
                        lines.append(line)
                        line = ""
                    line += character
                lines.append(line)
            self.tree.set(name, "detail", "\n".join(lines))
            row_height = max(row_height, len(lines) * font.metrics("linespace") + 12)
        style.configure("Source.Treeview", rowheight=row_height)

    def _broken_preview_image(self) -> ImageTk.PhotoImage:
        if self._broken_preview is None:
            image = Image.new("RGB", (50, 50), self.COLORS["surface_high"])
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 49, 49), outline=self.COLORS["danger"], width=2)
            draw.line((11, 11, 39, 39), fill=self.COLORS["danger"], width=6)
            draw.line((39, 11, 11, 39), fill=self.COLORS["danger"], width=6)
            self._broken_preview = ImageTk.PhotoImage(image, master=self.master)
        return self._broken_preview

    def _latest_preview_file(self, channel_name: str) -> Path | None:
        directory = getattr(self.debug_store, "directory", None)
        if not isinstance(directory, Path) or not directory.exists():
            return None
        try:
            files = [path for path in directory.glob(f"{channel_name}.*") if path.is_file()]
            return max(files, key=lambda path: path.stat().st_mtime_ns, default=None)
        except OSError:
            return None

    def _refresh_previews(self) -> None:
        """Refresh each row's diagnostic thumbnail once per second."""
        active_names = {channel.name for channel in self.config.channels}
        for stale_name in self._preview_images.keys() - active_names:
            self._preview_images.pop(stale_name, None)
            self._preview_files.pop(stale_name, None)

        for channel in self.config.channels:
            if not self.tree.exists(channel.name):
                continue
            if self.tree.set(channel.name, "status") == "Error":
                self.tree.item(channel.name, image=self._broken_preview_image())
                continue
            path = self._latest_preview_file(channel.name)
            if path is None:
                continue
            try:
                stat = path.stat()
                signature = (path, stat.st_mtime_ns, stat.st_size)
                if self._preview_files.get(channel.name) != signature:
                    with Image.open(path) as source:
                        thumbnail = ImageOps.pad(
                            source.convert("RGB"), (50, 50), Image.Resampling.LANCZOS,
                            color=self.COLORS["surface_high"],
                        )
                    self._preview_images[channel.name] = ImageTk.PhotoImage(thumbnail, master=self.master)
                    self._preview_files[channel.name] = signature
                self.tree.item(channel.name, image=self._preview_images[channel.name])
            except (OSError, ValueError):
                # A writer may replace a file between discovery and decoding; retry next tick.
                pass
        self.after(1000, self._refresh_previews)

    def _save(self, notify: bool = True) -> bool:
        previous_config = self.config
        previous_selection = self.tree.selection()
        try:
            self._apply_selected_editor(update_running=False)
            updated = self._resolve_legacy_windows(self._configuration_from_ui())
            updated.validate()
            save_config(self.config_path, updated)
            self._apply_running_configuration(updated)
            self.config = updated
            self._remember_editor()
            if notify:
                self.overall_status.set(f"Saved {self.config_path.name}")
            return True
        except (OSError, RuntimeError, ValueError) as exc:
            if self.config is not previous_config:
                self.config = previous_config
                self._refresh_channels()
                if previous_selection and self.tree.exists(previous_selection[0]):
                    self.tree.selection_set(previous_selection[0])
            messagebox.showerror("Could not save", str(exc))
            return False

    def _apply_selected_editor(self, update_running: bool = True) -> None:
        """Apply edits for the selected row before save/start/capture actions."""
        selected = self.tree.selection()
        if not selected or self._editor_values() == self._editor_baseline:
            return
        channel = self._channel_from_editor()
        original_name = selected[0]
        updated = self._with_channel(channel, original_name)
        if update_running:
            self._apply_running_configuration(updated)
        self.config = updated
        self._refresh_channels()
        if self.tree.exists(channel.name):
            self.tree.selection_set(channel.name)
        if update_running:
            self._remember_editor()

    def _start(self) -> None:
        if not self._save(notify=False):
            return
        try:
            self.runner.start(self.config)
        except (RuntimeError, ValueError) as exc:
            messagebox.showerror("Could not start", str(exc))
            return
        self.overall_status.set(f"Running {len([c for c in self.config.channels if c.enabled])} channel(s)")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def _stop(self) -> None:
        self._stopping = True
        self.overall_status.set("Stopping…")
        self.runner.stop()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")

    def _capture_once(self) -> None:
        try:
            selected = self.tree.selection()
            if not selected:
                raise ValueError("Select a channel first.")
            self._apply_selected_editor()
            selected = self.tree.selection()
            config = self.config
            config.validate()
            channel = next(item for item in config.channels if item.name == selected[0])
        except (RuntimeError, ValueError, StopIteration) as exc:
            messagebox.showerror("Cannot capture", str(exc))
            return

        self._post_status(channel.name, "Capturing", "")

        def work() -> None:
            try:
                self.runner.capture_once(config, replace(channel))
            except Exception:
                # The runner reports capture and upload failures separately.
                pass

        threading.Thread(target=work, name="capture-once", daemon=True).start()

    def _post_status(self, channel: str, status: str, detail: str) -> None:
        if status in ("Error", "Upload error"):
            logging.getLogger(__name__).error(
                "Channel %s %s: %s", channel, status, detail,
                exc_info=sys.exc_info()[0] is not None,
            )
        self.events.put((channel, status, detail))

    def _check_central_connection(self) -> None:
        if not self.connection_check_running:
            central_url = self.central_url.get().strip()
            self._set_connection_status("Checking…")
            self.connection_check_running = True
            threading.Thread(
                target=self._probe_central_connection,
                args=(central_url,),
                name="central-health-check",
                daemon=True,
            ).start()
        self.after(5000, self._check_central_connection)

    def _probe_central_connection(self, central_url: str) -> None:
        try:
            check_central_connection(central_url)
        except CentralConnectionError:
            logging.getLogger(__name__).warning("Central connection check failed", exc_info=True)
            status = "Not contactable"
        else:
            status = "Contactable"
        self.connection_events.put((central_url, status))

    def _check_window_titles(self) -> None:
        channels = [
            (channel.name, identity)
            for channel in self.config.channels
            if (identity := self._channel_window_identity(channel)) is not None
        ]
        if channels and (self.window_check_thread is None or not self.window_check_thread.is_alive()):
            self.window_check_thread = threading.Thread(
                target=self._probe_window_titles, args=(channels,),
                name="window-title-check", daemon=True,
            )
            self.window_check_thread.start()
        self.after(5000, self._check_window_titles)

    def _probe_window_titles(self, channels: list[tuple[str, WindowIdentity]]) -> None:
        results = []
        for name, identity in channels:
            try:
                window = resolve_window(identity)
                results.append((name, identity, window.title or None, None))
            except WindowDiscoveryError as exc:
                results.append((name, identity, None, str(exc)))
        self.window_events.put(results)

    def _drain_events(self) -> None:
        self.send_bandwidth_value.set(f"{self.send_bandwidth.bytes_per_second * 8 / 1_000_000:.2f} Mbps")
        results_changed = False
        try:
            while True:
                channel, status, detail = self.events.get_nowait()
                if self.tree.exists(channel):
                    results_changed = results_changed or self._result_details.get(channel) != detail
                    self.tree.set(channel, "status", status)
                    self._result_details[channel] = detail
                    if status == "Error":
                        self.tree.item(channel, image=self._broken_preview_image())
        except queue.Empty:
            pass
        if results_changed:
            self._wrap_results()
        try:
            while True:
                central_url, status = self.connection_events.get_nowait()
                self.connection_check_running = False
                if central_url == self.central_url.get().strip():
                    self._set_connection_status(status)
        except queue.Empty:
            pass
        try:
            while True:
                results = self.window_events.get_nowait()
                titles = {name: (identity, title, error) for name, identity, title, error in results}
                updated_channels = []
                for channel in self.config.channels:
                    result = titles.get(channel.name)
                    if result is not None and self._channel_window_identity(channel) == result[0]:
                        identity, title, error = result
                        if error is not None:
                            self._post_status(channel.name, "Error", error)
                        elif title is not None:
                            channel = replace(channel, capture_target=title)
                            if self.tree.exists(channel.name):
                                self.tree.set(channel.name, "target", title)
                            if self._editor_window_identity == identity:
                                self._editor_window_title = title
                                self.capture_target.set(self._window_label(title, identity.handle))
                    updated_channels.append(channel)
                self.config = replace(self.config, channels=updated_channels)
        except queue.Empty:
            pass
        if self._stopping and not self.runner.running:
            self._stopping = False
            if self._closing:
                self.master.destroy()
                return
            self.overall_status.set("Stopped")
            self.start_button.configure(state="normal")
        self.after(100, self._drain_events)

    def _close(self) -> None:
        if self.runner.running:
            self._closing = True
            self._stop()
        else:
            self.master.destroy()

