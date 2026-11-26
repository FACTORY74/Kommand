from io import BytesIO
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

import av
import requests
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "kommand-central" / "src"),
    str(ROOT / "kommand-source" / "src"),
    str(ROOT / "kommand-common" / "src"),
]

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402
from kommand_central.state import ChannelStore  # noqa: E402
from kommand_central.web import create_app  # noqa: E402
from kommand_source.capture import CapturedFrame  # noqa: E402
from kommand_source.capture.mss_provider import MssCaptureProvider  # noqa: E402
from kommand_source.config import ChannelConfig, SourceConfig  # noqa: E402
from kommand_source.transport import CentralClient, UploadError  # noqa: E402
from kommand_source.transport.video import VideoEncoder  # noqa: E402
from kommand_central.video import VideoConflict, VideoStreams  # noqa: E402


def frame(image: Image.Image, alignment: tuple[object, ...] = ("screen", 1)) -> CapturedFrame:
    output = BytesIO()
    image.save(output, format="WEBP", lossless=True)
    return CapturedFrame(output.getvalue(), image.width, image.height, "image/webp", "webp", alignment)


class TestSession:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.requests: list[tuple[str, int]] = []
        self.headers: list[dict[str, str]] = []
        self.payloads: list[bytes] = []

    def post(self, url: str, **kwargs):
        kwargs.pop("timeout")
        files = kwargs.get("files")
        self.requests.append((url.rsplit("/", 1)[-1], len(files["frame"][1]) if files else 0))
        self.headers.append(dict(kwargs["headers"]))
        self.payloads.append(files["frame"][1] if files else b"")
        response = self.client.post(url, **kwargs)

        class Response:
            def raise_for_status(self) -> None:
                if response.status_code >= 400:
                    raise requests.HTTPError(str(response.status_code))

            def json(self):
                return response.json()

        return Response()

    def close(self) -> None:
        pass


class PatchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ChannelStore()
        self.app = create_app(self.store)
        self.session = TestSession(TestClient(self.app))
        self.client = CentralClient(SourceConfig(central_url="http://testserver"))
        post_patch = patch.object(self.client.session, "post", side_effect=self.session.post)
        post_patch.start()
        self.addCleanup(post_patch.stop)
        self.addCleanup(self.client.close)
        self.channel = ChannelConfig(name="main")
        random_bytes = random.Random(42).randbytes(128 * 128 * 3)
        self.image = Image.frombytes("RGB", (128, 128), random_bytes)

    def assert_central_image(self, expected: Image.Image) -> None:
        stored = self.store.get("source-1", "main")
        self.assertIsNotNone(stored)
        assert stored is not None
        with Image.open(BytesIO(stored.content)) as actual:
            self.assertIsNone(ImageChops.difference(actual.convert("RGB"), expected).getbbox())

    def test_patch_heartbeat_alignment_and_periodic_full(self) -> None:
        first = frame(self.image)
        with patch("kommand_source.transport.http.time.monotonic", return_value=100):
            self.client.upload(self.channel, first)
            changed = self.image.copy()
            changed.paste((255, 0, 0), (10, 10, 20, 20))
            self.client.upload(self.channel, frame(changed))
            self.assertEqual(self.session.requests[-1][0], "patches")
            self.assertLess(self.session.requests[-1][1], len(first.content))
            self.assert_central_image(changed)
            self.client.upload(self.channel, frame(changed))
            self.assertEqual(self.session.requests[-1][0], "heartbeat")
            self.client.upload(self.channel, frame(changed, ("screen", 2)))
            self.assertEqual(self.session.requests[-1][0], "frames")
        with patch("kommand_source.transport.http.time.monotonic", return_value=160):
            self.client.upload(self.channel, frame(changed, ("screen", 2)))
            self.assertEqual(self.session.requests[-1][0], "frames")

    def test_server_restart_forces_resynchronisation(self) -> None:
        self.client.upload(self.channel, frame(self.image))
        self.session.client = TestClient(create_app(self.store))
        with self.assertRaises(UploadError):
            self.client.upload(self.channel, frame(self.image))
        self.client.upload(self.channel, frame(self.image))
        self.assertEqual(self.session.requests[-1][0], "frames")
        self.assert_central_image(self.image)

    def test_changed_dimensions_force_full_frame(self) -> None:
        self.client.upload(self.channel, frame(self.image))
        smaller = self.image.crop((0, 0, 64, 64))
        self.client.upload(self.channel, frame(smaller))
        self.assertEqual(self.session.requests[-1][0], "frames")
        self.assert_central_image(smaller)

    def test_lossy_capture_patch_reconstructs_the_encoded_image(self) -> None:
        initial = MssCaptureProvider._encode(self.image, 80, ("screen", 1))
        self.client.upload(self.channel, initial)
        first_frame = self.store.get("source-1", "main")
        assert first_frame is not None
        initial_corner = first_frame.image.getpixel((0, 0))
        changed = self.image.copy()
        changed.paste((255, 0, 0), (10, 10, 20, 20))
        updated = MssCaptureProvider._encode(changed, 80, ("screen", 1))
        self.client.upload(self.channel, updated)
        self.assertEqual(self.session.requests[-1][0], "patches")
        stored = self.store.get("source-1", "main")
        assert stored is not None
        self.assertEqual(stored.image.getpixel((15, 15)), (255, 0, 0))
        self.assertEqual(stored.image.getpixel((0, 0)), initial_corner)

    def test_failed_upload_forces_next_full_frame(self) -> None:
        self.client.upload(self.channel, frame(self.image))
        with patch.object(self.client.session, "post", side_effect=requests.Timeout("timed out")):
            with self.assertRaises(UploadError):
                self.client.upload(self.channel, frame(self.image))
        self.client.upload(self.channel, frame(self.image))
        self.assertEqual(self.session.requests[-1][0], "frames")

    def test_reused_capture_buffer_does_not_change_patch_baseline(self) -> None:
        self.client.upload(self.channel, MssCaptureProvider._encode(self.image, 80, ("screen", 1)))
        self.image.paste((255, 0, 0), (10, 10, 20, 20))
        self.client.upload(self.channel, MssCaptureProvider._encode(self.image, 80, ("screen", 1)))
        self.assertEqual(self.session.requests[-1][0], "patches")
        stored = self.store.get("source-1", "main")
        assert stored is not None
        self.assertEqual(stored.image.getpixel((15, 15)), (255, 0, 0))

    def test_sends_full_frame_when_patch_is_larger(self) -> None:
        self.client.upload(self.channel, MssCaptureProvider._encode(self.image, 80, ("screen", 1)))
        changed = Image.frombytes("RGB", (128, 128), random.Random(43).randbytes(128 * 128 * 3))
        self.client.upload(self.channel, MssCaptureProvider._encode(changed, 80, ("screen", 1)))
        self.assertEqual(self.session.requests[-1][0], "frames")

    def test_switching_channels_starts_with_full_frame(self) -> None:
        captured = frame(self.image)
        self.client.upload(self.channel, captured)
        self.client.upload(ChannelConfig(name="other"), captured)
        self.assertEqual(self.session.requests[-1][0], "frames")
        self.assertIsNotNone(self.store.get("source-1", "other"))

    def test_webp_format_sends_a_full_frame_even_when_unchanged(self) -> None:
        channel = ChannelConfig(name="main", format="webp")
        self.client.upload(channel, frame(self.image))
        self.client.upload(channel, frame(self.image))
        self.assertEqual([item[0] for item in self.session.requests], ["frames", "frames"])

    def test_video_streams_frames_and_restarts_after_resize_or_failure(self) -> None:
        channel = ChannelConfig(name="main", format="video", video_fps=15)
        red = Image.new("RGB", (127, 125), "red")
        blue = Image.new("RGB", red.size, "blue")
        self.client.upload(channel, frame(red))
        stream_id = self.session.headers[-1]["X-Kommand-Video-Stream"]
        self.client.upload(channel, frame(blue))
        self.assertEqual(self.session.headers[-1]["X-Kommand-Video-Stream"], stream_id)
        self.assertEqual(self.session.headers[-1]["X-Kommand-Video-Sequence"], "1")
        stored = self.store.get("source-1", "main")
        assert stored is not None
        self.assertEqual(stored.image.size, (127, 125))
        self.assertGreater(stored.image.getpixel((40, 40))[2], 200)
        self.assertTrue(stored.streaming)
        self.client.upload(channel, frame(blue, ("screen", 2)))
        self.assertNotEqual(self.session.headers[-1]["X-Kommand-Video-Stream"], stream_id)
        self.assertEqual(self.session.headers[-1]["X-Kommand-Video-Sequence"], "0")
        resized = Image.new("RGB", (64, 64), "green")
        self.client.upload(channel, frame(resized))
        self.assertNotEqual(self.session.headers[-1]["X-Kommand-Video-Stream"], stream_id)
        self.assertEqual(self.session.headers[-1]["X-Kommand-Video-Sequence"], "0")
        with patch.object(self.client.session, "post", side_effect=requests.Timeout("timed out")):
            with self.assertRaises(UploadError):
                self.client.upload(channel, frame(resized))
        self.client.upload(channel, frame(resized))
        self.assertEqual(self.session.headers[-1]["X-Kommand-Video-Sequence"], "0")
        stored = self.store.get("source-1", "main")
        assert stored is not None
        self.assertEqual(stored.image.size, (64, 64))

    def test_video_keyframe_interval_uses_elapsed_time(self) -> None:
        channel = ChannelConfig(name="main", format="video", video_fps=15, full_frame_interval_seconds=1)
        with patch("kommand_source.transport.http.time.monotonic", return_value=100):
            self.client.upload(channel, frame(self.image))
        with patch("kommand_source.transport.http.time.monotonic", return_value=101):
            self.client.upload(channel, frame(self.image))
        self.assertEqual(self.session.headers[0]["X-Kommand-Video-Stream"], self.session.headers[1]["X-Kommand-Video-Stream"])
        decoder = av.CodecContext.create("h264", "r")
        first = decoder.decode(av.Packet(self.session.payloads[0]))
        second = decoder.decode(av.Packet(self.session.payloads[1]))
        self.assertTrue(first[0].key_frame)
        self.assertTrue(second[0].key_frame)

    def test_browser_join_requests_keyframe_without_waiting_for_periodic_interval(self) -> None:
        channel = ChannelConfig(name="main", format="video", full_frame_interval_seconds=60)
        with self.session.client as central:
            with patch("kommand_source.transport.http.time.monotonic", return_value=100):
                self.client.upload(channel, frame(self.image))
                with central.websocket_connect("/api/v1/sources/source-1/channels/main/video-stream") as socket:
                    self.client.upload(channel, frame(self.image))
                    self.client.upload(channel, frame(self.image))
                    decoder = av.CodecContext.create("h264", "r")
                    decoded = [decoder.decode(av.Packet(packet))[0] for packet in self.session.payloads]
                    self.assertTrue(decoded[-1].key_frame)
                    self.assertEqual(self.session.headers[-1]["X-Kommand-Video-Sequence"], "2")
                    self.assertTrue(socket.receive_json()["keyframe"])
                    self.assertEqual(socket.receive_bytes(), self.session.payloads[-1])
                    socket.send_text("keyframe")
                    self.assertEqual(socket.receive_json(), {"type": "keyframe-requested"})
                    self.client.upload(channel, frame(self.image))
                    self.client.upload(channel, frame(self.image))
                    self.assertFalse(decoder.decode(av.Packet(self.session.payloads[-2]))[0].key_frame)
                    self.assertTrue(decoder.decode(av.Packet(self.session.payloads[-1]))[0].key_frame)
                    socket.receive_json()
                    socket.receive_bytes()
                    self.assertTrue(socket.receive_json()["keyframe"])
                    self.assertEqual(socket.receive_bytes(), self.session.payloads[-1])

    def test_browser_video_receives_only_accepted_original_packets(self) -> None:
        encoder = VideoEncoder(16, 16, 15, 80, 60)
        image = Image.new("RGB", (16, 16), "red")
        headers = {
            "X-Kommand-Protocol": "1",
            "X-Kommand-Captured-At": "2026-01-01T00:00:00Z",
            "X-Kommand-Width": "16",
            "X-Kommand-Height": "16",
            "X-Kommand-Video-Stream": "a" * 32,
            "X-Kommand-Video-Sequence": "0",
        }
        with TestClient(create_app(ChannelStore())) as client:
            with client.websocket_connect("/api/v1/sources/source-1/channels/main/video-stream") as socket:
                first = encoder.encode(image)
                response = client.post(
                    "/api/v1/sources/source-1/channels/main/video", headers=headers,
                    files={"frame": ("frame.h264", first, "video/h264")},
                )
                self.assertEqual(response.status_code, 202)
                self.assertEqual(socket.receive_json(), {
                    "keyframe": True, "stream": "a" * 32, "width": 16, "height": 16,
                    "sequence": 0, "revision": response.json()["revision"],
                })
                self.assertEqual(socket.receive_bytes(), first)
                headers["X-Kommand-Video-Sequence"] = "1"
                headers["X-Kommand-Generation"] = response.json()["generation"]
                second = encoder.encode(image)
                response = client.post(
                    "/api/v1/sources/source-1/channels/main/video", headers=headers,
                    files={"frame": ("frame.h264", second, "video/h264")},
                )
                self.assertEqual(response.status_code, 202)
                self.assertFalse(socket.receive_json()["keyframe"])
                self.assertEqual(socket.receive_bytes(), second)
                response = client.post(
                    "/api/v1/sources/source-1/channels/main/frames",
                    headers={key: value for key, value in headers.items()
                             if key in {"X-Kommand-Protocol", "X-Kommand-Captured-At",
                                        "X-Kommand-Width", "X-Kommand-Height"}},
                    files={"frame": ("frame.webp", frame(image).content, "image/webp")},
                )
                self.assertEqual(response.status_code, 202)
                with self.assertRaises(WebSocketDisconnect):
                    socket.receive_json()

    def test_video_rejects_out_of_order_packets_and_recovers_after_restart(self) -> None:
        channel = ChannelConfig(name="main", format="video")
        self.client.upload(channel, frame(self.image))
        headers = self.session.headers[-1].copy()
        headers["X-Kommand-Video-Sequence"] = "2"
        assert self.client._generation is not None
        headers["X-Kommand-Generation"] = self.client._generation
        response = self.session.client.post(
            "/api/v1/sources/source-1/channels/main/video", headers=headers,
            files={"frame": ("frame.h264", self.session.payloads[-1], "video/h264")},
        )
        self.assertEqual(response.status_code, 409)
        self.client.upload(channel, frame(self.image))
        self.assertEqual(self.session.headers[-1]["X-Kommand-Video-Sequence"], "1")
        self.session.client = TestClient(create_app(self.store))
        with self.assertRaises(UploadError):
            self.client.upload(channel, frame(self.image))
        self.client.upload(channel, frame(self.image))
        self.assertEqual(self.session.headers[-1]["X-Kommand-Video-Sequence"], "0")

    def test_video_decoder_expiration_requires_a_new_stream(self) -> None:
        now = [0.0]
        streams = VideoStreams(self.store, max_pixels=1024, clock=lambda: now[0], idle_seconds=5)
        encoder = VideoEncoder(16, 16, 15, 80, 60)
        image = Image.new("RGB", (16, 16), "red")
        streams.receive("source", "main", "a" * 32, 0, 16, 16, encoder.encode(image), "2026-01-01")
        now[0] = 5
        streams.expire()
        with self.assertRaises(VideoConflict):
            streams.receive("source", "main", "a" * 32, 1, 16, 16, encoder.encode(image), "2026-01-01")

    def test_video_rejects_bad_data_and_enforces_decoder_pixel_limit(self) -> None:
        from kommand_central import web

        headers = {
            "X-Kommand-Protocol": "1",
            "X-Kommand-Captured-At": "2026-01-01T00:00:00Z",
            "X-Kommand-Width": "16",
            "X-Kommand-Height": "16",
            "X-Kommand-Video-Stream": "a" * 32,
            "X-Kommand-Video-Sequence": "0",
        }
        url = "/api/v1/sources/source-1/channels/main/video"
        bad = self.session.client.post(
            url, headers=headers, files={"frame": ("frame.h264", b"bad", "video/h264")}
        )
        self.assertEqual(bad.status_code, 400)
        encoder = VideoEncoder(32, 32, 15, 80, 60)
        oversized = encoder.encode(Image.new("RGB", (32, 32), "red"))
        with patch.object(web, "MAX_FRAME_PIXELS", 256):
            limited_client = TestClient(create_app(ChannelStore()))
            response = limited_client.post(
                url, headers=headers, files={"frame": ("frame.h264", oversized, "video/h264")}
            )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
