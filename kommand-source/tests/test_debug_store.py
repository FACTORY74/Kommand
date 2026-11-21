from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kommand_source.capture import CapturedFrame  # noqa: E402
from kommand_source.debug_store import DebugStore  # noqa: E402


def frame(content: bytes, extension: str = "jpg") -> CapturedFrame:
    return CapturedFrame(
        content=content,
        width=1,
        height=1,
        media_type=f"image/{extension}",
        extension=extension,
    )


class DebugStoreTests(unittest.TestCase):
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

