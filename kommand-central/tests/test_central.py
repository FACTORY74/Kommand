from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "kommand-central" / "src"),
    str(ROOT / "kommand-common" / "src"),
]

from fastapi.testclient import TestClient  # noqa: E402

from kommand_central.bandwidth import ReceiveBandwidth  # noqa: E402
from kommand_central.state import ChannelStore  # noqa: E402
from kommand_central.layout import LayoutStore, Viewport  # noqa: E402
from kommand_central.web import create_app  # noqa: E402


def image_bytes(image_format: str = "WEBP", size: tuple[int, int] = (2, 1)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size).save(output, format=image_format)
    return output.getvalue()


class CentralTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ChannelStore()
        self.bandwidth = ReceiveBandwidth()
        self.client = TestClient(create_app(self.store, receive_bandwidth=self.bandwidth))

    def test_upload_list_and_retrieve_frame(self) -> None:
        captured_at = datetime.now(timezone.utc).isoformat()
        content = image_bytes()
        response = self.client.post(
            "/api/v1/sources/source-1/channels/main/frames",
            headers={
                "X-Kommand-Protocol": "1",
                "X-Kommand-Captured-At": captured_at,
                "X-Kommand-Width": "2",
                "X-Kommand-Height": "1",
            },
            files={"frame": ("main.webp", content, "image/webp")},
        )
        self.assertEqual(response.status_code, 202)
        listing = self.client.get("/api/v1/channels").json()["channels"]
        self.assertEqual(listing[0]["channel"], "main")
        image = self.client.get(listing[0]["image_url"])
        self.assertEqual(image.content, content)
        self.assertEqual(image.headers["content-type"], "image/webp")

    def test_inbound_bandwidth_counts_upload_request_body(self) -> None:
        content = image_bytes()
        before = self.bandwidth.bytes_in_window

        response = self._upload(content, "image/webp", 2, 1)

        self.assertEqual(response.status_code, 202)
        self.assertGreater(self.bandwidth.bytes_in_window - before, len(content))

    def test_rejects_non_webp_image_formats(self) -> None:
        for image_format, media_type in (
            ("JPEG", "image/jpeg"),
            ("PNG", "image/png"),
        ):
            with self.subTest(image_format=image_format):
                response = self._upload(image_bytes(image_format), media_type, 2, 1)
                self.assertEqual(response.status_code, 415)

    def test_rejects_corrupt_image(self) -> None:
        response = self._upload(b"not an image", "image/webp", 2, 1)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.store.get("source-1", "main"))

    def test_rejects_empty_and_truncated_images(self) -> None:
        content = image_bytes()
        empty = self._upload(b"", "image/webp", 2, 1)
        truncated = self._upload(content[:-5], "image/webp", 2, 1)
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(truncated.status_code, 400)

    def test_rejects_image_type_and_dimension_mismatches(self) -> None:
        content = image_bytes("PNG")
        wrong_type = self._upload(content, "image/webp", 2, 1)
        wrong_dimensions = self._upload(image_bytes(), "image/webp", 1, 2)
        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(wrong_dimensions.status_code, 400)

    def test_applies_patch_and_rejects_stale_base(self) -> None:
        initial = self._upload(image_bytes(), "image/webp", 2, 1).json()
        output = BytesIO()
        Image.new("RGB", (1, 1), (255, 0, 0)).save(output, "WEBP", lossless=True)
        headers = {
            "X-Kommand-Protocol": "1",
            "X-Kommand-Captured-At": datetime.now(timezone.utc).isoformat(),
            "X-Kommand-Width": "1",
            "X-Kommand-Height": "1",
            "X-Kommand-X": "1",
            "X-Kommand-Y": "0",
            "X-Kommand-Base-Revision": str(initial["revision"]),
            "X-Kommand-Generation": initial["generation"],
        }
        url = "/api/v1/sources/source-1/channels/main/patches"
        files = {"frame": ("patch.webp", output.getvalue(), "image/webp")}
        response = self.client.post(url, headers=headers, files=files)
        self.assertEqual(response.status_code, 202)
        with Image.open(BytesIO(self.store.get("source-1", "main").content)) as image:
            self.assertEqual(image.getpixel((1, 0)), (255, 0, 0))
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))
        stale = self.client.post(url, headers=headers, files=files)
        self.assertEqual(stale.status_code, 409)
        headers["X-Kommand-Base-Revision"] = str(response.json()["revision"])
        headers["X-Kommand-X"] = "2"
        out_of_bounds = self.client.post(url, headers=headers, files=files)
        self.assertEqual(out_of_bounds.status_code, 409)
        self.assertEqual(self.store.get("source-1", "main").revision, response.json()["revision"])

    def test_patch_does_not_encode_the_complete_image(self) -> None:
        initial = self._upload(image_bytes(), "image/webp", 2, 1).json()
        with patch.object(Image.Image, "save", side_effect=AssertionError("unexpected full encode")):
            updated = self.store.apply_patch(
                "source-1", "main", initial["revision"], 1, 0,
                Image.new("RGB", (1, 1), "red"), datetime.now(timezone.utc).isoformat(),
            )
        self.assertEqual(updated.image.getpixel((1, 0)), (255, 0, 0))
        with Image.open(BytesIO(updated.content)) as encoded:
            self.assertEqual(encoded.convert("RGB").getpixel((1, 0)), (255, 0, 0))

    def test_heartbeat_refreshes_channel_without_changing_frame_revision(self) -> None:
        initial = self._upload(image_bytes(), "image/webp", 2, 1).json()
        captured_at = "2026-01-02T03:04:05+00:00"
        response = self.client.post(
            "/api/v1/sources/source-1/channels/main/heartbeat",
            headers={
                "X-Kommand-Protocol": "1",
                "X-Kommand-Captured-At": captured_at,
                "X-Kommand-Base-Revision": str(initial["revision"]),
                "X-Kommand-Generation": initial["generation"],
            },
        )
        self.assertEqual(response.status_code, 200)
        frame = self.store.get("source-1", "main")
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.captured_at, captured_at)
        self.assertEqual(frame.revision, initial["revision"])

    def test_rejects_images_over_pixel_limit(self) -> None:
        from kommand_central import web

        with patch.object(web, "MAX_FRAME_PIXELS", 1):
            response = self._upload(image_bytes(), "image/webp", 1, 1)
        self.assertEqual(response.status_code, 413)

    def test_treats_pillow_decompression_warning_as_rejection(self) -> None:
        with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            response = self._upload(image_bytes(), "image/webp", 2, 1)
        self.assertEqual(response.status_code, 413)

    def _upload(self, content: bytes, media_type: str, width: int, height: int):
        return self.client.post(
            "/api/v1/sources/source-1/channels/main/frames",
            headers={
                "X-Kommand-Protocol": "1",
                "X-Kommand-Captured-At": datetime.now(timezone.utc).isoformat(),
                "X-Kommand-Width": str(width),
                "X-Kommand-Height": str(height),
            },
            files={"frame": ("frame", content, media_type)},
        )

    def test_rejects_wrong_protocol(self) -> None:
        response = self.client.post(
            "/api/v1/sources/s/channels/c/frames",
            headers={
                "X-Kommand-Protocol": "99",
                "X-Kommand-Captured-At": datetime.now(timezone.utc).isoformat(),
                "X-Kommand-Width": "1",
                "X-Kommand-Height": "1",
            },
            files={"frame": ("x.png", b"x", "image/png")},
        )
        self.assertEqual(response.status_code, 400)

    def test_recursive_layout_and_assignment(self) -> None:
        layouts = LayoutStore()
        layouts.resize(1, 2)
        layouts.nest((1,), 2, 1)
        layouts.assign((0,), "source/main")
        layouts.assign((1, 0), "source/detail")
        snapshot = layouts.snapshot()
        self.assertEqual((snapshot["rows"], snapshot["columns"]), (1, 2))
        self.assertEqual(snapshot["children"][0]["channel"], "source/main")
        self.assertEqual(snapshot["children"][1]["type"], "grid")
        self.assertEqual(snapshot["children"][1]["children"][0]["channel"], "source/detail")

    def test_root_resize_preserves_overlapping_panels(self) -> None:
        layouts = LayoutStore()
        layouts.resize(3, 3)
        layouts.assign((0,), "source/top-left")
        layouts.assign((2,), "source/removed-right")
        layouts.assign((3,), "source/middle-left")
        layouts.nest((4,), 1, 2)
        layouts.assign((4, 1), "source/nested")
        layouts.assign((6,), "source/removed-bottom")

        layouts.resize(2, 2)
        snapshot = layouts.snapshot()
        self.assertEqual(
            [child.get("channel") for child in snapshot["children"][:3]],
            ["source/top-left", None, "source/middle-left"],
        )
        self.assertEqual(snapshot["children"][3]["type"], "grid")
        self.assertEqual(
            snapshot["children"][3]["children"][1]["channel"], "source/nested"
        )

        layouts.resize(3, 3)
        expanded = layouts.snapshot()
        self.assertEqual(expanded["children"][4]["type"], "grid")
        self.assertIsNone(expanded["children"][2]["channel"])
        self.assertIsNone(expanded["children"][6]["channel"])

    def test_move_and_swap_nested_panels_are_atomic_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            layouts = LayoutStore(path)
            layouts.nest((1,), 1, 2)
            layouts.assign((0,), "source/main")
            layouts.assign((1, 1), "source/other")

            layouts.move_or_swap((0,), (1, 0), layouts.revision)
            self.assertEqual(
                [slot.channel for _, slot in layouts.leaves()],
                [None, "source/main", "source/other", None, None],
            )
            layouts.move_or_swap((1, 0), (1, 1), layouts.revision)
            self.assertEqual(
                [slot.channel for _, slot in layouts.leaves()],
                [None, "source/other", "source/main", None, None],
            )
            self.assertEqual(LayoutStore(path).snapshot(), layouts.snapshot())

            revision = layouts.revision
            layouts.move_or_swap((1, 0), (1, 0), revision)
            self.assertEqual(layouts.revision, revision)
            with self.assertRaises(ValueError):
                layouts.move_or_swap((0,), (1, 0), revision)
            with self.assertRaises(ValueError):
                layouts.move_or_swap((1, 0), (1,), revision)
            with self.assertRaises(ValueError):
                layouts.move_or_swap((1, 0), (1, 1), revision - 1)
            self.assertEqual(layouts.revision, revision)

            snapshot = layouts.snapshot()
            with patch.object(layouts, "_save", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    layouts.move_or_swap((1, 0), (1, 1), revision)
            self.assertEqual(layouts.snapshot(), snapshot)
            self.assertEqual(layouts.revision, revision)
            self.assertEqual(LayoutStore(path).snapshot(), snapshot)

    def test_panel_views_load_persist_and_remain_with_their_panels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            path.write_text(
                '{"type":"grid","rows":1,"columns":2,"children":'
                '[{"type":"slot","channel":"source/main"},{"type":"slot","channel":null}]}',
                encoding="utf-8",
            )
            layouts = LayoutStore(path)
            self.assertEqual(layouts.leaves()[0][1].view, Viewport())
            view = Viewport(2.0, 0.1, -0.2)
            revision = layouts.revision
            layouts.set_view((0,), view, revision)
            self.assertEqual(LayoutStore(path).leaves()[0][1].view, view)
            layouts.move_or_swap((0,), (1,), layouts.revision)
            leaves = layouts.leaves()
            self.assertEqual((leaves[0][1].channel, leaves[0][1].view), (None, view))
            self.assertEqual((leaves[1][1].channel, leaves[1][1].view), ("source/main", Viewport()))
            snapshot = layouts.snapshot()
            revision = layouts.revision
            with patch.object(layouts, "_save", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    layouts.set_view((1,), view, revision)
            self.assertEqual(layouts.snapshot(), snapshot)
            self.assertEqual(layouts.revision, revision)
            self.assertEqual(LayoutStore(path).snapshot(), snapshot)
            with self.assertRaises(ValueError):
                layouts.set_view((1,), view, revision - 1)
            for value in (float("nan"), 0.0, 0.5, 17.0):
                with self.assertRaises(ValueError):
                    Viewport(zoom=value)

    def test_layout_api_returns_grid(self) -> None:
        response = self.client.get("/api/v1/layout")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["layout"]["type"], "grid")

    def test_panel_view_save_retries_temporary_windows_replacement_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            layouts = LayoutStore(path)
            layouts.assign((0,), "source/main")
            view = Viewport(2.0, 0.1, -0.2)
            error = PermissionError("Temporary Windows lock")
            error.winerror = 5
            replace = Path.replace
            attempts = []

            def locked_replace(temporary, destination):
                attempts.append(temporary)
                if len(attempts) < 3:
                    raise error
                return replace(temporary, destination)

            with patch.object(Path, "replace", autospec=True, side_effect=locked_replace):
                with patch("kommand_central.layout.time.sleep") as sleep:
                    layouts.set_view((0,), view, layouts.revision)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(LayoutStore(path).leaves()[0][1].view, view)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_panel_view_save_failure_preserves_disk_and_memory_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            layouts = LayoutStore(path)
            layouts.assign((0,), "source/main")
            snapshot = layouts.snapshot()
            revision = layouts.revision
            for windows_error, expected_attempts in ((5, 6), (32, 6), (33, 6), (None, 1)):
                with self.subTest(windows_error=windows_error):
                    error = PermissionError("Access denied")
                    if windows_error is not None:
                        error.winerror = windows_error
                    with patch.object(Path, "replace", side_effect=error) as replace:
                        with patch("kommand_central.layout.time.sleep") as sleep:
                            with self.assertRaises(PermissionError):
                                layouts.set_view((0,), Viewport(2.0), revision)
                    self.assertEqual(replace.call_count, expected_attempts)
                    self.assertEqual(sleep.call_count, expected_attempts - 1)
                    self.assertEqual(layouts.snapshot(), snapshot)
                    self.assertEqual(layouts.revision, revision)
                    self.assertEqual(LayoutStore(path).snapshot(), snapshot)
                    self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_existing_zoomed_out_views_migrate_to_fit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            path.write_text(
                '{"type":"grid","rows":1,"columns":2,"children":['
                '{"type":"slot","channel":"source/main",'
                '"view":{"zoom":0.25,"pan_x":0.1,"pan_y":-0.2}},'
                '{"type":"slot","channel":"source/other",'
                '"view":{"zoom":2,"pan_x":0.3,"pan_y":0.4}}]}',
                encoding="utf-8",
            )
            layouts = LayoutStore(path)
            self.assertEqual(layouts.leaves()[0][1].view, Viewport(1.0, 0.1, -0.2))
            self.assertEqual(layouts.leaves()[1][1].view, Viewport(2.0, 0.3, 0.4))
            layouts.assign((0,), "source/main")
            self.assertEqual(LayoutStore(path).snapshot(), layouts.snapshot())


if __name__ == "__main__":
    unittest.main()
