"""Remux Annex-B H.264 into timestamped MPEG-TS without re-encoding."""

from fractions import Fraction
from io import BytesIO

import av


NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184


class _Output:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, content: bytes) -> int:
        self.chunks.append(bytes(content))
        return len(content)


class VideoMuxer:
    def __init__(self, keyframe: bytes, timestamp_us: int) -> None:
        self._output = _Output()
        self._origin = timestamp_us
        self._last_pts = -1
        self._container = av.open(
            self._output, "w", format="mpegts",
            options={
                "mpegts_flags": "resend_headers+initial_discontinuity",
                "flush_packets": "1",
                "omit_video_pes_length": "0",
            },
        )
        with av.open(BytesIO(keyframe), format="h264") as incoming:
            self._stream = self._container.add_stream_from_template(incoming.streams.video[0])

    def mux(self, content: bytes, timestamp_us: int, keyframe: bool) -> bytes:
        packet = av.Packet(content)
        packet.stream = self._stream
        packet.time_base = Fraction(1, 90_000)
        # Adjacent picture/keepalive packets can fall within the same MPEG-TS clock tick.
        pts = max((timestamp_us - self._origin) * 9 // 100, self._last_pts + 1)
        packet.pts = packet.dts = pts
        packet.is_keyframe = keyframe
        self._container.mux(packet)
        self._last_pts = pts
        result = b"".join(self._output.chunks)
        self._output.chunks.clear()
        return result

    def keepalive(self, timestamp_us: int) -> bytes:
        # A non-picture access unit flushes VLC's pending picture and advances PCR.
        content = bytearray(self.mux(b"\x00\x00\x00\x01\x0c\x80", timestamp_us, False))
        for offset in range(0, len(content), 188):
            if not content[offset + 1] & 0x40:
                continue
            payload = offset + 4
            if content[offset + 3] & 0x20:
                payload += content[payload] + 1
            if content[payload:payload + 4] != b"\x00\x00\x01\xe0":
                continue
            # Keep PCR, but don't let filler timestamps become the next picture's PTS.
            content[payload + 7] &= 0x3f
            length = content[payload + 8]
            content[payload + 9:payload + 9 + length] = b"\xff" * length
        return bytes(content)

    def close(self) -> None:
        self._container.close()
