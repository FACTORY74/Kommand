"""Desktop control shell for Kommand Central."""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path

import uvicorn

from kommand_central.bandwidth import ReceiveBandwidth
from kommand_central.display import DisplayWindow
from kommand_central.state import ChannelStore
from kommand_central.layout import LayoutStore, format_panel_label, format_path, parse_path
from kommand_central.web import create_app
from kommand_common.ui import NumberStepper, action_button, app_header, apply_theme, card


class CentralApp(ttk.Frame):
    def __init__(self, master: tk.Tk):
        apply_theme(master)
        super().__init__(master, padding=24, style="App.TFrame")
        self.master = master
        self.store = ChannelStore()
        self.layouts = LayoutStore(Path(__file__).resolve().parents[2] / "layout.json")
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
        self._seen_layout_revision = -1
        self._known_channels: frozenset[str] = frozenset()

        master.title("Kommand Central")
        master.geometry("1040x820")
        master.minsize(900, 700)
        master.protocol("WM_DELETE_WINDOW", self._close)
        self.pack(fill="both", expand=True)
        self._build()
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
        action_button(server_card, "Open display", self._open_display).grid(
            row=1, column=4, padx=(0, 12)
        )
        ttk.Label(server_card, text="Receive bandwidth").grid(row=0, column=5, sticky="w")
        self.receive_bandwidth_value = tk.StringVar(value="0.00 Mbps")
        ttk.Label(
            server_card,
            textvariable=self.receive_bandwidth_value,
            style="Status.TLabel",
        ).grid(row=1, column=5, sticky="w")

        layout_card = card(self, "Display layout")
        layout_card.pack(fill="both", expand=True, pady=(16, 0))
        controls = ttk.Frame(layout_card, style="Card.TFrame")
        controls.pack(fill="x", pady=(0, 10))
        ttk.Label(controls, text="Root rows").pack(side="left")
        NumberStepper(controls, self.layout_rows, minimum=1, maximum=12, width=3, zero_pad=2).pack(side="left", padx=(6, 12))
        ttk.Label(controls, text="Columns").pack(side="left")
        NumberStepper(controls, self.layout_columns, minimum=1, maximum=12, width=3, zero_pad=2).pack(side="left", padx=(6, 12))
        action_button(controls, "Apply root grid", self._resize_layout, "primary").pack(side="left")

        self.layout_tree = ttk.Treeview(
            layout_card, columns=("kind", "assignment"), show="tree headings", height=7
        )
        self.layout_tree.heading("#0", text="Panel")
        self.layout_tree.heading("kind", text="Type")
        self.layout_tree.heading("assignment", text="Assigned source")
        self.layout_tree.column("#0", width=170)
        self.layout_tree.column("kind", width=120)
        self.layout_tree.column("assignment", width=300)
        self.layout_tree.pack(fill="both", expand=True)

        assignment_controls = ttk.Frame(layout_card, style="Card.TFrame", padding=(0, 10, 0, 0))
        assignment_controls.pack(fill="x")
        ttk.Label(assignment_controls, text="Source").pack(side="left")
        self.assignment_input = ttk.Combobox(
            assignment_controls, textvariable=self.assignment, state="readonly", width=34
        )
        self.assignment_input.pack(side="left", padx=(6, 8))
        action_button(assignment_controls, "Assign", self._assign_panel, "primary").pack(side="left")
        action_button(assignment_controls, "Clear", self._clear_assignment).pack(side="left", padx=(8, 18))
        ttk.Label(assignment_controls, text="Subdivide").pack(side="left")
        NumberStepper(assignment_controls, self.nested_rows, minimum=1, maximum=12, width=3, zero_pad=2).pack(side="left", padx=(6, 4))
        ttk.Label(assignment_controls, text="×").pack(side="left")
        NumberStepper(assignment_controls, self.nested_columns, minimum=1, maximum=12, width=3, zero_pad=2).pack(side="left", padx=(4, 8))
        action_button(assignment_controls, "Nest panel", self._nest_panel).pack(side="left")
        action_button(assignment_controls, "Remove nesting", self._flatten_panel, "danger").pack(side="left", padx=8)

        channels_card = card(self, "Live channels")
        channels_card.pack(fill="both", expand=True, pady=(16, 0))
        self.channels_tree = ttk.Treeview(
            channels_card,
            columns=("channel", "size", "revision", "received"),
            show="headings",
        )
        for key, text, width in (
            ("channel", "Channel", 180),
            ("size", "Frame size", 110),
            ("revision", "Revision", 80),
            ("received", "Last received (UTC)", 210),
        ):
            self.channels_tree.heading(key, text=text)
            self.channels_tree.column(key, width=width)
        self.channels_tree.pack(fill="both", expand=True)
        footer = ttk.Frame(self, style="App.TFrame", padding=(0, 16, 0, 0))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status, style="Status.TLabel").pack(side="right")

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
            create_app(self.store, self.layouts, self.receive_bandwidth),
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
        if self.display_window is None or not self.display_window.winfo_exists():
            self.display_window = DisplayWindow(self.master, self.store, self.layouts)
        else:
            self.display_window.lift()
            self.display_window.focus_force()

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
        existing = set(self.channels_tree.get_children())
        current: set[str] = set()
        for frame in frames:
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
        for key in existing - current:
            self.channels_tree.delete(key)
        channel_set = frozenset(current)
        if channel_set != self._known_channels:
            self._known_channels = channel_set
            self.assignment_input.configure(values=[""] + sorted(channel_set))
        if self.layouts.revision != self._seen_layout_revision:
            self._refresh_layout_tree()
        self.after(500, self._refresh)

    def _refresh_layout_tree(self) -> None:
        selected = self.layout_tree.selection()
        selected_id = selected[0] if selected else None
        self.layout_tree.delete(*self.layout_tree.get_children())

        def add(node: dict, path: tuple[int, ...], parent: str) -> None:
            node_id = "root" if not path else format_path(path)
            label = "Root grid" if not path else format_panel_label(path)
            if node["type"] == "grid":
                kind = f"Grid {node['rows']} × {node['columns']}"
                self.layout_tree.insert(parent, "end", iid=node_id, text=label, values=(kind, ""), open=True)
                for index, child in enumerate(node["children"]):
                    add(child, path + (index,), node_id)
            else:
                self.layout_tree.insert(
                    parent, "end", iid=node_id, text=label, values=("Slot", node.get("channel") or "Unassigned")
                )

        add(self.layouts.snapshot(), (), "")
        if selected_id and self.layout_tree.exists(selected_id):
            self.layout_tree.selection_set(selected_id)
        self._seen_layout_revision = self.layouts.revision

    def _selected_layout_path(self, allow_root: bool = False) -> tuple[int, ...]:
        selected = self.layout_tree.selection()
        if not selected:
            raise ValueError("Select a panel first.")
        if selected[0] == "root":
            if allow_root:
                return ()
            raise ValueError("Select a panel below the root grid.")
        return parse_path(selected[0])

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
        self._stop()
        self.master.destroy()
