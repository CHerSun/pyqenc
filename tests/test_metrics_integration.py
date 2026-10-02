"""Phase integration tests for MetricsCollector timing instrumentation.

Verifies the OBSERVABLE metrics contract of each phase: after a phase runs
against a real ``YamlMetricsCollector``, the flushed ``metrics.yaml`` records
the expected timing rows (top-level phase keys, dotted sub-action keys) and
convergence statistics — and records nothing for work that did not happen
(reused paths, reused pairs).  The collector runs under a deterministic
stepping clock so every completed span accrues at least one second and
survives the report's integer-second rounding; assertions parse the YAML,
never the collector's internals.  External I/O (ffprobe, ffmpeg, crop
detect) is mocked out so tests run without real media files.

Tests live here per the spec: tests/test_metrics_integration.py
"""
# CHerSun 2026

import contextlib
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.metrics import (
    MetricKey,
    MetricsCollector,
    NoOpMetricsCollector,
    YamlMetricsCollector,
)
from pyqenc.models import (
    CleanupLevel,
    PhaseOutcome,
    Strategy,
)
from pyqenc.phase import Artifact, PhaseRegistry, Recovery
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
            strategy=_make_strategy_by_name("h265+slow"),
            output_path=out_path,
        ),
        state=state,
    )


def _make_strategy_by_name(name: str) -> Strategy:
    """A minimal Strategy for a ``profile+preset`` display name."""
    from decimal import Decimal

    from pyqenc.models import CodecConfig, Strategy

    profile, _, preset = name.partition("+")
    return Strategy(
        preset=preset or "ultrafast", profile=profile,
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

    Used only where a test needs SOME collector to satisfy a constructor and
    never asserts on it — never to inspect ``time()``/``step()`` call args
    (that pins internal instrumentation, not behavior).
    """
    collector = MagicMock(spec=MetricsCollector)
    collector.time.return_value = contextlib.nullcontext()
    return collector


class _SteppingClock:
    """Deterministic ``monotonic()`` source: every call advances one step.

    Used via ``patch("time.monotonic", ...)`` so each completed ``time()``
    span accrues at least one step (>= 1 s) and survives the integer-second
    rounding in the metrics report.  Real sub-millisecond test spans would
    round to 0 and be omitted from ``metrics.yaml`` entirely.
    """

    def __init__(self, step: float = 1.0) -> None:
        self._now  = 1_000.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


def _recorded_metrics(
    tmp_path: Path,
    run: Callable[[MetricsCollector], None],
) -> dict:
    """Run *run* against a real collector and return the parsed metrics.yaml.

    Creates a ``YamlMetricsCollector`` writing ``metrics.yaml`` below
    ``tmp_path/work``, patches ``time.monotonic`` to a stepping clock for the
    duration of *run*, flushes, closes, and returns ``yaml.safe_load`` of the
    written report.  Asserting on this dict is asserting on the run's
    external record — not on collector internals.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    collector = YamlMetricsCollector(work_dir=work_dir, force_wipe=True)
    try:
        with patch("time.monotonic", _SteppingClock()):
            run(collector)
        collector.flush()
    finally:
        collector.close()
    metrics_path = work_dir / "metrics.yaml"
    assert metrics_path.exists(), "metrics.yaml was not written"
    return yaml.safe_load(metrics_path.read_text(encoding="utf-8"))


def _top_level_keys(metrics: dict) -> set[str]:
    """Top-level timing keys with recorded (non-zero) seconds in metrics.yaml."""
    return {
        entry["key"]
        for entry in metrics["pipeline_metrics"]["time_distribution"]["top_level"]
    }


def _dotted_groups(metrics: dict) -> dict[str, dict]:
    """Dotted sub-action groups in metrics.yaml, keyed by prefix string."""
    return metrics["pipeline_metrics"]["time_distribution"].get("dotted", {})


def _dotted_keys(metrics: dict) -> set[str]:
    """Dotted sub-action keys with recorded (non-zero) seconds in metrics.yaml."""
    return {
        entry["key"]
        for group in _dotted_groups(metrics).values()
        for entry in group["breakdown"]
    }


# ---------------------------------------------------------------------------
# JobPhase — JOB_PROBE timing
# ---------------------------------------------------------------------------

class TestJobPhaseTiming:
    """Integration tests for ``JobPhase`` timing instrumentation (Req 6.5)."""

    def test_job_probe_recorded_on_run(self, tmp_path: Path) -> None:
        """A completed job run must leave its probe time in ``metrics.yaml``.

        Bug guarded: the run's report losing the job timing row — the
        pipeline's first phase silently missing from ``time_distribution``,
        leaving the report an incomplete record of what ran.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase

        config   = _make_config(tmp_path)
        volatile = _make_volatile(tmp_path)

        def run(collector: MetricsCollector) -> None:
            JobPhase(config, {}, collector=collector, **volatile).run()

        metrics    = _recorded_metrics(tmp_path, run)
        top_level  = _top_level_keys(metrics)
        assert "job" in top_level, (
            f"job timing missing from metrics.yaml, got: {sorted(top_level)}"
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
        phase    = JobPhase(config, {}, collector=collector, **volatile)

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
        phase    = JobPhase(config, {}, collector=collector, **volatile)

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
            source     = source,
            work_dir   = source.parent / "work",
            config     = _make_config(source.parent),
        )
        result.file     = Artifact(                       # type: ignore[attr-defined]
            payload=File(path=source, file_size_bytes=source.stat().st_size),
            state=ArtifactState.COMPLETE,
        )
        return result

    def _make_phase(
        self,
        tmp_path: Path,
        collector: MetricsCollector,
    ) -> ExtractionPhase:
        """Return an ``ExtractionPhase`` with a pre-wired job dependency."""
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.phases.job import JobPhase

        config   = _make_config(tmp_path)
        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = self._make_job_result(tmp_path)

        registry: PhaseRegistry = {}
        phase = ExtractionPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase] = job_mock  # type: ignore[index]
        return phase

    def test_reused_run_reports_recovery_without_extraction(self, tmp_path: Path) -> None:
        """An all-reused extraction run reports recovery time and no extraction time.

        Bug guarded: a resumed run either losing its recovery-scan seconds or
        claiming extraction execution seconds it never spent — the report
        must distinguish "we only looked" from "we did work".

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

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            with patch.object(
                ExtractionPhase, "_recover",
                return_value=Recovery.from_artifacts([stub_artifact]),
            ):
                phase.run()

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "recovery" in top_level, (
            f"recovery timing missing from metrics.yaml, got: {sorted(top_level)}"
        )
        assert "extraction" not in top_level, (
            "a fully-reused run must not report extraction execution time, "
            f"got: {sorted(top_level)}"
        )

    def test_extraction_recorded_for_mkvextract_tracks(self, tmp_path: Path) -> None:
        """A run that extracts tracks reports both recovery and extraction time.

        Bug guarded: pending extraction work finishing without leaving its
        execution seconds in the report — the run's cost would be invisible.

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

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
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

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "recovery" in top_level, (
            f"recovery timing missing from metrics.yaml, got: {sorted(top_level)}"
        )
        assert "extraction" in top_level, (
            f"extraction timing missing from metrics.yaml, got: {sorted(top_level)}"
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
            source     = source,
            work_dir   = tmp_path / "work",
            config     = _make_config(tmp_path),
        )
        return result

    def _make_phase(
        self,
        tmp_path:  Path,
        collector: MetricsCollector,
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
            video_stream=Artifact(payload=stream.stream, state=ArtifactState.COMPLETE),
        )

        probe_mock = MagicMock(spec=ProbePhase)
        probe_mock.result = ProbePhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="ok",
            stream=Artifact(payload=stream, state=ArtifactState.COMPLETE),
        )

        from pyqenc.phases.chunking import ChunkingPhase
        registry: PhaseRegistry = {}
        phase = ChunkingPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]        = job_mock         # type: ignore[index]
        registry[ExtractionPhase] = extraction_mock  # type: ignore[index]
        registry[ProbePhase]      = probe_mock       # type: ignore[index]
        return phase

    def test_reused_run_reports_recovery_without_chunking(self, tmp_path: Path) -> None:
        """A run with cached boundaries reports recovery time and no chunking time.

        Bug guarded: a resumed chunking run losing its recovery-scan seconds
        or claiming chunking execution (scene-detect) seconds it never spent.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.stream_model import ChunkingSidecar as _CS
        from pyqenc.stream_model import SceneRecord

        outcome: dict[str, str] = {}
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        write_yaml_atomic(
            work_dir / "chunking.yaml",
            _CS(scenes=[SceneRecord(timestamp_seconds=0.0, frame=0)]).model_dump(),
        )

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            outcome["value"] = phase.run().outcome.value

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "recovery" in top_level, (
            f"recovery timing missing from metrics.yaml, got: {sorted(top_level)}"
        )
        assert "chunking" not in top_level, (
            "a fully-reused chunking run must not report chunking execution "
            f"time, got: {sorted(top_level)}"
        )
        assert outcome["value"] == "reused"

    def test_scene_detect_recorded_when_no_cached_boundaries(self, tmp_path: Path) -> None:
        """A fresh chunking run reports chunking time including the scene-detect
        sub-action as a dotted key.

        Bug guarded: scene detection running without its cost being attributable
        in the report — the ``chunking.scene_detect`` dotted row is the only
        place the detection sub-action's share of chunking time is visible.

        Validates: Requirements 6.5
        """
        from pyqenc.models import SceneBoundary

        outcome: dict[str, str] = {}

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            with patch("pyqenc.phases.chunking.detect_scenes",
                       return_value=[SceneBoundary(frame=0, timestamp_seconds=0.0)]):
                outcome["value"] = phase.run().outcome.value

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "chunking" in top_level, (
            f"chunking timing missing from metrics.yaml, got: {sorted(top_level)}"
        )
        assert "chunking.scene_detect" in _dotted_keys(metrics), (
            f"scene_detect sub-action missing from metrics.yaml, "
            f"got: {sorted(_dotted_keys(metrics))}"
        )
        assert outcome["value"] == "completed"

    def test_scene_detect_not_recorded_when_boundaries_cached(self, tmp_path: Path) -> None:
        """Cached boundaries skip detection entirely — no scene-detect cost
        and no detection subprocess in the report.

        Bug guarded: a resumed run re-running (and re-timing) scene detection
        over boundaries it already has — the expensive ffmpeg scan must not
        run, and the report must not claim it did.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.stream_model import ChunkingSidecar as _CS
        from pyqenc.stream_model import SceneRecord

        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        write_yaml_atomic(
            work_dir / "chunking.yaml",
            _CS(scenes=[SceneRecord(timestamp_seconds=0.0, frame=0)]).model_dump(),
        )

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            with patch("pyqenc.phases.chunking.detect_scenes") as detect_mock:
                phase.run()
                detect_mock.assert_not_called()

        metrics = _recorded_metrics(tmp_path, run)
        assert "chunking" not in _top_level_keys(metrics)
        assert "chunking" not in _dotted_groups(metrics), (
            f"no chunking sub-action may be reported on a cached run, "
            f"got: {sorted(_dotted_groups(metrics))}"
        )

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
        collector: MetricsCollector,
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
            source     = source,
            work_dir   = work_dir,
            config     = config,
        )

        extraction_result = ExtractionPhaseResult(
            outcome      = PhaseOutcome.COMPLETED,
            message      = "ok",
            video_stream = _make_video_stream_fixture(source),
        )

        job_mock = MagicMock(spec=JobPhase)
        job_mock.result = job_result

        extraction_mock = MagicMock(spec=ExtractionPhase)
        extraction_mock.result = extraction_result

        registry: PhaseRegistry = {}
        phase = AudioPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]        = job_mock         # type: ignore[index]
        registry[ExtractionPhase] = extraction_mock  # type: ignore[index]
        return phase

    def test_reused_run_reports_recovery_without_audio(self, tmp_path: Path) -> None:
        """An all-reused audio run reports recovery time and no audio time.

        Bug guarded: a resumed run losing its recovery-scan seconds or
        claiming audio processing seconds it never spent — the report must
        distinguish "we only looked" from "we did work".  (Merges the former
        spy pair recovery-recorded / audio-not-recorded into one report
        assertion.)

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phases.audio import AudioPhase

        stub_row = Artifact(
            payload=_audio_output(tmp_path / "work" / "audio" / "track.aac"),
            state=ArtifactState.COMPLETE,
        )

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            with patch.object(AudioPhase, "_recover", return_value=Recovery.from_artifacts([stub_row])):
                phase.run()

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "recovery" in top_level, (
            f"recovery timing missing from metrics.yaml, got: {sorted(top_level)}"
        )
        assert "audio" not in top_level, (
            "a fully-reused audio run must not report audio execution time, "
            f"got: {sorted(top_level)}"
        )

    def test_audio_recorded_when_processing_runs(self, tmp_path: Path) -> None:
        """A run that processes audio reports audio execution time.

        Bug guarded: pending audio work finishing without leaving its
        execution seconds in the report — the run's cost would be invisible.

        Validates: Requirements 6.5
        """
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.audio import AudioPhase, AudioPhaseResult

        stub_artifact = Artifact(
            payload=_audio_output(tmp_path / "work" / "audio" / "track.aac"),
            state=ArtifactState.ABSENT,
        )

        stub_result = AudioPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "ok",
        )

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            with (
                patch.object(AudioPhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])),
                patch.object(AudioPhase, "_execute", return_value=stub_result),
            ):
                phase.run()

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "audio" in top_level, (
            f"audio timing missing from metrics.yaml, got: {sorted(top_level)}"
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
            source     = source,
            work_dir   = tmp_path / "work",
            config     = config if config is not None else _make_config(tmp_path),
        )
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
        collector: MetricsCollector,
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
            str(s.raw) if hasattr(s, "raw") else f"{s.profile}+{s.preset}"
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

        registry: PhaseRegistry = {}
        phase = OptimizationPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]      = job_mock       # type: ignore[index]
        registry[ProbePhase]    = probe_mock     # type: ignore[index]
        registry[ChunkingPhase] = chunking_mock  # type: ignore[index]
        return phase

    def test_recovery_recorded_with_cached_optimization_params(self, tmp_path: Path) -> None:
        """An optimization run with cached results must report its recovery scan.

        Bug guarded: the recovery scan's seconds being lost from the report —
        the scan (loading and validating persisted optimization params) runs
        before any decision and its cost must be visible.

        Note: with this fixture the persisted test-chunk IDs do not match the
        chunking result, so the phase legitimately re-selects and executes
        (outcome COMPLETED) — the guaranteed observable is the recovery row,
        not a reuse outcome.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.models import CropParams
        from pyqenc.state import OptimizationParams, ProbeState, StrategyTestResult

        strategy = _STRATEGY_SLOW_H265
        # tolerance_pct and metrics_sampling must match config defaults so the
        # full-reuse path (step 4) is attempted rather than falling through.
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

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector, optimize=True)
            with patch.object(OptimizationParams, "load", return_value=persisted):
                phase.run()

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "recovery" in top_level, (
            f"recovery timing missing from metrics.yaml, got: {sorted(top_level)}"
        )

    def test_shared_executor_records_no_top_level_span(self, tmp_path: Path) -> None:
        """``_encode_chunks_parallel`` must not open a timing span of its own.

        Bug guarded: the shared executor (driven by both the encoding and the
        optimization phase) recording a top-level span would double-count
        every encode — the owning phase's ``run()`` already wraps execution
        under its own key.  Externally: a direct executor run leaves
        ``time_distribution`` empty while its convergence data still reaches
        the report.

        (The former spy form also pinned the OPTIMIZATION vs ENCODING prefix
        passed to ``step()``; the YAML report does not distinguish prefixes,
        so that internal-instrumentation assertion was dropped — the run
        below still passes ``metric_prefix=OPTIMIZATION`` to exercise the
        shared-encoder path.)

        Validates: Requirements 6.5, 2.2a
        """
        import asyncio

        from pyqenc.phases.encoding import ChunkEncodingResult, _encode_chunks_parallel

        strategy = _STRATEGY_SLOW_H265

        chunk = _make_chunk_window(tmp_path / "source.mkv", 0.0, 1.0)

        encoded_path = tmp_path / "chunk_0_enc.mkv"
        encoded_path.write_bytes(bytes(128))

        successful_result = ChunkEncodingResult(
            chunk_id     = "chunk_0",
            strategy     = strategy.display_name(),
            success      = True,
            final_crf    = Decimal("28.0"),
            attempts     = 2,
            encoded_file = MagicMock(path=encoded_path, resolution="1920x1080"),
            reused       = False,
        )

        stub_recovery = MagicMock()
        stub_recovery.pairs = {}

        def run(collector: MetricsCollector) -> None:
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

        metrics = _recorded_metrics(tmp_path, run)
        assert _top_level_keys(metrics) == set(), (
            "executor must not record a top-level span (the owning phase owns it), "
            f"got: {sorted(_top_level_keys(metrics))}"
        )
        assert metrics["pipeline_metrics"]["convergence"], (
            "convergence data from the executor's encodes must still reach the report"
        )

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
            source     = source,
            work_dir   = tmp_path / "work",
            config     = _make_config(tmp_path),
        )
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
        collector: MetricsCollector,
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

        registry: PhaseRegistry = {}
        phase = EncodingPhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]          = job_mock           # type: ignore[index]
        registry[ProbePhase]        = probe_mock         # type: ignore[index]
        registry[ChunkingPhase]     = chunking_mock      # type: ignore[index]
        registry[OptimizationPhase] = optimization_mock  # type: ignore[index]
        return phase

    def test_reused_run_reports_recovery_without_encoding(self, tmp_path: Path) -> None:
        """An all-complete encoding run reports recovery time and no encoding time.

        Bug guarded: a resumed encoding run losing its recovery-scan seconds
        or claiming encode seconds it never spent.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phases.encoding import EncodingPhase

        stub_row = Artifact(
            payload=_encoded_chunk(tmp_path / "chunk_0.mkv", "chunk_0", "h265+slow"),
            state=ArtifactState.COMPLETE,
        )

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            with patch.object(EncodingPhase, "_recover", return_value=Recovery.from_artifacts([stub_row])):
                phase.run()

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "recovery" in top_level, (
            f"recovery timing missing from metrics.yaml, got: {sorted(top_level)}"
        )
        assert "encoding" not in top_level, (
            "a fully-reused encoding run must not report encoding execution "
            f"time, got: {sorted(top_level)}"
        )

    def test_encoding_main_recorded_when_encodes_run(self, tmp_path: Path) -> None:
        """A run that encodes reports encoding execution time in metrics.yaml.

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

        stub_artifact = Artifact(
            payload=_encoded_chunk(tmp_path / "chunk_0.mkv", "chunk_0", "h265+slow"),
            state=ArtifactState.ABSENT,
        )

        stub_result = EncodingPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "ok",
        )

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            with (
                patch.object(EncodingPhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])),
                patch.object(EncodingPhase, "_execute", return_value=stub_result),
            ):
                phase.run()

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "encoding" in top_level, (
            f"encoding timing missing from metrics.yaml, got: {sorted(top_level)}"
        )

    def test_converged_chunk_reaches_report_convergence_section(self, tmp_path: Path) -> None:
        """Each converged (non-reused) chunk/strategy pair must land in the
        report's ``convergence`` section with its strategy and attempt count.

        Bug guarded: convergence data collected in memory but lost on the way
        to ``metrics.yaml`` — the per-strategy attempt statistics are the
        report's record of how hard the CRF search worked.

        Validates: Requirements 6.5, 4.1a
        """
        import asyncio

        from pyqenc.phases.encoding import ChunkEncodingResult, _encode_chunks_parallel

        chunk = _make_chunk_window(tmp_path / "source.mkv", 0.0, 1.0)

        # Reference file must exist so the encode path is reached (not skipped)
        (tmp_path / "chunk_0.mkv").write_bytes(b"\x00" * 64)

        encoded_path = tmp_path / "chunk_0_enc.mkv"
        encoded_path.write_bytes(b"\x00" * 128)

        successful_result = ChunkEncodingResult(
            chunk_id     = "chunk_0",
            strategy     = "h265+slow",
            success      = True,
            final_crf    = Decimal("28.0"),
            attempts     = 3,
            encoded_file = MagicMock(path=encoded_path, resolution="1920x1080"),
            reused       = False,
        )

        def run(collector: MetricsCollector) -> None:
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

        metrics     = _recorded_metrics(tmp_path, run)
        convergence = metrics["pipeline_metrics"]["convergence"]
        assert convergence, f"convergence section missing from metrics.yaml: {metrics}"
        by_strategy = {stats["strategy"]: stats for stats in convergence}
        assert _STRATEGY_SLOW_H265.display_name() in by_strategy, (
            f"strategy missing from convergence section, got: {sorted(by_strategy)}"
        )
        stats = by_strategy[_STRATEGY_SLOW_H265.display_name()]
        assert stats["chunks"] == 1, f"expected 1 converged chunk, got: {stats['chunks']}"
        assert stats["attempts"]["total"] == 3, (
            f"expected 3 total attempts, got: {stats['attempts']['total']}"
        )
        assert stats["attempts"]["max"] == 3

    def test_reused_pairs_report_no_convergence_data(self, tmp_path: Path) -> None:
        """Reused pairs must contribute no convergence data to the report.

        Bug guarded: a resumed run re-counting already-reported pairs — the
        convergence statistics would inflate with each resume, double-crediting
        work done in earlier runs.

        Validates: Requirements 6.5, 4.1a
        """
        import asyncio

        from pyqenc.phases.encoding import ChunkEncodingResult, _encode_chunks_parallel

        chunk = _make_chunk_window(tmp_path / "source.mkv", 0.0, 1.0)

        encoded_path = tmp_path / "chunk_0_enc.mkv"
        encoded_path.write_bytes(b"\x00" * 128)

        reused_result = ChunkEncodingResult(
            chunk_id     = "chunk_0",
            strategy     = "h265+slow",
            success      = True,
            final_crf    = Decimal("28.0"),
            attempts     = 1,
            encoded_file = MagicMock(path=encoded_path, resolution="1920x1080"),
            reused       = True,
        )

        def run(collector: MetricsCollector) -> None:
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

        metrics = _recorded_metrics(tmp_path, run)
        assert metrics["pipeline_metrics"]["convergence"] is None, (
            "reused pairs must not produce convergence data, got: "
            f"{metrics['pipeline_metrics']['convergence']}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``EncodingPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.encoding import EncodingPhase

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        stub_row = Artifact(
            payload=_encoded_chunk(tmp_path / "chunk_0.mkv", "chunk_0", "h265+slow"),
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
            source     = source,
            work_dir   = tmp_path / "work",
            config     = _make_config(tmp_path),
        )
        return result

    def _make_encoding_result(self, tmp_path: Path) -> EncodingPhaseResult:
        """Return a minimal complete ``EncodingPhaseResult`` with one winner row."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.encoding import EncodingPhaseResult

        encoded_path = tmp_path / "work" / "encoded" / "h265+slow" / "chunk_0.mkv"
        encoded_path.parent.mkdir(parents=True, exist_ok=True)
        encoded_path.write_bytes(b"\x00" * 128)

        winner = Artifact(
            payload  = _encoded_chunk(encoded_path, "chunk_0", "h265+slow"),
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
        collector: MetricsCollector,
        *,
        probe_stream: ExtendedVideoStream | None = None,
    ) -> MergePhase:
        """Return a ``MergePhase`` with pre-wired job, extraction, encoding, and audio deps.

        ``probe_stream`` overrides the default real ``ExtendedVideoStream``
        probe payload (e.g. with specific fps for merge quality measurement).
        The dependency walk guarantees a resolved stream — the fixture always
        provides one.
        """
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
        if probe_stream is None:
            probe_stream = _make_extended_stream(
                tmp_path / "source.mkv", frame_count=640, duration=26.67,
            )
        probe_mock.result = ProbePhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "probe complete",
            stream    = Artifact(payload=probe_stream, state=ArtifactState.COMPLETE),
        )

        registry: PhaseRegistry = {}
        phase = MergePhase(config, registry, collector=collector)  # type: ignore[arg-type]
        registry[JobPhase]        = job_mock         # type: ignore[index]
        registry[ExtractionPhase] = extraction_mock  # type: ignore[index]
        registry[ProbePhase]      = probe_mock       # type: ignore[index]
        registry[EncodingPhase]   = encoding_mock    # type: ignore[index]
        registry[AudioPhase]      = audio_mock       # type: ignore[index]
        return phase

    def test_reused_run_reports_recovery_without_merge(self, tmp_path: Path) -> None:
        """An all-complete merge run reports recovery time and no merge time.

        Bug guarded: a resumed merge run losing its recovery-scan seconds or
        claiming merge execution seconds it never spent.

        Validates: Requirements 6.5, 2.7
        """
        from pyqenc.phases.merge import MergePhase
        from pyqenc.state import ArtifactState

        output_file = tmp_path / "work" / "merged" / "source h265+slow.mkv"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(b"\x00" * 64)

        stub_artifact = _merged_row(output_file, ArtifactState.COMPLETE)

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            with patch.object(MergePhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])):
                phase.run()

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "recovery" in top_level, (
            f"recovery timing missing from metrics.yaml, got: {sorted(top_level)}"
        )
        assert "merge" not in top_level, (
            "a fully-reused merge run must not report merge execution time, "
            f"got: {sorted(top_level)}"
        )

    def test_merge_concat_recorded_when_merge_runs(self, tmp_path: Path) -> None:
        """A run that merges reports merge time, with the concatenation
        sub-action visible as a dotted key.

        Bug guarded: the mkvmerge concatenation finishing without its cost
        being attributable in the report — the ``merge.concat`` dotted row is
        the only place the concat sub-action's share of merge time shows.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.merge import MergePhase
        from pyqenc.state import ArtifactState

        output_file = tmp_path / "work" / "merged" / "source h265+slow.mkv"
        output_file.parent.mkdir(parents=True, exist_ok=True)

        stub_artifact = _merged_row(output_file, ArtifactState.ABSENT)

        encoded_path = tmp_path / "work" / "encoded" / "h265+slow" / "chunk_0.mkv"
        encoded_path.parent.mkdir(parents=True, exist_ok=True)
        encoded_path.write_bytes(b"\x00" * 128)

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(tmp_path, collector)
            with (
                patch.object(MergePhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])),
                patch("pyqenc.phases.merge.subprocess.run") as mock_subprocess,
                patch("pyqenc.phases.merge.get_frame_count", return_value=100),
                patch.object(MergePhase, "_collect_encoded_chunks", return_value={
                    "chunk_0": {"h265+slow": _encoded_chunk(encoded_path, "chunk_0", "h265+slow")},
                }),
            ):
                mock_subprocess.return_value = MagicMock(returncode=0, stderr="")
                # The twin the mocked mkvmerge "wrote" — the phase promotes
                # it to the final name via rename.
                output_file.with_name(f"{output_file.stem}.tmp").write_bytes(b"\x00" * 128)
                phase.run()

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "merge" in top_level, (
            f"merge timing missing from metrics.yaml, got: {sorted(top_level)}"
        )
        assert "merge.concat" in _dotted_keys(metrics), (
            f"concat sub-action missing from metrics.yaml, got: {sorted(_dotted_keys(metrics))}"
        )

    def test_merge_quality_measure_recorded_when_targets_set(self, tmp_path: Path) -> None:
        """A merge run with quality targets reports the quality-measure
        sub-action as a dotted key in metrics.yaml.

        Bug guarded: quality measurement (a costly VMAF pass over the merged
        output) finishing without its cost being attributable in the report —
        the ``merge.quality_measure`` dotted row is the only place that
        sub-action's share of merge time shows.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.merge import MergePhase
        from pyqenc.state import ArtifactState

        output_file = tmp_path / "work" / "merged" / "source h265+slow.mkv"
        output_file.parent.mkdir(parents=True, exist_ok=True)

        stub_artifact = _merged_row(output_file, ArtifactState.ABSENT)

        encoded_path = tmp_path / "work" / "encoded" / "h265+slow" / "chunk_0.mkv"
        encoded_path.parent.mkdir(parents=True, exist_ok=True)
        encoded_path.write_bytes(b"\x00" * 128)

        def run(collector: MetricsCollector) -> None:
            phase = self._make_phase(
                tmp_path, collector,
                # Quality measurement needs a real source stream on the probe
                # result; without it the phase skips the whole sub-action.
                probe_stream=_make_extended_stream(
                    tmp_path / "source.mkv", frame_count=100, duration=4.0,
                ),
            )
            with (
                patch.object(MergePhase, "_recover", return_value=Recovery.from_artifacts([stub_artifact])),
                patch("pyqenc.phases.merge.subprocess.run") as mock_subprocess,
                patch("pyqenc.phases.merge.get_frame_count", return_value=100),
                patch.object(MergePhase, "_collect_encoded_chunks", return_value={
                    "chunk_0": {"h265+slow": _encoded_chunk(encoded_path, "chunk_0", "h265+slow")},
                }),
                patch("pyqenc.phases.merge.MergePhase._measure_quality", return_value=({}, False, None)),
            ):
                mock_subprocess.return_value = MagicMock(returncode=0, stderr="")
                # The twin the mocked mkvmerge "wrote" — the phase promotes
                # it to the final name via rename.
                output_file.with_name(f"{output_file.stem}.tmp").write_bytes(b"\x00" * 128)
                phase.run()

        metrics   = _recorded_metrics(tmp_path, run)
        dotted    = _dotted_keys(metrics)
        assert "merge.quality_measure" in dotted, (
            f"quality_measure sub-action missing from metrics.yaml, got: {sorted(dotted)}"
        )
        assert "merge" in _top_level_keys(metrics)

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``MergePhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.merge import MergePhase
        from pyqenc.state import ArtifactState

        collector = NoOpMetricsCollector()
        phase     = self._make_phase(tmp_path, collector)  # type: ignore[arg-type]

        output_file = tmp_path / "work" / "merged" / "source h265+slow.mkv"
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

        Bug guarded: recording a dotted sub-action overwriting (or being
        merged into) its same-named top-level row — the report must carry the
        phase total and the sub-action breakdown side by side.

        Validates: Requirements 1.3, 6.1, 6.2
        """
        from pyqenc.metrics import YamlMetricsCollector

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        collector = YamlMetricsCollector(work_dir=work_dir, force_wipe=True)
        try:
            # Record through the public API under the stepping clock so each
            # span accrues >= 1 s and survives the report's integer rounding.
            with patch("time.monotonic", _SteppingClock()):
                with collector.time(MetricKey.ENCODING):
                    pass
                with collector.time(MetricKey.ENCODING, "h265"):
                    pass
            collector.flush()
        finally:
            collector.close()

        metrics_path = work_dir / "metrics.yaml"
        assert metrics_path.exists(), "metrics.yaml was not written"

        raw = yaml.safe_load(metrics_path.read_text(encoding="utf-8"))
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
        from pyqenc.metrics import YamlMetricsCollector

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        collector = YamlMetricsCollector(work_dir=work_dir, force_wipe=True)
        try:
            # Only top-level keys — no dotted keys (public API, stepping clock).
            with patch("time.monotonic", _SteppingClock()):
                with collector.time(MetricKey.ENCODING):
                    pass
                with collector.time(MetricKey.MERGE):
                    pass
                with collector.time(MetricKey.EXTRACTION):
                    pass
            collector.flush()
        finally:
            collector.close()

        metrics_path = work_dir / "metrics.yaml"
        assert metrics_path.exists(), "metrics.yaml was not written"

        raw = yaml.safe_load(metrics_path.read_text(encoding="utf-8"))
        pm  = raw["pipeline_metrics"]
        td  = pm["time_distribution"]

        dotted = td.get("dotted", {})
        assert not dotted, (
            f"Expected dotted section to be absent or empty when no dotted keys used, got: {dotted}"
        )
