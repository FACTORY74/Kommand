from pathlib import Path
from dataclasses import replace
import queue
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "kommand-source" / "src"), str(ROOT / "kommand-common" / "src")]

from kommand_source.config import ChannelConfig, SourceConfig  # noqa: E402
from kommand_source.runner import SourceRunner, _Worker  # noqa: E402


class RunnerTests(unittest.TestCase):
    def test_rebinding_same_saved_match_does_not_restart_worker(self) -> None:
        channel = ChannelConfig(
            name="video", capture_type="window", capture_target="First video",
            window_handle=42, window_process_id=7, window_process_start=99,
            window_executable=r"C:\Apps\browser.exe", window_title_match="YouTube",
        )
        config = SourceConfig(channels=[channel])
        worker = _Worker(Mock(), threading.Event(), config, channel)
        rebound = replace(
            channel, capture_target="New video", window_handle=43, window_process_id=8, window_process_start=100,
        )
        self.assertTrue(SourceRunner._matches(worker, config, rebound))
        self.assertFalse(SourceRunner._matches(worker, config, replace(rebound, window_title_match="Different")))
        self.assertFalse(SourceRunner._matches(worker, config, replace(rebound, quality=90)))

    @patch("kommand_source.runner.time.monotonic", return_value=100.0)
    @patch("kommand_source.runner.CentralClient")
    @patch("kommand_source.runner.MssCaptureProvider")
    def test_full_frame_interval_limits_time_between_captures(
        self, provider, client, monotonic
    ) -> None:
        runner = SourceRunner(Mock(), Mock())
        stop = threading.Event()
        delays: list[float] = []

        def finish_after_wait(delay: float) -> None:
            delays.append(delay)
            stop.set()

        channel = ChannelConfig(name="main", interval_ms=120_000, full_frame_interval_seconds=60)
        with patch.object(stop, "wait", side_effect=finish_after_wait):
            runner._run_channel(SourceConfig(), channel, stop)
        self.assertEqual(delays, [60])

    @patch("kommand_source.runner.time.monotonic", return_value=100.0)
    @patch("kommand_source.runner.CentralClient")
    @patch("kommand_source.runner.MssCaptureProvider")
    def test_format_selects_the_capture_period(self, provider, client, monotonic) -> None:
        for format_name, expected_delay in (("webp", 120), ("video", 1 / 20)):
            with self.subTest(format=format_name):
                stop = threading.Event()
                delays = []
                def finish(delay):
                    delays.append(delay)
                    stop.set()
                channel = ChannelConfig(
                    name="main", format=format_name, video_fps=20,
                    interval_ms=120_000, full_frame_interval_seconds=60,
                )
                with patch.object(stop, "wait", side_effect=finish):
                    SourceRunner(Mock(), Mock())._run_channel(SourceConfig(), channel, stop)
                self.assertEqual(delays, [expected_delay])

    def test_running_edit_waits_for_old_worker_and_keeps_other_channels_running(self) -> None:
        runner = SourceRunner(Mock(), Mock())
        old_started = threading.Event()
        old_stopping = threading.Event()
        release_old = threading.Event()
        replacement_started = threading.Event()
        other_started = threading.Event()
        starts: list[tuple[str, int]] = []

        def work(config, channel, stop) -> None:
            starts.append((channel.name, channel.quality))
            if channel.name == "main" and channel.quality == 80:
                old_started.set()
                if stop.wait(3):
                    old_stopping.set()
                release_old.wait(3)
            else:
                if channel.name == "main":
                    replacement_started.set()
                else:
                    other_started.set()
                stop.wait(3)

        def cleanup() -> None:
            release_old.set()
            runner.stop()
            if runner._supervisor:
                runner._supervisor.join(3)

        self.addCleanup(cleanup)
        config = SourceConfig(channels=[ChannelConfig(name="main"), ChannelConfig(name="other")])
        with patch.object(runner, "_run_channel", side_effect=work):
            runner.start(config)
            self.assertTrue(old_started.wait(2))
            self.assertTrue(other_started.wait(2))
            changed = replace(config, channels=[replace(config.channels[0], quality=90), config.channels[1]])
            runner.reconfigure(changed)
            changed.channels[0].quality = 99
            self.assertTrue(old_stopping.wait(2))
            self.assertFalse(replacement_started.wait(0.2))
            release_old.set()
            self.assertTrue(replacement_started.wait(2))
            self.assertEqual(starts.count(("other", 80)), 1)
            self.assertIn(("main", 90), starts)
            runner.stop()
            if runner._supervisor:
                runner._supervisor.join(3)
            self.assertFalse(runner.running)

    def test_stop_keeps_busy_worker_registered_until_it_exits(self) -> None:
        runner = SourceRunner(Mock(), Mock())
        started = threading.Event()
        release = threading.Event()

        def work(config, channel, stop) -> None:
            started.set()
            release.wait(3)

        def cleanup() -> None:
            release.set()
            runner.stop()
            if runner._supervisor:
                runner._supervisor.join(3)

        self.addCleanup(cleanup)
        config = SourceConfig(channels=[ChannelConfig(name="main")])
        with patch.object(runner, "_run_channel", side_effect=work):
            runner.start(config)
            self.assertTrue(started.wait(2))
            runner.stop()
            self.assertTrue(runner.running)
            with self.assertRaises(RuntimeError):
                runner.start(config)
            release.set()
            if runner._supervisor:
                runner._supervisor.join(3)
            self.assertFalse(runner.running)

    def test_stop_during_capture_does_not_publish_stale_frame(self) -> None:
        debug = Mock()
        runner = SourceRunner(debug, Mock())
        entered = threading.Event()
        release = threading.Event()
        stop = threading.Event()
        provider = Mock()
        client = Mock()

        def capture(channel):
            entered.set()
            release.wait(3)
            return Mock()

        provider.capture.side_effect = capture
        with patch("kommand_source.runner.MssCaptureProvider", return_value=provider):
            with patch("kommand_source.runner.CentralClient", return_value=client):
                thread = threading.Thread(
                    target=runner._run_channel,
                    args=(SourceConfig(), ChannelConfig(name="main"), stop),
                )
                thread.start()
                try:
                    self.assertTrue(entered.wait(2))
                    stop.set()
                finally:
                    release.set()
                    thread.join(3)
                self.assertFalse(thread.is_alive())
        debug.write.assert_not_called()
        client.upload.assert_not_called()
        provider.close.assert_called_once()
        client.close.assert_called_once()

    def test_capture_once_closes_provider_after_upload_failure(self) -> None:
        runner = SourceRunner(Mock(), Mock())
        provider = Mock()
        client = Mock()
        client.upload.side_effect = RuntimeError("upload failed")
        with patch("kommand_source.runner.MssCaptureProvider", return_value=provider):
            with patch("kommand_source.runner.CentralClient", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "upload failed"):
                    runner.capture_once(SourceConfig(), ChannelConfig(name="main"))
        provider.close.assert_called_once()
        client.close.assert_called_once()
        runner.status_callback.assert_called_with("main", "Upload error", "upload failed")

    def test_rapid_edits_start_only_latest_configuration(self) -> None:
        runner = SourceRunner(Mock(), Mock())
        entered = threading.Event()
        release = threading.Event()
        replacement = threading.Event()
        qualities: list[int] = []

        def work(config, channel, stop) -> None:
            qualities.append(channel.quality)
            if channel.quality == 80:
                entered.set()
                release.wait(3)
            else:
                replacement.set()
                stop.wait(3)

        def cleanup() -> None:
            release.set()
            runner.stop()
            if runner._supervisor:
                runner._supervisor.join(3)

        self.addCleanup(cleanup)
        config = SourceConfig(channels=[ChannelConfig(name="main")])
        with patch.object(runner, "_run_channel", side_effect=work):
            runner.start(config)
            self.assertTrue(entered.wait(2))
            runner.reconfigure(replace(config, channels=[replace(config.channels[0], quality=90)]))
            runner.reconfigure(replace(config, channels=[replace(config.channels[0], quality=95)]))
            release.set()
            self.assertTrue(replacement.wait(2))
            self.assertEqual(qualities, [80, 95])
            runner.stop()
            if runner._supervisor:
                runner._supervisor.join(3)

    def test_running_add_disable_rename_and_connection_change(self) -> None:
        runner = SourceRunner(Mock(), Mock())
        events: queue.Queue[tuple[str, str, str]] = queue.Queue()

        def work(config, channel, stop) -> None:
            events.put(("started", channel.name, config.source_name))
            stop.wait(3)
            events.put(("stopped", channel.name, config.source_name))

        def cleanup() -> None:
            runner.stop()
            if runner._supervisor:
                runner._supervisor.join(3)

        self.addCleanup(cleanup)
        main = ChannelConfig(name="main")
        other = ChannelConfig(name="other")
        config = SourceConfig(channels=[main])
        with patch.object(runner, "_run_channel", side_effect=work):
            runner.start(config)
            self.assertEqual(events.get(timeout=2), ("started", "main", config.source_name))
            runner.reconfigure(replace(config, channels=[main, other]))
            self.assertEqual(events.get(timeout=2), ("started", "other", config.source_name))
            runner.reconfigure(replace(config, channels=[replace(main, enabled=False), other]))
            self.assertEqual(events.get(timeout=2), ("stopped", "main", config.source_name))
            runner.reconfigure(replace(config, source_name="new-source", channels=[replace(other, name="renamed")]))
            results = {events.get(timeout=2), events.get(timeout=2)}
            self.assertEqual(results, {
                ("stopped", "other", config.source_name),
                ("started", "renamed", "new-source"),
            })
            runner.stop()
            if runner._supervisor:
                runner._supervisor.join(3)
            self.assertFalse(runner.running)

    def test_title_only_changes_do_not_restart_a_bound_window_worker(self) -> None:
        runner = SourceRunner(Mock(), Mock())
        started = threading.Event()
        stopped = threading.Event()
        starts: list[str] = []

        def work(config, channel, stop) -> None:
            starts.append(channel.capture_target)
            started.set()
            stop.wait(3)
            stopped.set()

        def cleanup() -> None:
            runner.stop()
            if runner._supervisor:
                runner._supervisor.join(3)

        self.addCleanup(cleanup)
        channel = ChannelConfig(
            name="video", capture_type="window", capture_target="First",
            window_handle=42, window_process_id=7, window_process_start=99,
        )
        config = SourceConfig(channels=[channel])
        with patch.object(runner, "_run_channel", side_effect=work):
            runner.start(config)
            self.assertTrue(started.wait(2))
            runner.reconfigure(replace(config, channels=[replace(channel, capture_target="Next")]))
            self.assertFalse(stopped.wait(0.3))
            self.assertEqual(starts, ["First"])
            runner.stop()
            if runner._supervisor:
                runner._supervisor.join(3)


if __name__ == "__main__":
    unittest.main()
