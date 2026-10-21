"""Unit tests for encoding phase recovery.

Covers the fast presence-based recovery in ``_recover_pairs``:
- A pair is COMPLETE when both a winning .mkv and its .yaml sidecar exist in
  ``encoded/<strategy>/``.
- A pair is ABSENT when neither is present.
- ``winning_file`` is populated on COMPLETE pairs.
- The index is built from a single ``iterdir()`` per strategy — no per-pair globs.
"""

from decimal import Decimal
from pathlib import Path

from pyqenc.app_config import load_app_config
from pyqenc.phases.encoding import _recover_pairs
from pyqenc.state import ArtifactState
from pyqenc.stream_model import EncodedChunk as _EC
from pyqenc.utils.yaml_utils import write_yaml_atomic

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_STRATEGY_OBJ = next(
    s for s in load_app_config(default_only=True).build_encoding_plan(optimize=True, measure=True).strategies
    if s.preset == "slow" and s.profile == "h265-aq"
)
_CHUNK_ID   = "00꞉00꞉00․000-00꞉01꞉30․000"
_STRATEGY   = _STRATEGY_OBJ.display_name()
_SAFE_STRAT = _STRATEGY_OBJ.safe_name()
_RESOLUTION = "1920x800"
_QUALITY     = Decimal("18.0")


def _make_complete_pair(encoded_dir: Path, chunk_id: str = _CHUNK_ID, quality: Decimal = _QUALITY) -> Path:
    """Write a winning .mkv and its result sidecar into encoded_dir.

    Layout mirrors the real encoded/ directory:
      <chunk_id>.<res>.q<N>.mkv   — winning attempt
      <chunk_id>.<res>.yaml        — result sidecar (no quality in name)
    """
    encoded_dir.mkdir(parents=True, exist_ok=True)
    mkv     = encoded_dir / _EC.format_winner_file_name(chunk_id)
    sidecar = encoded_dir / _EC.format_winner_sidecar_name(chunk_id)
    mkv.write_bytes(b"\x00" * 512)
    write_yaml_atomic(sidecar, {"quality": str(quality), "targets_met": True, "frame_count": 24, "metrics": {"vmaf_min": 94.5}})
    return mkv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecoverEncodingAttempts:

    def test_complete_pair_detected(self, tmp_path: Path) -> None:
        """A pair with mkv + yaml in encoded/ is classified COMPLETE."""
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        winning     = _make_complete_pair(encoded_dir)

        recovery = _recover_pairs(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY_OBJ],
            require_winner_sidecar = True,
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state        == ArtifactState.COMPLETE
        assert pair.winning_file == winning
        assert recovery.pending  == []

    def test_absent_pair_when_no_encoded_dir(self, tmp_path: Path) -> None:
        """A pair with no encoded/ directory is ABSENT."""
        recovery = _recover_pairs(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY_OBJ],
            require_winner_sidecar = True,
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ABSENT
        assert (_CHUNK_ID, _STRATEGY) in recovery.pending

    def test_absent_pair_when_mkv_missing_sidecar(self, tmp_path: Path) -> None:
        """A .mkv without a .yaml sidecar is not COMPLETE — pair is ABSENT."""
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        encoded_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{_CHUNK_ID}.{_RESOLUTION}.q{_QUALITY:4.1f}"
        (encoded_dir / f"{stem}.mkv").write_bytes(b"\x00" * 512)
        # no .yaml written

        recovery = _recover_pairs(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY_OBJ],
            require_winner_sidecar = True,
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ABSENT

    def test_multiple_chunks_mixed_states(self, tmp_path: Path) -> None:
        """Multiple chunks: some COMPLETE, some ABSENT."""
        chunk_a     = "00꞉00꞉00․000-00꞉01꞉00․000"
        chunk_b     = "00꞉01꞉00․000-00꞉02꞉00․000"
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        _make_complete_pair(encoded_dir, chunk_id=chunk_a)
        # chunk_b has no files

        recovery = _recover_pairs(
            work_dir  = tmp_path,
            chunk_ids = [chunk_a, chunk_b],
            strategies = [_STRATEGY_OBJ],
            require_winner_sidecar = True,
        )

        assert recovery.pairs[(chunk_a, _STRATEGY)].state == ArtifactState.COMPLETE
        assert recovery.pairs[(chunk_b, _STRATEGY)].state == ArtifactState.ABSENT
        assert (chunk_b, _STRATEGY) in recovery.pending
        assert (chunk_a, _STRATEGY) not in recovery.pending

    def test_winning_file_path_is_correct(self, tmp_path: Path) -> None:
        """winning_file points to the actual .mkv, not a placeholder."""
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        winning     = _make_complete_pair(encoded_dir)

        recovery = _recover_pairs(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY_OBJ],
            require_winner_sidecar = True,
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.winning_file is not None
        assert pair.winning_file.exists()
        assert pair.winning_file == winning


# ---------------------------------------------------------------------------
# measure_attempts seam (fixed-quality spec Req 9.2, 9.3)
# ---------------------------------------------------------------------------

from decimal import Decimal as _D
from fractions import Fraction
from typing import cast as _cast
from unittest.mock import MagicMock as _MM
from unittest.mock import patch as _patch

import yaml as _yaml

from pyqenc.metrics import MetricsCollector as _MetricsCollector
from pyqenc.models import Strategy as _Strategy
from pyqenc.phases.encoding import ChunkEncoder as _ChunkEncoder
from pyqenc.phases.encoding import _ChunkEncodingResult
from pyqenc.quality import MetricType as _MetricType
from pyqenc.quality import QualityLogs as _QL
from pyqenc.quality import QualityMeasurement as _QE
from pyqenc.stream_model import VideoStreamChunk as _VSC
from pyqenc.utils.ffmpeg_runner import FFmpegRunResult as _FFR
from pyqenc.utils.visualization import QualityMeasurer as _QMeasurer


