"""Differential tests for the rehomed per-metric parsing (metric-stat-identity
spec, Req 10.5 / 12.2).

The rehoming (each metric's ``MetricInfo.parse_log`` + the measurer's shared
statistics step) must produce the same measured statistics as the wave-A
pipeline it replaced. Expected values below were produced by running the
pre-rehoming pipeline (commit c5f97c3) on these exact fixtures; the edges
named by the spec are covered: the PSNR-inf frame, single-frame chunks, and
the sampling factor. Tests mock at the process boundary (``run_metrics``)
and measure through the public ``QualityMeasurer.measure_chunk``.
"""
# CHerSun 2026

import json
import math
import os as _os
import tempfile as _tempfile
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pyqenc.models import CropParams
from pyqenc.quality_metrics import (
    MeasuredMetrics,
    MetricStatKey,
    MetricType,
    StatisticType,
)
from pyqenc.utils.ffmpeg_runner import FFmpegInput, FFmpegRunResult
from pyqenc.utils.visualization import QualityMeasurer

# ---------------------------------------------------------------------------
# Fixtures: synthetic measurement outputs (ffmpeg-shaped)
# ---------------------------------------------------------------------------

_PSNR_FULL = [38.5, 41.2, 55.0, 47.8, 43.1, 52.4, 39.9]
_SSIM_FULL = [0.921, 0.945, 0.978, 0.960, 0.933, 0.971, 0.928]
_VMAF_FULL = [88.2, 91.5, 96.4, 93.7, 90.1, 95.2, 89.4]
_VIF_FULL  = [0.81, 0.85, 0.93, 0.89, 0.83, 0.91, 0.82]

# Wave-A pipeline outputs on _PSNR_FULL/_SSIM_FULL/_VMAF_FULL/_VIF_FULL
# (identical at every sampling factor — statistics are factor-invariant).
_EXPECTED_FULL: dict[MetricType, dict[StatisticType, float]] = {
    MetricType.PSNR: {
        StatisticType.MIN: 38.5, StatisticType.P05: 38.92,
        StatisticType.P10: 39.339999999999996, StatisticType.P25: 40.55,
        StatisticType.MEDIAN: 43.1, StatisticType.P75: 50.099999999999994,
        StatisticType.P90: 53.44, StatisticType.P95: 54.22,
        StatisticType.MAX: 55.0, StatisticType.STD: 6.4253219300484785,
    },
    MetricType.SSIM: {
        StatisticType.MIN: 92.10000000000001, StatisticType.P05: 92.31000000000002,
        StatisticType.P10: 92.52000000000001, StatisticType.P25: 93.05000000000001,
        StatisticType.MEDIAN: 94.5, StatisticType.P75: 96.55,
        StatisticType.P90: 97.38, StatisticType.P95: 97.58999999999999,
        StatisticType.MAX: 97.8, StatisticType.STD: 2.2120880030716012,
    },
    MetricType.VMAF: {
        StatisticType.MIN: 88.2, StatisticType.P05: 88.56,
        StatisticType.P10: 88.92, StatisticType.P25: 89.75,
        StatisticType.MEDIAN: 91.5, StatisticType.P75: 94.45,
        StatisticType.P90: 95.68, StatisticType.P95: 96.04,
        StatisticType.MAX: 96.4, StatisticType.STD: 3.0960804714532686,
    },
    MetricType.VIF: {
        StatisticType.MIN: 81.0, StatisticType.P05: 81.3,
        StatisticType.P10: 81.6, StatisticType.P25: 82.5,
        StatisticType.MEDIAN: 85.0, StatisticType.P75: 90.0,
        StatisticType.P90: 91.8, StatisticType.P95: 92.4,
        StatisticType.MAX: 93.0, StatisticType.STD: 4.715728494951256,
    },
}

# Wave-A pipeline output on the PSNR-inf fixture (an infinite frame among
# finite ones): normalize clips ∞ at the PSNR ceiling — max 100, quantiles
# spanning the clip, std over the cutoff-filtered values.
_EXPECTED_PSNR_INF: dict[StatisticType, float] = {
    StatisticType.MIN: 40.0, StatisticType.P05: 40.15,
    StatisticType.P10: 40.3, StatisticType.P25: 40.75,
    StatisticType.MEDIAN: 41.75, StatisticType.P75: 56.875,
    StatisticType.P90: 82.75000000000001, StatisticType.P95: 91.37499999999997,
    StatisticType.MAX: 100.0, StatisticType.STD: 29.434602652886845,
}


def _psnr_log(values: list[float]) -> str:
    """A synthetic ffmpeg ``psnr`` stats file (one line per frame)."""
    return "\n".join(
        f"n:{i + 1} mse_avg:0.01 psnr_avg:{v:.4f} psnr_y:40.0"
        for i, v in enumerate(values)
    )


