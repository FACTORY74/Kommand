from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / name / "src") for name in ("kommand-central", "kommand-source", "kommand-common")]

from kommand_central.display import DisplayWindow
from kommand_central.layout import LayoutStore, Viewport
from kommand_central.native_video import load_vlc, video_bounds
from kommand_central.state import ChannelStore
from kommand_central.web import create_app
from kommand_source.capture import CapturedFrame
from kommand_source.config import ChannelConfig, SourceConfig
from kommand_source.transport import CentralClient


class NativeVideoTests(unittest.TestCase):
    def test_native_surface_geometry_preserves_fit_zoom_and_pan(self):
        self.assertEqual(video_bounds((100, 100), (200, 100), Viewport()), (0, 25, 100, 50))
        self.assertEqual(video_bounds((100, 100), (100, 100), Viewport(2, .5, 0)), (0, -50, 200, 200))

    @unittest.skipUnless(sys.platform == "win32", "Windows embedded playback integration")
    def test_idle_native_stream_displays_initial_and_single_updated_frame(self):
        try:
            vlc = load_vlc()
            root = tk.Tk()
        except (RuntimeError, tk.TclError) as exc:
            self.skipTest(str(exc))
        root.withdraw()
        store = ChannelStore()
        layouts = LayoutStore()
        layouts.assign((0,), "source-1/main")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            create_app(store), log_config=None, log_level="error", timeout_graceful_shutdown=2,
        ))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        client = CentralClient(SourceConfig(central_url=f"http://127.0.0.1:{port}"))
        window = None
        snapshots = tempfile.TemporaryDirectory()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(server.started)
            channel = ChannelConfig(name="main", video_fps=15)

            def upload(color):
                image = Image.new("RGB", (320, 180), color)
                client.upload(channel, CapturedFrame(b"", 320, 180, "image/webp", "webp", image=image))

            upload("red")
            window = DisplayWindow(root, store, layouts, stream_url=f"http://127.0.0.1:{port}")
            window.geometry("640x400-10000-10000")
            decoded = 0
            for color in ("red", "blue"):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    upload(color)
                    root.update()
                    time.sleep(.05)
                panel = window._slots[0].video
                self.assertIsNotNone(panel, window._video_error)
                stats = vlc.MediaStats()
                media = panel.player.get_media()
                try:
                    self.assertTrue(media.get_stats(stats))
                finally:
                    media.release()
                self.assertGreater(stats.decoded_video, decoded, f"{color} frame was buffered indefinitely")
                snapshot = Path(snapshots.name) / f"{color}.png"
                self.assertEqual(panel.player.video_take_snapshot(0, str(snapshot), 0, 0), 0)
                deadline = time.monotonic() + 2
                while not snapshot.exists() and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                self.assertTrue(snapshot.exists(), f"{color} frame was not presented")
                with Image.open(snapshot) as image:
                    pixel = image.convert("RGB").getpixel((100, 100))
                self.assertGreater(pixel[0 if color == "red" else 2], 200)
                self.assertLess(pixel[2 if color == "red" else 0], 20)
                decoded = stats.decoded_video
                revision = store.get("source-1", "main").revision
                upload(color)
                self.assertEqual(store.get("source-1", "main").revision, revision)
                self.assertIsNone(window._slots[0].photo)
        finally:
            if window is not None:
                window.destroy()
            root.destroy()
            client.close()
            server.should_exit = True
            thread.join(5)
            listener.close()
            snapshots.cleanup()
            self.assertFalse(thread.is_alive(), "Native stream shutdown left the server running")

    @unittest.skipUnless(sys.platform == "win32", "Windows embedded playback integration")
    def test_real_native_playback_never_uses_pillow_display_frames(self):
        try:
            vlc = load_vlc()
            root = tk.Tk()
        except (RuntimeError, tk.TclError) as exc:
            self.skipTest(str(exc))
        root.withdraw()
        store = ChannelStore()
        layouts = LayoutStore()
        layouts.assign((0,), "source-1/main")
        layouts.assign((1,), "source-1/main")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(store), log_config=None, log_level="error"))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        client = CentralClient(SourceConfig(central_url=f"http://127.0.0.1:{port}"))
        window = None
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(server.started)
            size = (2597, 1337)
            channel = ChannelConfig(name="main", video_fps=30, quality=50)
            image = Image.new("RGB", size, "red")
            client.upload(channel, CapturedFrame(b"", *size, "image/webp", "webp", image=image))
            with patch("kommand_central.display._render_view", side_effect=AssertionError("CPU video rendering")), patch(
                "kommand_central.display.messagebox.showerror", side_effect=AssertionError("Native playback failed"),
            ):
                with patch("kommand_central.display.ImageTk.PhotoImage", side_effect=AssertionError("Tk video frame")):
                    window = DisplayWindow(root, store, layouts, stream_url=f"http://127.0.0.1:{port}")
                    window.geometry("640x400-10000-10000")
                    before_reconnect = []
                    for index in range(60):
                        root.update()
                        if index == 30:
                            for slot in window._slots[:2]:
                                panel = slot.video
                                self.assertIsNotNone(panel)
                                stats = vlc.MediaStats()
                                media = panel.player.get_media()
                                try:
                                    self.assertTrue(media.get_stats(stats))
                                    before_reconnect.append(stats.displayed_pictures)
                                finally:
                                    media.release()
                            client.close()
                        image = Image.new("RGB", size, "blue" if index % 2 else "red")
                        client.upload(channel, CapturedFrame(b"", *size, "image/webp", "webp", image=image))
                        time.sleep(1 / 30)
                    root.update()
                    for slot, displayed in zip(window._slots[:2], before_reconnect):
                        self.assertIsNotNone(slot.video, window._video_error)
                        stats = vlc.MediaStats()
                        media = slot.video.player.get_media()
                        try:
                            self.assertTrue(media.get_stats(stats))
                        finally:
                            media.release()
                        self.assertGreater(stats.decoded_video, 40)
                        self.assertGreater(stats.displayed_pictures, displayed + 15)
                    self.assertIsNone(window._slots[0].photo)
                    self.assertIsNone(store.get("source-1", "main")._image)
                    slot = window._slots[0]
                    panel = slot.video
                    slot.view = Viewport(2, .1, 0)
                    window._refresh()
                    self.assertEqual(
                        panel._geometry,
                        video_bounds((slot.label.winfo_width(), slot.label.winfo_height()), size, slot.view),
                    )
        finally:
            if window is not None:
                window.destroy()
            root.destroy()
            client.close()
            server.should_exit = True
            thread.join(5)
            listener.close()
            self.assertFalse(thread.is_alive(), "Native stream shutdown left the server running")
