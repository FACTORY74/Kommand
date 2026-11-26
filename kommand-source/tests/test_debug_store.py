from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kommand_source.capture import CapturedFrame  # noqa: E402
from kommand_source.debug_store import DebugStore, LatestDebugWriter  # noqa: E402


def frame(content: bytes, extension: str = "jpg") -> CapturedFrame:
    return CapturedFrame(
        content=content,
        width=1,
        height=1,
        media_type=f"image/{extension}",
        extension=extension,
    )


class DebugStoreTests(unittest.TestCase):
    def test_background_writer_replaces_pending_frames_and_reports_failures(self) -> None:
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        stored = []
        errors = []
        def write(channel, captured):
            stored.append(captured.content)
            if len(stored) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test writer was not released")
            else:
                finished.set()
                raise PermissionError("diagnostic locked")
        store = Mock()
        store.write.side_effect = write
        writer = LatestDebugWriter(store, "main", errors.append)
        try:
            writer.submit(frame(b"first"))
            self.assertTrue(entered.wait(2))
            writer.submit(frame(b"replaced"))
            writer.submit(frame(b"latest"))
            release.set()
            self.assertTrue(finished.wait(2))
        finally:
            release.set()
            writer.close()
        self.assertEqual(stored, [b"first", b"latest"])
        self.assertEqual(errors, ["diagnostic locked"])

    def test_write_retries_windows_reader_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DebugStore(Path(directory))
            destination = store.write("main", frame(b"old"))
            original_replace = Path.replace
            attempts = []

            def replace(path, target):
                attempts.append(path)
                if len(attempts) < 3:
                    error = PermissionError("Reader has the image open")
                    error.winerror = 5
                    raise error
                return original_replace(path, target)

            with patch.object(Path, "replace", replace), patch("time.sleep"):
                store.write("main", frame(b"new"))
            self.assertEqual(destination.read_bytes(), b"new")
            self.assertEqual(len(attempts), 3)
            self.assertEqual(list(store.directory.iterdir()), [destination])

    def test_persistent_lock_preserves_previous_image_and_cleans_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DebugStore(Path(directory))
            destination = store.write("main", frame(b"old"))
            error = PermissionError("Image remains locked")
            error.winerror = 32
            with patch.object(Path, "replace", side_effect=error) as replace:
                with patch("time.sleep"):
                    with self.assertRaises(PermissionError):
                        store.write("main", frame(b"new"))
            self.assertEqual(replace.call_count, 6)
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(list(store.directory.iterdir()), [destination])

    def test_reset_removes_previous_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "debug"
            path.mkdir()
            (path / "old.jpg").write_bytes(b"old")
            store = DebugStore(path)
            store.reset()
            self.assertEqual(list(path.iterdir()), [])

    def test_write_replaces_latest_and_old_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DebugStore(Path(directory) / "debug")
            store.write("main", frame(b"first"))
            destination = store.write("main", frame(b"second", "png"))
            self.assertEqual(destination.read_bytes(), b"second")
            self.assertFalse((store.directory / "main.jpg").exists())
            self.assertEqual([item.name for item in store.directory.iterdir()], ["main.png"])

    def test_write_video_frame_as_debug_webp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DebugStore(Path(directory))
            video_frame = CapturedFrame(
                b"", 8, 8, "image/webp", "webp", image=Image.new("RGB", (8, 8), "red")
            )
            path = store.write("main", video_frame)
            with Image.open(path) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (8, 8))


if __name__ == "__main__":
    unittest.main()

