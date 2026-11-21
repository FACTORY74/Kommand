"""Low-latency H.264 encoding for one channel."""

from fractions import Fraction

import av
from av.video.frame import PictureType
from PIL import Image


class VideoEncodingError(RuntimeError):
    pass


class VideoEncoder:
    def __init__(
        self, width: int, height: int, fps: int, quality: int, keyframe_seconds: int
    ) -> None:
        self.width = width
        self.height = height
        self.sequence = 0
        self.codec = av.CodecContext.create("libx264", "w")
        self.codec.width = width + width % 2
        self.codec.height = height + height % 2
        self.codec.pix_fmt = "yuv420p"
        # Avoid a large default thread pool for every channel.
        self.codec.thread_count = 2
        self.codec.time_base = Fraction(1, fps)
        self.codec.framerate = Fraction(fps, 1)
        self.codec.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "crf": str(round(51 * (1 - (quality / 100) ** 2))),
            "x264-params": f"repeat-headers=1:scenecut=0:keyint={fps * keyframe_seconds}",
        }
        self.codec.open()

    def encode(self, image: Image.Image, force_keyframe: bool = False) -> bytes:
        if image.size != (self.width, self.height):
            raise VideoEncodingError("Video frame dimensions changed.")
        if image.size != (self.codec.width, self.codec.height):
            padded = Image.new("RGB", (self.codec.width, self.codec.height))
            padded.paste(image)
            image = padded
        frame = av.VideoFrame.from_image(image)
        frame.pts = self.sequence
        if force_keyframe:
            frame.pict_type = PictureType.I
        packets = self.codec.encode(frame)
        if len(packets) != 1 or (
            (self.sequence == 0 or force_keyframe) and not packets[0].is_keyframe
        ):
            raise VideoEncodingError("The H.264 encoder did not produce the expected frame.")
        self.sequence += 1
        return bytes(packets[0])