def _fixed_strategy() -> _Strategy:
    """A strategy mock whose codec domain is the single point CRF=18."""
    codec = _MM()
    codec.quality_better      = _D("18.0")
    codec.quality_worse       = _D("18.0")
    codec.quality_granularity = _D("0.5")
    codec.quality_max_step    = None
    codec.quality_label       = "CRF"
    codec.quality_log_padding = 4
    strategy = _MM()
    strategy.display_name.return_value = "test-strategy"
    strategy.safe_name.return_value    = "test_strategy"
    strategy.codec = codec
    return _cast(_Strategy, strategy)


def _chunk_mock(tmp_path: object) -> _VSC:
    chunk = _MM()
    chunk.safe_name.return_value   = "chunk_001"
    chunk.start_timestamp          = 0.0
    chunk.end_timestamp            = 5.0
    chunk.duration_seconds         = 5.0
    chunk.frame_count              = 120
    chunk.as_input.return_value    = _MM()
    info = _MM()
    info.resolution    = "1920x1080"
    info.fps_fraction  = Fraction(24, 1)
    stream = _MM()
    stream.stream.info = info
    chunk.stream = stream
    return _cast(_VSC, chunk)


def _run_encode(
    tmp_path: Path,
    *,
    measure_attempts: bool | None,
) -> tuple[_ChunkEncodingResult, _MM]:
    """encode_chunk over a fixed single-point domain with a fake encoder.

    ffmpeg encode and the existing-encoding check are patched; the
    evaluator is a mock so the measurement call itself is observable.
    Returns (ChunkEncodingResult, evaluator_mock).
    """
    strategy = _fixed_strategy()
    chunk    = _chunk_mock(tmp_path)

    fake_eval = _MM(spec=_QE)
    fake_eval.targets_met  = True
    fake_eval.logs         = _QL()
    fake_eval.metrics      = {_MetricType.VMAF: {"min": 95.0, "median": 96.5}}
    fake_eval.frame_count  = 120

    evaluator = _MM(spec=_QMeasurer)
    evaluator.work_dir = tmp_path
    evaluator.measure_chunk.return_value = fake_eval

    if measure_attempts is None:
        encoder = _ChunkEncoder(
            quality_measurer  = evaluator,
            work_dir          = tmp_path,
            collector         = _MM(spec=_MetricsCollector),
            crop_params       = None,
            cleanup_level     = CleanupLevel.NONE,
            visual_hash       = True,
            sampling           = 10,
            metric_prefix     = MetricKey.ENCODING,
            measure_attempts  = True,
        )
    else:
        encoder = _ChunkEncoder(
            quality_measurer  = evaluator,
            work_dir          = tmp_path,
            collector         = _MM(spec=_MetricsCollector),
            crop_params       = None,
            cleanup_level     = CleanupLevel.NONE,
            visual_hash       = True,
            sampling           = 10,
            metric_prefix     = MetricKey.ENCODING,
            measure_attempts  = measure_attempts,
        )

    attempt = tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.mkv"
    attempt.parent.mkdir(parents=True, exist_ok=True)
    attempt.write_bytes(b"fake mkv")

    run_result = _FFR(returncode=0, success=True, stderr_lines=[], frame_count=120)
    with (
        _patch.object(encoder, "_check_existing_encoding", return_value=None),
        _patch.object(encoder, "_encode_with_ffmpeg", return_value=run_result),
    ):
        result = encoder.encode_chunk(
            chunk           = chunk,
            strategy        = strategy,
            quality_targets = [],
            initial_quality     = _D("18.0"),
        )
    return result, evaluator


