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
from pyqenc.utils.yaml_utils import write_yaml_atomic

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_STRATEGY_OBJ = next(
    s for s in load_app_config(default_only=True).encoding.resolved_strategies
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
    mkv     = encoded_dir / f"{chunk_id}.{_RESOLUTION}.q{crf}.mkv"
    sidecar = encoded_dir / f"{chunk_id}.{_RESOLUTION}.yaml"
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

    attempt = tmp_path / "encoding" / "test_strategy" / "chunk_001.1920x1080.q18.0.mkv"
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
            tmp_path / "encoding" / "test_strategy" / "chunk_001.1920x1080.q18.0.yaml"
        )
        sidecar = _yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["metrics"] == {}
        assert sidecar["frame_count"] == 120

        winner_sidecar = (
            tmp_path / "encoded" / "test_strategy" / "chunk_001.1920x1080.yaml"
        )
        assert winner_sidecar.exists()

    def test_on_path_measures_and_persists_all_metrics(self, tmp_path: Path) -> None:
        """measure_attempts=True: evaluation runs and all metrics persist."""
        result, evaluator = _run_encode(tmp_path, measure_attempts=True)
        evaluator.evaluate_chunk.assert_called_once()
        assert result.success is True
        sidecar_path = (
            tmp_path / "encoding" / "test_strategy" / "chunk_001.1920x1080.q18.0.yaml"
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
