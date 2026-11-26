"""Capture scheduling and worker lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, replace
import random
import threading
import time
from typing import Callable

from kommand_source.capture import MssCaptureProvider
from kommand_source.config import ChannelConfig, SourceConfig
from kommand_source.debug_store import DebugStore
from kommand_source.transport import CentralClient
from kommand_common.bandwidth import RollingBandwidth


StatusCallback = Callable[[str, str, str], None]


@dataclass(slots=True)
class _Worker:
    thread: threading.Thread
    stop: threading.Event
    config: SourceConfig
    channel: ChannelConfig
    failure_reported: bool = False


class SourceRunner:
    """Runs one replaceable worker per enabled channel."""

    def __init__(
        self, debug_store: DebugStore, status_callback: StatusCallback,
        bandwidth: RollingBandwidth | None = None,
    ):
        self.debug_store = debug_store
        self.status_callback = status_callback
        self.bandwidth = bandwidth
        self._workers: dict[str, _Worker] = {}
        self._lock = threading.Lock()
        self._active = False
        self._desired: SourceConfig | None = None
        self._supervisor: threading.Thread | None = None
        self._wake = threading.Event()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._active or bool(self._supervisor and self._supervisor.is_alive())

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @staticmethod
    def _snapshot(config: SourceConfig) -> SourceConfig:
        return replace(config, channels=[replace(channel) for channel in config.channels])

    def start(self, config: SourceConfig) -> None:
        config.validate()
        with self._lock:
            if self._active or (self._supervisor and self._supervisor.is_alive()):
                raise RuntimeError("Capture is already running or stopping.")
            self._desired = self._snapshot(config)
            self._active = True
            self._wake.clear()
            self._supervisor = threading.Thread(
                target=self._supervise, name="capture-supervisor", daemon=True
            )
            self._supervisor.start()

    def reconfigure(self, config: SourceConfig) -> None:
        config.validate()
        with self._lock:
            if not self._active:
                raise RuntimeError("Capture is not running.")
            self._desired = self._snapshot(config)
            self._wake.set()

    def stop(self) -> None:
        with self._lock:
            self._active = False
            for worker in self._workers.values():
                worker.stop.set()
            self._wake.set()

    @staticmethod
    def _matches(worker: _Worker, config: SourceConfig, channel: ChannelConfig) -> bool:
        worker_channel = worker.channel
        if (
            worker_channel.capture_type == channel.capture_type == "window"
            and worker_channel.window_executable is not None
            and (worker_channel.window_executable, worker_channel.window_title_match)
            == (channel.window_executable, channel.window_title_match)
        ):
            worker_channel = replace(
                worker_channel, capture_target=channel.capture_target,
                window_handle=channel.window_handle, window_process_id=channel.window_process_id,
                window_process_start=channel.window_process_start,
            )
        if (
            worker_channel.capture_type == channel.capture_type == "window"
            and worker_channel.window_handle is not None
            and worker_channel.window_handle == channel.window_handle
            and worker_channel.window_process_id == channel.window_process_id
            and worker_channel.window_process_start == channel.window_process_start
        ):
            worker_channel = replace(worker_channel, capture_target=channel.capture_target)
        return (
            worker_channel == channel
            and worker.config.source_name == config.source_name
            and worker.config.central_url == config.central_url
            and worker.config.request_timeout_seconds == config.request_timeout_seconds
        )

    def _supervise(self) -> None:
        while True:
            with self._lock:
                config = self._desired if self._active else None
                desired = {
                    channel.name: channel
                    for channel in config.channels
                    if channel.enabled
                } if config is not None else {}

                for name, worker in list(self._workers.items()):
                    channel = desired.get(name)
                    matches = config is not None and channel is not None and self._matches(
                        worker, config, channel
                    )
                    if not worker.thread.is_alive():
                        if matches and not worker.stop.is_set():
                            if not worker.failure_reported:
                                self.status_callback(name, "Error", "Capture worker exited unexpectedly.")
                                worker.failure_reported = True
                        else:
                            del self._workers[name]
                    elif not matches and not worker.stop.is_set():
                        worker.stop.set()
                        self.status_callback(name, "Stopping", "Applying changes")

                if config is not None:
                    for name, channel in desired.items():
                        if name in self._workers:
                            continue
                        stop = threading.Event()
                        thread = threading.Thread(
                            target=self._run_channel,
                            args=(config, channel, stop),
                            name=f"capture-{name}",
                            daemon=True,
                        )
                        self._workers[name] = _Worker(thread, stop, config, channel)
                        self.status_callback(name, "Starting", "")
                        thread.start()

                if not self._active and not self._workers:
                    return
            self._wake.wait(0.1)
            self._wake.clear()

    def capture_once(self, config: SourceConfig, channel: ChannelConfig) -> None:
        """Capture, store, and upload one frame in the calling thread."""
        try:
            provider = MssCaptureProvider()
            try:
                client = CentralClient(config, self.bandwidth)
            except Exception:
                provider.close()
                raise
        except Exception as exc:
            self.status_callback(channel.name, "Error", str(exc))
            raise
        uploading = False
        try:
            frame = provider.capture(channel)
            self.debug_store.write(channel.name, frame)
            self.status_callback(channel.name, "Captured", self._now())
            uploading = True
            client.upload(channel, frame)
            self.status_callback(channel.name, "Uploaded", self._now())
        except Exception as exc:
            self.status_callback(channel.name, "Upload error" if uploading else "Error", str(exc))
            raise
        finally:
            try:
                provider.close()
            finally:
                client.close()

    def _run_channel(
        self, config: SourceConfig, channel: ChannelConfig, stop: threading.Event
    ) -> None:
        provider = MssCaptureProvider()
        client = CentralClient(config, self.bandwidth)
        failures = 0
        last_debug = float("-inf")
        try:
            while not stop.is_set():
                started = time.monotonic()
                uploading = False
                try:
                    frame = provider.capture(channel)
                    if stop.is_set():
                        break
                    if channel.format != "video" or started - last_debug >= 1:
                        self.debug_store.write(channel.name, frame)
                        last_debug = started
                    self.status_callback(channel.name, "Captured", self._now())
                    if stop.is_set():
                        break
                    uploading = True
                    client.upload(channel, frame)
                    failures = 0
                    self.status_callback(channel.name, "Uploaded", self._now())
                except Exception as exc:
                    failures += 1
                    self.status_callback(channel.name, "Upload error" if uploading else "Error", str(exc))

                if channel.format == "video":
                    capture_period = 1 / channel.video_fps
                elif channel.format == "webp":
                    capture_period = channel.interval_ms / 1000
                else:
                    capture_period = min(channel.interval_ms / 1000, channel.full_frame_interval_seconds)
                normal_delay = max(0.0, capture_period - (time.monotonic() - started))
                if failures:
                    backoff = min(60.0, 2 ** min(failures - 1, 6))
                    delay = max(normal_delay, backoff + random.uniform(0, backoff * 0.2))
                else:
                    delay = normal_delay
                stop.wait(delay)
        finally:
            try:
                provider.close()
            finally:
                client.close()
                self.status_callback(channel.name, "Stopped", self._now())

    @staticmethod
    def _now() -> str:
        return time.strftime("%H:%M:%S")

