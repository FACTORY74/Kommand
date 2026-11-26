from pathlib import Path
import sys
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "kommand-central" / "src"), str(ROOT / "kommand-common" / "src")]

from kommand_central.layout import LayoutStore
from kommand_central.windows import DisplayLayouts
from kommand_central.ui import CentralApp


class WindowTests(unittest.TestCase):
    def test_browser_display_button_uses_active_layout_and_running_server(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        with patch("kommand_central.ui.LayoutStore", return_value=LayoutStore()):
            app = CentralApp(root)
        root.update()
        buttons = [child for child in app.open_display_button.master.winfo_children()
                   if child.winfo_class() == "TButton"]
        browser_button = next(button for button in buttons if button.cget("text") == "BROWSER DISPLAY")
        self.assertTrue(browser_button.winfo_ismapped())
        with patch("kommand_central.ui.messagebox.showerror") as error:
            app._open_browser_display()
            error.assert_called_once()
        app._add_window()
        app.server = SimpleNamespace(started=True, config=SimpleNamespace(host="0.0.0.0", port=8000))
        app.server_thread = Mock()
        app.server_thread.is_alive.return_value = True
        with patch("kommand_central.ui.webbrowser.open_new", return_value=True) as opened:
            app._open_browser_display()
        opened.assert_called_once_with(f"http://127.0.0.1:8000/display/{app._active_window}")

    def test_tabs_have_equal_rendered_height_when_selected_or_hovered(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        with patch("kommand_central.ui.LayoutStore", return_value=LayoutStore()):
            app = CentralApp(root)
        root.geometry("1400x1000")
        app._add_window()
        root.update()
        tabs = app.window_tabs

        def tab_at(x, y):
            try:
                return tabs.index(f"@{x},{y}")
            except tk.TclError:
                return None

        centers = []
        for index in range(len(tabs.tabs())):
            positions = [x for x in range(tabs.winfo_width()) if tab_at(x, 20) == index]
            self.assertTrue(positions)
            centers.append(positions[len(positions) // 2])

        def vertical_bounds():
            return [
                [y for y in range(80) if tab_at(x, y) == index]
                for index, x in enumerate(centers)
            ]

        for page in app._tab_pages.values():
            tabs.select(page)
            root.update()
            for x in centers:
                tabs.event_generate("<Motion>", x=x, y=20)
                root.update()
                bounds = vertical_bounds()
                self.assertTrue(bounds[0])
                self.assertTrue(all(bound == bounds[0] for bound in bounds))

    def test_editor_stays_visible_above_selected_tab_page(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        with patch("kommand_central.ui.LayoutStore", return_value=LayoutStore()):
            app = CentralApp(root)
        root.geometry("1400x1000+0+0")
        root.lift()
        root.update()

        def assert_visible():
            root.update()
            entry = app.window_name_input
            self.assertTrue(entry.winfo_ismapped())
            widget = root.winfo_containing(entry.winfo_rootx() + 5, entry.winfo_rooty() + 5)
            self.assertEqual(widget, entry, (entry.winfo_rootx(), entry.winfo_rooty(), entry.winfo_geometry(), app.layout_editor.winfo_geometry(), root.winfo_geometry()))
            self.assertTrue(app.layout_canvas.winfo_ismapped())

        assert_visible()
        app._add_window()
        assert_visible()
        app.window_tabs.select(app._tab_pages["default"])
        assert_visible()
        self.assertEqual(app._active_window, "default")

    def test_independent_layouts_survive_restart_and_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            first = LayoutStore(path)
            first.assign((0,), "machine/one")
            collection = DisplayLayouts(first)
            key = collection.add()
            second = collection.layouts[key]
            second.set_window_name("Right monitor")
            second.resize(1, 3)
            second.assign((2,), "machine/two")
            loaded = DisplayLayouts(LayoutStore(path))
            self.assertEqual(loaded.layouts[key].window_name, "Right monitor")
            self.assertEqual(loaded.layouts[key].snapshot(), second.snapshot())
            self.assertEqual(loaded.layouts["default"].snapshot(), first.snapshot())
            loaded.remove("default")
            self.assertEqual(list(DisplayLayouts(LayoutStore(path)).layouts), [key])
            loaded.remove(key)
            self.assertEqual(DisplayLayouts(LayoutStore(path)).layouts, {})
            self.assertTrue(second.path.exists())

    def test_tabs_switch_open_rename_and_confirm_close(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        self.addCleanup(root.destroy)
        with tempfile.TemporaryDirectory() as directory:
            first = LayoutStore(Path(directory) / "layout.json")
            with patch("kommand_central.ui.LayoutStore", return_value=first):
                app = CentralApp(root)
            app._open_display()
            first_display = app.display_window
            app._add_window()
            key = app._active_window
            self.assertIsNot(app.layouts, first)
            app.window_name.set("Second screen")
            app._open_display()
            second_display = app.display_window
            self.assertIsNot(first_display, second_display)
            self.assertEqual(second_display.title(), "Second screen")
            app.layouts.assign((0,), "machine/two")
            app._switch_window("default")
            self.assertIs(app.display_window, first_display)
            self.assertIsNone(dict(first.leaves())[(0,)].channel)
            with patch("kommand_central.ui.messagebox.askyesno", return_value=False):
                app._remove_window(key)
            self.assertIn(key, app.window_layouts.layouts)
            with patch("kommand_central.ui.messagebox.askyesno", return_value=True):
                app._remove_window(key)
            self.assertFalse(second_display.winfo_exists())
            self.assertTrue(first_display.winfo_exists())
            with patch("kommand_central.ui.messagebox.askyesno", return_value=True):
                app._remove_window("default")
            self.assertIsNone(app._active_window)
            app._add_window()
            self.assertIsNotNone(app._active_window)
