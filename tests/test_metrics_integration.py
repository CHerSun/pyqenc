"""Phase integration tests for MetricsCollector timing instrumentation.

Verifies that phases call ``time()`` and ``step()`` with the expected ``TimeKey``
values when their core work methods run.  External I/O (ffprobe, ffmpeg, crop
detect) is mocked out so tests run without real media files.

Tests live here per the spec: tests/test_metrics_integration.py
"""
# CHerSun 2026

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.metrics import MetricsCollector, NoOpMetricsCollector, TimeKey
from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    CropParams,
    PipelineConfig,
    VideoMetadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path, *, crop_params: CropParams | None = None) -> PipelineConfig:
    """Return a minimal ``PipelineConfig`` with a stub source file."""
    source = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 64)
    return PipelineConfig(
        source_video    = source,
        work_dir        = tmp_path / "work",
        quality_targets = [],
        strategies      = [],
        optimize        = False,
        max_parallel    = 1,
        include         = None,
        exclude         = None,
        cleanup         = CleanupLevel.NONE,
        chunking_mode   = ChunkingMode.LOSSLESS,
        force           = False,
        crop_params     = crop_params,
    )


def _spy_collector() -> MagicMock:
    """Return a ``MagicMock`` that satisfies the ``MetricsCollector`` Protocol.

    ``time()`` returns a real no-op context manager so ``with collector.time(key):``
    works correctly in phase code.  ``step`` is a plain mock so calls
    can be inspected via ``assert_called_with`` / ``call_args_list``.
    """
    collector = MagicMock(spec=MetricsCollector)
    collector.time.return_value = contextlib.nullcontext()
    return collector


def _stub_video_metadata(source: Path) -> VideoMetadata:
    """Return a ``VideoMetadata`` with all private fields pre-populated.

    Avoids any real ffprobe / ffmpeg calls during tests.
    """
    vm = VideoMetadata(path=source)
    vm._file_size_bytes  = 64
    vm._duration_seconds = 120.0
    vm._fps              = 24.0
    vm._resolution       = "1920x1080"
    vm._frame_count      = 2880
    return vm


# ---------------------------------------------------------------------------
# JobPhase — JOB_PROBE timing
# ---------------------------------------------------------------------------

