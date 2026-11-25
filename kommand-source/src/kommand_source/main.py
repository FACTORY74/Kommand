"""Kommand Source application entry point."""

from __future__ import annotations

import os
import logging.config
from pathlib import Path
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from types import TracebackType

from kommand_source.config import SourceConfig, load_config
from kommand_source.debug_store import DebugStore
from kommand_source.ui import SourceApp


def _log_exception(
    exc_type: type[BaseException], exc_value: BaseException | None,
    traceback: TracebackType | None,
) -> None:
    logging.getLogger(__name__).error(
        "Unhandled exception", exc_info=(exc_type, exc_value, traceback),
    )


def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
    _log_exception(args.exc_type, args.exc_value, args.exc_traceback)


def configure_logging(log_path: Path | None = None) -> None:
    if log_path is None:
        log_path = Path(__file__).resolve().parents[2] / "logs" / "source.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s",
            },
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_path),
                "maxBytes": 5 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
                "formatter": "standard",
            },
        },
        "root": {"level": "INFO", "handlers": ["file"]},
    })
    sys.excepthook = _log_exception
    threading.excepthook = _log_thread_exception


def main() -> None:
    configure_logging()
    logging.getLogger(__name__).info("Kommand Source starting")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    application_dir = Path(__file__).resolve().parents[2]
    config_path = application_dir / "config.json"
    debug_store = DebugStore(application_dir / "debug")

    try:
        debug_store.reset()
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not clear the debug folder", exc_info=True)
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Debug folder", f"Could not clear the debug folder:\n{exc}")
        root.destroy()

    try:
        config = load_config(config_path)
    except ValueError as exc:
        logging.getLogger(__name__).warning("Invalid configuration; starting with defaults", exc_info=True)
        config = SourceConfig()
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Configuration", f"Starting with defaults:\n{exc}")
        root.destroy()

    root = tk.Tk()
    root.report_callback_exception = _log_exception
    SourceApp(root, config, config_path, debug_store)
    root.mainloop()
    logging.getLogger(__name__).info("Kommand Source stopped")


if __name__ == "__main__":
    main()

