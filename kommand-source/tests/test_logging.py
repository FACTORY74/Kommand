import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


class LoggingTests(unittest.TestCase):
    def run_child(self, code: str, log_path: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([
            str(ROOT / "kommand-source" / "src"),
            str(ROOT / "kommand-common" / "src"),
        ])
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code), str(log_path)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )

    def test_errors_rotate_and_logs_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "source.log"
            result = self.run_child("""
                import logging
                from pathlib import Path
                import sys
                from kommand_source.main import configure_logging

                path = Path(sys.argv[1])
                configure_logging(path)
                logging.getLogger().handlers[0].maxBytes = 250
                for index in range(20):
                    logging.getLogger("kommand_source.runner").error(
                        "capture failure %s %s", index, "x" * 150,
                    )
                configure_logging(path)
                logging.getLogger("kommand_source.main").info("restarted")
            """, path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertEqual(len(list(path.parent.glob("source.log*"))), 6)
            content = path.read_text(encoding="utf-8")
            self.assertIn("capture failure 19", content)
            self.assertIn("restarted", content)

    def test_capture_upload_connection_and_worker_failures_are_logged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.log"
            result = self.run_child("""
                from pathlib import Path
                import queue
                import sys
                import threading
                from unittest.mock import Mock, patch
                import requests
                from kommand_source.main import configure_logging
                from kommand_source.config import SourceConfig, ChannelConfig
                from kommand_source.runner import SourceRunner
                from kommand_source.ui.app import SourceApp

                configure_logging(Path(sys.argv[1]))
                app = SourceApp.__new__(SourceApp)
                app.events = queue.Queue()
                app.connection_events = queue.Queue()
                runner = SourceRunner(Mock(), app._post_status)
                provider = Mock()
                client = Mock()
                with patch("kommand_source.runner.MssCaptureProvider", return_value=provider):
                    with patch("kommand_source.runner.CentralClient", return_value=client):
                        provider.capture.side_effect = OSError("capture device lost")
                        try:
                            runner.capture_once(SourceConfig(), ChannelConfig(name="screen"))
                        except OSError:
                            pass
                        provider.capture.side_effect = None
                        client.upload.side_effect = requests.ConnectionError("upload disconnected")
                        try:
                            runner.capture_once(SourceConfig(), ChannelConfig(name="screen"))
                        except requests.ConnectionError:
                            pass
                with patch("kommand_source.transport.http.requests.get",
                           side_effect=requests.ConnectionError("health disconnected")):
                    app._probe_central_connection("http://127.0.0.1:8000")
                assert app.connection_events.get_nowait()[1] == "Not contactable"
                app._post_status("window", "Error", "selected window closed")

                def crash():
                    raise RuntimeError("worker crashed")
                thread = threading.Thread(target=crash, name="capture-test")
                thread.start()
                thread.join()
                try:
                    raise ValueError("callback failed")
                except ValueError:
                    sys.excepthook(*sys.exc_info())
            """, path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            content = path.read_text(encoding="utf-8")
            for message in (
                "Channel screen Error: capture device lost",
                "Channel screen Upload error: upload disconnected",
                "Central connection check failed",
                "health disconnected",
                "Channel window Error: selected window closed",
                "capture-test",
                "worker crashed",
                "callback failed",
                "Traceback (most recent call last)",
            ):
                self.assertIn(message, content)
