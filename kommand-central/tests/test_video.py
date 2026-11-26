from io import BytesIO
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

import av
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / name / "src") for name in ("kommand-central", "kommand-source", "kommand-common")]

from fastapi.testclient import TestClient
from kommand_central.mpegts import VideoMuxer
from kommand_central.state import ChannelStore
from kommand_central.video import VideoConflict, VideoStreams
from kommand_central.web import create_app
from kommand_source.capture import CapturedFrame
from kommand_source.config import ChannelConfig, SourceConfig
from kommand_source.transport import CentralClient, UploadError
from kommand_source.transport.video import VideoEncoder


def frame(color: str, size: tuple[int, int] = (127, 125)) -> CapturedFrame:
    image = Image.new("RGB", size, color)
    return CapturedFrame(b"", *size, "image/webp", "webp", ("screen", 1), image)


class _Socket:
    def __init__(self, central: TestClient, url: str, sent: list[dict], payloads: list[bytes]):
        self.context = central.websocket_connect(url)
        self.socket = self.context.__enter__()
        self.sent = sent
        self.payloads = payloads
        self.closed = False

    def send(self, content):
        if isinstance(content, bytes):
            self.payloads.append(content)
            self.socket.send_bytes(content)
        else:
            self.sent.append(json.loads(content))
            self.socket.send_text(content)

    def recv(self, timeout=None):
        return json.dumps(self.socket.receive_json())

    def close(self):
        if not self.closed:
            self.closed = True
            self.context.__exit__(None, None, None)


class VideoIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = ChannelStore()
        self.central = TestClient(create_app(self.store))
        self.central.__enter__()
        self.addCleanup(self.central.__exit__, None, None, None)
        self.sent = []
        self.payloads = []
        self.connections = []

        def connect(url, **kwargs):
            connection = _Socket(self.central, url, self.sent, self.payloads)
            self.connections.append(connection)
            return connection

        self.patch = patch("kommand_source.transport.http.connect", side_effect=connect)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = CentralClient(SourceConfig(central_url="http://testserver"))
        self.addCleanup(self.client.close)
        self.channel = ChannelConfig(name="main")

    def test_persistent_packets_idle_heartbeats_and_lazy_preview(self):
        self.client.upload(self.channel, frame("red"))
        self.client.upload(self.channel, frame("blue"))
        self.assertEqual(len(self.connections), 1)
        self.assertEqual([item["sequence"] for item in self.sent], [0, 1])
        stored = self.store.get("source-1", "main")
        self.assertIsNone(stored._image)
        self.assertGreater(stored.image.getpixel((40, 40))[2], 200)
        self.assertEqual(stored.image.size, (127, 125))
        revision = stored.revision
        self.client.upload(self.channel, frame("blue"))
        self.client.upload(self.channel, frame("blue"))
        self.assertEqual(len(self.payloads), 2)
        self.assertEqual(self.sent[-1]["type"], "heartbeat")
        self.assertEqual(self.store.get("source-1", "main").revision, revision)

    def test_resize_and_failed_ack_start_new_streams(self):
        self.client.upload(self.channel, frame("red"))
        first_stream = self.sent[-1]["stream"]
        self.client.upload(self.channel, frame("blue", (64, 64)))
        self.assertNotEqual(self.sent[-1]["stream"], first_stream)
        self.assertEqual(self.sent[-1]["sequence"], 0)
        with patch.object(self.client._socket, "recv", side_effect=TimeoutError("timed out")):
            with self.assertRaises(UploadError):
                self.client.upload(self.channel, frame("green", (64, 64)))
        self.client.upload(self.channel, frame("blue", (64, 64)))
        self.assertEqual(self.sent[-1]["sequence"], 0)
        self.assertEqual(self.store.get("source-1", "main").image.size, (64, 64))

    def test_idle_viewer_join_and_decoder_reset_request_keyframes(self):
        self.client.upload(self.channel, frame("red"))
        with self.central.websocket_connect("/api/v1/sources/source-1/channels/main/video-stream") as viewer:
            self.client.upload(self.channel, frame("red"))
            self.client.upload(self.channel, frame("red"))
            metadata = viewer.receive_json()
            self.assertTrue(metadata["keyframe"])
            self.assertGreater(metadata["timestamp_us"], 0)
            self.assertEqual(viewer.receive_bytes(), self.payloads[-1])
            viewer.send_text("keyframe")
            self.assertEqual(viewer.receive_json(), {"type": "keyframe-requested"})
            self.client._last_heartbeat = float("-inf")
            self.client.upload(self.channel, frame("red"))
            self.client.upload(self.channel, frame("red"))
            self.assertEqual(viewer.receive_json()["type"], "heartbeat")
            self.assertTrue(viewer.receive_json()["keyframe"])
            self.assertEqual(viewer.receive_bytes(), self.payloads[-1])

    def test_mutable_capture_buffer_is_not_used_as_comparison_baseline(self):
        captured = frame("red")
        self.client.upload(self.channel, captured)
        captured.image.paste("blue", (0, 0, 127, 125))
        self.client.upload(self.channel, captured)
        self.assertEqual(self.sent[-1]["type"], "frame")
        self.assertGreater(self.store.get("source-1", "main").image.getpixel((40, 40))[2], 200)

    def test_periodic_keyframe_is_sent_even_when_idle(self):
        self.client.upload(self.channel, frame("red"))
        stream = self.sent[-1]["stream"]
        self.client._last_video_keyframe -= 61
        self.client.upload(self.channel, frame("red"))
        self.assertEqual(self.sent[-1]["stream"], stream)
        decoder = av.CodecContext.create("h264", "r")
        self.assertTrue(decoder.decode(av.Packet(self.payloads[0]))[0].key_frame)
        self.assertTrue(decoder.decode(av.Packet(self.payloads[1]))[0].key_frame)

    def test_out_of_order_and_invalid_metadata_are_rejected(self):
        self.client.upload(self.channel, frame("red"))
        metadata = dict(self.sent[-1], sequence=2, generation=self.client._generation)
        with self.central.websocket_connect("/api/v1/sources/source-1/channels/main/video-upload") as upload:
            upload.send_json(metadata)
            upload.send_bytes(self.payloads[-1])
            self.assertFalse(upload.receive_json()["accepted"])
        with self.central.websocket_connect("/api/v1/sources/source-1/channels/main/video-upload") as upload:
            upload.send_json(dict(metadata, width=True))
            self.assertFalse(upload.receive_json()["accepted"])
        self.client.upload(self.channel, frame("blue"))
        self.assertEqual(self.sent[-1]["sequence"], 1)

    def test_timestamped_remux_decodes_all_frames_without_reencoding(self):
        encoder = VideoEncoder(32, 32, 15, 80, 60)
        first = encoder.encode(Image.new("RGB", (32, 32), "red"))
        muxer = VideoMuxer(first, 1_000_000)
        self.addCleanup(muxer.close)
        output = [muxer.mux(first, 1_000_000, True)]
        for index in range(1, 6):
            encoded = encoder.encode(Image.new("RGB", (32, 32), "blue"))
            output.append(muxer.mux(encoded, 1_000_000 + index * 66_667, False))
        with av.open(BytesIO(b"".join(output)), format="mpegts") as container:
            frames = list(container.decode(video=0))
        self.assertEqual(len(frames), 6)
        self.assertGreater(frames[-1].pts, frames[0].pts)
        self.assertGreater(frames[-1].to_image().getpixel((8, 8))[2], 200)

    def test_native_keepalives_advance_clock_without_adding_pictures(self):
        encoder = VideoEncoder(32, 32, 15, 80, 60)
        first = encoder.encode(Image.new("RGB", (32, 32), "red"))
        muxer = VideoMuxer(first, 1_000_000)
        self.addCleanup(muxer.close)
        output = [muxer.mux(first, 1_000_000, True), muxer.keepalive(1_000_000)]
        for index in range(1, 6):
            output.append(muxer.keepalive(1_000_000 + index * 100_000))
        second = encoder.encode(Image.new("RGB", (32, 32), "blue"))
        output.extend((muxer.mux(second, 1_500_000, False), muxer.keepalive(1_500_000)))
        with av.open(BytesIO(b"".join(output)), format="mpegts") as container:
            packets = [packet for packet in container.demux(video=0) if packet.dts is not None]
            frames = []
            for packet in packets:
                content = bytes(packet)
                if any(
                    match.end() < len(content) and 1 <= content[match.end()] & 0x1f <= 5
                    for match in re.finditer(b"\x00\x00(?:\x00)?\x01", content)
                ):
                    frames.extend(packet.decode())
        self.assertEqual(len(frames), 2)
        self.assertGreater(frames[-1].to_image().getpixel((8, 8))[2], 200)
        self.assertGreater(frames[-1].pts * frames[-1].time_base, .49)
        self.assertTrue(all(a.dts < b.dts for a, b in zip(packets, packets[1:])))

    def test_heartbeats_preserve_idle_decoder_state(self):
        now = [0.0]
        streams = VideoStreams(self.store, max_pixels=1024, clock=lambda: now[0], idle_seconds=5)
        encoder = VideoEncoder(16, 16, 15, 80, 60)
        image = Image.new("RGB", (16, 16), "red")
        stored = streams.receive("source", "main", "a" * 32, 0, 16, 16, encoder.encode(image), "2026-01-01")
        now[0] = 4
        streams.touch("source", "main", stored.revision)
        now[0] = 5
        streams.expire()
        streams.receive("source", "main", "a" * 32, 1, 16, 16, encoder.encode(image), "2026-01-01")
        now[0] = 10
        streams.expire()
        with self.assertRaises(VideoConflict):
            streams.receive("source", "main", "a" * 32, 2, 16, 16, encoder.encode(image), "2026-01-01")
