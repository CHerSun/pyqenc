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
``VideoMetadata.probe_extended``) — boundaries, not internals of the phase.

Covered observable paths:
- FAILED when extraction produced no video (source None, error set, no probe.yaml)
- REUSED when probe.yaml is fully cached and no --crop override
- crop override bypasses the REUSED shortcut
- COMPLETED when probe.yaml is absent: detection runs and probe.yaml persists
"""
# CHerSun 2026

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pyqenc.app_config import load_app_config
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import (
    CleanupLevel,
    CropParams,
    ExtendedVideoMetadata,
    PhaseOutcome,
    VideoMetadata,
)
from pyqenc.phase import Artifact, Phase
from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.state import ArtifactState, JobState, ProbeState

# ---------------------------------------------------------------------------
# Helpers — build REAL typed results and a REAL registry
# ---------------------------------------------------------------------------

_APP_CONFIG = load_app_config(default_only=True)


def _make_source_vm(path: Path) -> VideoMetadata:
    """Return a VideoMetadata with fast-probe fields pre-populated."""
    meta = VideoMetadata(path=path)
    meta._duration_seconds = 3600.0
    meta._fps              = 24.0
    meta._resolution       = "1920x1080"
    return meta


def _make_job_result(work_dir: Path, source_vm: VideoMetadata) -> JobPhaseResult:
    """Return a COMPLETED JobPhaseResult carrying the source and work_dir."""
    return JobPhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        artifacts = [Artifact(path=work_dir / "job.yaml", state=ArtifactState.COMPLETE)],
        message   = "job complete",
        job       = JobState(source=source_vm),
        work_dir  = work_dir,
        source    = source_vm.path,
    )


def _make_extraction_result(video: VideoMetadata | None) -> ExtractionPhaseResult:
    """Return a COMPLETED ExtractionPhaseResult carrying the extracted video."""
    artifacts = (
        [Artifact(path=video.path, state=ArtifactState.COMPLETE)]
        if video is not None else []
    )
    return ExtractionPhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        artifacts = artifacts,
        message   = "extraction complete",
        video     = video,
    )


def _make_probe_phase(
    job_result:        JobPhaseResult,
    extraction_result: ExtractionPhaseResult,
    *,
    crop_params:       CropParams | None = None,
) -> ProbePhase:
    """Construct ProbePhase via its real constructor and a real registry.

    Real JobPhase / ExtractionPhase instances are placed in the registry with
    their public ``result`` pre-set to a completed typed result, so the shared
    dependency walk treats them as already-run without any mocking.
    """
    collector = NoOpMetricsCollector()

    job = JobPhase(
        _APP_CONFIG, None,
        source     = job_result.source,      # type: ignore[arg-type]
        work_dir   = job_result.work_dir,    # type: ignore[arg-type]
        force      = False,
        cleanup    = CleanupLevel.NONE,
        no_metrics = True,
        collector  = collector,
    )
    job.result = job_result

    registry: dict[type[Phase], Phase] = {JobPhase: job}

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
        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        job_result        = _make_job_result(work_dir, source_vm)
        extraction_result = _make_extraction_result(video=None)
        phase             = _make_probe_phase(job_result, extraction_result)

        result = phase.run()

        assert result.outcome == PhaseOutcome.FAILED
        assert result.source is None
        assert result.error is not None
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

        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        extracted_vm      = _make_source_vm(tmp_path / "extracted.mkv")
        job_result        = _make_job_result(work_dir, source_vm)
        extraction_result = _make_extraction_result(video=extracted_vm)
        phase             = _make_probe_phase(job_result, extraction_result, crop_params=None)

        result = phase.run()

        assert result.outcome == PhaseOutcome.REUSED
        assert result.source is not None
        assert result.source.frame_count == 72000
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

        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        extracted_vm      = _make_source_vm(tmp_path / "extracted.mkv")
        job_result        = _make_job_result(work_dir, source_vm)
        extraction_result = _make_extraction_result(video=extracted_vm)
        override_crop     = CropParams(top=0, bottom=0)

        with (
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=override_crop),
            patch.object(
                VideoMetadata, "probe_extended",
                return_value=ExtendedVideoMetadata.from_base(source_vm, frame_count=1440),
            ),
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

        source_vm    = _make_source_vm(tmp_path / "source.mkv")
        extracted_vm = _make_source_vm(tmp_path / "extracted.mkv")
        job_result   = _make_job_result(work_dir, source_vm)
        extended_vm  = ExtendedVideoMetadata.from_base(
            source_vm, frame_count=self._DETECTED_FRAME_COUNT
        )
        extraction_result = _make_extraction_result(video=extracted_vm)
        phase             = _make_probe_phase(job_result, extraction_result, crop_params=None)

        with (
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=self._DETECTED_CROP),
            patch.object(VideoMetadata, "probe_extended", return_value=extended_vm),
        ):
            result = phase.run()

        return result, work_dir

    def test_completed_carries_detected_values(self, tmp_path: Path):
        result, _ = self._run_with_mocks(tmp_path)

        assert result.outcome == PhaseOutcome.COMPLETED
        assert result.source is not None
        assert result.source.frame_count == self._DETECTED_FRAME_COUNT
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
