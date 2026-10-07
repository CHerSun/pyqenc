"""Unit tests for encoding phase recovery.

Covers the fast presence-based recovery in ``_recover_encoding_attempts``:
- A pair is COMPLETE when both a winning .mkv and its .yaml sidecar exist in
  ``encoded/<strategy>/``.
- A pair is ABSENT when neither is present.
- ``winning_file`` is populated on COMPLETE pairs.
- The index is built from a single ``iterdir()`` per strategy — no per-pair globs.
"""

from decimal import Decimal
from pathlib import Path

from pyqenc.app_config import load_app_config
from pyqenc.phases.encoding import _recover_encoding_attempts
from pyqenc.state import ArtifactState
from pyqenc.stream_model import EncodedChunk as _EC
from pyqenc.utils.yaml_utils import write_yaml_atomic

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_STRATEGY_OBJ = next(
    s for s in load_app_config(default_only=True).resolve_encoding().strategies
    if s.preset == "slow" and s.profile == "h265-aq"
)
_CHUNK_ID   = "00꞉00꞉00․000-00꞉01꞉30․000"
_STRATEGY   = _STRATEGY_OBJ.display_name()
_SAFE_STRAT = _STRATEGY_OBJ.safe_name()
_RESOLUTION = "1920x800"
_CRF        = Decimal("18.0")


def _make_complete_pair(encoded_dir: Path, chunk_id: str = _CHUNK_ID, crf: Decimal = _CRF) -> Path:
    """Write a winning .mkv and its result sidecar into encoded_dir.

    Layout mirrors the real encoded/ directory:
      <chunk_id>.<res>.q<N>.mkv   — winning attempt
      <chunk_id>.<res>.yaml        — result sidecar (no quality in name)
    """
    encoded_dir.mkdir(parents=True, exist_ok=True)
    mkv     = encoded_dir / _EC.format_winner_file_name(chunk_id)
    sidecar = encoded_dir / _EC.format_winner_sidecar_name(chunk_id)
    mkv.write_bytes(b"\x00" * 512)
    write_yaml_atomic(sidecar, {"crf": str(crf), "targets_met": True, "metrics": {"vmaf_min": 94.5}})
    return mkv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecoverEncodingAttempts:

    def test_complete_pair_detected(self, tmp_path: Path) -> None:
        """A pair with mkv + yaml in encoded/ is classified COMPLETE."""
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        winning     = _make_complete_pair(encoded_dir)

        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY_OBJ],
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state        == ArtifactState.COMPLETE
        assert pair.winning_file == winning
        assert recovery.pending  == []

    def test_absent_pair_when_no_encoded_dir(self, tmp_path: Path) -> None:
        """A pair with no encoded/ directory is ABSENT."""
        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY_OBJ],
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ABSENT
        assert (_CHUNK_ID, _STRATEGY) in recovery.pending

    def test_absent_pair_when_mkv_missing_sidecar(self, tmp_path: Path) -> None:
        """A .mkv without a .yaml sidecar is not COMPLETE — pair is ABSENT."""
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        encoded_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{_CHUNK_ID}.{_RESOLUTION}.q{_CRF:4.1f}"
        (encoded_dir / f"{stem}.mkv").write_bytes(b"\x00" * 512)
        # no .yaml written

        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY_OBJ],
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

        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [chunk_a, chunk_b],
            strategies = [_STRATEGY_OBJ],
        )

        assert recovery.pairs[(chunk_a, _STRATEGY)].state == ArtifactState.COMPLETE
        assert recovery.pairs[(chunk_b, _STRATEGY)].state == ArtifactState.ABSENT
        assert (chunk_b, _STRATEGY) in recovery.pending
        assert (chunk_a, _STRATEGY) not in recovery.pending

    def test_winning_file_path_is_correct(self, tmp_path: Path) -> None:
        """winning_file points to the actual .mkv, not a placeholder."""
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        winning     = _make_complete_pair(encoded_dir)

        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY_OBJ],
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
from pyqenc.phases.encoding import ChunkEncodingResult
from pyqenc.quality import MetricType as _MetricType
from pyqenc.quality import QualityEvaluation as _QE
from pyqenc.quality import QualityLogs as _QL
from pyqenc.stream_model import VideoStreamChunk as _VSC
from pyqenc.utils.ffmpeg_runner import FFmpegRunResult as _FFR
from pyqenc.utils.visualization import QualityEvaluator as _QEvaluator


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
) -> tuple[ChunkEncodingResult, _MM]:
    """encode_chunk over a fixed single-point domain with a fake encoder.

    ffmpeg encode, resolution probe, and existing-encoding check are patched;
    the evaluator is a mock so the measurement call itself is observable.
    Returns (ChunkEncodingResult, evaluator_mock).
    """
    strategy = _fixed_strategy()
    chunk    = _chunk_mock(tmp_path)

    fake_eval = _MM(spec=_QE)
    fake_eval.targets_met = True
    fake_eval.logs        = _QL()
    fake_eval.metrics     = {_MetricType.VMAF: {"min": 95.0, "median": 96.5}}

    evaluator = _MM(spec=_QEvaluator)
    evaluator.work_dir = tmp_path
    evaluator.evaluate_chunk.return_value = fake_eval

    if measure_attempts is None:
        encoder = _ChunkEncoder(
            quality_evaluator = evaluator,
            work_dir          = tmp_path,
            collector         = _MM(spec=_MetricsCollector),
            metrics_sampling  = 10,
        )
    else:
        encoder = _ChunkEncoder(
            quality_evaluator = evaluator,
            work_dir          = tmp_path,
            collector         = _MM(spec=_MetricsCollector),
            metrics_sampling  = 10,
            measure_attempts  = measure_attempts,
        )

    attempt = tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.mkv"
    attempt.parent.mkdir(parents=True, exist_ok=True)
    attempt.write_bytes(b"fake mkv")

    run_result = _FFR(returncode=0, success=True, stderr_lines=[], frame_count=120)
    with (
        _patch.object(encoder, "_check_existing_encoding", return_value=None),
        _patch.object(encoder, "_encode_with_ffmpeg", return_value=run_result),
        _patch("pyqenc.phases.encoding._probe_resolution", return_value="1920x1080"),
    ):
        result = encoder.encode_chunk(
            chunk           = chunk,
            strategy        = strategy,
            quality_targets = [],
            initial_crf     = _D("18.0"),
            force           = False,
        )
    return result, evaluator


