from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    def test_sent_bandwidth_counts_actual_multipart_bodies_across_clients(self) -> None:
        received: list[int] = []
        reject = [False]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                received.append(len(body))
                content = b'{"revision":1,"generation":"test"}'
                self.send_response(503 if reject[0] else 202)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        now = [0.0]
        bandwidth = RollingBandwidth(clock=lambda: now[0])
        config = SourceConfig(central_url=f"http://127.0.0.1:{server.server_port}")
        first = CentralClient(config, bandwidth)
        second = CentralClient(config, bandwidth)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        channel = ChannelConfig(name="main")
        frame = MssCaptureProvider._encode(Image.new("RGB", (16, 16), "red"), 80, ("screen", 1))

        first.upload(channel, frame)
        first.upload(channel, frame)
        reject[0] = True
        with self.assertRaises(UploadError):
            second.upload(channel, frame)
        self.assertEqual(len(received), 3)
        self.assertEqual(received[1], 0)
        self.assertGreater(received[0], len(frame.content))
        self.assertEqual(bandwidth.bytes_in_window, sum(received))
        self.assertEqual(bandwidth.bytes_per_second, sum(received) / 5)
        now[0] = 5.0
        self.assertEqual(bandwidth.bytes_per_second, 0)


if __name__ == "__main__":
    unittest.main()
