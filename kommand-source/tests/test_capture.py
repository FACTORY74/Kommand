import gc
from pathlib import Path
import platform
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from kommand_source.capture.mss_provider import MssCaptureProvider  # noqa: E402
from kommand_source.capture.wgc_provider import (  # noqa: E402
    capture_rendered_window, WindowCaptureResized, WindowsGraphicsCaptureError,
)
from kommand_source.config import ChannelConfig  # noqa: E402
from kommand_source.capture.windows import (  # noqa: E402
    WindowIdentity, WindowInfo, WindowDiscoveryError, resolve_window,
    find_matching_window, resolve_matching_window,
)


class WindowCaptureTests(unittest.TestCase):
    def test_persistent_match_requires_unique_executable_and_title_fragment(self) -> None:
        windows = [
            WindowInfo(1, "Video - YouTube", 0, 0, 64, 48),
            WindowInfo(2, "Video - YouTube", 0, 0, 64, 48),
        ]
        with patch("kommand_source.capture.windows.list_visible_windows", return_value=windows):
            with patch("kommand_source.capture.windows.get_window_executable", side_effect=[r"C:\Apps\Browser.exe", r"C:\Apps\Other.exe"]):
                self.assertEqual(find_matching_window(r"c:\apps\browser.exe", "youtube"), windows[0])
            with patch("kommand_source.capture.windows.get_window_executable", return_value=r"C:\Apps\Browser.exe"):
                with self.assertRaisesRegex(WindowDiscoveryError, "Multiple"):
                    find_matching_window(r"C:\Apps\Browser.exe", "YouTube")
            with self.assertRaisesRegex(WindowDiscoveryError, "Waiting"):
                find_matching_window(r"C:\Apps\Browser.exe", "Missing")

    def test_persistent_binding_reconnects_after_reboot_and_retains_live_identity(self) -> None:
        old = WindowIdentity(42, 7, 99)
        new = WindowIdentity(43, 8, 100)
        window = WindowInfo(43, "New video - YouTube", 0, 0, 64, 48)
        with patch("kommand_source.capture.windows.get_window_identity", side_effect=[WindowDiscoveryError("gone"), new, new]):
            with patch("kommand_source.capture.windows.find_matching_window", return_value=window):
                with patch("kommand_source.capture.windows.resolve_window", return_value=window):
                    with patch("kommand_source.capture.windows.get_window_executable", return_value="browser.exe"):
                        self.assertEqual(resolve_matching_window("browser.exe", "YouTube", old), (window, new))
        with patch("kommand_source.capture.windows.get_window_identity", return_value=new):
            with patch("kommand_source.capture.windows.find_matching_window") as find:
                with patch("kommand_source.capture.windows.resolve_window", return_value=window), patch(
                    "kommand_source.capture.windows.get_window_executable", return_value="browser.exe",
                ):
                    self.assertEqual(resolve_matching_window("browser.exe", "YouTube", new), (window, new))
                find.assert_not_called()

    def test_saved_match_reconnects_capture_and_changes_frame_alignment(self) -> None:
        old, new = WindowIdentity(42, 7, 99), WindowIdentity(43, 8, 100)
        window = WindowInfo(43, "Video", 0, 0, 64, 48)
        first, second = Mock(identity=old), Mock(identity=new)
        first.capture.side_effect = [
            (Image.new("RGB", (64, 48)), WindowInfo(42, "Video", 0, 0, 64, 48)),
            WindowsGraphicsCaptureError("closed"),
        ]
        second.capture.return_value = (Image.new("RGB", (64, 48)), window)
        channel = ChannelConfig(
            name="video", capture_type="window", window_executable="browser.exe", window_title_match="Video",
        )
        with patch("kommand_source.capture.mss_provider.resolve_matching_window", side_effect=[(window, old), (window, new)]):
            with patch("kommand_source.capture.mss_provider.WindowsCaptureSession", side_effect=[first, second]):
                provider = MssCaptureProvider()
                previous = provider.capture(channel)
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    provider.capture(channel)
                current = provider.capture(channel)
                provider.close()
        self.assertNotEqual(previous.alignment, current.alignment)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_minimized_live_window_is_not_replaced_by_another_match(self) -> None:
        identity = WindowIdentity(42, 7, 99)
        with patch("kommand_source.capture.windows.get_window_identity", return_value=identity):
            with patch("kommand_source.capture.windows.get_window_executable", return_value="browser.exe"):
                with patch("kommand_source.capture.windows.resolve_window", side_effect=WindowDiscoveryError("minimized")):
                    with patch("kommand_source.capture.windows.find_matching_window") as find:
                        with self.assertRaisesRegex(WindowDiscoveryError, "minimized"):
                            resolve_matching_window("browser.exe", "Video", identity)
                        find.assert_not_called()

    @patch("kommand_source.capture.mss_provider.WindowsCaptureSession")
    def test_window_capture_reuses_rendered_window_session(self, session_type) -> None:
        identity = WindowIdentity(42, 7, 99)
        session = session_type.return_value
        session.identity = identity
        session.capture.return_value = (
            Image.new("RGB", (640, 480), "navy"),
            WindowInfo(42, "Example", 10, 20, 640, 480),
        )
        channel = ChannelConfig(
            name="example", capture_type="window", capture_target="Example",
            window_handle=42, window_process_id=7, window_process_start=99,
        )

        provider = MssCaptureProvider()
        frame = provider.capture(channel)
        provider.capture(channel)
        provider.close()

        session_type.assert_called_once_with(identity, fps=15)
        self.assertEqual(session.capture.call_count, 2)
        session.close.assert_called_once()
        self.assertEqual((frame.width, frame.height), (640, 480))
        self.assertEqual(frame.alignment, ("window", 42, 7, 99, 640, 480))
        self.assertEqual(frame.content, b"")
        self.assertEqual(frame.image.size, (640, 480))

    def test_legacy_window_binding_survives_title_change(self) -> None:
        identity = WindowIdentity(42, 7, 99)
        first = WindowInfo(42, "First video", 10, 20, 64, 48)
        second = WindowInfo(42, "Next video", 10, 20, 64, 48)
        channel = ChannelConfig(name="video", capture_type="window", capture_target="First video")
        with patch("kommand_source.capture.mss_provider.find_window", return_value=first) as find:
            with patch("kommand_source.capture.mss_provider.get_window_identity", return_value=identity):
                with patch("kommand_source.capture.mss_provider.WindowsCaptureSession") as session_type:
                    session = session_type.return_value
                    session.identity = identity
                    session.capture.side_effect = [
                        (Image.new("RGB", (64, 48)), first),
                        (Image.new("RGB", (64, 48)), second),
                    ]
                    provider = MssCaptureProvider()
                    first_frame = provider.capture(channel)
                    second_frame = provider.capture(channel)
                    provider.close()
        find.assert_called_once_with("First video")
        session_type.assert_called_once_with(identity, fps=15)
        self.assertEqual(first_frame.alignment, second_frame.alignment)

    def test_window_resize_restarts_the_capture_session(self) -> None:
        identity = WindowIdentity(42, 7, 99)
        first = Mock(identity=identity)
        second = Mock(identity=identity)
        first.capture.side_effect = [
            (Image.new("RGB", (64, 48)), WindowInfo(42, "Browser", 0, 0, 64, 48)),
            WindowCaptureResized("resized"),
        ]
        second.capture.return_value = (
            Image.new("RGB", (80, 60)), WindowInfo(42, "Browser", 0, 0, 80, 60)
        )
        channel = ChannelConfig(
            name="video", capture_type="window", capture_target="Browser",
            window_handle=42, window_process_id=7, window_process_start=99,
        )
        with patch("kommand_source.capture.mss_provider.WindowsCaptureSession", side_effect=[first, second]):
            provider = MssCaptureProvider()
            old_frame = provider.capture(channel)
            new_frame = provider.capture(channel)
            provider.close()
        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertNotEqual(old_frame.alignment, new_frame.alignment)

    def test_reused_handle_is_rejected(self) -> None:
        original = WindowIdentity(42, 7, 99)
        with patch(
            "kommand_source.capture.windows.get_window_identity",
            return_value=WindowIdentity(42, 7, 100),
        ):
            with self.assertRaisesRegex(WindowDiscoveryError, "select it again"):
                resolve_window(original)

    def test_native_capture_cycle_is_released_on_success_and_failure(self) -> None:
        identity = WindowIdentity(42, 7, 99)
        window = WindowInfo(42, "Browser", 0, 0, 4, 3)
        for outcome in ("frame", "timeout", "start_error"):
            with self.subTest(outcome=outcome):
                references = []
                control = Mock()
                control.is_finished.return_value = False

                class FakeCapture:
                    def __init__(self, **kwargs):
                        self.capture = SimpleNamespace(on_frame=self.on_frame, on_closed=self.on_closed)
                        self.handlers = {}
                        references.append(weakref.ref(self))

                    def on_frame(self):
                        pass

                    def on_closed(self):
                        pass

                    def event(self, handler):
                        self.handlers[handler.__name__] = handler
                        return handler

                    def start_free_threaded(self):
                        if outcome == "start_error":
                            raise RuntimeError("start failed")
                        if outcome == "frame":
                            self.handlers["on_frame_arrived"](
                                SimpleNamespace(frame_buffer=MagicMock(), width=4, height=3), control
                            )
                        return control

                backend = SimpleNamespace(
                    Frame=object, InternalCaptureControl=object, WindowsCapture=FakeCapture
                )
                gc_enabled = gc.isenabled()
                gc.disable()
                try:
                    with patch.dict(sys.modules, {"windows_capture": backend}):
                        with patch("kommand_source.capture.wgc_provider.resolve_window", return_value=window):
                            with patch("kommand_source.capture.wgc_provider.Image.frombuffer", return_value=Image.new("RGB", (4, 3), "red")):
                                if outcome == "frame":
                                    image, _ = capture_rendered_window(identity)
                                    self.assertEqual(image.size, (4, 3))
                                elif outcome == "timeout":
                                    with self.assertRaisesRegex(WindowsGraphicsCaptureError, "Timed out"):
                                        capture_rendered_window(identity, timeout_seconds=0)
                                else:
                                    with self.assertRaisesRegex(RuntimeError, "start failed"):
                                        capture_rendered_window(identity)
                    self.assertIsNone(references[0]())
                    if outcome != "start_error":
                        control.wait.assert_called_once()
                finally:
                    if gc_enabled:
                        gc.enable()
                    gc.collect()

    @unittest.skipUnless(platform.system() == "Windows", "Windows Graphics Capture required")
    def test_real_capture_uses_same_window_after_title_change(self) -> None:
        code = textwrap.dedent("""
            import sys
            import tkinter as tk
            import uuid
            from pathlib import Path
            sys.path.insert(0, str(Path("kommand-source") / "src"))
            from kommand_source.capture.mss_provider import MssCaptureProvider
            from kommand_source.capture.windows import find_window, get_window_identity
            from kommand_source.config import ChannelConfig
            try:
                root = tk.Tk()
            except tk.TclError:
                sys.exit(77)
            title = "Kommand-window-test-" + uuid.uuid4().hex
            root.title(title)
            root.configure(background="#d02020")
            root.geometry("360x220+15+15")
            root.update()
            identity = get_window_identity(find_window(title).handle)
            channel = ChannelConfig(
                name="video", capture_type="window", capture_target=title,
                window_handle=identity.handle, window_process_id=identity.process_id,
                window_process_start=identity.process_start,
            )
            provider = MssCaptureProvider()
            other = tk.Toplevel(root)
            other.title(title)
            other.configure(background="#2020d0")
            other.geometry("360x220+30+30")
            root.update()
            try:
                session = None
                for new_title in (title, "", title + "-next"):
                    root.title(new_title)
                    other.title(new_title)
                    root.update()
                    frame = provider.capture(channel)
                    image = frame.image
                    red, green, blue = image.getpixel((image.width // 2, image.height // 2))
                    assert red > blue and red > green, (red, green, blue)
                    if session is None:
                        session = provider._window_session
                    else:
                        assert provider._window_session is session
                root.geometry("480x280+15+15")
                root.update()
                frame = provider.capture(channel)
                assert frame.width > image.width
                assert provider._window_session is not session
            finally:
                provider.close()
                root.destroy()
        """)
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 77:
            self.skipTest("Tk display unavailable")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
