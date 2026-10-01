"""Unit tests for ProbePhase.run() — observable behavior only.

ProbePhase is constructed through its real public constructor with a real
phase registry: real ``JobPhase`` and ``ExtractionPhase`` instances whose
``result`` is pre-populated with real typed results. Because the shared
dependency walk only calls ``dep.run()`` when ``dep.result is None``, a phase
that already carries a completed ``result`` makes the walk a no-op — no phase
internals are mocked or monkeypatched.

Every test drives ``phase.run(...)`` and asserts only on the returned
``ProbePhaseResult`` and on-disk ``probe.yaml``. The only things mocked are the
genuinely external shell-outs (``detect_crop_parameters`` and
``get_frame_count`` / ``timestamps.txt``) — boundaries, not internals of the phase.

Covered observable paths:
- FAILED when extraction produced no video (source None, error set, no probe.yaml)
- REUSED when probe.yaml is fully cached and no --crop override
- crop override bypasses the REUSED shortcut
- COMPLETED when probe.yaml is absent: detection runs and probe.yaml persists
"""
# CHerSun 2026

from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from pyqenc.app_config import load_app_config
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import (
    CleanupLevel,
    CropParams,
    PhaseOutcome,
)
from pyqenc.phase import Artifact, PhaseRegistry
from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.state import ArtifactState, ProbeState
from pyqenc.stream_model import File, VideoStream, VideoStreamInfo
from tests.test_metrics_integration import (
    _dotted_groups,
    _dotted_keys,
    _recorded_metrics,
    _top_level_keys,
)

# ---------------------------------------------------------------------------
# Helpers — build REAL typed results and a REAL registry
# ---------------------------------------------------------------------------

_APP_CONFIG = load_app_config(default_only=True)


def _make_job_result(work_dir: Path, source: Path) -> JobPhaseResult:
    """Return a COMPLETED JobPhaseResult carrying the source and work_dir."""
    return JobPhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        message   = "job complete",
        file      = Artifact(payload=File(path=source, file_size_bytes=64), state=ArtifactState.COMPLETE),
        config    = _APP_CONFIG,
        work_dir  = work_dir,
        source    = source,
    )


def _make_video_stream(path: Path) -> VideoStream:
    """A video stream with the fast facet pre-populated (no probing)."""
    return VideoStream(
        file = File(path=path, file_size_bytes=64),
        info = VideoStreamInfo(
            track_id=0, codec_name="hevc", fps=24.0,
            fps_fraction=Fraction(24, 1), resolution="1920x1080",
            duration_seconds=3600.0,
        ),
    )


def _make_extraction_result(
    work_dir: Path, video_stream: VideoStream | None,
) -> ExtractionPhaseResult:
    """Return a COMPLETED ExtractionPhaseResult carrying the video artifact.

    The video row is COMPLETE (its material component — the per-frame index —
    present), so the derived ``timestamps_path`` resolves to the conventional
    location below ``work_dir``.
    """
    return ExtractionPhaseResult(
        outcome      = PhaseOutcome.COMPLETED,
        message      = "extraction complete",
        video_stream = (
            Artifact(payload=video_stream, state=ArtifactState.COMPLETE)
            if video_stream is not None else None
        ),
        work_dir     = work_dir,
    )


def _make_probe_phase(
    job_result:        JobPhaseResult,
    extraction_result: ExtractionPhaseResult,
    *,
    crop_params:       CropParams | None = None,
    collector          = None,
) -> ProbePhase:
    """Construct ProbePhase via its real constructor and a real registry.

    Real JobPhase / ExtractionPhase instances are placed in the registry with
    their public ``result`` pre-set to a completed typed result, so the shared
    dependency walk treats them as already-run without any mocking.
    """
    if collector is None:
        collector = NoOpMetricsCollector()

    job = JobPhase(
        _APP_CONFIG, {},
        source     = job_result.source,      # type: ignore[arg-type]
        work_dir   = job_result.work_dir,    # type: ignore[arg-type]
        force      = False,
        cleanup    = CleanupLevel.NONE,
        no_metrics = True,
        collector  = collector,
    )
    job.result = job_result

    registry: PhaseRegistry = {JobPhase: job}

    extraction = ExtractionPhase(_APP_CONFIG, registry, collector=collector)
    extraction.result = extraction_result
    registry[ExtractionPhase] = extraction

    return ProbePhase(
        _APP_CONFIG, registry,
        collector   = collector,
        crop_params = crop_params,
    )


