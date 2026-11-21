import gc
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "kommand-central" / "src"),
    str(ROOT / "kommand-common" / "src"),
]

from kommand_central.display import DisplayWindow  # noqa: E402
from kommand_central.layout import LayoutStore  # noqa: E402
from kommand_central.state import ChannelStore  # noqa: E402
from kommand_central.ui import CentralApp  # noqa: E402


class DisplayTests(unittest.TestCase):
    def tearDown(self) -> None:
        self.doCleanups()
        gc.collect()

    @unittest.skipUnless(sys.platform == "win32", "Windows native fullscreen placement")
    def test_fullscreen_uses_current_monitor_and_restores_window_bounds(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        window = DisplayWindow(root, ChannelStore(), LayoutStore())
        self.addCleanup(window.destroy)
        root.update()
        original = (-1700, 100, -900, 700)
        monitor = (-1920, 0, 0, 1080)
        with patch("kommand_central.display._windows_window_and_monitor", return_value=(original, monitor)):
            with patch("kommand_central.display._windows_place_window") as place:
                window._toggle_fullscreen()
                place.assert_called_with(window, monitor)
                self.assertEqual(window.fullscreen_button.cget("text"), "EXIT FULLSCREEN")
                window._toggle_fullscreen()
                place.assert_called_with(window, original)
                self.assertEqual(window.fullscreen_button.cget("text"), "FULLSCREEN")

    def test_displays_new_frame_and_layout(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        store = ChannelStore()
        layouts = LayoutStore()
        layouts.assign((0,), "source/main")

        def publish(color: str) -> None:
            image = Image.new("RGB", (16, 16), color)
            output = BytesIO()
            image.save(output, "WEBP")
            store.update(
                "source", "main", output.getvalue(), "image/webp",
                16, 16, "2026-01-01T00:00:00Z", image,
            )

        publish("red")
        window = DisplayWindow(root, store, layouts)
        self.addCleanup(window.destroy)
        root.update()
        window._refresh()
        root.update()
        slot = window._slots[0]
        self.assertIsNotNone(slot.photo)
        assert slot.photo is not None
        self.assertGreater(slot.photo.width(), 16)
        self.assertEqual(slot.revision, 1)
        self.assertEqual(slot.name.cget("text"), "Panel 1 / source/main")

        publish("blue")
        window._refresh()
        self.assertEqual(slot.revision, 2)
        self.assertEqual(slot.name.cget("text"), "Panel 1 / source/main")
        layouts.assign((1,), "source/main")
        window._refresh()
        self.assertEqual(window._slots[1].channel, "source/main")
        self.assertEqual(window._slots[1].name.cget("text"), "Panel 2 / source/main")

    def test_video_channels_use_the_faster_display_refresh(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        store = ChannelStore()
        layouts = LayoutStore()
        layouts.assign((0,), "source/main")
        window = DisplayWindow(root, store, layouts)
        self.addCleanup(window.destroy)
        root.update()
        with patch.object(window, "after", wraps=window.after) as after:
            window._refresh()
            self.assertEqual(after.call_args.args[0], 50)
            image = Image.new("RGB", (16, 16), "red")
            store.update_if_revision(
                "source", "main", None, "image/webp", 16, 16,
                "2026-01-01T00:00:00Z", image, None,
            )
            window._refresh()
            self.assertEqual(after.call_args.args[0], 16)

    def test_panel_labels_cover_waiting_nested_and_resized_layouts(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        layouts = LayoutStore()
        layouts.nest((1,), 1, 2)
        layouts.assign((1, 1), "source/waiting")
        layouts.assign((2,), "source/third")
        window = DisplayWindow(root, ChannelStore(), layouts)
        self.addCleanup(window.destroy)
        root.update()

        self.assertEqual(
            [slot.name.cget("text") for slot in window._slots],
            [
                "Panel 1 / Unassigned",
                "Panel 2.1 / Unassigned",
                "Panel 2.2 / source/waiting",
                "Panel 3 / source/third",
                "Panel 4 / Unassigned",
            ],
        )
        self.assertEqual(window._slots[2].label.cget("text"), "Waiting for source/waiting")
        self.assertEqual(window._slots[2].name.cget("anchor"), "center")
        self.assertEqual(window._slots[2].name.grid_info()["row"], 1)

        layouts.resize(2, 3)
        window._refresh()
        third = next(slot for slot in window._slots if slot.channel == "source/third")
        self.assertEqual(third.name.cget("text"), "Panel 4 / source/third")
        layouts.assign((3,), None)
        window._refresh()
        self.assertEqual(window._slots[4].name.cget("text"), "Panel 4 / Unassigned")

    def test_drag_moves_and_swaps_and_updates_central_tree(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        path = Path(directory.name) / "layout.json"
        layouts = LayoutStore(path)
        layouts.assign((0,), "source/main")
        layouts.assign((2,), "source/other")
        layouts.nest((3,), 1, 2)
        with patch("kommand_central.ui.LayoutStore", return_value=layouts):
            app = CentralApp(root)
        window = DisplayWindow(root, app.store, layouts)
        self.addCleanup(window.destroy)
        window.geometry("800x500+10+10")
        window.lift()
        root.update()

        def find(path: tuple[int, ...]):
            return next(slot for slot in window._slots if slot.path == path)

        def drag(source: tuple[int, ...], target: tuple[int, ...]) -> None:
            origin = find(source)
            destination = find(target)
            x = destination.name.winfo_rootx() + destination.name.winfo_width() // 2
            y = destination.name.winfo_rooty() + destination.name.winfo_height() // 2
            origin.label.event_generate("<ButtonPress-1>", x=5, y=5)
            with patch.object(window, "winfo_containing", return_value=destination.name):
                origin.label.event_generate("<B1-Motion>", x=5, y=5, rootx=x, rooty=y)
                self.assertIs(window._drag_target, destination)
                origin.label.event_generate("<ButtonRelease-1>", x=5, y=5, rootx=x, rooty=y)
            root.update()

        def wait_for_assignment(item: str, assignment: str) -> None:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                root.update()
                if app.layout_tree.exists(item) and app.layout_tree.item(item, "values")[1] == assignment:
                    return
                time.sleep(0.01)
            self.fail(f"Central tree did not show {item} as {assignment}")

        drag((0,), (3, 0))
        wait_for_assignment("3.0", "source/main")
        self.assertEqual(app.layout_tree.item("0", "values")[1], "Unassigned")
        self.assertEqual(find((3, 0)).name.cget("text"), "Panel 4.1 / source/main")

        drag((3, 0), (2,))
        wait_for_assignment("2", "source/main")
        self.assertEqual(app.layout_tree.item("3.0", "values")[1], "source/other")
        self.assertEqual(LayoutStore(path).snapshot(), layouts.snapshot())

        revision = layouts.revision
        empty = find((0,))
        empty.label.event_generate("<ButtonPress-1>", x=5, y=5)
        empty.label.event_generate("<ButtonRelease-1>", x=5, y=5, rootx=-10000, rooty=-10000)
        assigned = find((2,))
        assigned.label.event_generate("<ButtonPress-1>", x=5, y=5)
        assigned.label.event_generate("<ButtonRelease-1>", x=5, y=5, rootx=-10000, rooty=-10000)
        assigned.label.event_generate("<ButtonPress-1>", x=5, y=5)
        with patch.object(window, "winfo_containing", return_value=assigned.name):
            assigned.label.event_generate("<ButtonRelease-1>", x=5, y=5)
        self.assertEqual(layouts.revision, revision)

        destination = find((3, 0))
        assigned.label.event_generate("<ButtonPress-1>", x=5, y=5)
        with patch.object(window, "winfo_containing", return_value=destination.name):
            with patch.object(layouts, "_save", side_effect=OSError("disk error")):
                with patch("kommand_central.display.messagebox.showerror") as error:
                    assigned.label.event_generate("<ButtonRelease-1>", x=5, y=5)
        error.assert_called_once()
        self.assertEqual(layouts.revision, revision)
        self.assertEqual(LayoutStore(path).snapshot(), layouts.snapshot())

        assigned.label.event_generate("<ButtonPress-1>", x=5, y=5)
        layouts.assign((0,), "source/concurrent")
        snapshot = layouts.snapshot()
        with patch.object(window, "winfo_containing", return_value=destination.name):
            with patch("kommand_central.display.messagebox.showerror") as error:
                assigned.label.event_generate("<ButtonRelease-1>", x=5, y=5)
        error.assert_called_once()
        self.assertEqual(layouts.snapshot(), snapshot)


if __name__ == "__main__":
    unittest.main()
