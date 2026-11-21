"""Kommand Source application entry point."""

from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from kommand_source.config import SourceConfig, load_config
from kommand_source.debug_store import DebugStore
from kommand_source.ui import SourceApp


def main() -> None:
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    application_dir = Path(__file__).resolve().parents[2]
    config_path = application_dir / "config.json"
    debug_store = DebugStore(application_dir / "debug")

    try:
        debug_store.reset()
    except OSError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Debug folder", f"Could not clear the debug folder:\n{exc}")
        root.destroy()

    try:
        config = load_config(config_path)
    except ValueError as exc:
        config = SourceConfig()
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Configuration", f"Starting with defaults:\n{exc}")
        root.destroy()

    root = tk.Tk()
    SourceApp(root, config, config_path, debug_store)
    root.mainloop()


if __name__ == "__main__":
    main()