def _write_probe_yaml(work_dir: Path, *, frame_count: int, crop: CropParams) -> None:
    """Persist a valid probe.yaml with the given frame count and crop."""
    ProbeState(frame_count=frame_count, crop=crop).save(work_dir / "probe.yaml")


# ---------------------------------------------------------------------------
# FAILED path — no video extracted
# ---------------------------------------------------------------------------

class TestProbePhaseFailedNoVideo:
    """ProbePhase.run() must return FAILED when extraction produced no video.

    Bug guarded: without the ``video is None`` check the phase would attempt
    crop detection / frame probing on a nonexistent video and crash deep in
    ffprobe; and if it wrote probe.yaml on failure, a later run would wrongly
    treat probing as done and skip it.
    """

    def test_failed_outcome_with_no_source_and_error(self, tmp_path: Path):
        work_dir          = tmp_path / "work"
        work_dir.mkdir()
        job_result        = _make_job_result(work_dir, tmp_path / "source.mkv")
        extraction_result = _make_extraction_result(work_dir, video_stream=None)
        phase             = _make_probe_phase(job_result, extraction_result)

        result = phase.run()

        assert result.outcome == PhaseOutcome.FAILED
        assert result.stream is None
        assert result.message
        assert not (work_dir / "probe.yaml").exists()


# ---------------------------------------------------------------------------
# REUSED path — probe.yaml fully cached, no --crop override
# ---------------------------------------------------------------------------

