from pathlib import Path
import inspect
import sys
import tkinter as tk
import unittest

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kommand-common" / "src"))

from kommand_common.ui.widgets import NumberStepper, clamp_number  # noqa: E402
from kommand_common.ui.theme import apply_theme, card, COLORS  # noqa: E402


class CardTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Window capture requires Windows")
    def test_outer_background_matches_window_without_changing_interior(self) -> None:
        if "window" not in inspect.signature(ImageGrab.grab).parameters:
            self.skipTest("Installed Pillow does not support window capture")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        apply_theme(root)
        root.geometry("300x180")
        panel = card(root, "Server")
        panel.pack(fill="both", expand=True, padx=20, pady=20)
        root.update()

        def rgb(color: str) -> tuple[int, int, int]:
            red, green, blue = root.winfo_rgb(color)
            return red // 257, green // 257, blue // 257

        for _ in range(2):
            image = ImageGrab.grab(window=int(root.wm_frame(), 0))
            x, y = panel.winfo_x(), panel.winfo_y()
            self.assertEqual(image.getpixel((x + 5, y + 2)), rgb(COLORS["background"]))
            self.assertEqual(
                image.getpixel((x + panel.winfo_width() - 6, y + 2)),
                rgb(COLORS["background"]),
            )
            self.assertEqual(
                image.getpixel((x + panel.winfo_width() - 6, y + panel.winfo_height() - 6)),
                rgb(COLORS["surface"]),
            )
            self.assertEqual(
                image.getpixel((x, y + panel.winfo_height() // 2)),
                rgb(COLORS["border"]),
            )
            apply_theme(root)
            root.update()


class NumberStepperTests(unittest.TestCase):
    def test_clamps_to_bounds(self) -> None:
        self.assertEqual(clamp_number(-1, 1, 12), 1)
        self.assertEqual(clamp_number(20, 1, 12), 12)
        self.assertEqual(clamp_number(7, 1, 12), 7)

    def test_default_stepper_still_clamps_on_commit(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        value = tk.StringVar(value="5")
        stepper = NumberStepper(root, value, minimum=1, maximum=10)
        value.set("99")
        stepper._commit()
        self.assertEqual(value.get(), "10")


if __name__ == "__main__":
    unittest.main()
