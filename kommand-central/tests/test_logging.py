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
            str(ROOT / "kommand-central" / "src"),
            str(ROOT / "kommand-common" / "src"),
        ])
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code), str(log_path)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_log_file_rotates_and_keeps_a_bounded_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "logs" / "central.log"
            result = self.run_child("""
                import logging
                from pathlib import Path
                import sys
                from kommand_central.main import configure_logging

                configure_logging(Path(sys.argv[1]))
                logger = logging.getLogger("uvicorn.access")
                handler = logger.handlers[0]
                assert handler.maxBytes == 5 * 1024 * 1024
                assert handler.backupCount == 5
                handler.maxBytes = 250
                for index in range(20):
                    logger.info("request %s %s", index, "x" * 150)
            """, log_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            logs = list(log_path.parent.glob("central.log*"))
            self.assertEqual(len(logs), 6)
            self.assertIn("request 19", log_path.read_text(encoding="utf-8"))
            self.assertTrue((log_path.parent / "central.log.5").exists())

    def test_server_requests_and_restart_log_to_file_not_console(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "central.log"
            result = self.run_child("""
                from pathlib import Path
                import socket
                import sys
                import time
                import tkinter as tk

                import requests
                from kommand_central.main import configure_logging
                from kommand_central.ui import CentralApp

                configure_logging(Path(sys.argv[1]))
                try:
                    root = tk.Tk()
                except tk.TclError:
                    sys.exit(77)
                root.withdraw()
                app = CentralApp(root)
                app.host.set("127.0.0.1")

                def wait_for(predicate):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        root.update()
                        if predicate():
                            return
                        time.sleep(0.01)
                    raise AssertionError(f"server status: {app.status.get()}")

                for _ in range(2):
                    with socket.socket() as sock:
                        sock.bind(("127.0.0.1", 0))
                        port = sock.getsockname()[1]
                    app.port.set(str(port))
                    app._start()
                    wait_for(lambda: app.status.get().startswith("Listening"))
                    response = requests.get(f"http://127.0.0.1:{port}/api/v1/channels", timeout=3)
                    assert response.status_code == 200
                    app._stop()

                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    sock.listen()
                    app.port.set(str(sock.getsockname()[1]))
                    app._start()
                    wait_for(lambda: "unexpectedly" in app.status.get())
                root.destroy()
            """, log_path)
            if result.returncode == 77:
                self.skipTest("Tk display unavailable")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            log = log_path.read_text(encoding="utf-8")
            self.assertEqual(log.count('"GET /api/v1/channels HTTP/1.1" 200'), 2)
            self.assertIn("Application startup complete.", log)
            self.assertIn("ERROR", log)


if __name__ == "__main__":
    unittest.main()
