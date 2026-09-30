"""Phase integration tests for MetricsCollector timing instrumentation.

Verifies that phases call ``time()`` and ``step()`` with the expected ``MetricKey``
values when their core work methods run.  External I/O (ffprobe, ffmpeg, crop
detect) is mocked out so tests run without real media files.

Tests live here per the spec: tests/test_metrics_integration.py
"""
# CHerSun 2026

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.metrics import MetricKey, MetricsCollector, NoOpMetricsCollector
from pyqenc.models import (
    CleanupLevel,
    PhaseOutcome,
    Strategy,
)
from pyqenc.phase import Artifact, Recovery
from pyqenc.phases.audio import AudioPhase, AudioPhaseResult
from pyqenc.phases.chunking import ChunkingPhase, ChunkingPhaseResult
from pyqenc.phases.encoding import EncodingPhase, EncodingPhaseResult
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhaseResult
from pyqenc.phases.merge import MergePhase
from pyqenc.phases.optimization import OptimizationPhase, OptimizationPhaseResult
from pyqenc.state import ArtifactState
from pyqenc.stream_model import ExtendedVideoStream, File, VideoStreamChunk
from pyqenc.utils.yaml_utils import write_yaml_atomic

_SHARED_APP_CONFIG: AppConfig = load_app_config(default_only=True)

# Resolve a couple of known strategies once at module level for use in tests.
_resolved = _SHARED_APP_CONFIG.encoding.resolved_strategies
_STRATEGY_SLOW_H265 = next((s for s in _resolved if s.preset == "slow" and "h265" in s.profile and "aq" not in s.profile), _resolved[0])
_STRATEGY_H265_AQ   = next((s for s in _resolved if "h265" in s.profile and "aq" in s.profile), _resolved[min(1, len(_resolved) - 1)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path) -> AppConfig:
    """Return an ``AppConfig`` loaded from the bundled default (no modifications).

    Uses ``default_only=True`` so tests are not affected by any developer-local
    config files.
    """
    return load_app_config(default_only=True)

def _make_chunk_window(source: Path, start: float, end: float) -> VideoStreamChunk:
    """A VideoStreamChunk fixture over the source window."""
    from pyqenc.stream_model import VideoStreamChunk

    stream = _make_extended_stream(source, frame_count=640, duration=end)
    return VideoStreamChunk(
        stream=stream, start_timestamp=start, end_timestamp=end, frame_count=24,
    )


def _encoded_chunk(path: Path, chunk_id: str, strategy_name: str):
    """An EncodedChunk payload over a one-window chunk of the named strategy."""
    from decimal import Decimal

    from pyqenc.stream_model import EncodedChunk

    strategy = _make_strategy_by_name(strategy_name)
    return EncodedChunk(
        stream=_make_extended_stream(path, frame_count=24, duration=1.0),
        chunk=_make_chunk_window(path.parent / "source.mkv", 0.0, 1.0),
        strategy=strategy,
        crf=Decimal(20),
    )


def _audio_output(out_path: Path):
    """An AudioOutput payload over a minimal audio stream."""
    from pyqenc.audio.layout import ChannelLayout
    from pyqenc.stream_model import AudioOutput, AudioStream, AudioStreamInfo

    return AudioOutput(
        stream=AudioStream(
            file=File(path=out_path.parent.parent / "source.mkv", file_size_bytes=64),
            info=AudioStreamInfo(
                track_id=1, codec_name="flac", language="eng",
                layout=ChannelLayout.parse("stereo"), duration_seconds=100.0,
            ),
        ),
        chain_name="normal",
        output_path=out_path,
    )


def _merged_row(out_path: Path, state) -> Artifact:
    """A merged-output row over a minimal MergedVideo payload."""
    from pyqenc.stream_model import MergedVideo

    return Artifact(
        payload=MergedVideo(
            source_stem="source",
            strategy=_make_strategy_by_name("slow+h265"),
            output_path=out_path,
        ),
        state=state,
    )


def _make_strategy_by_name(name: str) -> Strategy:
    """A minimal Strategy for a ``preset+profile`` display name."""
    from decimal import Decimal

    from pyqenc.models import CodecConfig, Strategy

    preset, _, profile = name.partition("+")
    return Strategy(
        preset=preset, profile=profile,
        codec=CodecConfig(
            name="h265-10bit", default_quality=Decimal(20),
            default_preset="ultrafast",
            quality_range=(Decimal(0), Decimal(51)), presets=["ultrafast"],
        ),
        profile_args=[],
    )


def _make_video_stream_fixture(path: Path):
    """A VideoStream fixture for registry stubs."""
    from fractions import Fraction

    from pyqenc.stream_model import VideoStream, VideoStreamInfo

    return VideoStream(
        file=File(path=path, file_size_bytes=64),
        info=VideoStreamInfo(
            track_id=0, codec_name="hevc", fps=24.0,
            fps_fraction=Fraction(24, 1), resolution="1920x1080",
            duration_seconds=120.0,
        ),
    )



def _make_volatile(tmp_path: Path) -> dict:
    """Return a minimal dict of volatile kwargs for phases that require them."""
    source = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 64)
    return {
        "source":     source,
        "work_dir":   tmp_path / "work",
        "force":      False,
        "cleanup":    CleanupLevel.NONE,
        "no_metrics": False,
    }