def _ssim_log(values: list[float]) -> str:
    """A synthetic ffmpeg ``ssim`` stats file (the ``All`` column)."""
    return "\n".join(
        f"n:{i + 1} Y:0.99 U:0.99 V:0.99 All:{v:.6f} (0.99)"
        for i, v in enumerate(values)
    )


def _vmaf_json(vmaf: list[float], vif: list[float]) -> str:
    """A synthetic libvmaf JSON report with the embedded VIF scales."""
    frames = [
        {
            "frameNum": i,
            "metrics": {
                "vmaf": v,
                "integer_vif_scale0": f, "integer_vif_scale1": f,
                "integer_vif_scale2": f, "integer_vif_scale3": f,
            },
        }
        for i, (v, f) in enumerate(zip(vmaf, vif))
    ]
    return json.dumps({"frames": frames})


def _measure(
    tmp_path:   Path,
    log_files:  dict[str, str],
    sampling:   int = 1,
) -> MeasuredMetrics:
    """Measure through the public measurer with ffmpeg mocked out.

    The fake ``run_metrics`` (the process boundary) writes the synthetic
    measurement outputs where the real run would; everything downstream —
    the per-metric parse, the shared statistics step, the measured set —
    is the real pipeline.

    Args:
        tmp_path:  Scratch directory (outputs + plot land here).
        log_files: Metric file stem → file content (``psnr``/``ssim``/
                   ``vmaf``; VIF shares the VMAF report).
        sampling:  The frame subsampling factor for the run.

    Returns:
        The measurement's full measured set.
    """
    measurer = QualityMeasurer(work_dir=tmp_path)

    async def _fake_run_metrics(
        metrics: object, **kwargs: object,
    ) -> FFmpegRunResult:
        ext        = str(kwargs.get("output_extension") or ".tmp")
        output_dir = Path(str(kwargs.get("cwd")))
        prefix     = str(kwargs.get("output_prefix", ""))
        for stem, content in log_files.items():
            (output_dir / f"{prefix}{stem}{ext}").write_text(content, encoding="utf-8")
        return FFmpegRunResult(returncode=0, success=True, stderr_lines=[], frame_count=0)

    with patch("pyqenc.utils.visualization.run_metrics", side_effect=_fake_run_metrics):
        measurement = measurer.measure_chunk(
            encoded          = tmp_path / "encoded.mkv",
            reference        = FFmpegInput(path=tmp_path / "reference.mkv"),
            ref_crop         = CropParams(),
            output_dir       = tmp_path,
            duration_seconds = 1.0,
            fps_value        = Fraction(24, 1),
            sampling         = sampling,
            plot_path        = tmp_path / "plot.png",
        )
    return measurement.metrics


def _assert_stats_equal(
    measured:   MeasuredMetrics,
    expected:   dict[MetricType, dict[StatisticType, float]],
) -> None:
    """Assert the measured set equals the expected statistics, metric by metric."""
    for metric_type, row in expected.items():
        for statistic, want in row.items():
            got = measured[MetricStatKey(metric_type, statistic)]
            assert got == pytest.approx(want), (
                f"{metric_type.value}.{statistic.value}: {got} != {want}"
            )


# ---------------------------------------------------------------------------
# Happy path — differential equivalence with the pre-rehoming pipeline
# ---------------------------------------------------------------------------

class TestDifferentialEquivalence:
    """The rehomed pipeline reproduces the wave-A pipeline's statistics."""

    def test_full_four_metric_fixture_matches_the_old_pipeline(self, tmp_path: Path) -> None:
        """Every statistic of every metric is numerically identical to the
        pre-rehoming pipeline's output on the same measurement outputs."""
        measured = _measure(tmp_path, {
            "psnr": _psnr_log(_PSNR_FULL),
            "ssim": _ssim_log(_SSIM_FULL),
            "vmaf": _vmaf_json(_VMAF_FULL, _VIF_FULL),
        })
        _assert_stats_equal(measured, _EXPECTED_FULL)

    @pytest.mark.parametrize("sampling", [1, 10])
    def test_statistics_are_sampling_factor_invariant(self, tmp_path: Path, sampling: int) -> None:
        """The subsample factor positions frames on the x-axis only — the
        statistics never change with it."""
        measured = _measure(tmp_path, {
            "psnr": _psnr_log(_PSNR_FULL),
            "ssim": _ssim_log(_SSIM_FULL),
            "vmaf": _vmaf_json(_VMAF_FULL, _VIF_FULL),
        }, sampling=sampling)
        _assert_stats_equal(measured, _EXPECTED_FULL)

    def test_psnr_inf_frame_matches_the_old_pipeline(self, tmp_path: Path) -> None:
        """A lossless (∞ dB) frame clips at the PSNR ceiling exactly as
        before: max 100, quantiles spanning the clip, std over the
        cutoff-filtered values."""
        measured = _measure(tmp_path, {"psnr": _psnr_log([40.0, float("inf"), 42.5, 41.0])})
        _assert_stats_equal(measured, {MetricType.PSNR: _EXPECTED_PSNR_INF})

    def test_single_frame_chunk_matches_the_old_pipeline(self, tmp_path: Path) -> None:
        """A one-frame measurement: every quantile collapses to the frame's
        value and std is undefined (NaN) — as before."""
        measured = _measure(tmp_path, {
            "psnr": _psnr_log([44.4]),
            "ssim": _ssim_log([0.95]),
            "vmaf": _vmaf_json([92.0], [0.88]),
        })
        for metric_type, value in [
            (MetricType.PSNR, 44.4), (MetricType.SSIM, 95.0),
            (MetricType.VMAF, 92.0), (MetricType.VIF, 88.0),
        ]:
            for statistic in StatisticType:
                got = measured[MetricStatKey(metric_type, statistic)]
                if statistic is StatisticType.STD:
                    assert math.isnan(got)
                else:
                    assert got == pytest.approx(value)


