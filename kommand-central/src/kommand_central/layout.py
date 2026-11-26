"""Persistent recursive grid layout model."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import math
from pathlib import Path
import tempfile
import threading
import time
from typing import Any


MIN_ZOOM = 1.0
MAX_ZOOM = 16.0
MAX_PAN = 100.0


@dataclass(frozen=True, slots=True)
class Viewport:
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0

    def __post_init__(self) -> None:
        if (
            not all(math.isfinite(value) for value in (self.zoom, self.pan_x, self.pan_y))
            or not MIN_ZOOM <= self.zoom <= MAX_ZOOM
            or abs(self.pan_x) > MAX_PAN
            or abs(self.pan_y) > MAX_PAN
        ):
            raise ValueError("Invalid panel zoom or pan.")


@dataclass(slots=True)
class SlotNode:
    channel: str | None = None
    view: Viewport = field(default_factory=Viewport)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "slot", "channel": self.channel,
            "view": {"zoom": self.view.zoom, "pan_x": self.view.pan_x, "pan_y": self.view.pan_y},
        }


@dataclass(slots=True)
class GridNode:
    rows: int
    columns: int
    children: list[SlotNode | GridNode] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_dimensions(self.rows, self.columns)
        expected = self.rows * self.columns
        if not self.children:
            self.children = [SlotNode() for _ in range(expected)]
        if len(self.children) != expected:
            raise ValueError("Grid child count does not match its dimensions.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "grid",
            "rows": self.rows,
            "columns": self.columns,
            "children": [child.to_dict() for child in self.children],
        }


LayoutNode = SlotNode | GridNode


def _validate_dimensions(rows: int, columns: int) -> None:
    if not 1 <= rows <= 12 or not 1 <= columns <= 12:
        raise ValueError("Rows and columns must be between 1 and 12.")


def _node_from_dict(raw: dict[str, Any]) -> LayoutNode:
    if raw.get("type") == "slot":
        view = raw.get("view", {})
        if not isinstance(view, dict):
            raise ValueError("Invalid panel view.")
        zoom = float(view.get("zoom", 1.0))
        # Older layouts allowed zoom levels from 0.25 up to fit-to-panel.
        if 0.25 <= zoom < MIN_ZOOM:
            zoom = MIN_ZOOM
        return SlotNode(
            channel=raw.get("channel"),
            view=Viewport(
                zoom=zoom,
                pan_x=float(view.get("pan_x", 0.0)),
                pan_y=float(view.get("pan_y", 0.0)),
            ),
        )
    if raw.get("type") == "grid":
        return GridNode(
            rows=int(raw["rows"]),
            columns=int(raw["columns"]),
            children=[_node_from_dict(child) for child in raw.get("children", [])],
        )
    raise ValueError("Unknown layout node type.")


class LayoutStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.RLock()
        self._root = GridNode(2, 2)
        self._revision = 0
        self.window_name = "Kommand Central Display"
        if path and path.exists():
            self._root = self._load(path)
            name = json.loads(path.read_text(encoding="utf-8")).get("window_name")
            if isinstance(name, str) and name.strip():
                self.window_name = name.strip()

    def set_window_name(self, name: str) -> None:
        name = name.strip() or "Kommand Central Display"
        with self._lock:
            if name == self.window_name:
                return
            previous = self.window_name
            self.window_name = name
            try:
                self._save()
            except OSError:
                self.window_name = previous
                raise

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._root.to_dict()

    def display_snapshot(self) -> tuple[dict[str, Any], int, str]:
        with self._lock:
            return self._root.to_dict(), self._revision, self.window_name

    def resize(self, rows: int, columns: int) -> None:
        _validate_dimensions(rows, columns)
        with self._lock:
            previous = self._root
            children = [
                previous.children[row * previous.columns + column]
                if row < previous.rows and column < previous.columns
                else SlotNode()
                for row in range(rows)
                for column in range(columns)
            ]
            self._root = GridNode(rows, columns, children)
            self._revision += 1
            self._save()

    def nest(self, path: tuple[int, ...], rows: int, columns: int) -> None:
        with self._lock:
            parent, index = self._parent(path)
            if isinstance(parent.children[index], GridNode):
                raise ValueError("Only a leaf slot can be subdivided.")
            parent.children[index] = GridNode(rows, columns)
            self._revision += 1
            self._save()

    def flatten(self, path: tuple[int, ...]) -> None:
        with self._lock:
            parent, index = self._parent(path)
            parent.children[index] = SlotNode()
            self._revision += 1
            self._save()

    def assign(self, path: tuple[int, ...], channel: str | None) -> None:
        with self._lock:
            node = self._resolve(path)
            if not isinstance(node, SlotNode):
                raise ValueError("Channels can only be assigned to leaf slots.")
            node.channel = channel or None
            self._revision += 1
            self._save()

    def move_or_swap(
        self, source: tuple[int, ...], destination: tuple[int, ...], expected_revision: int
    ) -> None:
        with self._lock:
            if self._revision != expected_revision:
                raise ValueError("The layout changed during the drag. Please try again.")
            source_node = self._resolve(source)
            destination_node = self._resolve(destination)
            if not isinstance(source_node, SlotNode) or not isinstance(destination_node, SlotNode):
                raise ValueError("Channels can only be moved between leaf slots.")
            if source_node.channel is None:
                raise ValueError("The source panel has no assigned channel.")
            if source == destination:
                return
            source_node.channel, destination_node.channel = destination_node.channel, source_node.channel
            try:
                self._save()
            except OSError:
                source_node.channel, destination_node.channel = destination_node.channel, source_node.channel
                raise
            self._revision += 1

    def set_view(self, path: tuple[int, ...], view: Viewport, expected_revision: int) -> None:
        with self._lock:
            if self._revision != expected_revision:
                raise ValueError("The layout changed while editing the panel. Please try again.")
            node = self._resolve(path)
            if not isinstance(node, SlotNode):
                raise ValueError("Only leaf panels have a view.")
            if node.view == view:
                return
            previous = node.view
            node.view = view
            try:
                self._save()
            except OSError:
                node.view = previous
                raise
            self._revision += 1

    def leaves(self) -> list[tuple[tuple[int, ...], SlotNode]]:
        with self._lock:
            found: list[tuple[tuple[int, ...], SlotNode]] = []

            def walk(node: LayoutNode, path: tuple[int, ...]) -> None:
                if isinstance(node, SlotNode):
                    found.append((path, SlotNode(node.channel, node.view)))
                else:
                    for index, child in enumerate(node.children):
                        walk(child, path + (index,))

            walk(self._root, ())
            return found

    def grids(self) -> list[tuple[tuple[int, ...], GridNode]]:
        with self._lock:
            found: list[tuple[tuple[int, ...], GridNode]] = []

            def walk(node: LayoutNode, path: tuple[int, ...]) -> None:
                if isinstance(node, GridNode):
                    found.append((path, node))
                    for index, child in enumerate(node.children):
                        walk(child, path + (index,))

            walk(self._root, ())
            return found

    def _resolve(self, path: tuple[int, ...]) -> LayoutNode:
        node: LayoutNode = self._root
        for index in path:
            if not isinstance(node, GridNode) or index < 0 or index >= len(node.children):
                raise ValueError("Invalid layout slot path.")
            node = node.children[index]
        return node

    def _parent(self, path: tuple[int, ...]) -> tuple[GridNode, int]:
        if not path:
            raise ValueError("The root grid cannot be replaced as a slot.")
        parent = self._resolve(path[:-1])
        if not isinstance(parent, GridNode):
            raise ValueError("Invalid layout slot path.")
        index = path[-1]
        if index < 0 or index >= len(parent.children):
            raise ValueError("Invalid layout slot path.")
        return parent, index

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._root.to_dict()
        data["window_name"] = self.window_name
        stream = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent,
            prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
        )
        temporary = Path(stream.name)
        try:
            with stream:
                stream.write(json.dumps(data, indent=2) + "\n")
            for attempt in range(6):
                try:
                    temporary.replace(self.path)
                    break
                except PermissionError as exc:
                    # Windows readers/scanners may briefly deny atomic replacement.
                    if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                        raise
                    time.sleep(0.02 * 2 ** attempt)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning(
                    "Could not remove temporary layout file %s", temporary, exc_info=True,
                )

    @staticmethod
    def _load(path: Path) -> GridNode:
        raw = json.loads(path.read_text(encoding="utf-8"))
        node = _node_from_dict(raw)
        if not isinstance(node, GridNode):
            raise ValueError("Layout root must be a grid.")
        return node


def parse_path(value: str) -> tuple[int, ...]:
    if value == "":
        return ()
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise ValueError("Invalid layout slot path.") from exc


def format_path(path: tuple[int, ...]) -> str:
    return ".".join(str(index) for index in path)


def format_panel_label(path: tuple[int, ...]) -> str:
    return f"Panel {'.'.join(str(index + 1) for index in path)}"
