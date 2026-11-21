"""Persistent configuration for Kommand Source."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any


CHANNEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(slots=True)
class ChannelConfig:
    name: str
    capture_type: str = "screen"
    capture_target: str = "1"
    interval_ms: int = 5000
    full_frame_interval_seconds: int = 60
    quality: int = 80
    enabled: bool = True
    window_handle: int | None = None
    window_process_id: int | None = None
    window_process_start: int | None = None
    format: str = "webp_patch"
    video_fps: int = 15

    def validate(self) -> None:
        if not CHANNEL_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "Channel name must be 1-64 letters, numbers, dots, underscores, or hyphens."
            )
        if self.capture_type not in {"screen", "region", "window"}:
            raise ValueError("Capture type must be 'screen', 'region', or 'window'.")
        if not 250 <= self.interval_ms <= 86_400_000:
            raise ValueError("Capture interval must be between 250 ms and 24 hours.")
        if not 1 <= self.full_frame_interval_seconds <= 86_400:
            raise ValueError("Full frame interval must be between 1 second and 24 hours.")
        if not 1 <= self.quality <= 100:
            raise ValueError("Image quality must be between 1 and 100.")
        if self.format not in {"webp", "webp_patch", "video"}:
            raise ValueError("Format must be WEBP, WEBP + PATCH, or VIDEO.")
        if not isinstance(self.video_fps, int) or isinstance(self.video_fps, bool) or not 1 <= self.video_fps <= 60:
            raise ValueError("Video FPS must be between 1 and 60.")
        identity = (self.window_handle, self.window_process_id, self.window_process_start)
        if any(value is not None for value in identity):
            if self.capture_type != "window" or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in identity
            ):
                raise ValueError("Window identity must contain a valid handle and process identity.")


@dataclass(slots=True)
class SourceConfig:
    source_name: str = "source-1"
    central_url: str = "http://127.0.0.1:8000"
    request_timeout_seconds: float = 10.0
    channels: list[ChannelConfig] = field(default_factory=list)

    def validate(self) -> None:
        if not CHANNEL_NAME_PATTERN.fullmatch(self.source_name):
            raise ValueError(
                "Source name must be 1-64 letters, numbers, dots, underscores, or hyphens."
            )
        if not self.central_url.startswith(("http://", "https://")):
            raise ValueError("Central URL must begin with http:// or https://.")
        if self.request_timeout_seconds <= 0:
            raise ValueError("Request timeout must be positive.")
        names: set[str] = set()
        for channel in self.channels:
            channel.validate()
            if channel.name in names:
                raise ValueError(f"Duplicate channel name: {channel.name}")
            names.add(channel.name)


def load_config(path: Path) -> SourceConfig:
    if not path.exists():
        return SourceConfig()
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("auth_token", None)  # Migrate configurations from the removed token UI.
        channel_items = raw.pop("channels", [])
        # Migrate configurations written before channel/display names were unified.
        for item in channel_items:
            item.pop("display_name", None)
            item.pop("image_format", None)
        channels = [ChannelConfig(**item) for item in channel_items]
        config = SourceConfig(channels=channels, **raw)
        config.validate()
        return config
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"Could not load {path}: {exc}") from exc


def save_config(path: Path, config: SourceConfig) -> None:
    config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)