# ---------------------------------------------------------------------------
# Properties — the per-metric parse seam
# ---------------------------------------------------------------------------

class TestParseLogProperties:
    """Each metric's ``parse_log`` owns its own output format."""

    _TMP_FD, _TMP_STR = _tempfile.mkstemp(suffix=".log", prefix="test_parse_")
    _os.close(_TMP_FD)

    @classmethod
    def _tmp_log(cls, content: str) -> Path:
        """Overwrite the shared scratch file and return its path."""
        path = Path(cls._TMP_STR)
        path.write_text(content, encoding="utf-8")
        return path

    @given(values=st.lists(st.floats(min_value=1.0, max_value=90.0), min_size=1, max_size=40))
    def test_psnr_parse_preserves_frame_order(self, values: list[float]) -> None:
        """PSNR stats lines parse to one value per frame, in frame order."""
        parsed = MetricType.PSNR.info.parse_log(self._tmp_log(_psnr_log(values)))
        assert len(parsed) == len(values)
        assert parsed == pytest.approx(values, abs=1e-3)  # the log carries 4 decimals

    @given(values=st.lists(st.floats(min_value=0.1, max_value=0.99), min_size=1, max_size=40))
    def test_ssim_parse_takes_the_all_column(self, values: list[float]) -> None:
        """SSIM stats lines parse to the ``All`` column, in frame order."""
        parsed = MetricType.SSIM.info.parse_log(self._tmp_log(_ssim_log(values)))
        assert parsed == pytest.approx(values, abs=1e-5)

    @given(
        vmaf=st.lists(st.floats(min_value=0.0, max_value=100.0), min_size=1, max_size=40),
    )
    def test_vmaf_parse_preserves_frame_order(self, vmaf: list[float]) -> None:
        """The VMAF JSON parses to one score per frame, in frame order."""
        path = self._tmp_log(_vmaf_json(vmaf, [0.5] * len(vmaf))).with_suffix(".json")
        path.write_text(_vmaf_json(vmaf, [0.5] * len(vmaf)), encoding="utf-8")
        assert MetricType.VMAF.info.parse_log(path) == pytest.approx(vmaf)

    @given(
        scales=st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=20),
    )
    def test_vif_parse_combines_the_four_scales(self, scales: list[float]) -> None:
        """The combined VIF is the average of the four per-scale scores."""
        path = Path(_tempfile.mkstemp(suffix=".json", prefix="test_parse_")[1])
        path.write_text(_vmaf_json([90.0] * len(scales), scales), encoding="utf-8")
        assert MetricType.VIF.info.parse_log(path) == pytest.approx(scales)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestParseErrors:
    """Malformed measurement outputs fail loudly but never sink the run."""

    def test_no_valid_metric_file_raises(self, tmp_path: Path) -> None:
        """Nothing parseable at all — the measurement fails loudly."""
        with pytest.raises(ValueError, match="[Nn]o metric files"):
            _measure(tmp_path, {"psnr": "not a psnr log\n"})

    def test_one_valid_metric_survives_others_malformed(self, tmp_path: Path) -> None:
        """A malformed file drops only its own metric — the rest measure."""
        measured = _measure(tmp_path, {
            "psnr": _psnr_log(_PSNR_FULL),
            "ssim": "garbage\n",
            "vmaf": "{ not json",
        })
        assert not [key for key in measured if key.metric is MetricType.SSIM]
        assert not [key for key in measured if key.metric is MetricType.VMAF]
        assert measured[MetricStatKey(MetricType.PSNR, StatisticType.MEDIAN)] == pytest.approx(43.1)

    def test_empty_metric_file_rejects(self, tmp_path: Path) -> None:
        """An empty stats file is not a valid log of its metric."""
        empty = tmp_path / "empty.log"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(ValueError):
            MetricType.PSNR.info.parse_log(empty)
