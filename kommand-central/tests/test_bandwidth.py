from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "kommand-central" / "src"), str(ROOT / "kommand-common" / "src")]

from kommand_central.bandwidth import ReceiveBandwidth  # noqa: E402


class ReceiveBandwidthTests(unittest.TestCase):
    def test_reports_rolling_bytes_per_second(self) -> None:
        now = [0.0]
        bandwidth = ReceiveBandwidth(window_seconds=5.0, clock=lambda: now[0])

        bandwidth.record(1000)
        self.assertEqual(bandwidth.bytes_per_second, 200)

        now[0] = 5.0
        self.assertEqual(bandwidth.bytes_per_second, 0)


if __name__ == "__main__":
    unittest.main()
