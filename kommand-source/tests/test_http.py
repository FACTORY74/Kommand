from pathlib import Path
import json
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "kommand-source" / "src"), str(ROOT / "kommand-common" / "src")]

from kommand_source.transport.http import (  # noqa: E402
    CentralClient,
    CentralConnectionError,
    UploadError,
    check_central_connection,
    _error_explanation,
)
from kommand_source.capture.mss_provider import MssCaptureProvider  # noqa: E402
from kommand_source.config import ChannelConfig, SourceConfig  # noqa: E402
from kommand_common.bandwidth import RollingBandwidth  # noqa: E402


class CentralConnectionTests(unittest.TestCase):
    def test_connection_error_shows_underlying_explanation(self) -> None:
        from urllib3.exceptions import MaxRetryError, NewConnectionError

        underlying = ConnectionRefusedError(10061, "The target machine actively refused the connection")
        connection = NewConnectionError(None, "Failed to establish a new connection")
        connection.__cause__ = underlying
        error = requests.ConnectionError(MaxRetryError(None, "/long/upload/path", connection))
        self.assertEqual(_error_explanation(error), underlying.strerror)

    @patch("kommand_source.transport.http.requests.get")
    def test_checks_health_endpoint(self, get) -> None:
        response = Mock()
        get.return_value = response

        check_central_connection("http://central.example/")

        get.assert_called_once_with("http://central.example/health", timeout=3.0)
        response.raise_for_status.assert_called_once_with()

    @patch("kommand_source.transport.http.requests.get")
    def test_reports_http_errors(self, get) -> None:
        get.return_value.raise_for_status.side_effect = requests.HTTPError("503")

        with self.assertRaisesRegex(CentralConnectionError, "503"):
            check_central_connection("http://central.example")

    @patch("kommand_source.transport.http.requests.get")
    def test_reports_connection_errors(self, get) -> None:
        get.side_effect = requests.ConnectionError("connection refused")

        with self.assertRaisesRegex(CentralConnectionError, "connection refused"):
            check_central_connection("http://central.example")

    @patch("kommand_source.transport.http.connect")
    def test_sent_bandwidth_counts_video_payload_and_heartbeats(self, connect) -> None:
        socket = connect.return_value
        socket.recv.return_value = json.dumps({"accepted": True, "revision": 1, "generation": "test"})
        now = [0.0]
        bandwidth = RollingBandwidth(clock=lambda: now[0])
        config = SourceConfig(central_url="http://central.example")
        first = CentralClient(config, bandwidth)
        self.addCleanup(first.close)
        channel = ChannelConfig(name="main")
        frame = MssCaptureProvider._encode(Image.new("RGB", (16, 16), "red"), 80, ("screen", 1))

        first.upload(channel, frame)
        first.upload(channel, frame)
        transmitted = [call.args[0] for call in socket.send.call_args_list]
        total = sum(len(value) if isinstance(value, bytes) else len(value.encode("utf-8")) for value in transmitted)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(len([value for value in transmitted if isinstance(value, bytes)]), 1)
        self.assertEqual(json.loads(transmitted[-1])["type"], "heartbeat")
        self.assertEqual(bandwidth.bytes_in_window, total)
        self.assertEqual(bandwidth.bytes_per_second, total / 5)
        now[0] = 5.0
        self.assertEqual(bandwidth.bytes_per_second, 0)


if __name__ == "__main__":
    unittest.main()
