from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kommand-common" / "src"))

from kommand_common.ui.widgets import clamp_number  # noqa: E402


class NumberStepperTests(unittest.TestCase):
    def test_clamps_to_bounds(self) -> None:
        self.assertEqual(clamp_number(-1, 1, 12), 1)
        self.assertEqual(clamp_number(20, 1, 12), 12)
        self.assertEqual(clamp_number(7, 1, 12), 7)


if __name__ == "__main__":
    unittest.main()
