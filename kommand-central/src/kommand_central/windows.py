"""Persistent collection of independent display layouts."""

import json
from pathlib import Path
from uuid import uuid4

from kommand_central.layout import LayoutStore


class DisplayLayouts:
    def __init__(self, first: LayoutStore):
        self.path = first.path.parent / "windows.json" if first.path else None
        self.layouts: dict[str, LayoutStore] = {"default": first}
        if self.path and self.path.exists():
            ids = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(ids, list) or any(
                not isinstance(key, str) or (key != "default" and (len(key) != 32 or any(c not in "0123456789abcdef" for c in key)))
                for key in ids
            ):
                raise ValueError("Invalid display window list")
            self.layouts = {
                key: first if key == "default" else LayoutStore(self.path.parent / f"layout-{key}.json")
                for key in ids
            }

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(list(self.layouts), indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)

    def add(self) -> str:
        key = uuid4().hex
        layout = LayoutStore(self.path.parent / f"layout-{key}.json" if self.path else None)
        names = {item.window_name for item in self.layouts.values()}
        number = 1
        while f"Display {number}" in names:
            number += 1
        layout.set_window_name(f"Display {number}")
        self.layouts[key] = layout
        try:
            self._save()
        except OSError:
            del self.layouts[key]
            raise
        return key

    def remove(self, key: str) -> None:
        previous = self.layouts.copy()
        del self.layouts[key]
        try:
            self._save()
        except OSError:
            self.layouts = previous
            raise
        # Retain the layout file so a closed definition can be recovered.