class TestMeasureAttemptsSeam:
    """The measure_attempts control on the shared chunk-encoding machinery."""

    def test_default_measures_attempts(self, tmp_path: Path) -> None:
        """Without the kwarg the encoder measures (today's behavior)."""
        result, evaluator = _run_encode(tmp_path, measure_attempts=None)
        assert result.success is True
        evaluator.measure_chunk.assert_called_once()

    def test_off_path_skips_evaluation_and_accepts_single_point(self, tmp_path: Path) -> None:
        """measure_attempts=False: no measurement, no sidecars anywhere, one
        accepted attempt at the fixed value.

        No measurement facts, no sidecars (§83): the attempt and the winner
        are video-only — a later attempt-measuring run sees the pair pending
        and re-measures the existing videos in place.
        """
        result, evaluator = _run_encode(tmp_path, measure_attempts=False)
        evaluator.measure_chunk.assert_not_called()
        assert result.success is True
        assert result.targets_met is True
        assert result.final_quality == _D("18.0")
        assert result.attempts == 1

        attempt_sidecar = (
            tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.yaml"
        )
        assert not attempt_sidecar.exists()

        winner_video = tmp_path / "encoded" / "test_strategy" / "chunk_001.mkv"
        winner_sidecar = (
            tmp_path / "encoded" / "test_strategy" / "chunk_001.yaml"
        )
        assert winner_video.exists()
        assert not winner_sidecar.exists()

    def test_on_path_measures_and_persists_all_metrics(self, tmp_path: Path) -> None:
        """measure_attempts=True: measurement runs and all metrics persist."""
        result, evaluator = _run_encode(tmp_path, measure_attempts=True)
        evaluator.measure_chunk.assert_called_once()
        assert result.success is True
        sidecar_path = (
            tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.yaml"
        )
        sidecar = _yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["metrics"]["vmaf_min"] == 95.0

    def test_sidecarless_attempt_video_heals_by_remeasure(self, tmp_path: Path) -> None:
        """The upgrade path: an attempt video left by an unmeasured run (no
        attempt sidecar) is picked up by its exact name and re-measured in
        place — the encoder never runs, the sidecars materialize with real
        metrics."""
        strategy = _fixed_strategy()
        chunk    = _chunk_mock(tmp_path)

        fake_eval = _MM(spec=_QE)
        fake_eval.targets_met  = True
        fake_eval.logs         = _QL()
        fake_eval.metrics      = {_MetricType.VMAF: {"min": 95.0, "median": 96.5}}
        fake_eval.frame_count  = 120

        evaluator = _MM(spec=_QMeasurer)
        evaluator.work_dir = tmp_path
        evaluator.measure_chunk.return_value = fake_eval

        encoder = _ChunkEncoder(
            quality_measurer = evaluator,
            work_dir         = tmp_path,
            collector        = _MM(spec=_MetricsCollector),
            crop_params       = None,
            cleanup_level     = CleanupLevel.NONE,
            visual_hash       = True,
            sampling          = 10,
            metric_prefix     = MetricKey.ENCODING,
            measure_attempts = True,
        )

        # The unmeasured run's residue: the attempt video, nothing else.
        attempt = tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.mkv"
        attempt.parent.mkdir(parents=True, exist_ok=True)
        attempt.write_bytes(b"fake mkv")

        encode_mock = _MM(return_value=_FFR(returncode=0, success=True, stderr_lines=[], frame_count=120))
        with _patch.object(encoder, "_encode_with_ffmpeg", encode_mock):
            result = encoder.encode_chunk(
                chunk       = chunk,
                strategy     = strategy,
                quality_targets = [],
                initial_quality = _D("18.0"),
            )

        encode_mock.assert_not_called()
        evaluator.measure_chunk.assert_called_once()
        assert result.success is True

        sidecar = _yaml.safe_load(attempt.with_suffix(".yaml").read_text(encoding="utf-8"))
        assert sidecar["metrics"]["vmaf_min"] == 95.0
        assert (
            tmp_path / "encoded" / "test_strategy" / "chunk_001.yaml"
        ).exists()

    def test_seam_threaded_explicitly_at_both_call_sites(self) -> None:
        """Optimization always measures; the encoding phase reads the plan."""
        import inspect

        from pyqenc.phases.encoding import EncodingPhase
        from pyqenc.phases.optimization import _make_encoder

        assert "measure_attempts  = True" in inspect.getsource(_make_encoder)
        assert "plan.measures_attempts" in inspect.getsource(EncodingPhase._execute)


# ---------------------------------------------------------------------------
# Fixed-mode encoding: degenerate single-point path + presentation anchor
# (fixed-quality spec Req 9.1, 9.4, 9.5, 9.7)
# ---------------------------------------------------------------------------

from pyqenc.metrics import MetricKey, NoOpMetricsCollector
from pyqenc.models import CleanupLevel, CropParams, PhaseOutcome, QualityTarget
from pyqenc.phase import Artifact as _PAArtifact
from pyqenc.phase import PhaseRegistry
from pyqenc.phases.chunking import ChunkingPhase as _CCP
from pyqenc.phases.chunking import ChunkingPhaseResult
from pyqenc.phases.encoding import (
    EncodingPhase,
    EncodingPhaseResult,
    _build_encoded_chunk,
    _EncodingResult,
)
from pyqenc.phases.job import JobPhase
from pyqenc.phases.optimization import OptimizationPhase, OptimizationPhaseResult
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.state import ArtifactState as _PAState
from pyqenc.stream_model import ExtendedVideoStream


class TestFixedDegenerateSinglePoint:
    """Exactly one accepted attempt per pair via the existing degenerate path (Req 9.1)."""

    def test_single_attempt_accepted_with_measurement(self, tmp_path: Path) -> None:
        """Uncompared (empty targets): one measured attempt, vacuous
        pass, winner promoted at the fixed value."""
        result, evaluator = _run_encode_with_targets(
            tmp_path, targets=[], measure_attempts=True,
        )
        evaluator.measure_chunk.assert_called_once()
        assert result.success is True
        assert result.targets_met is True
        assert result.attempts == 1
        assert result.final_quality == _D("18.0")

    def test_single_attempt_accepted_when_anchor_targets_miss(self, tmp_path: Path) -> None:
        """Compared-run anchor-targets miss: the single-point domain cannot produce a
        second candidate — the attempt is accepted unconditionally with a
        presentation-only targets_met=False verdict (Req 9.7), and the
        acceptance line still logs in the uniform success shape (INFO for a
        fixed-mode miss — expected behavior, not a search failure)."""
        unreachable = [QualityTarget(metric="vmaf", statistic="min", value=99.0)]
        with _patch("pyqenc.phases.encoding.logger") as captured:
            result, _ = _run_encode_with_targets(
                tmp_path, targets=unreachable, measure_attempts=True,
            )
        assert result.success is True
        assert result.targets_met is False
        assert result.attempts == 1
        assert result.final_quality == _D("18.0")
        # The winner is still promoted.
        assert (tmp_path / "encoded" / "test_strategy" / "chunk_001.yaml").exists()
        # Every accepted winner logs its acceptance, uniform with the success
        # line: visual hash + strategy + chunk + soft-miss status + limiter.
        # The ≈ marks a matter-of-fact miss against the approximate anchor —
        # softer than per-attempt ✘ and exhausted-search ❌.
        assert any(
            "miss ≈ with CRF" in str(call) and "limited by" in str(call)
            for call in captured.info.call_args_list
        )
        # The fixed-mode miss is informational — no warning-level acceptance.
        assert not any("miss" in str(c) for c in captured.warning.call_args_list)

    def test_searched_exhaustion_is_pronounced(self, tmp_path: Path) -> None:
        """A ranged domain that exhausts short of user-requested quality is a
        real (bypassable) problem: ❌ at WARNING, 'best' marking the accepted
        fallback — visibly stronger than the fixed-mode soft miss."""
        codec = _MM()
        codec.quality_better      = _D("18.0")
        codec.quality_worse       = _D("19.0")   # narrow range: exhausts fast
        codec.quality_granularity = _D("0.5")
        codec.quality_max_step    = None
        codec.quality_label       = "CRF"
        codec.quality_log_padding = 4
        strategy = _MM()
        strategy.display_name.return_value = "test-strategy"
        strategy.safe_name.return_value    = "test_strategy"
        strategy.codec = codec

        unreachable = [QualityTarget(metric="vmaf", statistic="min", value=99.0)]
        with _patch("pyqenc.phases.encoding.logger") as captured:
            result, _ = _run_encode_with_targets(
                tmp_path, targets=unreachable, measure_attempts=True,
                strategy=_cast(_Strategy, strategy),
            )
        assert result.success is True
        assert result.targets_met is False
        assert any(
            "exhausted ❌ with best CRF" in str(call) and "limited by" in str(call)
            for call in captured.warning.call_args_list
        )


