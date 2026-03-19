"""Unit tests for core refactored pipeline logic.

Covers:
- ChunkMetadata serialisation round-trip (including timestamps)
- ENCODED_ATTEMPT_NAME_PATTERN parsing of CRF-only filenames
- Merge strategy selection (optimal-only vs all-strategies)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pyqenc.constants import ENCODED_ATTEMPT_NAME_PATTERN
from pyqenc.models import (
    ChunkMetadata,
    CropParams,
    PhaseOutcome,
    PipelineConfig,
    VideoMetadata,
)
from pyqenc.phases.merge import merge_final_video
from pyqenc.state import JobState

# ---------------------------------------------------------------------------
# ChunkMetadata serialisation round-trip
# ---------------------------------------------------------------------------

class TestChunkMetadataRoundTrip:
    """ChunkMetadata must survive a model_dump_full / model_validate_full cycle."""

    def test_basic_round_trip(self) -> None:
        """Timestamps and chunk_id survive serialisation."""
        chunk = ChunkMetadata(
            path=Path("chunks/00꞉00꞉00․000-00꞉00꞉13․330.mkv"),
            chunk_id="00꞉00꞉00․000-00꞉00꞉13․330",
            start_timestamp=0.0,
            end_timestamp=13.33,
        )
        data = chunk.model_dump_full()
        restored = ChunkMetadata.model_validate_full(data)

        assert restored.chunk_id        == chunk.chunk_id
        assert restored.start_timestamp == chunk.start_timestamp
        assert restored.end_timestamp   == chunk.end_timestamp
        assert restored.path            == chunk.path

    def test_crop_params_round_trip(self) -> None:
        """crop_params on JobState survives serialisation."""
        source = VideoMetadata(path=Path("source.mkv"))
        job = JobState(source=source, crop_params=CropParams(top=140, bottom=140, left=0, right=0))
        data = job.to_yaml_dict()
        restored = JobState.from_yaml_dict(data)

        assert restored.crop is not None
        assert restored.crop.top    == 140
        assert restored.crop.bottom == 140
        assert restored.crop.left   == 0
        assert restored.crop.right  == 0

    def test_empty_crop_params_round_trip(self) -> None:
        """All-zero CropParams on JobState survives serialisation."""
        source = VideoMetadata(path=Path("source.mkv"))
        job = JobState(source=source, crop_params=CropParams())
        data = job.to_yaml_dict()
        restored = JobState.from_yaml_dict(data)

        assert restored.crop is not None
        assert restored.crop.is_empty()

    def test_none_crop_params_round_trip(self) -> None:
        """None crop_params on JobState (detection not yet run) survives serialisation."""
        source = VideoMetadata(path=Path("source.mkv"))
        job = JobState(source=source, crop_params=None)
        data = job.to_yaml_dict()
        restored = JobState.from_yaml_dict(data)

        assert restored.crop is None

    def test_private_fields_round_trip(self) -> None:
        """Cached probe fields survive serialisation."""
        chunk = ChunkMetadata(
            path=Path("chunks/chunk.mkv"),
            chunk_id="00꞉00꞉00․000-00꞉00꞉10․000",
            start_timestamp=0.0,
            end_timestamp=10.0,
        )
        chunk._duration_seconds = 10.0
        chunk._frame_count      = 240
        chunk._fps              = 24.0
        chunk._resolution       = "1920x800"

        data = chunk.model_dump_full()
        restored = ChunkMetadata.model_validate_full(data)

        assert restored._duration_seconds == 10.0
        assert restored._frame_count      == 240
        assert restored._fps              == 24.0
        assert restored._resolution       == "1920x800"

    def test_json_serialisable(self) -> None:
        """model_dump_full output must be JSON-serialisable (no Path objects)."""
        chunk = ChunkMetadata(
            path=Path("chunks/chunk.mkv"),
            chunk_id="00꞉00꞉00․000-00꞉00꞉10․000",
            start_timestamp=0.0,
            end_timestamp=10.0,
        )
        data = chunk.model_dump_full()
        # Should not raise
        json_str = json.dumps(data, default=str)
        assert json_str  # non-empty


# ---------------------------------------------------------------------------
# ENCODED_ATTEMPT_NAME_PATTERN
# ---------------------------------------------------------------------------

class TestEncodedAttemptNamePattern:
    """ENCODED_ATTEMPT_NAME_PATTERN must parse filenames from the CRF-only scheme."""

    def test_parses_basic_filename(self) -> None:
        """Standard CRF-only filename is parsed correctly."""
        name = "00꞉00꞉00․000-00꞉00꞉13․330.1920x800.crf18.0.mkv"
        m = ENCODED_ATTEMPT_NAME_PATTERN.match(name)
        assert m is not None
        assert m.group("chunk_id")   == "00꞉00꞉00․000-00꞉00꞉13․330"
        assert m.group("resolution") == "1920x800"
        assert m.group("crf")        == "18.0"

    def test_parses_integer_crf(self) -> None:
        """Integer CRF value (no decimal) is parsed correctly."""
        name = "00꞉00꞉00․000-00꞉00꞉13․330.1280x720.crf22.mkv"
        m = ENCODED_ATTEMPT_NAME_PATTERN.match(name)
        assert m is not None
        assert m.group("crf") == "22"

    def test_parses_fractional_crf(self) -> None:
        """Fractional CRF value is parsed correctly."""
        name = "00꞉01꞉30․500-00꞉03꞉00․000.3840x2160.crf17.5.mkv"
        m = ENCODED_ATTEMPT_NAME_PATTERN.match(name)
        assert m is not None
        assert m.group("chunk_id")   == "00꞉01꞉30․500-00꞉03꞉00․000"
        assert m.group("resolution") == "3840x2160"
        assert m.group("crf")        == "17.5"

    def test_no_match_for_tmp_file(self) -> None:
        """Temp files (.tmp suffix) must NOT match the pattern."""
        name = "00꞉00꞉00․000-00꞉00꞉13․330.1920x800.crf18.0.tmp"
        assert ENCODED_ATTEMPT_NAME_PATTERN.match(name) is None

    def test_no_match_for_chunk_file(self) -> None:
        """Plain chunk files (no crf segment) must NOT match."""
        name = "00꞉00꞉00․000-00꞉00꞉13․330.mkv"
        assert ENCODED_ATTEMPT_NAME_PATTERN.match(name) is None

    def test_crf_value_parseable_as_float(self) -> None:
        """The parsed crf group must be convertible to float."""
        name = "00꞉00꞉00․000-00꞉00꞉13․330.1920x1080.crf20.5.mkv"
        m = ENCODED_ATTEMPT_NAME_PATTERN.match(name)
        assert m is not None
        assert float(m.group("crf")) == 20.5


# ---------------------------------------------------------------------------
# Merge strategy selection
# ---------------------------------------------------------------------------

class TestMergeStrategySelection:
    """merge_final_video must respect optimal_strategy parameter."""

    _SOURCE_STEM = "test_video"

    def _make_encoded_chunks(
        self,
        tmp_path: Path,
        strategies: list[str],
        chunk_ids: list[str] | None = None,
    ) -> dict[str, dict[str, Path]]:
        """Create fake encoded chunk files and return the nested dict."""
        if chunk_ids is None:
            chunk_ids = ["00꞉00꞉00․000-00꞉00꞉10․000"]

        encoded: dict[str, dict[str, Path]] = {}
        for chunk_id in chunk_ids:
            encoded[chunk_id] = {}
            for strategy in strategies:
                safe = strategy.replace("+", "_").replace(":", "_")
                chunk_dir = tmp_path / "encoded" / safe
                chunk_dir.mkdir(parents=True, exist_ok=True)
                chunk_file = chunk_dir / f"{chunk_id}.1920x1080.crf18.0.mkv"
                chunk_file.write_bytes(b"\x00" * 100)
                encoded[chunk_id][strategy] = chunk_file

        return encoded

    def _fake_ffmpeg_run(self, cmd: list, output_file: Path | None = None, **_kw):  # type: ignore[override]
        """Simulate a successful ffmpeg run that creates the output file."""
        from pyqenc.utils.ffmpeg_runner import FFmpegRunResult
        if output_file is not None:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_bytes(b"\x00" * 100)
        return FFmpegRunResult(success=True, returncode=0)

    def test_optimal_strategy_only_merged(self, tmp_path: Path) -> None:
        """When optimal_strategy is set, only that strategy's output is produced."""
        strategies = ["slow_h265", "fast_h264"]
        encoded = self._make_encoded_chunks(tmp_path, strategies)
        output_dir = tmp_path / "final"

        with (
            patch("pyqenc.phases.merge.run_ffmpeg", side_effect=self._fake_ffmpeg_run),
            patch("pyqenc.phases.merge.get_frame_count", return_value=240),
        ):
            result = merge_final_video(
                encoded_chunks=encoded,
                output_dir=output_dir,
                source_stem=self._SOURCE_STEM,
                optimal_strategy="slow_h265",
                verify_frames=False,
                measure_quality=False,
            )

        assert result.outcome == PhaseOutcome.COMPLETED
        assert "slow_h265" in result.output_files
        assert "fast_h264" not in result.output_files
        assert result.output_files["slow_h265"].name == f"{self._SOURCE_STEM} slow_h265.mkv"

    def test_all_strategies_merged_when_no_optimal(self, tmp_path: Path) -> None:
        """When optimal_strategy is None, all strategies are merged."""
        strategies = ["slow_h265", "fast_h264"]
        encoded = self._make_encoded_chunks(tmp_path, strategies)
        output_dir = tmp_path / "final"

        with (
            patch("pyqenc.phases.merge.run_ffmpeg", side_effect=self._fake_ffmpeg_run),
            patch("pyqenc.phases.merge.get_frame_count", return_value=240),
        ):
            result = merge_final_video(
                encoded_chunks=encoded,
                output_dir=output_dir,
                source_stem=self._SOURCE_STEM,
                optimal_strategy=None,
                verify_frames=False,
                measure_quality=False,
            )

        assert result.outcome == PhaseOutcome.COMPLETED
        assert "slow_h265" in result.output_files
        assert "fast_h264" in result.output_files

    def test_unknown_optimal_strategy_returns_failed(self, tmp_path: Path) -> None:
        """When optimal_strategy is not in encoded chunks, merge returns FAILED."""
        strategies = ["slow_h265"]
        encoded = self._make_encoded_chunks(tmp_path, strategies)
        output_dir = tmp_path / "final"

        result = merge_final_video(
            encoded_chunks=encoded,
            output_dir=output_dir,
            source_stem=self._SOURCE_STEM,
            optimal_strategy="nonexistent_strategy",
            verify_frames=False,
            measure_quality=False,
        )

        assert result.outcome == PhaseOutcome.FAILED

    def test_dry_run_returns_dry_run_outcome(self, tmp_path: Path) -> None:
        """In dry-run mode, merge_final_video returns DRY_RUN outcome."""
        strategies = ["slow_h265"]
        encoded = self._make_encoded_chunks(tmp_path, strategies)
        output_dir = tmp_path / "final"

        result = merge_final_video(
            encoded_chunks=encoded,
            output_dir=output_dir,
            source_stem=self._SOURCE_STEM,
            dry_run=True,
            measure_quality=False,
        )

        assert result.outcome == PhaseOutcome.DRY_RUN

    def test_output_filename_uses_source_stem(self, tmp_path: Path) -> None:
        """Output filename must be '{source_stem} {safe_strategy}.mkv'."""
        strategies = ["slow_h265"]
        encoded = self._make_encoded_chunks(tmp_path, strategies)
        output_dir = tmp_path / "final"

        with (
            patch("pyqenc.phases.merge.run_ffmpeg", side_effect=self._fake_ffmpeg_run),
            patch("pyqenc.phases.merge.get_frame_count", return_value=240),
        ):
            result = merge_final_video(
                encoded_chunks=encoded,
                output_dir=output_dir,
                source_stem="My Movie (2024)",
                optimal_strategy="slow_h265",
                verify_frames=False,
                measure_quality=False,
            )

        assert result.outcome == PhaseOutcome.COMPLETED
        assert result.output_files["slow_h265"].name == "My Movie (2024) slow_h265.mkv"