class TestMeasureAttemptsSeam:
    """The measure_attempts control on the shared chunk-encoding machinery."""

    def test_default_measures_attempts(self, tmp_path: Path) -> None:
        """Without the kwarg the encoder measures (today's behavior)."""
        result, evaluator = _run_encode(tmp_path, measure_attempts=None)
        assert result.success is True
        evaluator.evaluate_chunk.assert_called_once()

    def test_off_path_skips_evaluation_and_accepts_single_point(self, tmp_path: Path) -> None:
        """measure_attempts=False: no evaluation, empty sidecar metrics, one
        accepted attempt at the pinned value.

        Documents the seam (TODO §83 owns default-flipping and the
        metrics-absence tolerance): the attempt sidecar metric-keys
        requirement is lifted — the sidecar records the attempt with empty
        metrics, and the winner is promoted unconditionally.
        """
        result, evaluator = _run_encode(tmp_path, measure_attempts=False)
        evaluator.evaluate_chunk.assert_not_called()
        assert result.success is True
        assert result.targets_met is True
        assert result.final_crf == _D("18.0")
        assert result.attempts == 1

        sidecar_path = (
            tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.yaml"
        )
        sidecar = _yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["metrics"] == {}
        assert sidecar["frame_count"] == 120

        winner_sidecar = (
            tmp_path / "encoded" / "test_strategy" / "chunk_001.yaml"
        )
        assert winner_sidecar.exists()

    def test_on_path_measures_and_persists_all_metrics(self, tmp_path: Path) -> None:
        """measure_attempts=True: evaluation runs and all metrics persist."""
        result, evaluator = _run_encode(tmp_path, measure_attempts=True)
        evaluator.evaluate_chunk.assert_called_once()
        assert result.success is True
        sidecar_path = (
            tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.yaml"
        )
        sidecar = _yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["metrics"]["vmaf_min"] == 95.0

    def test_seam_threaded_explicitly_at_both_call_sites(self) -> None:
        """Optimization always measures; the encoding phase passes its value."""
        import inspect

        from pyqenc.phases.encoding import EncodingPhase
        from pyqenc.phases.optimization import _make_encoder

        assert "measure_attempts  = True" in inspect.getsource(_make_encoder)
        assert "measure_attempts = True" in inspect.getsource(EncodingPhase._execute)