def _run_encode_with_targets(
    tmp_path: Path,
    *,
    targets: list[QualityTarget],
    measure_attempts: bool,
    strategy: _Strategy | None = None,
):
    """encode_chunk over a strategy's domain with explicit targets.

    Defaults to the fixed single-point strategy; a ranged strategy exercises
    the searched-exhaustion path.
    """
    if strategy is None:
        strategy = _fixed_strategy()
    chunk = _chunk_mock(tmp_path)

    fake_eval = _MM(spec=_QE)
    fake_eval.targets_met = all(
        95.0 >= t.value for t in targets
    )
    fake_eval.logs = _QL()
    fake_eval.metrics = {_MetricType.VMAF: {"min": 95.0, "median": 96.5}}
    fake_eval.frame_count = 120

    evaluator = _MM(spec=_QMeasurer)
    evaluator.work_dir = tmp_path
    evaluator.measure_chunk.return_value = fake_eval

    encoder = _ChunkEncoder(
        quality_measurer  = evaluator,
        work_dir          = tmp_path,
        collector         = _MM(spec=_MetricsCollector),
        crop_params       = None,
        cleanup_level     = CleanupLevel.NONE,
        visual_hash       = True,
        sampling           = 10,
        metric_prefix     = MetricKey.ENCODING,
        measure_attempts  = measure_attempts,
    )

    attempt = tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.mkv"
    attempt.parent.mkdir(parents=True, exist_ok=True)
    attempt.write_bytes(b"fake mkv")

    run_result = _FFR(returncode=0, success=True, stderr_lines=[], frame_count=120)
    with (
        _patch.object(encoder, "_check_existing_encoding", return_value=None),
        _patch.object(encoder, "_encode_with_ffmpeg", return_value=run_result),
    ):
        result = encoder.encode_chunk(
            chunk=chunk, strategy=strategy, quality_targets=targets,
            initial_quality=_D("18.0"),        )
    return result, evaluator


class TestAttemptFrameCountSourcing:
    """The attempt sidecar's frame count comes from the last whole-file decode.

    Live finding 2026-10-09: re-measured attempts (sidecar missing or
    incomplete) carried the incoming sidecar's count — 0 when absent — and
    the zeros propagated into winner sidecars, darkening the
    frame-preservation chain. The metric run decodes the attempt whole, so
    its progress count IS the file's count; a 0-count sidecar is incomplete
    (Req 9a: frame count present) and re-measure heals it.
    """

    def _seed_attempt(self, tmp_path: Path, *, frame_count: int) -> Path:
        """A real attempt on disk with complete metrics and a given count."""
        attempt = tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.mkv"
        attempt.parent.mkdir(parents=True, exist_ok=True)
        attempt.write_bytes(b"fake mkv")
        attempt.with_suffix(".yaml").write_text(
            _yaml.safe_dump({
                "metrics": {"vmaf_min": 95.0},
                "sampling": 10,
                "frame_count": frame_count,
            }),
            encoding="utf-8",
        )
        return attempt

    def _encoder(self, tmp_path: Path, evaluator) -> _ChunkEncoder:
        return _ChunkEncoder(
            quality_measurer  = evaluator,
            work_dir          = tmp_path,
            collector         = _MM(spec=_MetricsCollector),
            crop_params       = None,
            cleanup_level     = CleanupLevel.NONE,
            visual_hash       = True,
            sampling           = 10,
            metric_prefix     = MetricKey.ENCODING,
            measure_attempts  = True,
        )

    def _target(self):
        from pyqenc.models import QualityTarget

        return [QualityTarget(metric="vmaf", statistic="min", value=95.0)]

    def test_zero_count_sidecar_remeasures_and_heals_the_count(self, tmp_path: Path) -> None:
        """A complete-metrics sidecar with frame_count 0 is NOT a cache hit;
        the re-measure writes the metric run's own count back."""
        attempt = self._seed_attempt(tmp_path, frame_count=0)

        fake_eval = _MM(spec=_QE)
        fake_eval.targets_met  = True
        fake_eval.logs         = _QL()
        fake_eval.metrics      = {_MetricType.VMAF: {"min": 95.0, "median": 96.5}}
        fake_eval.frame_count  = 120
        evaluator = _MM(spec=_QMeasurer)
        evaluator.work_dir = tmp_path
        evaluator.measure_chunk.return_value = fake_eval
        encoder = self._encoder(tmp_path, evaluator)

        with _patch.object(encoder, "_encode_with_ffmpeg") as mock_encode:
            result = encoder.encode_chunk(
                chunk           = _chunk_mock(tmp_path),
                strategy        = _fixed_strategy(),
                quality_targets = self._target(),
                initial_quality     = _D("18.0"),
            )

        assert result.success is True
        mock_encode.assert_not_called()          # the file existed — measure-only
        evaluator.measure_chunk.assert_called_once()
        data = _yaml.safe_load(attempt.with_suffix(".yaml").read_text(encoding="utf-8"))
        assert data["frame_count"] == 120

    def test_counted_sidecar_is_a_pure_cache_hit(self, tmp_path: Path) -> None:
        """Metrics + a real count = complete: no encode, no measure."""
        attempt = self._seed_attempt(tmp_path, frame_count=120)

        evaluator = _MM(spec=_QMeasurer)
        evaluator.work_dir = tmp_path
        encoder = self._encoder(tmp_path, evaluator)

        with (
            _patch.object(encoder, "_encode_with_ffmpeg") as mock_encode,
        ):
            result = encoder.encode_chunk(
                chunk           = _chunk_mock(tmp_path),
                strategy        = _fixed_strategy(),
                quality_targets = self._target(),
                initial_quality     = _D("18.0"),
            )

        assert result.success is True
        mock_encode.assert_not_called()
        evaluator.measure_chunk.assert_not_called()
        data = _yaml.safe_load(attempt.with_suffix(".yaml").read_text(encoding="utf-8"))
        assert data["frame_count"] == 120


