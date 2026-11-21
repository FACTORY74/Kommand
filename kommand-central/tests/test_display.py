import gc
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageTk

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "kommand-central" / "src"),
    str(ROOT / "kommand-common" / "src"),
]

from kommand_central.display import DisplayWindow, _render_view  # noqa: E402
from kommand_central.layout import LayoutStore, Viewport  # noqa: E402
from kommand_central.state import ChannelStore  # noqa: E402
from kommand_central.ui import CentralApp  # noqa: E402


class DisplayTests(unittest.TestCase):
    def tearDown(self) -> None:
        self.doCleanups()
        gc.collect()

    def test_zoom_and_pan_render_only_the_panel_view(self) -> None:
        image = Image.new("RGB", (100, 100), "red")
        image.paste("blue", (50, 0, 100, 100))
        normal = _render_view(image, (100, 100), Viewport())
        zoomed = _render_view(image, (100, 100), Viewport(2.0, 0.0, 0.0))
        panned = _render_view(image, (100, 100), Viewport(2.0, 0.5, 0.0))
        fitted = _render_view(Image.new("RGB", (200, 100)), (100, 100), Viewport())
        self.assertEqual(normal.getpixel((90, 50)), (0, 0, 255))
        self.assertEqual(zoomed.getpixel((90, 50)), (0, 0, 255))
        self.assertEqual(panned.getpixel((90, 50)), (255, 0, 0))
        self.assertEqual(panned.size, (100, 100))
        self.assertEqual(fitted.size, (100, 50))

    def test_panel_wheel_pan_and_move_persist_independently(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            layouts = LayoutStore(path)
            layouts.assign((0,), "source/main")
            store = ChannelStore()
            image = Image.new("RGB", (100, 100), "red")
            image.paste("blue", (50, 0, 100, 100))
            store.update("source", "main", None, "image/webp", 100, 100, "now", image)
            window = DisplayWindow(root, store, layouts)
            self.addCleanup(window.destroy)
            window.geometry("800x500-10000-10000")
            root.update()
            window._refresh()
            slot = window._slots[0]
            label = slot.label
            width, height = label.winfo_width(), label.winfo_height()
            x, y = width * 3 // 4, height // 2
            label.event_generate("<MouseWheel>", x=x, y=y, delta=120 if sys.platform == "win32" else 1)
            self.assertAlmostEqual(slot.view.zoom, 1.2)
            self.assertAlmostEqual(slot.view.pan_x, (x / width - 0.5) * -0.2)
            self.assertEqual(LayoutStore(path).leaves()[0][1].view, slot.view)
            label.event_generate("<MouseWheel>", x=x, y=y, delta=-120 if sys.platform == "win32" else -1)
            self.assertAlmostEqual(slot.view.zoom, 1.0)
            self.assertAlmostEqual(slot.view.pan_x, 0.0)
            minimum_view = slot.view
            revision = layouts.revision
            for _ in range(3):
                label.event_generate("<MouseWheel>", x=x, y=y, delta=-120 if sys.platform == "win32" else -1)
            self.assertEqual(slot.view, minimum_view)
            self.assertEqual(layouts.revision, revision)
            self.assertEqual(LayoutStore(path).leaves()[0][1].view, minimum_view)
            label.event_generate("<MouseWheel>", x=x, y=y, delta=120 if sys.platform == "win32" else 1)
            before_pan = slot.view
            assert slot.photo is not None
            before_image = ImageTk.getimage(slot.photo).convert("RGB")
            left, top = label.winfo_rootx(), label.winfo_rooty()
            label.event_generate("<ButtonPress-1>", x=x, y=y, rootx=left + x, rooty=top + y)
            label.event_generate("<B1-Motion>", x=x + 20, y=y + 10, rootx=left + x + 20, rooty=top + y + 10)
            self.assertAlmostEqual(slot.view.pan_x, before_pan.pan_x + 20 / width)
            self.assertEqual(layouts.leaves()[0][1].view, before_pan)
            window._refresh()
            assert slot.photo is not None
            after_image = ImageTk.getimage(slot.photo).convert("RGB")
            self.assertIsNotNone(ImageChops.difference(before_image, after_image).getbbox())
            label.event_generate("<ButtonRelease-1>", x=x + 20, y=y + 10, rootx=left + x + 20, rooty=top + y + 10)
            self.assertEqual(layouts.leaves()[0][1].view, slot.view)
            self.assertEqual(LayoutStore(path).leaves()[0][1].view, slot.view)

            saved_view = slot.view
            updated_image = Image.new("RGB", (100, 100), "green")
            store.update("source", "main", None, "image/webp", 100, 100, "later", updated_image)
            window._refresh()
            self.assertEqual(slot.view, saved_view)
            assert slot.photo is not None
            displayed = ImageTk.getimage(slot.photo).convert("RGB")
            self.assertEqual(displayed.getpixel((width // 2, height // 2)), (0, 128, 0))
            revision = layouts.revision
            label.event_generate("<ButtonPress-1>", x=x, y=y, rootx=left + x, rooty=top + y)
            label.event_generate("<B1-Motion>", rootx=left + x + 10, rooty=top + y)
            label.event_generate("<B1-Motion>", rootx=left - 10, rooty=top + y)
            with patch.object(window, "winfo_containing", return_value=label):
                label.event_generate("<ButtonRelease-1>", rootx=left + x, rooty=top + y)
            self.assertEqual(slot.view, saved_view)
            self.assertEqual(layouts.revision, revision)

            destination = window._slots[1]
            target_x = destination.label.winfo_rootx() + destination.label.winfo_width() // 2
            target_y = destination.label.winfo_rooty() + destination.label.winfo_height() // 2
            label.event_generate("<ButtonPress-1>", x=x, y=y, rootx=left + x, rooty=top + y)
            label.event_generate("<B1-Motion>", x=x + 10, y=y, rootx=left + x + 10, rooty=top + y)
            with patch.object(window, "winfo_containing", return_value=destination.label):
                label.event_generate("<B1-Motion>", rootx=target_x, rooty=target_y)
                self.assertEqual(slot.view, saved_view)
                label.event_generate("<ButtonRelease-1>", rootx=target_x, rooty=target_y)
            self.assertEqual(layouts.leaves()[0][1].view, saved_view)
            self.assertIsNone(layouts.leaves()[0][1].channel)
            self.assertEqual(layouts.leaves()[1][1].channel, "source/main")
            self.assertEqual(layouts.leaves()[1][1].view, Viewport())
            self.assertEqual(LayoutStore(path).snapshot(), layouts.snapshot())

    def test_failed_view_save_is_reported_and_rolled_back(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        layouts = LayoutStore()
        layouts.assign((0,), "source/main")
        window = DisplayWindow(root, ChannelStore(), layouts)
        self.addCleanup(window.destroy)
        window.geometry("800x500-10000-10000")
        root.update()
        slot = window._slots[0]
        revision = layouts.revision
        with patch.object(layouts, "_save", side_effect=OSError("disk error")):
            with patch("kommand_central.display.messagebox.showerror") as error:
                slot.label.event_generate(
                    "<MouseWheel>", x=20, y=20, delta=120 if sys.platform == "win32" else 1
                )
        error.assert_called_once()
        self.assertEqual(slot.view, Viewport())
        self.assertEqual(layouts.leaves()[0][1].view, Viewport())
        self.assertEqual(layouts.revision, revision)

    def test_wheel_targets_panel_under_pointer_not_focused_widget(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        layouts = LayoutStore()
        layouts.assign((0,), "source/first")
        layouts.assign((1,), "source/second")
        window = DisplayWindow(root, ChannelStore(), layouts)
        self.addCleanup(window.destroy)
        window.geometry("800x500-10000-10000")
        root.update()
        first, second = window._slots[:2]
        x = second.label.winfo_rootx() + second.label.winfo_width() // 2
        y = second.label.winfo_rooty() + second.label.winfo_height() // 2
        with patch.object(window, "winfo_containing", return_value=second.label):
            first.label.event_generate(
                "<MouseWheel>", rootx=x, rooty=y, delta=120 if sys.platform == "win32" else 1
            )
            with patch("kommand_central.display.sys.platform", "linux"):
                first.label.event_generate("<MouseWheel>", rootx=x, rooty=y, delta=120)
        self.assertEqual(first.view, Viewport())
        self.assertAlmostEqual(second.view.zoom, 1.44)

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
