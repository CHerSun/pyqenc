"""End-to-end tests for the intent command surface (2026-10-05 cli-intent-commands).

Execute runs over the real sample: `auto` (product + reuse replay), `video`
(no audio work), `audio` (never touches the video stream), the two-pass
composition, and `extract` materialization with byte-verification against a
mkvextract reference. The speed rule applies — ultrafast presets only.
"""

# CHerSun 2026

import json
import subprocess
from pathlib import Path

import pytest

from pyqenc import api
from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.constants import EXTRACTED_DIR, TIMESTAMPS_FILENAME
from tests.fixtures.video_fixtures import get_sample_video_path, sample_video_exists


def _make_config(*, targets: list[str] | None = None) -> AppConfig:
    """Default config with the e2e strategy override and optional targets."""
    config_dict = load_app_config(default_only=True).model_dump()
    config_dict["encoding"]["strategies"] = ["h265+ultrafast"]
    if targets is not None:
        config_dict["encoding"]["targets"] = targets
    return AppConfig.model_validate(config_dict)


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
@pytest.mark.slow
class TestAutoEndToEnd:
    def test_execute_produces_outputs_and_reuse_replays(self, tmp_path: Path) -> None:
        config = _make_config(targets=["vmaf-med:70.0"])
        source = get_sample_video_path()
        work = tmp_path / "work"

        first = api.run_pipeline(
            config, config.resolve_encoding(), source, work,
            no_metrics=True, dry_run=False,
        )
        assert first.success, first.error
        assert first.output_files, "auto must produce merged outputs"
        for path in first.output_files:
            assert path.exists() and path.stat().st_size > 0
        audio_dir = work / "audio"
        assert audio_dir.is_dir() and any(audio_dir.iterdir()), \
            "auto must process audio (default chains)"

        second = api.run_pipeline(
            config, config.resolve_encoding(), source, work,
            no_metrics=True, dry_run=False,
        )
        assert second.success, second.error
        assert second.phases_executed == [], "a re-run over complete work must fast-exit"
        assert second.output_files == first.output_files


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
@pytest.mark.slow
class TestVideoIntent:
    def test_video_chain_only_no_audio_work(self, tmp_path: Path) -> None:
        config = _make_config(targets=["vmaf-med:70.0"])
        source = get_sample_video_path()
        work = tmp_path / "work"

        result = api.merge_final(
            config, config.resolve_encoding(), source, work,
            no_metrics=True, dry_run=False,
        )
        assert result.success, result.error
        assert result.output_files, "video must produce merged outputs"
        assert "audio" not in result.phases_executed, \
            "the video intent must not execute the audio phase"
        assert not (work / "audio").exists(), "no audio outputs from a video-only run"


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
@pytest.mark.slow
class TestAudioIntent:
    def test_audio_only_never_touches_video_stream(self, tmp_path: Path) -> None:
        config = _make_config()
        source = get_sample_video_path()
        work = tmp_path / "work"

        result = api.process_audio(
            config, source, work,
            no_metrics=True, dry_run=False,
        )
        assert result.success, result.error
        audio_dir = work / "audio"
        assert audio_dir.is_dir() and any(audio_dir.iterdir())
        # The video stream's material component stays untouched.
        assert not (work / EXTRACTED_DIR / TIMESTAMPS_FILENAME).exists()
        assert not (work / "probe.yaml").exists(), "no probe in an audio-only run"


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
@pytest.mark.slow
class TestTwoPassComposition:
    def test_video_then_audio_then_auto_merges_only(self, tmp_path: Path) -> None:
        config = _make_config(targets=["vmaf-med:70.0"])
        source = get_sample_video_path()
        work = tmp_path / "work"

        video = api.merge_final(
            config, config.resolve_encoding(), source, work,
            no_metrics=True, dry_run=False,
        )
        assert video.success, video.error

        audio = api.process_audio(
            config, source, work,
            no_metrics=True, dry_run=False,
        )
        assert audio.success, audio.error

        final = api.run_pipeline(
            config, config.resolve_encoding(), source, work,
            no_metrics=True, dry_run=False,
        )
        assert final.success, final.error
        assert final.phases_executed == [], \
            "auto after both passes must perform no work — everything is complete"
        assert final.output_files == video.output_files


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
@pytest.mark.slow
class TestExtractMaterializeEndToEnd:
    def test_materializes_every_kind_and_preserves_on_rerun(self, tmp_path: Path) -> None:
        config = _make_config()
        source = get_sample_video_path()
        work = tmp_path / "work"

        first = api.extract_streams(
            config, source, work,
            no_metrics=True, dry_run=False, materialize=True,
        )
        assert first.success, first.error

        extracted = work / EXTRACTED_DIR
        names = {f.name for f in extracted.iterdir() if f.is_file()}
        assert any(n.endswith(".h265") for n in names), "video elementary stream"
        assert any(n.endswith(".aac") for n in names), "aac elementary stream"
        assert any(n.endswith(".flac") for n in names), "flac elementary stream"
        assert any(n.endswith(".srt") for n in names), "subtitle"
        assert any(n.endswith(".jpg") for n in names), "attachment"
        assert "chapters.xml" in names, "chapters"
        assert TIMESTAMPS_FILENAME not in names, \
            "extract derives no video-need — no timestamps component"

        # Verify CORRECTNESS semantically: each elementary file must decode as
        # its expected codec (a byte-compare against a reference mkvextract
        # call would share this code's track-id convention and prove nothing
        # about which stream was extracted).
        def _codec_of(path: Path) -> str:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                check=True, capture_output=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            return streams[0]["codec_name"]

        assert _codec_of(extracted / next(n for n in names if n.endswith(".h265"))) == "hevc"
        assert _codec_of(extracted / next(n for n in names if n.endswith(".aac"))) == "aac"
        assert _codec_of(extracted / next(n for n in names if n.endswith(".flac"))) == "flac"

        # Re-run: complete work fast-exits and leaves the files untouched.
        snapshots = {n: (extracted / n).read_bytes() for n in names}
        second = api.extract_streams(
            config, source, work,
            no_metrics=True, dry_run=False, materialize=True,
        )
        assert second.success, second.error
        assert second.phases_executed == []
        for name, payload in snapshots.items():
            assert (extracted / name).read_bytes() == payload