class TestEncodingPresentationTargets:
    """What the encoding phase judges winner presentation against (Req 9.4, 9.5)."""

    def _run_phase(
        self,
        tmp_path: Path,
        monkeypatch,
        *,
        quality_range_override: tuple[_D, _D] | None,
        anchor_targets: list[QualityTarget],
        winner_metrics: dict[str, float] | None = None,
        winner_targets_met: bool = True,
        optimize: bool = True,
        measure: bool = True,
    ) -> tuple[EncodingPhaseResult, dict[str, object]]:
        """Run EncodingPhase through run() with a stubbed encode pool.

        Returns the phase result and the kwargs the pool (``_encode_pairs``)
        received. When *winner_metrics* is given, a winner result sidecar is
        written next to the winner file so the real winner scan reads it.
        """
        from pyqenc.app_config import load_app_config

        config = load_app_config(default_only=True).model_copy(deep=True)
        plan = config.build_encoding_plan(optimize=optimize, measure=measure,
            strategies = ["h265-aq+slow"],
            targets    = ["vmaf-min:93.0"],
            quality    = quality_range_override,
        )

        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 64)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        job = JobPhase(
            config, {}, source=src, work_dir=work_dir, force=False,
            cleanup=CleanupLevel.NONE, no_metrics=True,
            collector=NoOpMetricsCollector(),
        )
        job.run(dry_run=False)

        from fractions import Fraction

        from pyqenc.stream_model import (
            File as _File,
        )
        from pyqenc.stream_model import (
            VideoStream,
            VideoStreamChunk,
            VideoStreamInfo,
        )

        info = VideoStreamInfo(
            track_id=0, codec_name="hevc", fps=24.0,
            fps_fraction=Fraction(24, 1), resolution="1920x1080",
            duration_seconds=10.0,
        )
        extended = ExtendedVideoStream(
            stream=VideoStream(
                file=_File(path=src), info=info,
            ),
            frame_count=240, crop=CropParams(),
        )
        probe = ProbePhase(config, {}, collector=NoOpMetricsCollector(), crop_params=None, plan=plan)
        probe.result = ProbePhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="stub",
            plan=plan,
            stream=_PAArtifact(payload=extended, state=_PAState.COMPLETE),
        )

        chunk = VideoStreamChunk(stream=extended, start_timestamp=0.0,
                                 end_timestamp=10.0, frame_count=240)
        chunking = _CCP(config, {}, collector=NoOpMetricsCollector())
        chunking.result = ChunkingPhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="stub",
            chunks=[_PAArtifact(payload=chunk, state=_PAState.COMPLETE)],
        )

        strategy = plan.strategies[0]
        optimization = OptimizationPhase(config, {}, collector=NoOpMetricsCollector())
        optimization.result = OptimizationPhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="stub",
            selected_strategies=[strategy],
            anchor_targets=anchor_targets,
        )

        registry: PhaseRegistry = {
            JobPhase: job, ProbePhase: probe,
            _CCP: chunking, OptimizationPhase: optimization,
        }
        phase = EncodingPhase(config, registry, collector=NoOpMetricsCollector())

        # The encoded winner the stubbed pool reports back. Pre-seeded only
        # when attempts are measured — a video-only winner is COMPLETE under
        # attempt-aware recovery, which would fast-exit the phase before the
        # pool ever runs.
        winner_dir = work_dir / "encoded" / strategy.safe_name()
        winner_dir.mkdir(parents=True, exist_ok=True)
        winner_path = winner_dir / _EC.format_winner_file_name(chunk.safe_name())
        if plan.measures_attempts:
            winner_path.write_bytes(b"x" * 32)
        winner = _build_encoded_chunk(
            chunk=chunk, strategy=strategy,
            path=winner_path, frame_count=240,
        )

        captured: dict[str, object] = {}

        async def _fake_encode_pairs(**kwargs: object) -> _EncodingResult:
            captured.update(kwargs)
            # The winner sidecar materializes DURING the run (a pre-written
            # one would make recovery classify the pair complete and skip
            # execution entirely); the phase's winner scan then reads it.
            if winner_metrics is not None:
                write_yaml_atomic(
                    winner_dir / _EC.format_winner_sidecar_name(chunk.safe_name()),
                    {
                        "quality": "20", "targets_met": winner_targets_met,
                        "frame_count": 240, "metrics": winner_metrics,
                    },
                )
            result = _EncodingResult()
            result.encoded_chunks = {strategy.display_name(): [winner]}
            result.encoded_count = 1
            return result

        monkeypatch.setattr(
            "pyqenc.phases.encoding._encode_pairs", _fake_encode_pairs,
        )
        result = phase.run(dry_run=False)
        assert result.is_complete
        return result, captured

    def test_fixed_compared_uses_anchor_targets(self, tmp_path: Path, monkeypatch) -> None:
        anchor_targets_fixture = [
            QualityTarget(metric="vmaf", statistic="median", value=91.0),
            QualityTarget(metric="vif", statistic="median", value=84.0),
        ]
        _, captured = self._run_phase(
            tmp_path, monkeypatch,
            quality_range_override=(_D("18"), _D("18")),
            anchor_targets=anchor_targets_fixture,
        )
        received = _cast(list[QualityTarget], captured["quality_targets"])
        assert received == anchor_targets_fixture

    def test_fixed_uncompared_uses_no_anchor_targets(self, tmp_path: Path, monkeypatch) -> None:
        _, captured = self._run_phase(
            tmp_path, monkeypatch,
            quality_range_override=(_D("18"), _D("18")),
            anchor_targets=[],
        )
        assert captured["quality_targets"] == []

    def test_measurement_optout_judges_and_measures_nothing(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """``--no-measure --no-optimize`` fixed run: attempts are not
        measured, so nothing is judged either — the pool receives empty
        targets (guarding the search-steering crash on empty metrics) and
        the measurement opt-out, and the pinned attempt is accepted."""
        result, captured = self._run_phase(
            tmp_path, monkeypatch,
            quality_range_override=(_D("18"), _D("18")),
            anchor_targets=[QualityTarget(metric="vmaf", statistic="median", value=91.0)],
            optimize=False,
            measure=False,
        )
        assert result.is_complete
        assert captured["quality_targets"] == []
        # Consequences of the sidecar-free fast path: no encoding.yaml (the
        # replay aggregate holds only measurement-derived data).
        assert not (tmp_path / "work" / "encoding.yaml").exists()

    def test_searched_uses_config_targets_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        result, captured = self._run_phase(
            tmp_path, monkeypatch,
            quality_range_override=None,
            anchor_targets=[],
        )
        assert result.outcome is PhaseOutcome.COMPLETED
        # The config targets flow through verbatim — searched behavior is
        # byte-identical to before the spec.
        from pyqenc.models import QualityTarget as _QT
        assert captured["quality_targets"] == [
            _QT(metric="vmaf", statistic="min", value=93.0),
        ]


class TestSuboptimalOutcome:
    """``SUBOPTIMAL`` (2026-10-19 ruling): search mode + every pair won +
    at least one winner below the targets. Fixed runs never qualify — an
    anchor-target miss is a matter of fact about an approximate anchor, not
    an unmet user target."""

    def test_search_mode_missed_winner_is_suboptimal(self, tmp_path: Path, monkeypatch) -> None:
        """Bug guarded: a search run that had to accept a winner below the
        user's targets reported plain COMPLETED — indistinguishable from a
        clean run in the outcome, the closer, and the run summary."""
        result, _ = self._run_searched(tmp_path, monkeypatch, missed=True)

        assert result.outcome is PhaseOutcome.SUBOPTIMAL
        assert result.is_complete
        assert "1 winner(s) below targets" in result.message

    def test_fixed_mode_missed_winner_stays_completed(self, tmp_path: Path, monkeypatch) -> None:
        """The fixed-mode anchor miss must NOT re-label the phase — the
        quality was pinned by the user; the anchor comparison is
        informational. The tallies still record the miss (the limiter table
        shows it); only the outcome stays COMPLETED."""
        result, _ = TestEncodingPresentationTargets()._run_phase(
            tmp_path, monkeypatch,
            quality_range_override=(_D("18"), _D("18")),
            anchor_targets=[QualityTarget(metric="vmaf", statistic="median", value=91.0)],
            winner_metrics={"vmaf_median": 80.0},
            winner_targets_met=False,
        )
        assert result.outcome is PhaseOutcome.COMPLETED

    def test_search_mode_clean_winner_stays_completed(self, tmp_path: Path, monkeypatch) -> None:
        result, _ = self._run_searched(
            tmp_path, monkeypatch, missed=False,
            metrics={"vmaf_min": 95.0},
        )
        assert result.outcome is PhaseOutcome.COMPLETED

    def test_search_mode_without_sidecar_stays_completed(self, tmp_path: Path, monkeypatch) -> None:
        """No readable winner sidecar → no limiter tallies → nothing can be
        judged suboptimal; the run stays COMPLETED."""
        result, _ = self._run_searched(tmp_path, monkeypatch, missed=False, metrics=None)
        assert result.outcome is PhaseOutcome.COMPLETED

    @staticmethod
    def _run_searched(
        tmp_path: Path,
        monkeypatch,
        *,
        missed: bool,
        metrics: dict[str, float] | None = None,
    ) -> tuple[EncodingPhaseResult, dict[str, object]]:
        if metrics is None and missed:
            metrics = {"vmaf_min": 80.0}
        return TestEncodingPresentationTargets()._run_phase(
            tmp_path, monkeypatch,
            quality_range_override=None,
            anchor_targets=[],
            winner_metrics=metrics,
            winner_targets_met=not missed,
        )


import logging
import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyqenc.phases.encoding import EncodingSidecar, LimiterSummary, LimiterSummaryRow
from pyqenc.utils.yaml_utils import save_model


class TestWinningLimiterBasisLine:
    """The winning-limiter table states its absolute judging basis (§125).

    The surplus/deficit cells of the winning-limiter table are deltas —
    a reader could not turn them into absolute metric values because the
    judging basis (the configured targets, or the anchor targets on fixed
    runs) was stated nowhere. Covered on the fully-reused path, which
    renders the table from ``encoding.yaml``; the processing path feeds
    the same renderer the same inputs.
    """

    def _run_reused_phase(
        self,
        base: Path,
        caplog,
        *,
        fixed: bool,
        plan_targets: list[str],
        anchor_targets: list[QualityTarget],
        limiter_keys: list[str],
    ) -> list[str]:
        """Run EncodingPhase into full reuse and return its INFO messages.

        The work dir carries one complete winner pair plus the replay
        aggregate, so the phase takes the reuse fast exit and renders the
        winning-limiter table from the persisted summary.
        """
        from fractions import Fraction as _Fraction

        from pyqenc.stream_model import File as _File
        from pyqenc.stream_model import VideoStream, VideoStreamChunk, VideoStreamInfo

        config = load_app_config(default_only=True).model_copy(deep=True)
        plan = config.build_encoding_plan(optimize=True, measure=True,
            strategies = ["h265-aq+slow"],
            targets    = plan_targets,
            quality    = (_D("18"), _D("18")) if fixed else None,
        )

        base.mkdir(parents=True, exist_ok=True)
        src = base / "source.mkv"
        src.write_bytes(b"\x00" * 64)
        work_dir = base / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        job = JobPhase(
            config, {}, source=src, work_dir=work_dir, force=False,
            cleanup=CleanupLevel.NONE, no_metrics=True,
            collector=NoOpMetricsCollector(),
        )
        job.run(dry_run=False)

        info = VideoStreamInfo(
            track_id=0, codec_name="hevc", fps=24.0,
            fps_fraction=_Fraction(24, 1), resolution="1920x1080",
            duration_seconds=10.0,
        )
        extended = ExtendedVideoStream(
            stream=VideoStream(file=_File(path=src), info=info),
            frame_count=240, crop=CropParams(),
        )
        probe = ProbePhase(config, {}, collector=NoOpMetricsCollector(), crop_params=None, plan=plan)
        probe.result = ProbePhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="stub",
            plan=plan,
            stream=_PAArtifact(payload=extended, state=_PAState.COMPLETE),
        )

        chunk = VideoStreamChunk(stream=extended, start_timestamp=0.0,
                                 end_timestamp=10.0, frame_count=240)
        chunking = _CCP(config, {}, collector=NoOpMetricsCollector())
        chunking.result = ChunkingPhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="stub",
            chunks=[_PAArtifact(payload=chunk, state=_PAState.COMPLETE)],
        )

        strategy = plan.strategies[0]
        optimization = OptimizationPhase(config, {}, collector=NoOpMetricsCollector())
        optimization.result = OptimizationPhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="stub",
            selected_strategies=[strategy],
            anchor_targets=anchor_targets,
        )

        winner_dir = work_dir / "encoded" / strategy.safe_name()
        winner_dir.mkdir(parents=True, exist_ok=True)
        (winner_dir / _EC.format_winner_file_name(chunk.safe_name())).write_bytes(b"x" * 32)
        write_yaml_atomic(
            winner_dir / _EC.format_winner_sidecar_name(chunk.safe_name()),
            {"quality": "18.0", "targets_met": True, "frame_count": 240, "metrics": {}},
        )
        save_model(work_dir / EncodingPhase.SIDECAR_NAME, EncodingSidecar(
            frames={},
            summary=[LimiterSummary(
                strategy=strategy.display_name(), chunks=1,
                rows=[LimiterSummaryRow(limiter=key, passed=1, missed=0, med_surplus=2.0,
                                        med_quality=_D("18.0"))
                      for key in limiter_keys],
            )] if limiter_keys else [],
        ))

        phase = EncodingPhase(config, {
            JobPhase: job, ProbePhase: probe,
            _CCP: chunking, OptimizationPhase: optimization,
        }, collector=NoOpMetricsCollector())
        caplog.clear()
        with caplog.at_level(logging.INFO):
            result = phase.run(dry_run=False)
        assert result.outcome is PhaseOutcome.REUSED
        return caplog.messages

    def test_search_run_states_configured_targets_above_table(self, tmp_path, caplog) -> None:
        """A searched run states the configured targets above the table."""
        messages = self._run_reused_phase(
            tmp_path, caplog,
            fixed=False, plan_targets=["vmaf-min:93.0", "psnr-median:40.0"],
            anchor_targets=[], limiter_keys=["vmaf_min", "psnr_median"],
        )
        assert "Winning-limiter distribution" in messages
        assert "  targets: ◎  vmaf_min=93.0 · psnr_median=40.0" in messages

    def test_fixed_run_labels_the_basis_as_anchor_targets(self, tmp_path, caplog) -> None:
        """A fixed run states the anchor targets — never the search-tuned config targets."""
        messages = self._run_reused_phase(
            tmp_path, caplog,
            fixed=True, plan_targets=["vmaf-min:93.0"],
            anchor_targets=[
                QualityTarget(metric="vmaf", statistic="median", value=91.0),
                QualityTarget(metric="vif", statistic="median", value=84.0),
            ],
            limiter_keys=["vmaf_median"],
        )
        assert "  anchor targets: ◎  vmaf_median=91.0 · vif_median=84.0" in messages

    def test_uncompared_fixed_run_renders_no_table(self, tmp_path, caplog) -> None:
        """A run with no judging basis (uncompared fixed) renders no table at all."""
        messages = self._run_reused_phase(
            tmp_path, caplog,
            fixed=True, plan_targets=["vmaf-min:93.0"],
            anchor_targets=[], limiter_keys=[],
        )
        assert "Winning-limiter distribution" not in messages
        assert not [m for m in messages if "targets:" in m]

    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        targets=st.lists(st.tuples(
            st.sampled_from(["vmaf", "psnr", "ssim", "vif"]),
            st.sampled_from(["min", "median", "p10"]),
            st.integers(min_value=10, max_value=1000).map(lambda t: t / 10),
        ), min_size=1, max_size=3, unique=True),
        limiter_count=st.integers(min_value=1, max_value=3),
    )
    def test_every_limiter_key_reads_against_a_stated_absolute(
        self, tmp_path, caplog, targets, limiter_count,
    ) -> None:
        """Every limiter key of the table has its absolute value on the basis line.

        The basis line appears exactly once, directly under the table
        title, and states every configured target — limiters are
        worst-targets, so each row's deltas read against a visible number.
        """
        base = tmp_path / f"run-{uuid.uuid4().hex[:8]}"
        specs = [f"{metric}-{stat}:{value}" for metric, stat, value in targets]
        limiter_keys = [f"{metric}_{stat}"
                        for metric, stat, _ in targets[:limiter_count]]
        messages = self._run_reused_phase(
            base, caplog,
            fixed=False, plan_targets=specs,
            anchor_targets=[], limiter_keys=limiter_keys,
        )
        title_at = messages.index("Winning-limiter distribution")
        basis_lines = [m for m in messages if m.startswith("  targets: ")]
        assert len(basis_lines) == 1
        assert messages.index(basis_lines[0]) == title_at + 1
        for metric, stat, value in targets:
            assert f"{metric}_{stat}={value:.1f}" in basis_lines[0]


