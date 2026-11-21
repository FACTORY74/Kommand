"""Latest-frame diagnostic storage."""

from __future__ import annotations

from pathlib import Path
import shutil

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
        temporary = self.directory / f".{channel_name}.{frame.extension}.tmp"
        if frame.content:
            temporary.write_bytes(frame.content)
        elif frame.image is not None:
            frame.image.save(temporary, format="WEBP")
        else:
            raise ValueError("A debug frame must contain an image.")
        temporary.replace(destination)
        self._remove_other_formats(channel_name, destination)
        return destination

    def _remove_other_formats(self, channel_name: str, keep: Path) -> None:
        for candidate in self.directory.glob(f"{channel_name}.*"):
            if candidate != keep and candidate.is_file():
                candidate.unlink()

