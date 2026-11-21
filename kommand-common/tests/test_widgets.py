from pathlib import Path
import sys
import tkinter as tk
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kommand-common" / "src"))

from kommand_common.ui.widgets import NumberStepper, clamp_number  # noqa: E402


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