# ---------------------------------------------------------------------------
# The chunk->winner 1:1 span contract guard
# ---------------------------------------------------------------------------

import pytest

from pyqenc.phases.encoding import (
    _assert_one_winner_per_chunk,
)
from pyqenc.stream_model import EncodedChunk as _EncodedChunk


class TestOneWinnerPerChunkGuard:
    """``_assert_one_winner_per_chunk``: exact 1:1 spans, per strategy.

    The guard pins the transitional invariant between phases: every
    non-failed chunk must have exactly one winner per strategy. Losses,
    same-span duplicates, and foreign winners fail loudly at the encode
    conclusion — long before the merge-time frame check could only hint
    that something dropped.
    """

    def _chunk(self, start: float, end: float) -> _VSC:
        from fractions import Fraction

        from pyqenc.stream_model import (
            ExtendedVideoStream,
            File,
            VideoStream,
            VideoStreamInfo,
        )

        stream = ExtendedVideoStream(
            stream=VideoStream(
                file=File(path=Path(f"c_{start}_{end}.mkv")),
                info=VideoStreamInfo(
                    track_id=0, codec_name="hevc", fps=24.0,
                    fps_fraction=Fraction(24, 1), resolution="1920x1080",
                    duration_seconds=end - start,
                ),
            ),
            frame_count=24,
            crop=CropParams(),
        )
        return _VSC(
            stream=stream, start_timestamp=start, end_timestamp=end, frame_count=24,
        )

    def _winner(self, chunk: _VSC, tmp_path: Path) -> _EncodedChunk:
        from pyqenc.phases.encoding import _build_encoded_chunk

        mkv = tmp_path / _EC.format_winner_file_name(chunk.safe_name())
        mkv.write_bytes(b"x" * 64)
        return _build_encoded_chunk(
            chunk=chunk, strategy=_STRATEGY_OBJ,
            path=mkv, frame_count=24,
        )

    def test_exact_match_passes(self, tmp_path: Path) -> None:
        chunks = [self._chunk(0.0, 10.0), self._chunk(10.0, 20.0)]
        result = _EncodingResult()
        result.encoded_chunks[_STRATEGY] = [self._winner(c, tmp_path) for c in chunks]
        _assert_one_winner_per_chunk(result, chunks, [_STRATEGY_OBJ])

    def test_missing_winner_fails(self, tmp_path: Path) -> None:
        chunks = [self._chunk(0.0, 10.0), self._chunk(10.0, 20.0)]
        result = _EncodingResult()
        result.encoded_chunks[_STRATEGY] = [self._winner(chunks[0], tmp_path)]
        with pytest.raises(AssertionError, match="missing"):
            _assert_one_winner_per_chunk(result, chunks, [_STRATEGY_OBJ])

    def test_same_span_duplicate_fails(self, tmp_path: Path) -> None:
        chunks = [self._chunk(0.0, 10.0)]
        result = _EncodingResult()
        result.encoded_chunks[_STRATEGY] = [
            self._winner(chunks[0], tmp_path), self._winner(chunks[0], tmp_path),
        ]
        with pytest.raises(AssertionError, match="duplicate"):
            _assert_one_winner_per_chunk(result, chunks, [_STRATEGY_OBJ])

    def test_foreign_winner_fails(self, tmp_path: Path) -> None:
        chunks = [self._chunk(0.0, 10.0)]
        result = _EncodingResult()
        result.encoded_chunks[_STRATEGY] = [self._winner(self._chunk(30.0, 40.0), tmp_path)]
        with pytest.raises(AssertionError, match="unexpected"):
            _assert_one_winner_per_chunk(result, chunks, [_STRATEGY_OBJ])

    def test_failed_chunk_excluded(self, tmp_path: Path) -> None:
        chunks = [self._chunk(0.0, 10.0), self._chunk(10.0, 20.0)]
        result = _EncodingResult()
        result.failed_chunks = [chunks[1].safe_name()]
        result.encoded_chunks[_STRATEGY] = [self._winner(chunks[0], tmp_path)]
        _assert_one_winner_per_chunk(result, chunks, [_STRATEGY_OBJ])
