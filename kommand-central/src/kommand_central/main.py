"""Kommand Central entry point."""

import logging.config
from pathlib import Path
import tkinter as tk

from kommand_central.ui import CentralApp
from kommand_central.native_video import prepare_native_video


def configure_logging(log_path: Path | None = None) -> None:
    if log_path is None:
        log_path = Path(__file__).resolve().parents[2] / "logs" / "central.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
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
        "loggers": {
            "uvicorn": {"level": "INFO", "handlers": ["file"], "propagate": False},
            "uvicorn.error": {"level": "INFO", "handlers": [], "propagate": True},
            "uvicorn.access": {"level": "INFO", "handlers": ["file"], "propagate": False},
        },
    })


def main() -> None:
    configure_logging()
    prepare_native_video()
    root = tk.Tk()
    CentralApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
