"""Unit tests for winner-sidecar frame accounting (TODO §69).

Covers the persistence of ``frame_count`` in both encoding sidecars, the
end-of-run winner-sidecar scan that feeds the frame-preservation invariant
(per-strategy sums + skip-if-any-unknown semantics), the ``encoding.yaml``
aggregate round-trip, and the fast-exit re-assertion.
"""

from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from pyqenc.app_config import load_app_config
from pyqenc.models import CropParams, PhaseOutcome, QualityTarget
from pyqenc.phase import Artifact, ArtifactState, PhaseRegistry
from pyqenc.phases.encoding import (
    EncodingPhase,
    _scan_winner_sidecars,
    _write_encoding_result_sidecar,
    _write_metrics_sidecar,
    build_encoded_chunk,
)
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.state import EncodingParams
from pyqenc.stream_model import (
    EncodedChunk,
    ExtendedVideoStream,
    File,
    VideoStream,
    VideoStreamChunk,
    VideoStreamInfo,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_APP_CONFIG = load_app_config(default_only=True)
_ALL_STRATS = _APP_CONFIG.resolve_encoding().strategies

_STRATEGY = next(
    s for s in _ALL_STRATS
    if s.preset == "slow" and s.profile == "h265-aq"
)
_STRATEGY_B = next(
    s for s in _ALL_STRATS
    if s.preset == "slow" and s.profile == "h265"
)
_RESOLUTION = "1920x1080"
_TARGETS = [QualityTarget(metric="vmaf", statistic="min", value=93.0)]


def _make_chunk(cid_start: float, cid_end: float, tmp_path: Path) -> VideoStreamChunk:
    """A real VideoStreamChunk over a minimal extended stream."""
    return VideoStreamChunk(
        stream = ExtendedVideoStream(
            stream = VideoStream(
                file = File(path=tmp_path / "source.mkv", file_size_bytes=64),
                info = VideoStreamInfo(
                    track_id=0, codec_name="hevc", fps=24.0,
                    fps_fraction=Fraction(24, 1), resolution=_RESOLUTION,
                    duration_seconds=100.0,
                ),
            ),
            frame_count=2400,
            crop=CropParams(),
        ),
        start_timestamp=cid_start,
        end_timestamp=cid_end,
        frame_count=24,
    )


def _make_winner(
    work_dir: Path,
    chunk:    VideoStreamChunk,
    strategy,
    crf:      Decimal,
    frames:   int,
) -> EncodedChunk:
    """Materialize a winner on disk (mkv + result sidecar) and compose it.

    The composed payload mirrors the recovery composition (frame_count=0
    sentinel) — the scan reads counts from the sidecar, never the payload.
    ``frames=0`` persists the unknown sentinel.
    """
    strategy_dir = work_dir / "encoded" / strategy.safe_name()
    strategy_dir.mkdir(parents=True, exist_ok=True)
    name = EncodedChunk.format_file_name(chunk.safe_name(), _RESOLUTION, crf)
    winner = strategy_dir / name
    winner.write_bytes(b"x" * 16)
    sidecar: dict = {
        "winning_attempt": name,
        "crf": str(crf),
        "metrics": {"vmaf_min": 94.5},
        "targets_met": True,
        "frame_count": frames,
    }
    (strategy_dir / f"{chunk.safe_name()}.{_RESOLUTION}.yaml").write_text(
        yaml.safe_dump(sidecar), encoding="utf-8",
    )
    return build_encoded_chunk(
        chunk=chunk, strategy=strategy, crf=crf,
        path=winner, resolution=_RESOLUTION, frame_count=0,
    )


# ---------------------------------------------------------------------------
# Sidecar persistence
# ---------------------------------------------------------------------------

class TestSidecarFrameCount:

    def test_metrics_sidecar_persists_frame_count(self, tmp_path: Path) -> None:
        """The per-attempt sidecar carries the attempt's frame count."""
        attempt = tmp_path / "chunk.1920x1080.q18.0.mkv"
        _write_metrics_sidecar(
            attempt, Decimal("18.0"), {"vmaf_min": 94.5}, 3, 2400,
        )
        data = yaml.safe_load(attempt.with_suffix(".yaml").read_text(encoding="utf-8"))
        assert data["frame_count"] == 2400

    def test_metrics_sidecar_writes_zero_for_unknown_frame_count(self, tmp_path: Path) -> None:
        """An unknown count is persisted as the 0 sentinel (no None in yaml)."""
        attempt = tmp_path / "chunk.1920x1080.q18.0.mkv"
        _write_metrics_sidecar(
            attempt, Decimal("18.0"), {"vmaf_min": 94.5}, 3, 0,
        )
        data = yaml.safe_load(attempt.with_suffix(".yaml").read_text(encoding="utf-8"))
        assert data["frame_count"] == 0

    def test_result_sidecar_persists_frame_count(self, tmp_path: Path) -> None:
        """The winner result sidecar carries the winning attempt's count."""
        winner = tmp_path / "chunk.1920x1080.q18.0.mkv"
        winner.write_bytes(b"x" * 8)
        _write_encoding_result_sidecar(
            tmp_path, "chunk", _RESOLUTION, winner,
            Decimal("18.0"), {"vmaf_min": 94.5}, 2400,
        )
        data = yaml.safe_load(
            (tmp_path / f"chunk.{_RESOLUTION}.yaml").read_text(encoding="utf-8"),
        )
        assert data["frame_count"] == 2400


# ---------------------------------------------------------------------------
# The winner-sidecar scan
# ---------------------------------------------------------------------------

class TestScanWinnerSidecars:

    def test_per_strategy_sums(self, tmp_path: Path) -> None:
        """Two strategies over the same chunks: each sums its own winners."""
        chunks = [_make_chunk(0.0, 10.0, tmp_path), _make_chunk(10.0, 20.0, tmp_path)]
        encoded = {
            _STRATEGY.display_name(): [
                _make_winner(tmp_path, c, _STRATEGY, Decimal("18.0"), 100 + i)
                for i, c in enumerate(chunks)
            ],
            _STRATEGY_B.display_name(): [
                _make_winner(tmp_path, c, _STRATEGY_B, Decimal("20.0"), 200 + i)
                for i, c in enumerate(chunks)
            ],
        }

        scan = _scan_winner_sidecars(
            tmp_path, encoded,
            [_STRATEGY.display_name(), _STRATEGY_B.display_name()],
            _TARGETS,
        )

        assert scan.frames_known is True
        assert scan.frame_totals() == {
            _STRATEGY.display_name(): 201,   # 100 + 101
            _STRATEGY_B.display_name(): 401, # 200 + 201
        }

    def test_zero_count_marks_unknown(self, tmp_path: Path) -> None:
        """A winner sidecar with the 0 sentinel → frames_known=False (skip semantics)."""
        chunk = _make_chunk(0.0, 10.0, tmp_path)
        encoded = {_STRATEGY.display_name(): [_make_winner(
            tmp_path, chunk, _STRATEGY, Decimal("18.0"), 0,
        )]}

        scan = _scan_winner_sidecars(tmp_path, encoded, [_STRATEGY.display_name()], _TARGETS)

        assert scan.frames_known is False
        assert scan.frames == {}

    def test_missing_count_key_marks_unknown(self, tmp_path: Path) -> None:
        """A sidecar predating the field (no key) → frames_known=False."""
        chunk = _make_chunk(0.0, 10.0, tmp_path)
        winner = _make_winner(tmp_path, chunk, _STRATEGY, Decimal("18.0"), 240)
        sidecar_path = (
            tmp_path / "encoded" / _STRATEGY.safe_name()
            / f"{chunk.safe_name()}.{_RESOLUTION}.yaml"
        )
        data = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        del data["frame_count"]
        sidecar_path.write_text(yaml.safe_dump(data), encoding="utf-8")
        encoded = {_STRATEGY.display_name(): [winner]}

        scan = _scan_winner_sidecars(tmp_path, encoded, [_STRATEGY.display_name()], _TARGETS)

        assert scan.frames_known is False
        assert scan.frames == {}

    def test_missing_sidecar_marks_unknown(self, tmp_path: Path) -> None:
        """A winner without its result sidecar → frames_known=False."""
        chunk = _make_chunk(0.0, 10.0, tmp_path)
        winner = _make_winner(tmp_path, chunk, _STRATEGY, Decimal("18.0"), 100)
        # Remove the sidecar the helper wrote.
        (tmp_path / "encoded" / _STRATEGY.safe_name()
         / f"{chunk.safe_name()}.{_RESOLUTION}.yaml").unlink()
        encoded = {_STRATEGY.display_name(): [winner]}

        scan = _scan_winner_sidecars(tmp_path, encoded, [_STRATEGY.display_name()], _TARGETS)

        assert scan.frames_known is False

    def test_frame_accounting_runs_without_targets(self, tmp_path: Path) -> None:
        """Frames are target-independent: no targets → summaries None, sums live."""
        chunk = _make_chunk(0.0, 10.0, tmp_path)
        encoded = {_STRATEGY.display_name(): [_make_winner(
            tmp_path, chunk, _STRATEGY, Decimal("18.0"), 240,
        )]}

        scan = _scan_winner_sidecars(tmp_path, encoded, [_STRATEGY.display_name()], [])

        assert scan.summaries is None
        assert scan.frames_known is True
        assert scan.frame_totals() == {_STRATEGY.display_name(): 240}

    def test_limiter_summaries_still_built(self, tmp_path: Path) -> None:
        """The limiter table path is unchanged by the frame accounting."""
        chunk = _make_chunk(0.0, 10.0, tmp_path)
        encoded = {_STRATEGY.display_name(): [_make_winner(
            tmp_path, chunk, _STRATEGY, Decimal("18.0"), 240,
        )]}

        scan = _scan_winner_sidecars(tmp_path, encoded, [_STRATEGY.display_name()], _TARGETS)

        assert scan.summaries is not None
        [group] = scan.summaries
        assert group.strategy == _STRATEGY.display_name()
        assert group.chunks == 1
        assert group.rows[0].limiter == "vmaf_min"
        assert group.rows[0].passed == 1
        # Passing rows record the worst-target surplus (94.5 vs target 93.0).
        assert group.rows[0].med_surplus == 1.5

    def test_limiter_table_renders_surplus_and_percentages(
        self, tmp_path: Path, caplog,
    ) -> None:
        """The limiter table shows the pass surplus beside the count (mirroring
        the miss deficit) and a per-strategy Passed/missed percentage row."""
        import logging as _logging
        from decimal import Decimal as _Decimal

        from pyqenc.metrics import NoOpMetricsCollector
        from pyqenc.phases.encoding import EncodingPhase
        from pyqenc.state import LimiterSummary, LimiterSummaryRow

        phase = EncodingPhase(
            load_app_config(default_only=True), {}, collector=NoOpMetricsCollector(),
        )
        summaries = [LimiterSummary(
            strategy = "h265-aq+slow",
            chunks   = 107,
            rows     = [
                LimiterSummaryRow(
                    limiter="ssim_median", passed=97, missed=2,
                    med_deficit=-0.1, med_surplus=1.4, med_crf=_Decimal("18.0"),
                ),
                LimiterSummaryRow(
                    limiter="ssim_p10", passed=3, missed=5,
                    med_deficit=-0.2, med_surplus=0.9, med_crf=_Decimal("18.0"),
                ),
            ],
        )]
        with caplog.at_level(_logging.INFO, logger="pyqenc.phases.encoding"):
            phase._log_limiter_summary(summaries)

        text = caplog.text
        assert "97 (+1.4)" in text          # pass count with median surplus
        assert "2 (-0.1)" in text           # miss count with median deficit
        assert "Passed 93.5% · missed 6.5%" in text

    def test_empty_winners_is_known_empty(self, tmp_path: Path) -> None:
        """No winners at all → nothing pending, nothing unknown."""
        scan = _scan_winner_sidecars(tmp_path, {}, [_STRATEGY.display_name()], _TARGETS)
        assert scan.summaries is None
        assert scan.frames_known is True
        assert scan.frame_totals() == {}


# ---------------------------------------------------------------------------
# encoding.yaml aggregate round-trip
# ---------------------------------------------------------------------------

class TestEncodingParamsTotals:

    def test_round_trip(self, tmp_path: Path) -> None:
        """winners_frame_totals persists into encoding.yaml and loads back."""
        path = tmp_path / "encoding.yaml"
        EncodingParams(
            winners_frame_totals={_STRATEGY.display_name(): 9526},
        ).save(path)

        loaded = EncodingParams.load(path)

        assert loaded is not None
        assert loaded.winners_frame_totals == {_STRATEGY.display_name(): 9526}

    def test_absent_totals_load_as_empty(self, tmp_path: Path) -> None:
        """Files written before the field load as {} (unknown — no mismatch)."""
        path = tmp_path / "encoding.yaml"
        path.write_text("probe:\n  frame_count: 2400\n", encoding="utf-8")

        loaded = EncodingParams.load(path)

        assert loaded is not None
        assert loaded.winners_frame_totals == {}


# ---------------------------------------------------------------------------
# Fast-exit re-assertion
# ---------------------------------------------------------------------------

def _make_encoding_phase(tmp_path: Path, probe_frames: int) -> EncodingPhase:
    """An EncodingPhase wired to a completed ProbePhase with a known frame count."""
    probe = ProbePhase(
        _APP_CONFIG, {}, collector=MagicMock(), crop_params=None,
        plan=_APP_CONFIG.resolve_encoding(),
    )
    probe.result = ProbePhaseResult(
        outcome = PhaseOutcome.COMPLETED,
        message = "probe complete",
        plan    = _APP_CONFIG.resolve_encoding(),
        stream  = Artifact(
            payload = ExtendedVideoStream(
                stream = VideoStream(
                    file = File(path=tmp_path / "source.mkv", file_size_bytes=64),
                    info = VideoStreamInfo(
                        track_id=0, codec_name="hevc", fps=24.0,
                        fps_fraction=Fraction(24, 1), resolution=_RESOLUTION,
                    ),
                ),
                frame_count = probe_frames,
                crop        = CropParams(),
            ),
            state = ArtifactState.COMPLETE,
        ),
    )
    phases: PhaseRegistry = {ProbePhase: probe}
    return EncodingPhase(_APP_CONFIG, phases, collector=MagicMock())


class TestReassertFramePreservation:

    def test_matching_totals_stay_silent(self, tmp_path: Path, caplog) -> None:
        import logging

        phase = _make_encoding_phase(tmp_path, probe_frames=9526)
        with caplog.at_level(logging.WARNING):
            phase._reassert_frame_preservation(EncodingParams(
                winners_frame_totals={_STRATEGY.display_name(): 9526},
            ))
        assert not [r for r in caplog.records if "disagree" in r.message]

    def test_mismatching_totals_warn(self, tmp_path: Path, caplog) -> None:
        import logging

        phase = _make_encoding_phase(tmp_path, probe_frames=9526)
        with caplog.at_level(logging.WARNING):
            phase._reassert_frame_preservation(EncodingParams(
                winners_frame_totals={_STRATEGY.display_name(): 9000},
            ))
        assert any(
            "disagree" in r.message and "source=9526" in r.message
            for r in caplog.records
        )

    def test_unknown_totals_stay_silent(self, tmp_path: Path, caplog) -> None:
        import logging

        phase = _make_encoding_phase(tmp_path, probe_frames=9526)
        with caplog.at_level(logging.WARNING):
            phase._reassert_frame_preservation(EncodingParams(winners_frame_totals={}))
            phase._reassert_frame_preservation(None)
        assert not [r for r in caplog.records if "disagree" in r.message]
