from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kommand_source.config import (  # noqa: E402
    ChannelConfig,
    SourceConfig,
    load_config,
    save_config,
)


class ConfigTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        expected = SourceConfig(
            source_name="desk-1",
            central_url="https://central.example",
            channels=[ChannelConfig(name="primary")],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(path, expected)
            actual = load_config(path)
        self.assertEqual(actual, expected)

    def test_duplicate_channels_are_rejected(self) -> None:
        config = SourceConfig(
            channels=[
                ChannelConfig(name="same"),
                ChannelConfig(name="same"),
            ]
        )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            config.validate()

    def test_unsafe_channel_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ChannelConfig(name="../escape").validate()

    def test_legacy_display_name_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                '{"channels":[{"name":"main","display_name":"Old label"}]}',
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(config.channels, [ChannelConfig(name="main")])

    def test_legacy_formats_migrate_to_webp_and_default_full_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                '{"channels":[{"name":"main","image_format":"JPEG"}]}',
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(config.channels[0].full_frame_interval_seconds, 60)

    def test_full_frame_interval_is_validated_and_persisted(self) -> None:
        with self.assertRaisesRegex(ValueError, "Full frame interval"):
            ChannelConfig(name="main", full_frame_interval_seconds=0).validate()
        expected = SourceConfig(channels=[ChannelConfig(name="main", full_frame_interval_seconds=15)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(path, expected)
            self.assertEqual(load_config(path), expected)

    def test_format_and_video_fps_are_validated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in ("webp", "webp_patch", "video"):
                expected = SourceConfig(channels=[ChannelConfig(name="main", format=value, video_fps=24)])
                save_config(path, expected)
                self.assertEqual(load_config(path), expected)
        with self.assertRaisesRegex(ValueError, "Format"):
            ChannelConfig(name="main", format="invalid").validate()
        for fps in (0, 61, True):
            with self.assertRaisesRegex(ValueError, "Video FPS"):
                ChannelConfig(name="main", video_fps=fps).validate()

    def test_window_identity_is_validated_and_persisted(self) -> None:
        channel = ChannelConfig(
            name="browser", capture_type="window", capture_target="Video",
            window_handle=42, window_process_id=7, window_process_start=99,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(path, SourceConfig(channels=[channel]))
            self.assertEqual(load_config(path).channels, [channel])
        with self.assertRaisesRegex(ValueError, "Window identity"):
            ChannelConfig(name="bad", capture_type="window", window_handle=42).validate()


if __name__ == "__main__":
    unittest.main()