# ---------------------------------------------------------------------------
# Fixed-mode encoding: degenerate single-point path + presentation ruler
# (fixed-quality spec Req 9.1, 9.4, 9.5, 9.7)
# ---------------------------------------------------------------------------

from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import CleanupLevel, CropParams, PhaseOutcome, QualityTarget
from pyqenc.phase import Artifact as _PAArtifact
from pyqenc.phase import PhaseRegistry
from pyqenc.phases.chunking import ChunkingPhase as _CCP
from pyqenc.phases.chunking import ChunkingPhaseResult
from pyqenc.phases.encoding import (
    EncodingPhase,
    EncodingResult,
    build_encoded_chunk,
)
from pyqenc.phases.job import JobPhase
from pyqenc.phases.optimization import OptimizationPhase, OptimizationPhaseResult
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.state import ArtifactState as _PAState
from pyqenc.stream_model import ExtendedVideoStream


class TestFixedDegenerateSinglePoint:
    """Exactly one accepted attempt per pair via the existing degenerate path (Req 9.1)."""

    def test_single_attempt_accepted_with_measurement(self, tmp_path: Path) -> None:
        """Uncompared ruler (empty targets): one measured attempt, vacuous
        pass, winner promoted at the pinned value."""
        result, evaluator = _run_encode_with_targets(
            tmp_path, targets=[], measure_attempts=True,
        )
        evaluator.evaluate_chunk.assert_called_once()
        assert result.success is True
        assert result.targets_met is True
        assert result.attempts == 1
        assert result.final_crf == _D("18.0")

    def test_single_attempt_accepted_when_ruler_misses(self, tmp_path: Path) -> None:
        """Compared-run ruler miss: the single-point domain cannot produce a
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
        assert result.final_crf == _D("18.0")
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

    evaluator = _MM(spec=_QEvaluator)
    evaluator.work_dir = tmp_path
    evaluator.evaluate_chunk.return_value = fake_eval

    encoder = _ChunkEncoder(
        quality_evaluator = evaluator,
        work_dir          = tmp_path,
        collector         = _MM(spec=_MetricsCollector),
        metrics_sampling  = 10,
        measure_attempts  = measure_attempts,
    )

    attempt = tmp_path / "encoding" / "test_strategy" / "chunk_001.q18.0.mkv"
    attempt.parent.mkdir(parents=True, exist_ok=True)
    attempt.write_bytes(b"fake mkv")

    run_result = _FFR(returncode=0, success=True, stderr_lines=[], frame_count=120)
    with (
        _patch.object(encoder, "_check_existing_encoding", return_value=None),
        _patch.object(encoder, "_encode_with_ffmpeg", return_value=run_result),
        _patch("pyqenc.phases.encoding._probe_resolution", return_value="1920x1080"),
    ):
        result = encoder.encode_chunk(
            chunk=chunk, strategy=strategy, quality_targets=targets,
            initial_crf=_D("18.0"), force=False,
        )
    return result, evaluator


class TestEncodingPresentationTargets:
    """What the encoding phase judges winner presentation against (Req 9.4, 9.5)."""

    def _run_phase(
        self,
        tmp_path: Path,
        monkeypatch,
        *,
        quality_range_override: tuple[_D, _D] | None,
        synthetic_targets: list[QualityTarget],
    ) -> list[QualityTarget]:
        """Run EncodingPhase through run() with a stubbed encode pool.

        Returns the ``quality_targets`` the pool received.
        """
        from pyqenc.app_config import load_app_config

        config = load_app_config(default_only=True).model_copy(deep=True)
        plan = config.resolve_encoding(
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
                file=_File(path=src, file_size_bytes=64), info=info,
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
            synthetic_targets=synthetic_targets,
        )

        registry: PhaseRegistry = {
            JobPhase: job, ProbePhase: probe,
            _CCP: chunking, OptimizationPhase: optimization,
        }
        phase = EncodingPhase(config, registry, collector=NoOpMetricsCollector())

        # The encoded winner the stubbed pool reports back.
        winner_dir = work_dir / "encoded" / strategy.safe_name()
        winner_dir.mkdir(parents=True, exist_ok=True)
        winner_path = winner_dir / _EC.format_winner_file_name(chunk.safe_name())
        winner_path.write_bytes(b"x" * 32)
        winner = build_encoded_chunk(
            chunk=chunk, strategy=strategy,
            path=winner_path, resolution="1920x1080", frame_count=240,
        )

        captured: dict[str, object] = {}

        def _fake_encode_all(**kwargs: object) -> EncodingResult:
            captured.update(kwargs)
            result = EncodingResult()
            result.encoded_chunks = {strategy.display_name(): [winner]}
            result.encoded_count = 1
            return result

        monkeypatch.setattr(
            "pyqenc.phases.encoding.encode_all_chunks", _fake_encode_all,
        )
        outcome = phase.run(dry_run=False)
        assert outcome.outcome is PhaseOutcome.COMPLETED
        received = _cast(list[QualityTarget], captured["quality_targets"])
        return received

    def test_fixed_compared_uses_synthetic_set(self, tmp_path: Path, monkeypatch) -> None:
        synthetic = [
            QualityTarget(metric="vmaf", statistic="median", value=91.0),
            QualityTarget(metric="vif", statistic="median", value=84.0),
        ]
        received = self._run_phase(
            tmp_path, monkeypatch,
            quality_range_override=(_D("18"), _D("18")),
            synthetic_targets=synthetic,
        )
        assert received == synthetic

    def test_fixed_uncompared_uses_no_ruler(self, tmp_path: Path, monkeypatch) -> None:
        received = self._run_phase(
            tmp_path, monkeypatch,
            quality_range_override=(_D("18"), _D("18")),
            synthetic_targets=[],
        )
        assert received == []

    def test_searched_uses_config_targets_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        received = self._run_phase(
            tmp_path, monkeypatch,
            quality_range_override=None,
            synthetic_targets=[],
        )
        # The config targets flow through verbatim — searched behavior is
        # byte-identical to before the spec.
        from pyqenc.models import QualityTarget as _QT
        assert received == [
            _QT(metric="vmaf", statistic="min", value=93.0),
        ]


# ---------------------------------------------------------------------------
# The chunk->winner 1:1 span contract guard
# ---------------------------------------------------------------------------

import pytest

from pyqenc.phases.encoding import (
    EncodingResult as _EncodingResult,
)
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
                file=File(path=Path(f"c_{start}_{end}.mkv"), file_size_bytes=64),
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
        from pyqenc.phases.encoding import build_encoded_chunk

        mkv = tmp_path / _EC.format_winner_file_name(chunk.safe_name())
        mkv.write_bytes(b"x" * 64)
        return build_encoded_chunk(
            chunk=chunk, strategy=_STRATEGY_OBJ,
            path=mkv, resolution="1920x1080", frame_count=24,
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
