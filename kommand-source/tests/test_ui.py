from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "kommand-source" / "src"),
    str(ROOT / "kommand-common" / "src"),
]

from kommand_source.config import ChannelConfig, SourceConfig  # noqa: E402
from kommand_source.capture.windows import WindowDiscoveryError, WindowIdentity, WindowInfo  # noqa: E402
from kommand_source.ui.app import SourceApp  # noqa: E402


class SourceUiTests(unittest.TestCase):
    def test_format_and_video_fps_can_be_edited(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        app = SourceApp(root, SourceConfig(), Path("unused.json"), Mock())
        self.assertEqual(app.capture_format.get(), "WEBP + PATCH")
        self.assertEqual(app.format_input.cget("values"), ("WEBP", "WEBP + PATCH", "VIDEO"))
        self.assertEqual(int(app.format_input.grid_info()["column"]), int(app.capture_type_input.grid_info()["column"]) + 1)
        app.name.set("main")
        app.capture_format.set("VIDEO")
        app.video_fps.set("24")
        app._upsert()
        self.assertEqual((app.config.channels[0].format, app.config.channels[0].video_fps), ("video", 24))
        app.tree.selection_set("main")
        app._select_channel()
        self.assertEqual((app.capture_format.get(), app.video_fps.get()), ("VIDEO", "24"))

    def test_send_bandwidth_is_shown_in_connection_panel(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        app = SourceApp(root, SourceConfig(), Path("unused.json"), Mock())
        app.send_bandwidth.record(625_000)
        app._drain_events()
        self.assertEqual(app.send_bandwidth_value.get(), "1.00 Mbps")

    def test_window_title_poll_updates_channel_without_reconfiguring_capture(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        identity = WindowIdentity(42, 7, 99)
        first = WindowInfo(42, "First video", 0, 0, 800, 600)
        second = WindowInfo(42, "Next video", 0, 0, 800, 600)
        channel = ChannelConfig(
            name="video", capture_type="window", capture_target=first.title,
            window_handle=42, window_process_id=7, window_process_start=99,
        )
        with patch("kommand_source.ui.app.list_visible_windows", return_value=[first]) as windows:
            with patch("kommand_source.ui.app.get_window_identity", return_value=identity):
                with patch("kommand_source.ui.app.resolve_window", return_value=second):
                    app = SourceApp(root, SourceConfig(channels=[channel]), Path("unused.json"), Mock())
                    app.runner = Mock()
                    app.runner.active = True
                    app.tree.selection_set("video")
                    app._select_channel()
                    app.quality.set("90")
                    windows.return_value = [second]
                    app._probe_window_titles([("video", identity)])
                    app._drain_events()
                    self.assertEqual(app.config.channels[0].capture_target, "Next video")
                    self.assertEqual(app.tree.set("video", "target"), "Next video")
                    self.assertEqual(app.capture_target.get(), "Next video [0x2A]")
                    self.assertEqual(app.quality.get(), "90")
                    self.assertEqual(app._channel_from_editor().window_handle, 42)
                    app.runner.reconfigure.assert_not_called()

    def test_reused_handle_does_not_silently_change_selection(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        identity = WindowIdentity(42, 7, 99)
        other = WindowIdentity(42, 7, 100)
        window = WindowInfo(42, "Different window", 0, 0, 800, 600)
        channel = ChannelConfig(
            name="video", capture_type="window", capture_target="Old video",
            window_handle=42, window_process_id=7, window_process_start=99,
        )
        with patch("kommand_source.ui.app.list_visible_windows", return_value=[window]):
            with patch("kommand_source.ui.app.get_window_identity", return_value=other):
                app = SourceApp(root, SourceConfig(channels=[channel]), Path("unused.json"), Mock())
                app.tree.selection_set("video")
                app._select_channel()
                self.assertEqual(app._channel_from_editor().window_process_start, 99)
                with patch("kommand_source.ui.app.resolve_window", side_effect=WindowDiscoveryError("select it again")):
                    app._probe_window_titles([("video", identity)])
                app._drain_events()
                app._drain_events()
                self.assertEqual(app.tree.set("video", "status"), "Error")
                self.assertEqual(app.config.channels[0].capture_target, "Old video")
                app.capture_target.set("Different window [0x2A]")
                app._window_selected()
                app._upsert()
                self.assertEqual(app.config.channels[0].window_process_start, 100)

    def test_title_poll_retries_after_unexpected_worker_failure(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        channel = ChannelConfig(
            name="video", capture_type="window", capture_target="Browser",
            window_handle=42, window_process_id=7, window_process_start=99,
        )
        app = SourceApp(root, SourceConfig(channels=[channel]), Path("unused.json"), Mock())
        with patch("kommand_source.ui.app.list_visible_windows", side_effect=RuntimeError("probe failed")) as probe:
            with patch("threading.excepthook") as errors:
                app._check_window_titles()
                first_thread = app.window_check_thread
                assert first_thread is not None
                first_thread.join(timeout=5)
                self.assertFalse(first_thread.is_alive())
                app._check_window_titles()
                second_thread = app.window_check_thread
                assert second_thread is not None
                second_thread.join(timeout=5)
                self.assertFalse(second_thread.is_alive())
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(errors.call_count, 2)

    def test_legacy_window_configuration_is_bound_on_save(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        window = WindowInfo(42, "Browser", 0, 0, 800, 600)
        with tempfile.TemporaryDirectory() as directory:
            config = SourceConfig(channels=[
                ChannelConfig(name="video", capture_type="window", capture_target="Browser"),
            ])
            app = SourceApp(root, config, Path(directory) / "config.json", Mock())
            with patch("kommand_source.ui.app.find_window", return_value=window):
                with patch("kommand_source.ui.app.get_window_identity", return_value=WindowIdentity(42, 7, 99)):
                    self.assertTrue(app._save())
            self.assertEqual(app.config.channels[0].window_handle, 42)
            self.assertEqual(app.config.channels[0].window_process_start, 99)

    def test_selects_between_windows_with_identical_titles(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        windows = [
            WindowInfo(42, "Browser", 0, 0, 800, 600),
            WindowInfo(43, "Browser", 0, 0, 800, 600),
        ]
        with patch("kommand_source.ui.app.list_visible_windows", return_value=windows):
            with patch("kommand_source.ui.app.get_window_identity", return_value=WindowIdentity(43, 8, 100)):
                app = SourceApp(root, SourceConfig(), Path("unused.json"), Mock())
                app.name.set("video")
                app.capture_type.set("window")
                app._capture_type_changed()
                self.assertEqual(app.target_input.cget("values"), ("Browser [0x2A]", "Browser [0x2B]"))
                app.capture_target.set("Browser [0x2B]")
                app._window_selected()
                app._upsert()
                self.assertEqual(app.config.channels[0].capture_target, "Browser")
                self.assertEqual(app.config.channels[0].window_handle, 43)

    def test_capture_type_choices_keep_existing_region_channels(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        region = ChannelConfig(name="existing", capture_type="region", capture_target="0,0,100,100")
        app = SourceApp(root, SourceConfig(channels=[region]), Path("unused.json"), Mock())

        self.assertEqual(app.capture_type_input.cget("values"), ("screen", "window"))
        app.tree.selection_set("existing")
        app._select_channel()
        self.assertEqual(app._channel_from_editor(), region)

    def test_running_channel_edits_and_connection_save_are_applied(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        with tempfile.TemporaryDirectory() as directory:
            config = SourceConfig(channels=[ChannelConfig(name="main")])
            app = SourceApp(root, config, Path(directory) / "config.json", Mock())
            app.runner = Mock()
            app.runner.active = True
            app.tree.selection_set("main")
            app._select_channel()
            app.quality.set("90")
            app._upsert()
            self.assertEqual(app.runner.reconfigure.call_args.args[0].channels[0].quality, 90)
            app.central_url.set("http://127.0.0.1:8001")
            self.assertTrue(app._save())
            self.assertEqual(app.runner.reconfigure.call_args.args[0].central_url, "http://127.0.0.1:8001")
            app._remove()
            self.assertEqual(app.runner.reconfigure.call_args.args[0].channels, [])

    def test_running_rename_cannot_replace_another_channel(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        config = SourceConfig(channels=[ChannelConfig(name="main"), ChannelConfig(name="other")])
        app = SourceApp(root, config, Path("config.json"), Mock())
        app.runner = Mock()
        app.runner.active = True
        app.tree.selection_set("main")
        app._select_channel()
        app.name.set("other")
        with patch("kommand_source.ui.app.messagebox.showerror") as error:
            app._upsert()
        error.assert_called_once()
        app.runner.reconfigure.assert_not_called()
        self.assertEqual([channel.name for channel in app.config.channels], ["main", "other"])

    def test_failed_save_does_not_apply_editor_changes(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        config = SourceConfig(channels=[ChannelConfig(name="main")])
        app = SourceApp(root, config, Path("config.json"), Mock())
        app.runner = Mock()
        app.runner.active = True
        app.tree.selection_set("main")
        app._select_channel()
        app.quality.set("90")
        app.central_url.set("invalid")
        with patch("kommand_source.ui.app.messagebox.showerror") as error:
            self.assertFalse(app._save())
        error.assert_called_once()
        app.runner.reconfigure.assert_not_called()
        self.assertEqual(app.config.channels[0].quality, 80)
        self.assertEqual(app.tree.selection(), ("main",))

    def test_stopping_is_not_reported_as_stopped_until_runner_exits(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        app = SourceApp(root, SourceConfig(), Path("config.json"), Mock())
        app.runner = Mock()
        app.runner.running = True
        app._stop()
        app._drain_events()
        self.assertEqual(app.overall_status.get(), "Stopping…")
        self.assertEqual(str(app.start_button["state"]), "disabled")
        app.runner.running = False
        app._drain_events()
        self.assertEqual(app.overall_status.get(), "Stopped")
        self.assertEqual(str(app.start_button["state"]), "normal")


if __name__ == "__main__":
    unittest.main()
