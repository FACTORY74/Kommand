"""Latest-frame diagnostic storage."""

from __future__ import annotations

from pathlib import Path
import logging
import shutil
import tempfile
import time
import threading
from typing import Callable

from kommand_source.capture import CapturedFrame


class DebugStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def reset(self) -> None:
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, channel_name: str, frame: CapturedFrame) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"{channel_name}.{frame.extension}"
        stream = tempfile.NamedTemporaryFile(
            mode="wb", dir=self.directory,
            prefix=f".{channel_name}.{frame.extension}.", suffix=".tmp", delete=False,
        )
        temporary = Path(stream.name)
        try:
            with stream:
                if frame.content:
                    stream.write(frame.content)
                elif frame.image is not None:
                    frame.image.save(stream, format="WEBP")
                else:
                    raise ValueError("A debug frame must contain an image.")
            for attempt in range(6):
                try:
                    temporary.replace(destination)
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                        raise
                    time.sleep(0.02 * 2 ** attempt)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning(
                    "Could not remove temporary debug image %s", temporary, exc_info=True,
                )
        self._remove_other_formats(channel_name, destination)
        return destination

    def _remove_other_formats(self, channel_name: str, keep: Path) -> None:
        for candidate in self.directory.glob(f"{channel_name}.*"):
            if candidate != keep and candidate.is_file():
                candidate.unlink()


class LatestDebugWriter:
    """Keep at most one pending diagnostic image; never delay video uploads."""

    def __init__(
        self, store: DebugStore, channel: str, report_error: Callable[[str], None],
    ) -> None:
        self._store = store
        self._channel = channel
        self._report_error = report_error
        self._changed = threading.Condition()
        self._pending: CapturedFrame | None = None
        self._closing = False
        self._thread = threading.Thread(target=self._run, name=f"diagnostic-{channel}", daemon=True)
        self._thread.start()

    def submit(self, frame: CapturedFrame) -> None:
        with self._changed:
            if self._closing:
                raise RuntimeError("Diagnostic writer has stopped.")
            self._pending = frame
            self._changed.notify()

    def _run(self) -> None:
        while True:
            with self._changed:
                self._changed.wait_for(lambda: self._closing or self._pending is not None)
                if self._closing:
                    return
                frame, self._pending = self._pending, None
            assert frame is not None
            try:
                self._store.write(self._channel, frame)
            except (OSError, ValueError) as exc:
                self._report_error(str(exc))

    def close(self) -> None:
        with self._changed:
            self._closing = True
            self._pending = None
            self._changed.notify()
        self._thread.join()