class TestJobPhaseTiming:
    """Integration tests for ``JobPhase`` timing instrumentation (Req 6.5)."""

    def test_job_probe_recorded_on_run(self, tmp_path: Path) -> None:
        """``time(TimeKey.JOB_PROBE)`` must be called when ``run()`` executes.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase

        config    = _make_config(tmp_path)
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        stub_vm = _stub_video_metadata(config.source_video)

        # Patch VideoMetadata so no real ffprobe/ffmpeg calls happen.
        # Patch detect_crop_parameters to return a zero-crop immediately.
        with (
            patch("pyqenc.phases.job.VideoMetadata", return_value=stub_vm),
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=CropParams()),
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        # Verify time() was called with JOB_PROBE
        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_PROBE in time_keys_called, (
            f"Expected TimeKey.JOB_PROBE in time() calls, got: {time_keys_called}"
        )

    def test_job_probe_not_recorded_when_reused(self, tmp_path: Path) -> None:
        """When ``job.yaml`` already exists and source matches, probing is skipped.

        The phase returns REUSED without re-probing, so ``time(JOB_PROBE)``
        must NOT be called.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase
        from pyqenc.state import JobState

        config    = _make_config(tmp_path)
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        # Pre-create a valid job.yaml so the phase takes the REUSED path.
        config.work_dir.mkdir(parents=True, exist_ok=True)
        stub_vm = _stub_video_metadata(config.source_video)
        job = JobState(source=stub_vm, crop=CropParams())
        job.save(config.work_dir / "job.yaml")

        with (
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_PROBE not in time_keys_called, (
            f"Expected JOB_PROBE NOT called on reuse, but got: {time_keys_called}"
        )

    def test_job_crop_detect_recorded_when_no_cache(self, tmp_path: Path) -> None:
        """``time(JOB_CROP_DETECT)`` must be called when auto-detect runs.

        Auto-detect runs when neither manual crop nor cached crop is available.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase

        config    = _make_config(tmp_path)  # crop_params=None → auto-detect path
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        stub_vm = _stub_video_metadata(config.source_video)

        with (
            patch("pyqenc.phases.job.VideoMetadata", return_value=stub_vm),
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=CropParams()),
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_CROP_DETECT in time_keys_called, (
            f"Expected TimeKey.JOB_CROP_DETECT in time() calls, got: {time_keys_called}"
        )

    def test_job_crop_detect_not_recorded_for_manual_crop(self, tmp_path: Path) -> None:
        """``time(JOB_CROP_DETECT)`` must NOT be called when manual crop is set.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase

        config    = _make_config(tmp_path, crop_params=CropParams(top=140, bottom=140))
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        stub_vm = _stub_video_metadata(config.source_video)

        with (
            patch("pyqenc.phases.job.VideoMetadata", return_value=stub_vm),
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_CROP_DETECT not in time_keys_called, (
            f"Expected JOB_CROP_DETECT NOT called for manual crop, got: {time_keys_called}"
        )

    def test_job_crop_detect_not_recorded_for_cached_crop(self, tmp_path: Path) -> None:
        """``time(JOB_CROP_DETECT)`` must NOT be called when crop is cached in job.yaml.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase
        from pyqenc.state import JobState

        config    = _make_config(tmp_path)  # no manual crop
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        # Pre-create job.yaml with a cached crop — phase will load it and skip detect.
        config.work_dir.mkdir(parents=True, exist_ok=True)
        stub_vm = _stub_video_metadata(config.source_video)
        job = JobState(source=stub_vm, crop=CropParams(top=100, bottom=100))
        job.save(config.work_dir / "job.yaml")

        with (
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_CROP_DETECT not in time_keys_called, (
            f"Expected JOB_CROP_DETECT NOT called for cached crop, got: {time_keys_called}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``JobPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.job import JobPhase

        config    = _make_config(tmp_path)
        collector = NoOpMetricsCollector()
        phase     = JobPhase(config, collector=collector)

        stub_vm = _stub_video_metadata(config.source_video)

        with (
            patch("pyqenc.phases.job.VideoMetadata", return_value=stub_vm),
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=CropParams()),
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# ExtractionPhase — EXTRACTION and RECOVERY timing
# ---------------------------------------------------------------------------

class TestExtractionPhaseTiming:
    """Integration tests for ``ExtractionPhase`` timing instrumentation (Req 6.5)."""

    def _make_job_result(self) -> "JobPhaseResult":
        """Return a minimal complete ``JobPhaseResult`` stub."""
        from pyqenc.phases.job import JobPhaseResult
        from pyqenc.models import PhaseOutcome
        return JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            artifacts  = [],
            message    = "ok",
            force_wipe = False,
        )

    def _make_phase(
        self,
        tmp_path: Path,
        collector: MagicMock,
    ) -> "ExtractionPhase":
        """Return an ``ExtractionPhase`` with a pre-wired job dependency."""
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.phases.job import JobPhase

        config   = _make_config(tmp_path)
        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result()

        phase = ExtractionPhase(config, collector=collector)
        phase._job = job_mock  # type: ignore[assignment]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(TimeKey.RECOVERY)`` must be called even when all artifacts are reused.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.state import ArtifactState
        from pyqenc.phases.extraction import VideoArtifact

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        # Stub a complete artifact so _recover returns all-complete → REUSED path
        stub_artifact = MagicMock(spec=VideoArtifact)
        stub_artifact.state = ArtifactState.COMPLETE

        with patch.object(
            ExtractionPhase, "_recover",
            return_value=([stub_artifact], None, []),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.RECOVERY in time_keys_called, (
            f"Expected TimeKey.RECOVERY in time() calls, got: {time_keys_called}"
        )

    def test_extraction_recorded_for_mkvextract_tracks(self, tmp_path: Path) -> None:
        """``time(TimeKey.EXTRACTION)`` must be called when mkvextract runs for other tracks.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.extraction import (
            ExtractionPhase,
            MKVTrackExtractor,
            OtherArtifact,
            SubtitleStream,
            VideoStream,
        )
        from pyqenc.state import ArtifactState

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        extracted_dir = phase._config.work_dir / "extracted"
        extracted_dir.mkdir(parents=True, exist_ok=True)

        # Stub a subtitle stream (other track) that is ABSENT — triggers mkvextract
        absent_path = extracted_dir / "sub_0_eng.srt"
        stub_sub = MagicMock(spec=SubtitleStream)
        stub_sub.codec_type = "subtitle"
        stub_sub.display_name.return_value = absent_path.name

        # Stub a video stream so _execute_extraction doesn't bail out early
        stub_video = MagicMock(spec=VideoStream)
        stub_video.codec_type = "video"
        stub_video.track_id   = 0
        stub_video.display_name.return_value = "video_0.mkv"

        stub_extractor = MagicMock(spec=MKVTrackExtractor)
        stub_extractor.tracks = [stub_video, stub_sub]

        # ABSENT artifact for the subtitle track
        stub_artifact = MagicMock(spec=OtherArtifact)
        stub_artifact.state = ArtifactState.ABSENT
        stub_artifact.path  = absent_path

        with (
            patch.object(
                ExtractionPhase, "_recover",
                return_value=([stub_artifact], None, []),
            ),
            patch(
                "pyqenc.phases.extraction.MKVTrackExtractor",
                return_value=stub_extractor,
            ),
            patch(
                "pyqenc.phases.extraction.streams_filter_plain_regex",
                return_value=[stub_video, stub_sub],
            ),
            patch("pyqenc.phases.extraction.run_ffmpeg") as mock_ffmpeg,
        ):
            # Make ffmpeg succeed for the video track extraction
            mock_ffmpeg.return_value = MagicMock(success=True)
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.EXTRACTION in time_keys_called, (
            f"Expected TimeKey.EXTRACTION in time() calls, got: {time_keys_called}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``ExtractionPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.extraction import ExtractionPhase, VideoArtifact
        from pyqenc.state import ArtifactState

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        stub_artifact = MagicMock(spec=VideoArtifact)
        stub_artifact.state = ArtifactState.COMPLETE

        with patch.object(
            ExtractionPhase, "_recover",
            return_value=([stub_artifact], None, []),
        ):
            result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# ChunkingPhase — CHUNKING_SCENE_DETECT, CHUNKING_SPLIT, and RECOVERY timing
# ---------------------------------------------------------------------------

class TestChunkingPhaseTiming:
    """Integration tests for ``ChunkingPhase`` timing instrumentation (Req 6.5)."""

    def _make_job_result(self, tmp_path: Path) -> "JobPhaseResult":
        """Return a minimal complete ``JobPhaseResult`` stub with a real source file."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.job import JobPhaseResult
        from pyqenc.state import JobState

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)
        stub_vm = _stub_video_metadata(source)
        job_state = JobState(source=stub_vm, crop=CropParams())

        result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            artifacts  = [],
            message    = "ok",
            force_wipe = False,
        )
        result.job = job_state  # type: ignore[attr-defined]
        return result

    def _make_extraction_result(self, tmp_path: Path) -> "ExtractionPhaseResult":
        """Return a minimal complete ``ExtractionPhaseResult`` stub."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.extraction import ExtractionPhaseResult

        source = tmp_path / "source.mkv"
        stub_vm = _stub_video_metadata(source)
        result = ExtractionPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = [],
            message   = "ok",
            video     = stub_vm,
        )
        return result

    def _make_phase(
        self,
        tmp_path: Path,
        collector: MagicMock,
    ) -> "ChunkingPhase":
        """Return a ``ChunkingPhase`` with pre-wired job and extraction dependencies."""
        from pyqenc.phases.chunking import ChunkingPhase
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.phases.job import JobPhase

        config = _make_config(tmp_path)
        config.work_dir.mkdir(parents=True, exist_ok=True)

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result(tmp_path)

        extraction_mock = MagicMock(spec=ExtractionPhase)
        extraction_mock.result = self._make_extraction_result(tmp_path)

        phase = ChunkingPhase(config, collector=collector)
        phase._job        = job_mock         # type: ignore[assignment]
        phase._extraction = extraction_mock  # type: ignore[assignment]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(TimeKey.RECOVERY)`` must be called even when all chunks are reused.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.chunking import ChunkingPhase, ChunkingPhaseResult
        from pyqenc.state import ArtifactState
        from pyqenc.phases.chunking import ChunkArtifact

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        # Stub _recover to return all-complete → REUSED path
        stub_artifact = MagicMock(spec=ChunkArtifact)
        stub_artifact.state    = ArtifactState.COMPLETE
        stub_artifact.metadata = None

        with patch.object(ChunkingPhase, "_recover", return_value=[stub_artifact]):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.RECOVERY in time_keys_called, (
            f"Expected TimeKey.RECOVERY in time() calls, got: {time_keys_called}"
        )

    def test_scene_detect_recorded_when_no_cached_boundaries(self, tmp_path: Path) -> None:
        """``time(TimeKey.CHUNKING_SCENE_DETECT)`` must be called when detection runs.

        Validates: Requirements 6.5
        """
        from pyqenc.models import SceneBoundary
        from pyqenc.phases.chunking import ChunkingPhase, ChunkArtifact

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        # No cached scenes → detection will run
        phase._recovered_scenes = []  # type: ignore[attr-defined]

        stub_boundaries = [SceneBoundary(frame=0, timestamp_seconds=0.0)]

        with (
            patch.object(ChunkingPhase, "_recover", return_value=[]),
            patch("pyqenc.phases.chunking.detect_scenes", return_value=stub_boundaries),
            patch("pyqenc.phases.chunking.split_chunks", return_value=[]),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.CHUNKING_SCENE_DETECT in time_keys_called, (
            f"Expected TimeKey.CHUNKING_SCENE_DETECT in time() calls, got: {time_keys_called}"
        )

    def test_scene_detect_not_recorded_when_boundaries_cached(self, tmp_path: Path) -> None:
        """``time(TimeKey.CHUNKING_SCENE_DETECT)`` must NOT be called when scenes are cached.

        Validates: Requirements 6.5
        """
        from pyqenc.models import SceneBoundary
        from pyqenc.phases.chunking import ChunkingPhase

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        # Pre-populate cached scenes so detection is skipped
        cached_boundaries = [SceneBoundary(frame=0, timestamp_seconds=0.0)]
        phase._recovered_scenes = cached_boundaries  # type: ignore[attr-defined]

        with (
            patch.object(ChunkingPhase, "_recover", return_value=[]),
            patch("pyqenc.phases.chunking.split_chunks", return_value=[]),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.CHUNKING_SCENE_DETECT not in time_keys_called, (
            f"Expected CHUNKING_SCENE_DETECT NOT called when cached, got: {time_keys_called}"
        )

    def test_chunking_split_recorded_when_chunks_pending(self, tmp_path: Path) -> None:
        """``time(TimeKey.CHUNKING_SPLIT)`` must be called when split_chunks runs.

        Validates: Requirements 6.5, 2.2a
        """
        from pyqenc.models import ChunkMetadata, SceneBoundary
        from pyqenc.phases.chunking import ChunkingPhase

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        cached_boundaries = [SceneBoundary(frame=0, timestamp_seconds=0.0)]
        phase._recovered_scenes = cached_boundaries  # type: ignore[attr-defined]

        stub_chunk = MagicMock(spec=ChunkMetadata)
        stub_chunk.path       = tmp_path / "chunk.mkv"
        stub_chunk.chunk_id   = "chunk_0"
        stub_chunk._frame_count = 100

        with (
            patch.object(ChunkingPhase, "_recover", return_value=[]),
            patch("pyqenc.phases.chunking.split_chunks", return_value=[stub_chunk]),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.CHUNKING_SPLIT in time_keys_called, (
            f"Expected TimeKey.CHUNKING_SPLIT in time() calls, got: {time_keys_called}"
        )

    def test_step_called_per_successful_split(self, tmp_path: Path) -> None:
        """``step(TimeKey.CHUNKING_SPLIT)`` must be called once per successful chunk split.

        Two boundaries produce two chunks, so two ``step`` calls are expected.

        Validates: Requirements 6.5, 2.2a
        """
        from pyqenc.models import SceneBoundary, VideoMetadata
        from pyqenc.phases.chunking import split_chunks
        from pyqenc.phases.recovery import ChunkingRecovery

        collector = _spy_collector()

        # Build a minimal video_meta and two fake boundaries
        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)
        vm = _stub_video_metadata(source)

        output_dir = tmp_path / "chunks"
        output_dir.mkdir()

        boundaries = [
            SceneBoundary(frame=0,  timestamp_seconds=0.0),
            SceneBoundary(frame=24, timestamp_seconds=1.0),
        ]

        # Build a recovery object with no complete chunks
        from pyqenc.phases.recovery import ChunkingRecovery as RecoveryObj
        recovery = RecoveryObj(scenes=boundaries, chunks={}, pending=[])        # Patch run_ffmpeg to succeed and create the output file
        def _fake_ffmpeg(cmd: list, output_file: Path | None = None, **kwargs: object) -> MagicMock:
            if output_file is not None:
                output_file.write_bytes(b"\x00" * 32)
            result = MagicMock()
            result.success = True
            return result

        with (
            patch("pyqenc.phases.chunking.run_ffmpeg", side_effect=_fake_ffmpeg),
            patch("pyqenc.phases.chunking._write_chunk_sidecar"),
        ):
            split_chunks(
                video_meta    = vm,
                output_dir    = output_dir,
                boundaries    = boundaries,
                recovery      = recovery,
                collector     = collector,
            )
        step_keys = [call.args[0] for call in collector.step.call_args_list]
        assert step_keys.count(TimeKey.CHUNKING_SPLIT) == 2, (
            f"Expected 2 step(CHUNKING_SPLIT) calls (one per boundary), got: {step_keys}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``ChunkingPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.models import SceneBoundary
        from pyqenc.phases.chunking import ChunkingPhase

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        cached_boundaries = [SceneBoundary(frame=0, timestamp_seconds=0.0)]
        phase._recovered_scenes = cached_boundaries  # type: ignore[attr-defined]

        with (
            patch.object(ChunkingPhase, "_recover", return_value=[]),
            patch("pyqenc.phases.chunking.split_chunks", return_value=[]),
        ):
            result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# AudioPhase — AUDIO and RECOVERY timing
# ---------------------------------------------------------------------------

class TestAudioPhaseTiming:
    """Integration tests for ``AudioPhase`` timing instrumentation (Req 6.5)."""

    def _make_phase(
        self,
        tmp_path: Path,
        collector: MagicMock,
    ) -> "AudioPhase":
        """Return an ``AudioPhase`` with pre-wired job and extraction dependencies."""
        from pyqenc.phases.audio import AudioPhase
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.phases.job import JobPhase
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.job import JobPhaseResult
        from pyqenc.phases.extraction import ExtractionPhaseResult
        from pyqenc.state import JobState

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)
        stub_vm = _stub_video_metadata(source)

        config = _make_config(tmp_path)
        config.work_dir.mkdir(parents=True, exist_ok=True)

        job_state = JobState(source=stub_vm, crop=CropParams())
        job_result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            artifacts  = [],
            message    = "ok",
            force_wipe = False,
        )
        job_result.job = job_state  # type: ignore[attr-defined]

        extraction_result = ExtractionPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = [],
            message   = "ok",
            video     = stub_vm,
        )

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = job_result

        extraction_mock = MagicMock(spec=ExtractionPhase)
        extraction_mock.result = extraction_result

        phase = AudioPhase(config, collector=collector)
        phase._job        = job_mock         # type: ignore[assignment]
        phase._extraction = extraction_mock  # type: ignore[assignment]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(TimeKey.RECOVERY)`` must be called even when all artifacts are reused.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phases.audio import AudioPhase, AudioArtifact
        from pyqenc.state import ArtifactState

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        stub_artifact = MagicMock(spec=AudioArtifact)
        stub_artifact.state = ArtifactState.COMPLETE
        stub_artifact.path  = tmp_path / "track.aac"

        with patch.object(AudioPhase, "_recover", return_value=[stub_artifact]):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.RECOVERY in time_keys_called, (
            f"Expected TimeKey.RECOVERY in time() calls, got: {time_keys_called}"
        )

    def test_audio_recorded_when_processing_runs(self, tmp_path: Path) -> None:
        """``time(TimeKey.AUDIO)`` must be called when audio processing executes.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.audio import AudioPhase, AudioArtifact, AudioPhaseResult
        from pyqenc.state import ArtifactState
        from pyqenc.models import PhaseOutcome

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        stub_artifact = MagicMock(spec=AudioArtifact)
        stub_artifact.state = ArtifactState.ABSENT

        stub_result = AudioPhaseResult(
            outcome     = PhaseOutcome.COMPLETED,
            artifacts   = [],
            message     = "ok",
            audio_files = [],
        )

        with (
            patch.object(AudioPhase, "_recover", return_value=[stub_artifact]),
            patch.object(AudioPhase, "_execute_audio", return_value=stub_result),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.AUDIO in time_keys_called, (
            f"Expected TimeKey.AUDIO in time() calls, got: {time_keys_called}"
        )

    def test_audio_not_recorded_when_all_reused(self, tmp_path: Path) -> None:
        """``time(TimeKey.AUDIO)`` must NOT be called when all artifacts are already complete.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.audio import AudioPhase, AudioArtifact
        from pyqenc.state import ArtifactState

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        stub_artifact = MagicMock(spec=AudioArtifact)
        stub_artifact.state = ArtifactState.COMPLETE
        stub_artifact.path  = tmp_path / "track.aac"

        with patch.object(AudioPhase, "_recover", return_value=[stub_artifact]):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.AUDIO not in time_keys_called, (
            f"Expected TimeKey.AUDIO NOT called on reuse, got: {time_keys_called}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``AudioPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.audio import AudioPhase, AudioArtifact
        from pyqenc.state import ArtifactState

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        stub_artifact = MagicMock(spec=AudioArtifact)
        stub_artifact.state = ArtifactState.COMPLETE
        stub_artifact.path  = tmp_path / "track.aac"

        with patch.object(AudioPhase, "_recover", return_value=[stub_artifact]):
            result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# OptimizationPhase — ENCODING_OPTIMIZATION and RECOVERY timing
# ---------------------------------------------------------------------------


class TestOptimizationPhaseTiming:
    """Integration tests for ``OptimizationPhase`` timing instrumentation (Req 6.5)."""

    def _make_job_result(self, tmp_path: Path) -> "JobPhaseResult":
        """Return a minimal complete ``JobPhaseResult`` stub."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.job import JobPhaseResult
        from pyqenc.state import JobState

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)
        stub_vm = _stub_video_metadata(source)
        job_state = JobState(source=stub_vm, crop=CropParams())

        result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            artifacts  = [],
            message    = "ok",
            force_wipe = False,
        )
        result.job = job_state  # type: ignore[attr-defined]
        return result

    def _make_chunking_result(self, tmp_path: Path) -> "ChunkingPhaseResult":
        """Return a minimal complete ``ChunkingPhaseResult`` stub with one chunk."""
        from pyqenc.models import ChunkMetadata, PhaseOutcome
        from pyqenc.phases.chunking import ChunkingPhaseResult

        chunk = MagicMock(spec=ChunkMetadata)
        chunk.chunk_id         = "chunk_0"
        chunk.start_timestamp  = 0.0
        chunk.end_timestamp    = 1.0
        chunk.path             = tmp_path / "chunks" / "chunk_0.mkv"

        return ChunkingPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = [],
            message   = "ok",
            chunks    = [chunk],
        )

    def _make_phase(
        self,
        tmp_path:  Path,
        collector: MagicMock,
        *,
        optimize:  bool = True,
    ) -> "OptimizationPhase":
        """Return an ``OptimizationPhase`` with pre-wired job and chunking dependencies."""
        from pyqenc.models import Strategy
        from pyqenc.phases.chunking import ChunkingPhase
        from pyqenc.phases.job import JobPhase
        from pyqenc.phases.optimization import OptimizationPhase

        config = _make_config(tmp_path)
        config.work_dir.mkdir(parents=True, exist_ok=True)
        config.optimize   = optimize  # type: ignore[attr-defined]
        config.strategies = [Strategy.from_name("slow+h265")]  # type: ignore[attr-defined]

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result(tmp_path)

        chunking_mock = MagicMock(spec=ChunkingPhase)
        chunking_mock.result = self._make_chunking_result(tmp_path)

        phase = OptimizationPhase(config, collector=collector)
        phase._job      = job_mock       # type: ignore[assignment]
        phase._chunking = chunking_mock  # type: ignore[assignment]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(TimeKey.RECOVERY)`` must be called when all results are already cached.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.models import Strategy
        from pyqenc.state import OptimizationParams, StrategyTestResult

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector, optimize=True)

        strategy = Strategy.from_name("slow+h265")
        # tolerance_pct and metrics_sampling must match config defaults so the
        # full-reuse path (step 4) is taken rather than falling through to encodes.
        persisted = OptimizationParams(
            crop             = CropParams(),
            test_chunks      = ["chunk_0"],
            strategy_results = [StrategyTestResult(strategy=strategy, total_size=1024, avg_crf=28.0)],
            tolerance_pct    = 5.0,   # matches PipelineConfig.strategy_selection_tolerance default
            selected         = [strategy],
            quality_targets  = [],
            metrics_sampling = 10,    # matches PipelineConfig.metrics_sampling default
        )

        # All results cached with matching tolerance → reuse path (step 4 in run())
        with patch.object(OptimizationParams, "load", return_value=persisted):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.RECOVERY in time_keys_called, (
            f"Expected TimeKey.RECOVERY in time() calls on reuse path, got: {time_keys_called}"
        )

    def test_encoding_optimization_recorded_when_test_encodes_run(self, tmp_path: Path) -> None:
        """``time(TimeKey.ENCODING_OPTIMIZATION)`` must be called when test encodes run.

        The ``time()`` call wraps the entire loop inside ``_encode_strategy_test_chunks``,
        so we verify the collector receives it by running the real function with mocked
        inner encode calls.

        Validates: Requirements 6.5, 2.2a
        """
        import asyncio
        from pyqenc.models import ChunkMetadata, Strategy
        from pyqenc.phases.encoding import ChunkEncodingResult
        from pyqenc.phases.optimization import _encode_strategy_test_chunks

        collector = _spy_collector()
        strategy  = Strategy.from_name("slow+h265")

        chunk = MagicMock(spec=ChunkMetadata)
        chunk.chunk_id        = "chunk_0"
        chunk.start_timestamp = 0.0
        chunk.end_timestamp   = 1.0
        chunk.path            = tmp_path / "chunk_0.mkv"

        encoded_path = tmp_path / "chunk_0_enc.mkv"
        encoded_path.write_bytes(b"\x00" * 128)

        successful_result = ChunkEncodingResult(
            chunk_id     = "chunk_0",
            strategy     = "slow+h265",
            success      = True,
            final_crf    = 28.0,
            attempts     = 2,
            encoded_file = MagicMock(path=encoded_path),
            reused       = False,
        )

        stub_recovery = MagicMock()
        stub_recovery.pairs = {}

        with (
            patch("pyqenc.phases.encoding._recover_encoding_attempts", return_value=stub_recovery),
            patch("pyqenc.phases.encoding._encode_chunk_async", return_value=successful_result),
        ):
            asyncio.run(
                _encode_strategy_test_chunks(
                    encoder         = MagicMock(),
                    test_chunks     = [chunk],
                    reference_dir   = tmp_path,
                    strategy        = strategy,
                    quality_targets = [],
                    max_parallel    = 1,
                    work_dir        = tmp_path,
                    collector       = collector,
                )
            )

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.ENCODING_OPTIMIZATION in time_keys_called, (
            f"Expected TimeKey.ENCODING_OPTIMIZATION in time() calls, got: {time_keys_called}"
        )

    def test_step_called_with_convergence_update_per_chunk(self, tmp_path: Path) -> None:
        """``step(TimeKey.ENCODING_OPTIMIZATION, convergence_update=...)`` must be called
        once per successfully converged test chunk inside ``_encode_strategy_test_chunks``.

        Validates: Requirements 6.5, 4.1a
        """
        import asyncio
        from pyqenc.metrics import ConvergenceUpdate
        from pyqenc.models import ChunkMetadata, Strategy
        from pyqenc.phases.encoding import ChunkEncodingResult
        from pyqenc.phases.optimization import _encode_strategy_test_chunks

        collector = _spy_collector()
        strategy  = Strategy.from_name("slow+h265")

        chunk = MagicMock(spec=ChunkMetadata)
        chunk.chunk_id        = "chunk_0"
        chunk.start_timestamp = 0.0
        chunk.end_timestamp   = 1.0
        chunk.path            = tmp_path / "chunk_0.mkv"

        # Reference file must exist so the encode path is reached (not skipped)
        (tmp_path / "chunk_0.mkv").write_bytes(b"\x00" * 64)

        encoded_path = tmp_path / "chunk_0_enc.mkv"
        encoded_path.write_bytes(b"\x00" * 128)

        successful_result = ChunkEncodingResult(
            chunk_id     = "chunk_0",
            strategy     = "slow+h265",
            success      = True,
            final_crf    = 28.0,
            attempts     = 3,
            encoded_file = MagicMock(path=encoded_path),
            reused       = False,
        )

        stub_recovery = MagicMock()
        stub_recovery.pairs = {}

        with (
            patch("pyqenc.phases.encoding._recover_encoding_attempts", return_value=stub_recovery),
            patch("pyqenc.phases.encoding._encode_chunk_async", return_value=successful_result),
        ):
            asyncio.run(
                _encode_strategy_test_chunks(
                    encoder         = MagicMock(),
                    test_chunks     = [chunk],
                    reference_dir   = tmp_path,
                    strategy        = strategy,
                    quality_targets = [],
                    max_parallel    = 1,
                    work_dir        = tmp_path,
                    collector       = collector,
                )
            )

        step_calls = collector.step.call_args_list
        assert len(step_calls) == 1, f"Expected 1 step() call, got {len(step_calls)}"
        call_key    = step_calls[0].args[0]
        call_update = step_calls[0].kwargs.get("convergence_update")
        assert call_key == TimeKey.ENCODING_OPTIMIZATION, f"Wrong key: {call_key}"
        assert isinstance(call_update, ConvergenceUpdate), f"Expected ConvergenceUpdate, got: {call_update}"
        assert call_update.strategy      == "slow+h265", f"Wrong strategy: {call_update.strategy}"
        assert call_update.attempt_count == 3,           f"Wrong attempt_count: {call_update.attempt_count}"

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``OptimizationPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.models import Strategy
        from pyqenc.phases.optimization import OptimizationPhase
        from pyqenc.state import OptimizationParams, StrategyTestResult

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector, optimize=True)  # type: ignore[arg-type]

        strategy = Strategy.from_name("slow+h265")
        persisted = OptimizationParams(
            crop             = CropParams(),
            test_chunks      = ["chunk_0"],
            strategy_results = [StrategyTestResult(strategy=strategy, total_size=1024, avg_crf=28.0)],
            tolerance_pct    = 0.0,
            selected         = [strategy],
            quality_targets  = [],
            metrics_sampling = 1,
        )

        with patch.object(OptimizationParams, "load", return_value=persisted):
            result = phase.run()

        assert result is not None