def _make_extended_stream(path: Path, frame_count: int, duration: float) -> ExtendedVideoStream:
    """An ExtendedVideoStream fixture (fast facet + frame count)."""
    from fractions import Fraction

    from pyqenc.models import CropParams
    from pyqenc.stream_model import (
        ExtendedVideoStream,
        VideoStream,
        VideoStreamInfo,
    )

    return ExtendedVideoStream(
        stream=VideoStream(
            file=File(path=path, file_size_bytes=64),
            info=VideoStreamInfo(
                track_id=0, codec_name="hevc", fps=25.0,
                fps_fraction=Fraction(25, 1), resolution="1920x1080",
                duration_seconds=duration,
            ),
        ),
        frame_count=frame_count,
        crop=CropParams(),
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


# ---------------------------------------------------------------------------
# JobPhase — JOB_PROBE timing
# ---------------------------------------------------------------------------

class TestJobPhaseTiming:
    """Integration tests for ``JobPhase`` timing instrumentation (Req 6.5)."""

    def test_job_probe_recorded_on_run(self, tmp_path: Path) -> None:
        """``time(MetricKey.JOB)`` must be called when ``run()`` executes.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase

        config   = _make_config(tmp_path)
        volatile = _make_volatile(tmp_path)
        collector = _spy_collector()
        phase    = JobPhase(config, collector=collector, **volatile)

        phase.run()

        # Verify the execute span was recorded under the job metric key
        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.JOB in time_keys_called, (
            f"Expected MetricKey.JOB in time() calls, got: {time_keys_called}"
        )

    def test_job_probe_not_recorded_when_reused(self, tmp_path: Path) -> None:
        """When ``job.yaml`` already exists and the source identity matches,
        the sidecar is kept untouched and the phase returns REUSED.

        The shrunk sidecar no longer persists fast metadata, so a reuse run
        still re-probes it in-memory (interim, until downstream phases migrate
        to the stream model) — the observable reuse contract is the outcome
        plus an unmodified ``job.yaml``.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase
        from pyqenc.stream_model import File, JobSidecar

        config   = _make_config(tmp_path)
        volatile = _make_volatile(tmp_path)
        collector = _spy_collector()
        phase    = JobPhase(config, collector=collector, **volatile)

        # Pre-create a valid job.yaml (the File dump) so the phase takes the
        # REUSED path.
        volatile["work_dir"].mkdir(parents=True, exist_ok=True)
        sidecar = JobSidecar(source=File(
            path            = volatile["source"],
            file_size_bytes = volatile["source"].stat().st_size,
        ))
        job_yaml = volatile["work_dir"] / "job.yaml"
        write_yaml_atomic(job_yaml, sidecar.model_dump(exclude_none=True))
        before = job_yaml.read_bytes()

        result = phase.run()

        assert result.outcome == PhaseOutcome.REUSED
        assert job_yaml.read_bytes() == before, "reuse must not rewrite job.yaml"

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``JobPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.job import JobPhase

        config   = _make_config(tmp_path)
        volatile = _make_volatile(tmp_path)
        collector = NoOpMetricsCollector()
        phase    = JobPhase(config, collector=collector, **volatile)

        result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# ExtractionPhase — EXTRACTION and RECOVERY timing
# ---------------------------------------------------------------------------

class TestExtractionPhaseTiming:
    """Integration tests for ``ExtractionPhase`` timing instrumentation (Req 6.5)."""

    def _make_job_result(self, tmp_path: Path | None = None) -> JobPhaseResult:
        """Return a minimal complete ``JobPhaseResult`` stub."""
        import tempfile as _tempfile
        from pathlib import Path as _Path

        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.job import JobPhaseResult

        # Use a real source path so result.source.name works in phase code.
        if tmp_path is None:
            _td = _Path(_tempfile.mkdtemp())
            source = _td / "source.mkv"
        else:
            source = tmp_path / "source.mkv"
        if not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"\x00" * 64)

        result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            message    = "ok",
            force_wipe = False,
            file       = Artifact(payload=File(path=source, file_size_bytes=64), state=ArtifactState.COMPLETE),
        )
        result.source   = source                          # type: ignore[attr-defined]
        result.work_dir = source.parent / "work"          # type: ignore[attr-defined]
        result.config   = _make_config(source.parent)     # type: ignore[attr-defined]
        result.file     = File(                           # type: ignore[attr-defined]
            path=source, file_size_bytes=source.stat().st_size,
        )
        return result

    def _make_phase(
        self,
        tmp_path: Path,
        collector: MagicMock,
    ) -> ExtractionPhase:
        """Return an ``ExtractionPhase`` with a pre-wired job dependency."""
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.phases.job import JobPhase

        config   = _make_config(tmp_path)
        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result(tmp_path)

        registry: dict[type, object] = {}
        phase = ExtractionPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase] = job_mock  # type: ignore[index]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(MetricKey.RECOVERY)`` must be called even when all artifacts are reused.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phase import Artifact
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.state import ArtifactState
        from pyqenc.stream_model import (
            File,
            SubtitleStream,
            SubtitleStreamInfo,
        )

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        # A complete subtitle row so _recover returns all-complete → REUSED path
        stub_artifact = Artifact(
            payload = SubtitleStream(
                file = File(path=tmp_path / "source.mkv"),
                info = SubtitleStreamInfo(
                    track_id=3, codec_name="subrip",
                    extracted_path=tmp_path / "work" / "extracted" / "sub.srt",
                ),
            ),
            state   = ArtifactState.COMPLETE,
            wanted  = True,
        )

        with patch.object(
            ExtractionPhase, "_recover",
            return_value=Recovery.from_artifacts([stub_artifact]),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.RECOVERY in time_keys_called, (
            f"Expected MetricKey.RECOVERY in time() calls, got: {time_keys_called}"
        )
        assert MetricKey.EXTRACTION not in time_keys_called, (
            "EXTRACTION must not be timed when all artifacts are reused, "
            f"got: {time_keys_called}"
        )

    def test_extraction_recorded_for_mkvextract_tracks(self, tmp_path: Path) -> None:
        """Both ``time(MetricKey.RECOVERY)`` and ``time(MetricKey.EXTRACTION)``
        are called when extraction produces pending artifacts.

        Validates: Requirements 6.5, 2.5
        """
        from pyqenc.phase import Artifact
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.state import ArtifactState
        from pyqenc.stream_model import (
            File,
            SubtitleStream,
            SubtitleStreamInfo,
        )

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        extracted_dir = tmp_path / "work" / "extracted"
        extracted_dir.mkdir(parents=True, exist_ok=True)

        absent_path = extracted_dir / "sub_0_eng.srt"

        stub_artifact = Artifact(
            payload = SubtitleStream(
                file = File(path=tmp_path / "source.mkv"),
                info = SubtitleStreamInfo(
                    track_id=3, codec_name="subrip",
                    extracted_path=absent_path,
                ),
            ),
            state   = ArtifactState.ABSENT,
            wanted  = True,
        )

        with (
            patch.object(
                ExtractionPhase, "_recover",
                return_value=Recovery.from_artifacts([stub_artifact]),
            ),
            patch("pyqenc.phases.extraction._probe_streams_json",
                  return_value={"streams": [], "chapters": []}),
            patch("pyqenc.phases.extraction._extract_timestamps"),
            patch("pyqenc.phases.extraction.run_ffmpeg") as mock_ffmpeg,
        ):
            mock_ffmpeg.return_value = MagicMock(success=True)
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.RECOVERY in time_keys_called, (
            f"Expected MetricKey.RECOVERY in time() calls, got: {time_keys_called}"
        )
        assert MetricKey.EXTRACTION in time_keys_called, (
            f"Expected MetricKey.EXTRACTION in time() calls, got: {time_keys_called}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``ExtractionPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phase import Artifact
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.state import ArtifactState
        from pyqenc.stream_model import (
            File,
            SubtitleStream,
            SubtitleStreamInfo,
        )

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        stub_artifact = Artifact(
            payload = SubtitleStream(
                file = File(path=tmp_path / "source.mkv"),
                info = SubtitleStreamInfo(
                    track_id=3, codec_name="subrip",
                    extracted_path=tmp_path / "work" / "extracted" / "sub.srt",
                ),
            ),
            state   = ArtifactState.COMPLETE,
            wanted  = True,
        )

        with patch.object(
            ExtractionPhase, "_recover",
            return_value=Recovery.from_artifacts([stub_artifact]),
        ):
            result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# ChunkingPhase — CHUNKING_SCENE_DETECT, CHUNKING_SPLIT, and RECOVERY timing
# ---------------------------------------------------------------------------

class TestChunkingPhaseTiming:
    """Integration tests for ``ChunkingPhase`` timing instrumentation (Req 6.5)."""

    def _make_job_result(self, tmp_path: Path) -> JobPhaseResult:
        """Return a minimal complete ``JobPhaseResult`` stub with a real source file."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.job import JobPhaseResult

        source = tmp_path / "source.mkv"
        source.write_bytes(b"" * 64)

        result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            message    = "ok",
            force_wipe = False,
            file       = Artifact(payload=File(path=source, file_size_bytes=64), state=ArtifactState.COMPLETE),
        )
        result.source   = source              # type: ignore[attr-defined]
        result.work_dir = tmp_path / "work"   # type: ignore[attr-defined]
        result.config   = _make_config(tmp_path)  # type: ignore[attr-defined]
        return result

    def _make_phase(
        self,
        tmp_path:  Path,
        collector: MagicMock,
    ) -> ChunkingPhase:
        """Return a ``ChunkingPhase`` with pre-wired job/extraction/probe deps."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
        from pyqenc.phases.job import JobPhase
        from pyqenc.phases.probe import ProbePhase, ProbePhaseResult

        config = _make_config(tmp_path)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        source = tmp_path / "source.mkv"
        stream = _make_extended_stream(source, frame_count=640, duration=26.67)

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result(tmp_path)

        extraction_mock = MagicMock(spec=ExtractionPhase)
        extraction_mock.result = ExtractionPhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="ok",
            video_stream=stream.stream,
        )

        probe_mock = MagicMock(spec=ProbePhase)
        probe_mock.result = ProbePhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="ok",
            stream=Artifact(payload=stream, state=ArtifactState.COMPLETE),
        )

        from pyqenc.phases.chunking import ChunkingPhase
        registry: dict[type, object] = {}
        phase = ChunkingPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]        = job_mock         # type: ignore[index]
        registry[ExtractionPhase] = extraction_mock  # type: ignore[index]
        registry[ProbePhase]      = probe_mock       # type: ignore[index]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(MetricKey.RECOVERY)`` must be called when boundaries are cached.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phases.chunking import (
            ChunkingSidecar,  # noqa: F401 — via stream_model
        )
        from pyqenc.stream_model import ChunkingSidecar as _CS
        from pyqenc.stream_model import SceneRecord

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        write_yaml_atomic(
            work_dir / "chunking.yaml",
            _CS(scenes=[SceneRecord(timestamp_seconds=0.0, frame=0)]).model_dump(),
        )

        result = phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.RECOVERY in time_keys_called, (
            f"Expected MetricKey.RECOVERY in time() calls, got: {time_keys_called}"
        )
        assert result.outcome.value == "reused"

    def test_scene_detect_recorded_when_no_cached_boundaries(self, tmp_path: Path) -> None:
        """``time(MetricKey.CHUNKING, "scene_detect")`` runs when detection runs."""
        from pyqenc.models import SceneBoundary

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        with patch("pyqenc.phases.chunking.detect_scenes",
                   return_value=[SceneBoundary(frame=0, timestamp_seconds=0.0)]):
            result = phase.run()

        calls = [call.args for call in collector.time.call_args_list]
        assert (MetricKey.CHUNKING, "scene_detect") in calls, f"scene_detect span missing: {calls}"
        assert result.outcome.value == "completed"

    def test_scene_detect_not_recorded_when_boundaries_cached(self, tmp_path: Path) -> None:
        """Cached boundaries skip detection entirely — no scene_detect span."""
        from pyqenc.stream_model import ChunkingSidecar as _CS
        from pyqenc.stream_model import SceneRecord

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        write_yaml_atomic(
            work_dir / "chunking.yaml",
            _CS(scenes=[SceneRecord(timestamp_seconds=0.0, frame=0)]).model_dump(),
        )

        with patch("pyqenc.phases.chunking.detect_scenes") as detect_mock:
            phase.run()

        detect_mock.assert_not_called()
        calls = [call.args for call in collector.time.call_args_list]
        assert (MetricKey.CHUNKING, "scene_detect") not in calls

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``ChunkingPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.models import SceneBoundary

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        with patch("pyqenc.phases.chunking.detect_scenes",
                   return_value=[SceneBoundary(frame=0, timestamp_seconds=0.0)]):
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
    ) -> AudioPhase:
        """Return an ``AudioPhase`` with pre-wired job and extraction dependencies."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.audio import AudioPhase
        from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
        from pyqenc.phases.job import JobPhase, JobPhaseResult

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        config   = _make_config(tmp_path)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        job_result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            message    = "ok",
            force_wipe = False,
            file       = Artifact(payload=File(path=source, file_size_bytes=64), state=ArtifactState.COMPLETE),
        )
        job_result.source   = source      # type: ignore[attr-defined]
        job_result.work_dir = work_dir    # type: ignore[attr-defined]
        job_result.config   = config      # type: ignore[attr-defined]

        extraction_result = ExtractionPhaseResult(
            outcome      = PhaseOutcome.COMPLETED,
            message      = "ok",
            video_stream = _make_video_stream_fixture(source),
        )

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = job_result

        extraction_mock = MagicMock(spec=ExtractionPhase)
        extraction_mock.result = extraction_result

        registry: dict[type, object] = {}
        phase = AudioPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]        = job_mock         # type: ignore[index]
        registry[ExtractionPhase] = extraction_mock  # type: ignore[index]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(MetricKey.RECOVERY)`` must be called even when all artifacts are reused.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phases.audio import AudioPhase

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        stub_row = Artifact(
            payload=_audio_output(tmp_path / "work" / "audio" / "track.aac"),
            state=ArtifactState.COMPLETE,
        )

        with patch.object(AudioPhase, "_recover", return_value=Recovery.from_artifacts([stub_row])):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.RECOVERY in time_keys_called, (
            f"Expected MetricKey.RECOVERY in time() calls, got: {time_keys_called}"
        )

    def test_audio_recorded_when_processing_runs(self, tmp_path: Path) -> None:
        """``time(MetricKey.AUDIO)`` must be called when audio processing executes.

        Validates: Requirements 6.5
        """
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.audio import AudioPhase, AudioPhaseResult

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        stub_artifact = Artifact(
            payload=_audio_output(tmp_path / "work" / "audio" / "track.aac"),
            state=ArtifactState.ABSENT,
        )

        stub_result = AudioPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "ok",
        )

        with (
            patch.object(AudioPhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])),
            patch.object(AudioPhase, "_execute", return_value=stub_result),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.AUDIO in time_keys_called, (
            f"Expected MetricKey.AUDIO in time() calls, got: {time_keys_called}"
        )

    def test_audio_not_recorded_when_all_reused(self, tmp_path: Path) -> None:
        """``time(MetricKey.AUDIO)`` must NOT be called when all artifacts are already complete.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.audio import AudioPhase

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        stub_row = Artifact(
            payload=_audio_output(tmp_path / "work" / "audio" / "track.aac"),
            state=ArtifactState.COMPLETE,
        )

        with patch.object(AudioPhase, "_recover", return_value=Recovery.from_artifacts([stub_row])):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.AUDIO not in time_keys_called, (
            f"Expected MetricKey.AUDIO NOT called on reuse, got: {time_keys_called}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``AudioPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.audio import AudioPhase

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        stub_row = Artifact(
            payload=_audio_output(tmp_path / "work" / "audio" / "track.aac"),
            state=ArtifactState.COMPLETE,
        )

        with patch.object(AudioPhase, "_recover", return_value=Recovery.from_artifacts([stub_row])):
            result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# OptimizationPhase — ENCODING_OPTIMIZATION and RECOVERY timing
# ---------------------------------------------------------------------------


class TestOptimizationPhaseTiming:
    """Integration tests for ``OptimizationPhase`` timing instrumentation (Req 6.5)."""

    def _make_job_result(self, tmp_path: Path, *, config: AppConfig | None = None) -> JobPhaseResult:
        """Return a minimal complete ``JobPhaseResult`` stub."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.job import JobPhaseResult

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            message    = "ok",
            force_wipe = False,
            file       = Artifact(payload=File(path=source, file_size_bytes=64), state=ArtifactState.COMPLETE),
        )
        result.source   = source                                       # type: ignore[attr-defined]
        result.work_dir = tmp_path / "work"                            # type: ignore[attr-defined]
        result.config   = config if config is not None else _make_config(tmp_path)  # type: ignore[attr-defined]
        return result

    def _make_chunking_result(self, tmp_path: Path) -> ChunkingPhaseResult:
        """Return a minimal complete ``ChunkingPhaseResult`` stub with one chunk."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.chunking import ChunkingPhaseResult

        chunk = _make_chunk_window(tmp_path / "source.mkv", 0.0, 1.0)

        return ChunkingPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "ok",
            chunks    = [Artifact(payload=chunk, state=ArtifactState.COMPLETE)],
        )

    def _make_phase(
        self,
        tmp_path:  Path,
        collector: MagicMock,
        *,
        optimize:  bool = True,
    ) -> OptimizationPhase:
        """Return an ``OptimizationPhase`` with pre-wired job, probe and chunking dependencies."""
        from pyqenc.phase import PhaseOutcome
        from pyqenc.phases.chunking import ChunkingPhase
        from pyqenc.phases.job import JobPhase
        from pyqenc.phases.optimization import OptimizationPhase
        from pyqenc.phases.probe import ProbePhase

        config = _make_config(tmp_path)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        config.encoding.optimize   = optimize
        config.encoding.strategies = [
            s.raw if hasattr(s, "raw") else f"{s.profile}+{s.preset}"
            for s in [_STRATEGY_SLOW_H265, _STRATEGY_H265_AQ]
        ]
        # Reset resolved caches so they re-resolve from the updated strategy strings.
        config.encoding._resolved_targets   = None
        config.encoding._resolved_strategies = None
        config.encoding.resolve(config.codecs, config.profiles)

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result(tmp_path, config=config)

        # ProbePhase is a dependency; the uniform run() resolves deps first, so a
        # completed probe result must be present for the reuse path to be reached.
        # The result is a REAL typed result (recovery builds a ProbeState from
        # .source/.crop — bare Mocks would fail pydantic validation).
        from pyqenc.phases.probe import ProbePhaseResult
        probe_mock = MagicMock(spec=ProbePhase)
        probe_mock.result = ProbePhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "probe complete",
            stream    = None,
        )

        chunking_mock = MagicMock(spec=ChunkingPhase)
        chunking_mock.result = self._make_chunking_result(tmp_path)

        registry: dict[type, object] = {}
        phase = OptimizationPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]      = job_mock       # type: ignore[index]
        registry[ProbePhase]    = probe_mock     # type: ignore[index]
        registry[ChunkingPhase] = chunking_mock  # type: ignore[index]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(MetricKey.RECOVERY)`` must be called when all results are already cached.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.models import CropParams
        from pyqenc.state import OptimizationParams, ProbeState, StrategyTestResult

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector, optimize=True)

        strategy = _STRATEGY_SLOW_H265
        # tolerance_pct and metrics_sampling must match config defaults so the
        # full-reuse path (step 4) is taken rather than falling through to encodes.
        persisted = OptimizationParams(
            probe            = ProbeState(frame_count=0, crop=CropParams()),
            test_chunks      = ["chunk_0"],
            strategy_results = [
                StrategyTestResult(strategy=strategy.display_name(), total_size=1024),
                StrategyTestResult(strategy=_STRATEGY_H265_AQ.display_name(), total_size=512),
            ],
            tolerance_pct    = 5.0,   # matches AppConfig.encoding.strategy_selection_tolerance default
            selected         = [strategy.display_name()],
            quality_targets  = [],
            sampling = 1,     # matches AppConfig.encoding.sampling default
        )

        # All results cached with matching tolerance → reuse path (step 4 in run())
        with patch.object(OptimizationParams, "load", return_value=persisted):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.RECOVERY in time_keys_called, (
            f"Expected MetricKey.RECOVERY in time() calls on reuse path, got: {time_keys_called}"
        )

    def test_optimization_prefix_threads_through_shared_encoder(self, tmp_path: Path) -> None:
        """Test encodes are attributed to the optimization phase, not encoding.

        ``_encode_chunks_parallel`` records NO top-level span any more (the
        owning phase's template owns it); with ``metric_prefix=OPTIMIZATION``
        its convergence ``step`` lands under the optimization key (Req: TODO-1
        fix — optimization follows the encoding contract via the shared
        encoder, no code duplication).

        Validates: Requirements 6.5, 2.2a
        """
        import asyncio

        from pyqenc.phases.encoding import ChunkEncodingResult, _encode_chunks_parallel

        collector = _spy_collector()
        strategy  = _STRATEGY_SLOW_H265

        chunk = _make_chunk_window(tmp_path / "source.mkv", 0.0, 1.0)

        encoded_path = tmp_path / "chunk_0_enc.mkv"
        encoded_path.write_bytes(bytes(128))

        successful_result = ChunkEncodingResult(
            chunk_id     = "chunk_0",
            strategy     = strategy.display_name(),
            success      = True,
            final_crf    = 28.0,
            attempts     = 2,
            encoded_file = MagicMock(path=encoded_path, resolution="1920x1080"),
            reused       = False,
        )

        stub_recovery = MagicMock()
        stub_recovery.pairs = {}

        with (
            patch("pyqenc.phases.encoding._recover_encoding_attempts", return_value=stub_recovery),
            patch("pyqenc.phases.encoding._encode_chunk_async", return_value=successful_result),
        ):
            asyncio.run(
                _encode_chunks_parallel(
                    encoder          = MagicMock(),
                    chunks           = [chunk],
                    strategies       = [strategy],
                    quality_targets  = [],
                    max_parallel     = 1,
                    force            = False,
                    collector        = collector,
                    metric_prefix    = MetricKey.OPTIMIZATION,
                )
            )

        # No top-level span is recorded by the executor itself...
        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.ENCODING not in time_keys_called, (
            f"Executor must not record a top-level span (phase template owns it), got: {time_keys_called}"
        )
        # ...and convergence steps land under the owning phase's prefix.
        step_keys = [call.args[0] for call in collector.step.call_args_list]
        assert MetricKey.OPTIMIZATION in step_keys, (
            f"Expected step(OPTIMIZATION) for the optimization-owned encode, got: {step_keys}"
        )

    def test_step_called_with_convergence_update_per_chunk(self, tmp_path: Path) -> None:
        """``step(MetricKey.ENCODING, convergence_update=...)`` must be called
        once per successfully converged test chunk inside _encode_chunks_parallel.

        Validates: Requirements 6.5, 4.1a
        """
        import asyncio

        from pyqenc.metrics import ConvergenceUpdate
        from pyqenc.phases.encoding import ChunkEncodingResult, _encode_chunks_parallel

        collector = _spy_collector()
        strategy  = _STRATEGY_SLOW_H265

        chunk = _make_chunk_window(tmp_path / "source.mkv", 0.0, 1.0)

        # Reference file must exist so the encode path is reached (not skipped)
        (tmp_path / "chunk_0.mkv").write_bytes(b"\x00" * 64)

        encoded_path = tmp_path / "chunk_0_enc.mkv"
        encoded_path.write_bytes(b"\x00" * 128)

        successful_result = ChunkEncodingResult(
            chunk_id     = "chunk_0",
            strategy     = strategy.display_name(),
            success      = True,
            final_crf    = 28.0,
            attempts     = 3,
            encoded_file = MagicMock(path=encoded_path, resolution="1920x1080"),
            reused       = False,
        )

        stub_recovery = MagicMock()
        stub_recovery.pairs = {}

        with (
            patch("pyqenc.phases.encoding._recover_encoding_attempts", return_value=stub_recovery),
            patch("pyqenc.phases.encoding._encode_chunk_async", return_value=successful_result),
        ):
            asyncio.run(
                _encode_chunks_parallel(
                    encoder          = MagicMock(),
                    chunks           = [chunk],
                    strategies       = [strategy],
                    quality_targets  = [],
                    max_parallel     = 1,
                    force            = False,
                    collector        = collector,
                )
            )

        step_calls = collector.step.call_args_list
        assert len(step_calls) == 1, f"Expected 1 step() call, got {len(step_calls)}"
        call_key    = step_calls[0].args[0]
        call_update = step_calls[0].kwargs.get("convergence_update")
        assert call_key == MetricKey.ENCODING, f"Wrong key: {call_key}"
        assert isinstance(call_update, ConvergenceUpdate), f"Expected ConvergenceUpdate, got: {call_update}"
        assert call_update.strategy      == strategy.display_name(), f"Wrong strategy: {call_update.strategy}"
        assert call_update.attempt_count == 3,             f"Wrong attempt_count: {call_update.attempt_count}"

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``OptimizationPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.models import CropParams
        from pyqenc.state import OptimizationParams, ProbeState, StrategyTestResult

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector, optimize=True)  # type: ignore[arg-type]

        strategy = _STRATEGY_SLOW_H265
        persisted = OptimizationParams(
            probe            = ProbeState(frame_count=0, crop=CropParams()),
            test_chunks      = ["chunk_0"],
            strategy_results = [
                StrategyTestResult(strategy=strategy.display_name(), total_size=1024),
                StrategyTestResult(strategy=_STRATEGY_H265_AQ.display_name(), total_size=512),
            ],
            tolerance_pct    = 0.0,
            selected         = [strategy.display_name()],
            quality_targets  = [],
            sampling = 1,
        )

        with patch.object(OptimizationParams, "load", return_value=persisted):
            result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# EncodingPhase — ENCODING_MAIN and RECOVERY timing