class TestProbePhaseReused:
    """ProbePhase.run() must return REUSED when probe.yaml is fully cached.

    Bug guarded: without the REUSED shortcut the phase would re-run crop
    detection and frame counting on every invocation even when both values are
    already persisted, and it must hand callers back the cached values verbatim.
    """

    def test_reused_carries_cached_frame_count_and_crop(self, tmp_path: Path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        cached_crop = CropParams(top=140, bottom=140)
        _write_probe_yaml(work_dir, frame_count=72000, crop=cached_crop)

        job_result        = _make_job_result(work_dir, tmp_path / "source.mkv")
        extraction_result = _make_extraction_result(
            work_dir, _make_video_stream(tmp_path / "source.mkv"))
        phase             = _make_probe_phase(job_result, extraction_result, crop_params=None)

        result = phase.run()

        assert result.outcome == PhaseOutcome.REUSED
        assert result.stream is not None
        assert result.stream is not None
        assert result.stream.state == ArtifactState.COMPLETE
        assert result.stream.payload.frame_count == 72000
        assert result.crop.top    == 140
        assert result.crop.bottom == 140

    def test_crop_override_bypasses_reused(self, tmp_path: Path):
        """A manual --crop override must prevent the REUSED shortcut.

        Bug guarded: if crop_params were ignored when probe.yaml is present,
        the user's explicit crop override would be silently discarded.
        """
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        _write_probe_yaml(work_dir, frame_count=1440, crop=CropParams(top=140, bottom=140))

        job_result        = _make_job_result(work_dir, tmp_path / "source.mkv")
        extraction_result = _make_extraction_result(
            work_dir, _make_video_stream(tmp_path / "source.mkv"))
        override_crop     = CropParams(top=0, bottom=0)

        with (
            patch("pyqenc.phases.probe.detect_crop_parameters", return_value=override_crop),
            patch("pyqenc.phases.probe.get_frame_count", return_value=1440),
        ):
            phase  = _make_probe_phase(job_result, extraction_result, crop_params=override_crop)
            result = phase.run()

        assert result.outcome != PhaseOutcome.REUSED


# ---------------------------------------------------------------------------
# COMPLETED path — probe.yaml absent → detection runs and persists
# ---------------------------------------------------------------------------

class TestProbePhaseCompleted:
    """ProbePhase.run() must return COMPLETED after running detection.

    Bug guarded: without the detection path a fresh run would never resolve
    crop or frame count; and if the detected values were not persisted to
    probe.yaml, every later run would re-probe instead of reusing the result.
    """

    _DETECTED_CROP        = CropParams(top=140, bottom=140)
    _DETECTED_FRAME_COUNT = 1440

    def _run_with_mocks(self, tmp_path: Path) -> tuple[ProbePhaseResult, Path]:
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        job_result        = _make_job_result(work_dir, tmp_path / "source.mkv")
        extraction_result = _make_extraction_result(
            work_dir, _make_video_stream(tmp_path / "source.mkv"))
        phase             = _make_probe_phase(job_result, extraction_result, crop_params=None)

        # The video artifact's index (its material component) exists at the
        # conventional location → the frame count is read from it.
        timestamps = work_dir / "extracted" / "timestamps.txt"
        timestamps.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(f"{i * 42}\n" for i in range(self._DETECTED_FRAME_COUNT))
        timestamps.write_text("# timestamp format v2\n" + body, encoding="utf-8")

        with patch("pyqenc.phases.probe.detect_crop_parameters", return_value=self._DETECTED_CROP):
            result = phase.run()

        return result, work_dir

    def test_completed_carries_detected_values(self, tmp_path: Path):
        result, _ = self._run_with_mocks(tmp_path)

        assert result.outcome == PhaseOutcome.COMPLETED
        assert result.stream is not None
        assert result.stream is not None
        assert result.stream.state == ArtifactState.COMPLETE
        assert result.stream.payload.frame_count == self._DETECTED_FRAME_COUNT
        assert result.crop.top    == self._DETECTED_CROP.top
        assert result.crop.bottom == self._DETECTED_CROP.bottom

    def test_probe_yaml_persists_detected_values(self, tmp_path: Path):
        """probe.yaml must be written with the detected values after COMPLETED.

        Bug guarded: if detection results were not persisted, the REUSED path
        on the next run would load stale or zero values (or re-probe).
        """
        _, work_dir = self._run_with_mocks(tmp_path)

        loaded = ProbeState.load(work_dir / "probe.yaml")
        assert loaded is not None
        assert loaded.frame_count == self._DETECTED_FRAME_COUNT
        assert loaded.crop is not None
        assert loaded.crop.top    == self._DETECTED_CROP.top
        assert loaded.crop.bottom == self._DETECTED_CROP.bottom


# ---------------------------------------------------------------------------
# Timing instrumentation — top-level probe + dotted crop_detect / frame_count
# ---------------------------------------------------------------------------


class TestProbeTiming:
    """The probe phase's work is visible in metrics.yaml (closes the TODO-7 gap)."""

    _DETECTED_CROP = CropParams(top=8, bottom=8, left=0, right=0)
    _DETECTED_FRAME_COUNT = 1234

    def test_fresh_run_records_probe_and_sub_actions(self, tmp_path: Path) -> None:
        """A fresh probe run reports the top-level ``probe`` row plus both
        dotted sub-action rows (crop_detect, frame_count) in metrics.yaml.

        Bug guarded: the probe phase's work (crop detection, frame counting —
        both external ffmpeg/ffprobe passes) finishing without its cost being
        attributable in the run's report.
        """
        source = tmp_path / "source.mkv"
        source.write_bytes(bytes(64))
        job_result = _make_job_result(tmp_path, source)
        extraction_result = _make_extraction_result(tmp_path, _make_video_stream(source))

        def run(collector) -> None:
            phase = _make_probe_phase(
                job_result, extraction_result, collector=collector
            )
            # Frame count via the null-count fallback (no timestamps file).
            with (
                patch("pyqenc.phases.probe.detect_crop_parameters", return_value=self._DETECTED_CROP),
                patch("pyqenc.phases.probe.get_frame_count", return_value=self._DETECTED_FRAME_COUNT),
            ):
                result = phase.run()
            assert result.outcome is PhaseOutcome.COMPLETED

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert "probe" in top_level, f"probe row missing: {sorted(top_level)}"
        dotted = _dotted_keys(metrics)
        assert "probe.crop_detect" in dotted, f"crop_detect missing: {sorted(dotted)}"
        assert "probe.frame_count" in dotted, f"frame_count missing: {sorted(dotted)}"

    def test_cached_run_records_no_probe_work_rows(self, tmp_path: Path) -> None:
        """A fully cached probe run reports only recovery — no probe work rows.

        Bug guarded: a cached probe.yaml re-running (and re-timing) crop
        detection or frame counting over values it already has.
        """
        source = tmp_path / "source.mkv"
        source.write_bytes(bytes(64))
        job_result = _make_job_result(tmp_path, source)
        extraction_result = _make_extraction_result(tmp_path, _make_video_stream(source))
        _write_probe_yaml(
            tmp_path, frame_count=42, crop=CropParams(top=1, bottom=1, left=0, right=0)
        )

        def run(collector) -> None:
            phase = _make_probe_phase(
                job_result, extraction_result, collector=collector
            )
            result = phase.run()
            assert result.outcome is PhaseOutcome.REUSED

        metrics = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert top_level == {"recovery"}, (
            f"a cached probe must report only its recovery scan, got: {sorted(top_level)}"
        )
        assert not _dotted_groups(metrics), (
            f"a cached probe must report no sub-action rows, got: {_dotted_groups(metrics)}"
        )