# ---------------------------------------------------------------------------


class TestEncodingPhaseTiming:
    """Integration tests for ``EncodingPhase`` timing instrumentation (Req 6.5)."""

    def _make_job_result(self, tmp_path: Path) -> JobPhaseResult:
        """Return a minimal complete ``JobPhaseResult`` stub."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.job import JobPhaseResult

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            message    = "ok",
            force_wipe = False,
            file       = Artifact(payload=File(path=source, file_size_bytes=64), state=ArtifactState.COMPLETE),
        )
        result.source   = source              # type: ignore[attr-defined]
        result.work_dir = tmp_path / "work"   # type: ignore[attr-defined]
        result.config   = _make_config(tmp_path)  # type: ignore[attr-defined]
        return result

    def _make_chunking_result(self, tmp_path: Path) -> ChunkingPhaseResult:
        """Return a minimal complete ``ChunkingPhaseResult`` stub with one chunk."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.chunking import ChunkingPhaseResult

        chunk = _make_chunk_window(tmp_path / "source.mkv", 0.0, 1.0)

        return ChunkingPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "ok",
            chunks    = [Artifact(payload=chunk, state=ArtifactState.COMPLETE)],
        )

    def _make_optimization_result(self, tmp_path: Path) -> OptimizationPhaseResult:
        """Return a minimal complete ``OptimizationPhaseResult`` stub."""
        from pyqenc.phases.optimization import OptimizationPhaseResult

        return OptimizationPhaseResult(
            outcome           = PhaseOutcome.COMPLETED,
            message           = "ok",
            selected_strategies = [_STRATEGY_SLOW_H265],
        )

    def _make_phase(
        self,
        tmp_path:  Path,
        collector: MagicMock,
    ) -> EncodingPhase:
        """Return an ``EncodingPhase`` with pre-wired job, probe, chunking, and optimization deps."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.chunking import ChunkingPhase
        from pyqenc.phases.encoding import EncodingPhase
        from pyqenc.phases.job import JobPhase
        from pyqenc.phases.optimization import OptimizationPhase
        from pyqenc.phases.probe import ProbePhase, ProbePhaseResult

        config   = _make_config(tmp_path)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result(tmp_path)

        probe_result = ProbePhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "ok",
            stream    = None,
        )
        probe_mock = MagicMock(spec=ProbePhase)
        probe_mock.result = probe_result

        chunking_mock = MagicMock(spec=ChunkingPhase)
        chunking_mock.result = self._make_chunking_result(tmp_path)

        optimization_mock = MagicMock(spec=OptimizationPhase)
        optimization_mock.result = self._make_optimization_result(tmp_path)

        registry: dict[type, object] = {}
        phase = EncodingPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]          = job_mock           # type: ignore[index]
        registry[ProbePhase]        = probe_mock         # type: ignore[index]
        registry[ChunkingPhase]     = chunking_mock      # type: ignore[index]
        registry[OptimizationPhase] = optimization_mock  # type: ignore[index]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(MetricKey.RECOVERY)`` must be called even when all pairs are already complete.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phases.encoding import EncodingPhase

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        stub_row = Artifact(
            payload=_encoded_chunk(tmp_path / "chunk_0.mkv", "chunk_0", "slow+h265"),
            state=ArtifactState.COMPLETE,
        )

        with patch.object(EncodingPhase, "_recover", return_value=Recovery.from_artifacts([stub_row])):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.RECOVERY in time_keys_called, (
            f"Expected MetricKey.RECOVERY in time() calls, got: {time_keys_called}"
        )

    def test_encoding_main_recorded_when_encodes_run(self, tmp_path: Path) -> None:
        """``time(MetricKey.ENCODING)`` must be called when encoding executes.

        The top-level span is owned by the phase template's ``run()`` (it wraps
        ``_execute``), so drive the phase with one pending artifact and a
        stubbed executor.

        Validates: Requirements 6.5, 2.2a
        """
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.encoding import (
            EncodingPhase,
            EncodingPhaseResult,
        )

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        stub_artifact = Artifact(
            payload=_encoded_chunk(tmp_path / "chunk_0.mkv", "chunk_0", "slow+h265"),
            state=ArtifactState.ABSENT,
        )

        stub_result = EncodingPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "ok",
        )

        with (
            patch.object(EncodingPhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])),
            patch.object(EncodingPhase, "_execute", return_value=stub_result),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.ENCODING in time_keys_called, (
            f"Expected MetricKey.ENCODING in time() calls, got: {time_keys_called}"
        )

    def test_step_called_with_convergence_update_per_chunk(self, tmp_path: Path) -> None:
        """``step(MetricKey.ENCODING, convergence_update=...)`` must be called
        once per successfully converged (non-reused) chunk/strategy pair.

        Validates: Requirements 6.5, 4.1a
        """
        import asyncio

        from pyqenc.metrics import ConvergenceUpdate
        from pyqenc.phases.encoding import ChunkEncodingResult, _encode_chunks_parallel

        collector = _spy_collector()

        chunk = _make_chunk_window(tmp_path / "source.mkv", 0.0, 1.0)

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
            encoded_file = MagicMock(path=encoded_path, resolution="1920x1080"),
            reused       = False,
        )

        with patch("pyqenc.phases.encoding._encode_chunk_async", return_value=successful_result):
            asyncio.run(
                _encode_chunks_parallel(
                    encoder          = MagicMock(),
                    chunks           = [chunk],
                    strategies       = [_STRATEGY_SLOW_H265],
                    quality_targets  = [],
                    max_parallel     = 1,
                    force            = False,
                    collector        = collector,
                )
            )

        step_calls = collector.step.call_args_list
        assert len(step_calls) == 1, f"Expected 1 step() call, got {len(step_calls)}"
        call_key    = step_calls[0].args[0]
        call_update = step_calls[0].kwargs.get("convergence_update")
        assert call_key == MetricKey.ENCODING, f"Wrong key: {call_key}"
        assert isinstance(call_update, ConvergenceUpdate), f"Expected ConvergenceUpdate, got: {call_update}"
        assert call_update.strategy      == _STRATEGY_SLOW_H265.display_name(), f"Wrong strategy: {call_update.strategy}"
        assert call_update.attempt_count == 3,                         f"Wrong attempt_count: {call_update.attempt_count}"

    def test_step_not_called_for_reused_pairs(self, tmp_path: Path) -> None:
        """``step(MetricKey.ENCODING)`` must NOT be called for reused pairs.

        Reused pairs have already been counted in a prior run — no new convergence
        data to record.

        Validates: Requirements 6.5, 4.1a
        """
        import asyncio

        from pyqenc.phases.encoding import ChunkEncodingResult, _encode_chunks_parallel

        collector = _spy_collector()

        chunk = _make_chunk_window(tmp_path / "source.mkv", 0.0, 1.0)

        encoded_path = tmp_path / "chunk_0_enc.mkv"
        encoded_path.write_bytes(b"\x00" * 128)

        reused_result = ChunkEncodingResult(
            chunk_id     = "chunk_0",
            strategy     = "slow+h265",
            success      = True,
            final_crf    = 28.0,
            attempts     = 1,
            encoded_file = MagicMock(path=encoded_path, resolution="1920x1080"),
            reused       = True,
        )

        with patch("pyqenc.phases.encoding._encode_chunk_async", return_value=reused_result):
            asyncio.run(
                _encode_chunks_parallel(
                    encoder          = MagicMock(),
                    chunks           = [chunk],
                    strategies       = [_STRATEGY_SLOW_H265],
                    quality_targets  = [],
                    max_parallel     = 1,
                    force            = False,
                    collector        = collector,
                )
            )

        step_calls = collector.step.call_args_list
        assert len(step_calls) == 0, (
            f"Expected no step() calls for reused pair, got: {step_calls}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``EncodingPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.encoding import EncodingPhase

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        stub_row = Artifact(
            payload=_encoded_chunk(tmp_path / "chunk_0.mkv", "chunk_0", "slow+h265"),
            state=ArtifactState.COMPLETE,
        )

        with patch.object(EncodingPhase, "_recover", return_value=Recovery.from_artifacts([stub_row])):
            result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# MergePhase — MERGE_CONCAT, MERGE_QUALITY_MEASURE, RECOVERY timing
# ---------------------------------------------------------------------------


class TestMergePhaseTiming:
    """Integration tests for ``MergePhase`` timing instrumentation (Req 6.5)."""

    def _make_job_result(self, tmp_path: Path) -> JobPhaseResult:
        """Return a minimal complete ``JobPhaseResult`` stub."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.job import JobPhaseResult

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            message    = "ok",
            force_wipe = False,
            file       = Artifact(payload=File(path=source, file_size_bytes=64), state=ArtifactState.COMPLETE),
        )
        result.source   = source              # type: ignore[attr-defined]
        result.work_dir = tmp_path / "work"   # type: ignore[attr-defined]
        result.config   = _make_config(tmp_path)  # type: ignore[attr-defined]
        return result

    def _make_encoding_result(self, tmp_path: Path) -> EncodingPhaseResult:
        """Return a minimal complete ``EncodingPhaseResult`` with one winner row."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.encoding import EncodingPhaseResult

        encoded_path = tmp_path / "work" / "encoded" / "slow+h265" / "chunk_0.mkv"
        encoded_path.parent.mkdir(parents=True, exist_ok=True)
        encoded_path.write_bytes(b"\x00" * 128)

        winner = Artifact(
            payload  = _encoded_chunk(encoded_path, "chunk_0", "slow+h265"),
            state    = ArtifactState.COMPLETE,
        )
        return EncodingPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "ok",
            winners   = [winner],
        )

    def _make_audio_result(self) -> AudioPhaseResult:
        """Return a minimal complete ``AudioPhaseResult`` stub."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.audio import AudioPhaseResult

        return AudioPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "ok",
        )

    def _make_phase(
        self,
        tmp_path:  Path,
        collector: MagicMock,
    ) -> MergePhase:
        """Return a ``MergePhase`` with pre-wired job, extraction, encoding, and audio deps."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.audio import AudioPhase
        from pyqenc.phases.encoding import EncodingPhase
        from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
        from pyqenc.phases.job import JobPhase
        from pyqenc.phases.merge import MergePhase

        config   = _make_config(tmp_path)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        # Create a real timestamps.txt so timestamps_path.exists() passes
        ts_file = work_dir / "extracted" / "timestamps.txt"
        ts_file.parent.mkdir(parents=True, exist_ok=True)
        ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result(tmp_path)

        from pyqenc.phase import Artifact as _Artifact
        from pyqenc.state import ArtifactState as _ArtifactState
        from pyqenc.stream_model import (
            File as _File,
        )
        from pyqenc.stream_model import (
            VideoStream as _VideoStream,
        )
        from pyqenc.stream_model import (
            VideoStreamInfo as _VideoStreamInfo,
        )

        video_row = _Artifact(
            payload = _VideoStream(
                file = _File(path=work_dir / "source.mkv"),
                info = _VideoStreamInfo(track_id=0),
            ),
            state   = _ArtifactState.COMPLETE,
        )
        extraction_result = ExtractionPhaseResult(
            outcome      = PhaseOutcome.COMPLETED,
            message      = "ok",
            video_stream = video_row,
            work_dir     = work_dir,
        )
        extraction_mock = MagicMock(spec=ExtractionPhase)
        extraction_mock.result = extraction_result

        encoding_mock = MagicMock(spec=EncodingPhase)
        encoding_mock.result = self._make_encoding_result(tmp_path)

        audio_mock = MagicMock(spec=AudioPhase)
        audio_mock.result = self._make_audio_result()

        from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
        probe_mock = MagicMock(spec=ProbePhase)
        probe_mock.result = ProbePhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "probe complete",
            stream    = None,
        )

        registry: dict[type, object] = {}
        phase = MergePhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]        = job_mock         # type: ignore[index]
        registry[ExtractionPhase] = extraction_mock  # type: ignore[index]
        registry[ProbePhase]      = probe_mock       # type: ignore[index]
        registry[EncodingPhase]   = encoding_mock    # type: ignore[index]
        registry[AudioPhase]      = audio_mock       # type: ignore[index]
        return phase

    def test_recovery_recorded_on_reused_path(self, tmp_path: Path) -> None:
        """``time(MetricKey.RECOVERY)`` must be called even when all artifacts are already complete.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phases.merge import MergePhase
        from pyqenc.state import ArtifactState

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        output_file = tmp_path / "work" / "merged" / "source slow+h265.mkv"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(b"\x00" * 64)

        stub_artifact = _merged_row(output_file, ArtifactState.COMPLETE)

        with patch.object(MergePhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.RECOVERY in time_keys_called, (
            f"Expected MetricKey.RECOVERY in time() calls, got: {time_keys_called}"
        )

    def test_merge_concat_recorded_when_merge_runs(self, tmp_path: Path) -> None:
        """``time(MetricKey.MERGE)`` must be called when a pending strategy is merged.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.merge import MergePhase
        from pyqenc.state import ArtifactState

        collector = _spy_collector()
        phase     = self._make_phase(tmp_path, collector)

        output_file = tmp_path / "work" / "merged" / "source slow+h265.mkv"
        output_file.parent.mkdir(parents=True, exist_ok=True)

        stub_artifact = _merged_row(output_file, ArtifactState.ABSENT)

        encoded_path = tmp_path / "work" / "encoded" / "slow+h265" / "chunk_0.mkv"
        encoded_path.parent.mkdir(parents=True, exist_ok=True)
        encoded_path.write_bytes(b"\x00" * 128)

        with (
            patch.object(MergePhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])),
            patch("pyqenc.phases.merge.subprocess.run") as mock_subprocess,
            patch("pyqenc.phases.merge.get_frame_count", return_value=100),
            patch.object(MergePhase, "_collect_encoded_chunks", return_value={
                "chunk_0": {"slow+h265": encoded_path},
            }),
        ):
            mock_subprocess.return_value = MagicMock(returncode=0, stderr="")
            # Create the output file so the merge "succeeds"
            output_file.write_bytes(b"\x00" * 128)
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.MERGE in time_keys_called, (
            f"Expected MetricKey.MERGE in time() calls, got: {time_keys_called}"
        )

    def test_merge_quality_measure_recorded_when_targets_set(self, tmp_path: Path) -> None:
        """``time(MetricKey.MERGE)`` must be called when quality targets are configured.

        Validates: Requirements 6.5
        """
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.merge import MergePhase
        from pyqenc.state import ArtifactState

        collector = _spy_collector()

        # Build config with a quality target so _measure_quality branch is entered
        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)
        config = _make_config(tmp_path)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        from pyqenc.phases.audio import AudioPhase
        from pyqenc.phases.encoding import EncodingPhase
        from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
        from pyqenc.phases.job import JobPhase

        ts_file = work_dir / "extracted" / "timestamps.txt"
        ts_file.parent.mkdir(parents=True, exist_ok=True)
        ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result(tmp_path)

        from pyqenc.phase import Artifact as _Artifact
        from pyqenc.state import ArtifactState as _ArtifactState
        from pyqenc.stream_model import (
            File as _File,
        )
        from pyqenc.stream_model import (
            VideoStream as _VideoStream,
        )
        from pyqenc.stream_model import (
            VideoStreamInfo as _VideoStreamInfo,
        )

        video_row = _Artifact(
            payload = _VideoStream(
                file = _File(path=work_dir / "source.mkv"),
                info = _VideoStreamInfo(track_id=0),
            ),
            state   = _ArtifactState.COMPLETE,
        )
        extraction_result = ExtractionPhaseResult(
            outcome      = PhaseOutcome.COMPLETED,
            message      = "ok",
            video_stream = video_row,
            work_dir     = work_dir,
        )
        extraction_mock = MagicMock(spec=ExtractionPhase)
        extraction_mock.result = extraction_result

        encoding_mock = MagicMock(spec=EncodingPhase)
        encoding_mock.result = self._make_encoding_result(tmp_path)

        audio_mock = MagicMock(spec=AudioPhase)
        audio_mock.result = self._make_audio_result()

        from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
        probe_mock = MagicMock(spec=ProbePhase)
        probe_mock.result = ProbePhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "probe complete",
            stream    = None,
        )

        registry: dict[type, object] = {}
        phase = MergePhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]        = job_mock         # type: ignore[index]
        registry[ExtractionPhase] = extraction_mock  # type: ignore[index]
        registry[ProbePhase]      = probe_mock       # type: ignore[index]
        registry[EncodingPhase]   = encoding_mock    # type: ignore[index]
        registry[AudioPhase]      = audio_mock       # type: ignore[index]

        output_file = tmp_path / "work" / "merged" / "source slow+h265.mkv"
        output_file.parent.mkdir(parents=True, exist_ok=True)

        stub_artifact = _merged_row(output_file, ArtifactState.ABSENT)

        encoded_path = tmp_path / "work" / "encoded" / "slow+h265" / "chunk_0.mkv"
        encoded_path.parent.mkdir(parents=True, exist_ok=True)
        encoded_path.write_bytes(b"\x00" * 128)

        with (
            patch.object(MergePhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])),
            patch("pyqenc.phases.merge.subprocess.run") as mock_subprocess,
            patch("pyqenc.phases.merge.get_frame_count", return_value=100),
            patch.object(MergePhase, "_collect_encoded_chunks", return_value={
                "chunk_0": {"slow+h265": encoded_path},
            }),
            patch("pyqenc.phases.merge._measure_quality", return_value=({}, False, None)),
        ):
            mock_subprocess.return_value = MagicMock(returncode=0, stderr="")
            output_file.write_bytes(b"\x00" * 128)
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert MetricKey.MERGE in time_keys_called, (
            f"Expected MetricKey.MERGE in time() calls, got: {time_keys_called}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``MergePhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.merge import MergePhase
        from pyqenc.state import ArtifactState

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        output_file = tmp_path / "work" / "merged" / "source slow+h265.mkv"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(b"\x00" * 64)

        stub_artifact = _merged_row(output_file, ArtifactState.COMPLETE)

        with patch.object(MergePhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])):
            result = phase.run()

        assert result is not None


# ---------------------------------------------------------------------------
# Smoke tests — MetricKey, time(), step(), NoOp, Strategy dots, YAML tiers
# ---------------------------------------------------------------------------


class TestMetricKeySmoke:
    """Smoke tests for MetricKey enum, collector API, strategy dot sanitization,
    and two-tier YAML structure.

    Validates: Requirements 6.1, 6.2, 6.4, 6.5, 6.6
    """

    # ------------------------------------------------------------------
    # 1. MetricKey has exactly 9 members with correct string values
    # ------------------------------------------------------------------

    def test_metric_key_has_nine_members(self) -> None:
        """MetricKey must have exactly 9 members with the correct string values.

        Validates: Requirements 6.1, 6.2 (PROBE added by phase-run-template)
        """
        expected = {
            "JOB":          "job",
            "EXTRACTION":   "extraction",
            "PROBE":        "probe",
            "CHUNKING":     "chunking",
            "AUDIO":        "audio",
            "ENCODING":     "encoding",
            "OPTIMIZATION": "optimization",
            "MERGE":        "merge",
            "RECOVERY":     "recovery",
        }
        assert len(MetricKey) == 9, (
            f"Expected 9 MetricKey members, got {len(MetricKey)}: {list(MetricKey)}"
        )
        for name, value in expected.items():
            member = MetricKey[name]
            assert member.value == value, (
                f"MetricKey.{name} expected value {value!r}, got {member.value!r}"
            )

    # ------------------------------------------------------------------
    # 2. time() accepts MetricKey with zero or more suffix parts
    # ------------------------------------------------------------------

    def test_time_accepts_metric_key_no_suffix(self) -> None:
        """collector.time(MetricKey.ENCODING) must work without error.

        Validates: Requirements 6.4, 6.5
        """
        collector = NoOpMetricsCollector()
        with collector.time(MetricKey.ENCODING):
            pass  # no error expected

    def test_time_accepts_metric_key_with_suffix(self) -> None:
        """collector.time(MetricKey.JOB, "probe") must work without error.

        Validates: Requirements 6.4, 6.5
        """
        collector = NoOpMetricsCollector()
        with collector.time(MetricKey.JOB, "probe"):
            pass  # no error expected

    # ------------------------------------------------------------------
    # 3. step() accepts MetricKey with zero or more suffix parts
    # ------------------------------------------------------------------

    def test_step_accepts_metric_key_no_suffix(self) -> None:
        """collector.step(MetricKey.ENCODING) must work without error.

        Validates: Requirements 6.4, 6.6
        """
        collector = NoOpMetricsCollector()
        collector.step(MetricKey.ENCODING)  # no error expected

    def test_step_accepts_metric_key_with_suffix(self) -> None:
        """collector.step(MetricKey.CHUNKING, "split") must work without error.

        Validates: Requirements 6.4, 6.6
        """
        collector = NoOpMetricsCollector()
        collector.step(MetricKey.CHUNKING, "split")  # no error expected

    # ------------------------------------------------------------------
    # 4. NoOpMetricsCollector accepts both time() and step() with MetricKey
    # ------------------------------------------------------------------

    def test_noop_accepts_time_and_step_with_metric_key(self) -> None:
        """NoOpMetricsCollector must accept time() and step() with MetricKey and suffix parts.

        Validates: Requirements 6.4, 6.5, 6.6
        """
        collector = NoOpMetricsCollector()
        # time() — top-level and dotted
        with collector.time(MetricKey.MERGE):
            pass
        with collector.time(MetricKey.MERGE, "concat"):
            pass
        # step() — top-level and dotted
        collector.step(MetricKey.ENCODING)
        collector.step(MetricKey.ENCODING, "h265")
        # flush() — no-op
        collector.flush()

    # ------------------------------------------------------------------
    # 5. Strategy construction with dots in preset/profile produces name with no ASCII dots
    # ------------------------------------------------------------------

    def test_strategy_dots_sanitized_in_name(self) -> None:
        """Strategy(preset="h265.fast", profile="slow.2") must produce name with no ASCII dots.

        Validates: Requirements 8.1, 8.3, 8.4
        """
        from decimal import Decimal

        from pyqenc.models import CodecConfig

        codec = CodecConfig(
            name="h265-8bit",
            default_quality=Decimal(28),
            default_preset="slow",
            quality_range=(Decimal(0), Decimal(51)),
            encoder_args=["-i", "{input}", "-c:v", "libx265", "-crf", "{quality}", "{input}"],
            presets=["slow"],
        )
        strategy = Strategy(preset="h265.fast", profile="slow.2", codec=codec, profile_args=[])
        assert "." not in strategy.display_name(), (
            f"Expected no ASCII dot in strategy.display_name(), got: {strategy.display_name()!r}"
        )

    # ------------------------------------------------------------------
    # 7. Top-level and dotted keys coexist in the same store
    # ------------------------------------------------------------------

    def test_top_level_and_dotted_keys_coexist(self, tmp_path: Path) -> None:
        """YamlMetricsCollector must store both top-level and dotted keys independently.

        Call time(MetricKey.ENCODING) and time(MetricKey.ENCODING, "h265"), flush,
        parse YAML, assert both top_level has an "encoding" entry and dotted has
        an "encoding" group.

        Validates: Requirements 1.3, 6.1, 6.2
        """
        import yaml as _yaml

        from pyqenc.metrics import YamlMetricsCollector

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        collector = YamlMetricsCollector(work_dir=work_dir, force_wipe=True)

        # Inject non-zero values directly into _store to avoid real timing
        collector._store["encoding"]      = 10.0
        collector._store["encoding.h265"] = 8.0

        collector.flush()

        metrics_path = work_dir / "metrics.yaml"
        assert metrics_path.exists(), "metrics.yaml was not written"

        raw = _yaml.safe_load(metrics_path.read_text(encoding="utf-8"))
        pm  = raw["pipeline_metrics"]
        td  = pm["time_distribution"]

        top_level_keys = [e["key"] for e in td.get("top_level", [])]
        assert "encoding" in top_level_keys, (
            f"Expected 'encoding' in top_level, got: {top_level_keys}"
        )

        dotted = td.get("dotted", {})
        assert "encoding" in dotted, (
            f"Expected 'encoding' group in dotted, got: {list(dotted.keys())}"
        )
        breakdown_keys = [e["key"] for e in dotted["encoding"].get("breakdown", [])]
        assert "encoding.h265" in breakdown_keys, (
            f"Expected 'encoding.h265' in dotted breakdown, got: {breakdown_keys}"
        )

    # ------------------------------------------------------------------
    # 8. YAML dotted section is absent when no dotted keys have non-zero values
    # ------------------------------------------------------------------

    def test_yaml_dotted_absent_when_no_dotted_keys(self, tmp_path: Path) -> None:
        """YAML dotted section must be absent or empty when only top-level keys are used.

        Validates: Requirements 5.4
        """
        import yaml as _yaml

        from pyqenc.metrics import YamlMetricsCollector

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        collector = YamlMetricsCollector(work_dir=work_dir, force_wipe=True)

        # Only top-level keys — no dotted keys
        collector._store["encoding"]   = 10.0
        collector._store["merge"]      = 5.0
        collector._store["extraction"] = 3.0

        collector.flush()

        metrics_path = work_dir / "metrics.yaml"
        assert metrics_path.exists(), "metrics.yaml was not written"

        raw = _yaml.safe_load(metrics_path.read_text(encoding="utf-8"))
        pm  = raw["pipeline_metrics"]
        td  = pm["time_distribution"]

        dotted = td.get("dotted", {})
        assert not dotted, (
            f"Expected dotted section to be absent or empty when no dotted keys used, got: {dotted}"
        )
