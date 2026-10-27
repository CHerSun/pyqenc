"""Property-based tests for VIF metric support.

Each test is tagged with the feature and property it validates.
Run with: uv run python -m pytest tests/test_vif_properties.py
"""

# Feature: vif-metric-support

import asyncio
import json
import math
import os as _os
import tempfile as _tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyqenc.models import QualityTarget

# Module-level imports avoid first-example import overhead (pandas/matplotlib
# take ~2-3 s on first import on Windows, which would exceed the deadline).
from pyqenc.quality_metrics import (
    MeasuredMetrics,
    MetricInfo,
    MetricStatKey,
    MetricType,
    StatisticType,
)
from pyqenc.utils.visualization import QualityMeasurer

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_VALID_STATS   = ["min", "median", "max", "p05", "p10", "p25", "p75", "p90", "p95"]
_st_valid_stat = st.sampled_from(_VALID_STATS)
_st_normalized = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)
_st_unit       = st.floats(min_value=0.0, max_value=1.0,   allow_nan=False, allow_infinity=False)

# ---------------------------------------------------------------------------
# Shared temp files — created once per process, overwritten per example.
# ---------------------------------------------------------------------------

_VIF_TMP_FD,  _VIF_TMP_STR  = _tempfile.mkstemp(suffix=".vmaf.tmp", prefix="test_vif_")
_VIF_TMP_FD2, _VIF_TMP2_STR = _tempfile.mkstemp(suffix=".vmaf2.tmp", prefix="test_vif_")
_PSNR_TMP_FD, _PSNR_TMP_STR = _tempfile.mkstemp(suffix=".psnr.tmp", prefix="test_vif_")
_os.close(_VIF_TMP_FD)
_os.close(_VIF_TMP_FD2)
_os.close(_PSNR_TMP_FD)
_VIF_TMP  = Path(_VIF_TMP_STR)
_VIF_TMP2 = Path(_VIF_TMP2_STR)
_PSNR_TMP = Path(_PSNR_TMP_STR)


def _write_vif(values: list[float], dest: Path = _VIF_TMP) -> Path:
    """Write a synthetic VMAF JSON with VIF scale data and return its path.

    VIF data is embedded in the VMAF JSON as integer_vif_scale0–scale3.
    Each scale value equals *v* for simplicity; combined VIF = v.
    """
    frames = [
        {
            "frameNum": i,
            "metrics": {
                "vmaf": 95.0,
                "integer_vif_scale0": v,
                "integer_vif_scale1": v,
                "integer_vif_scale2": v,
                "integer_vif_scale3": v,
            },
        }
        for i, v in enumerate(values)
    ]
    dest.write_text(json.dumps({"frames": frames}), encoding="utf-8")
    return dest


def _write_psnr(values: list[float]) -> Path:
    """Overwrite _PSNR_TMP with a synthetic PSNR log and return its path."""
    lines = [
        f"n:{i + 1} mse_avg:0.01 mse_y:0.01 mse_u:0.01 mse_v:0.01 "
        f"psnr_avg:{40.0 + v * 10:.4f} psnr_y:40.0 psnr_u:40.0 psnr_v:40.0"
        for i, v in enumerate(values)
    ]
    _PSNR_TMP.write_text("\n".join(lines), encoding="utf-8")
    return _PSNR_TMP


# ---------------------------------------------------------------------------
# Property 12: QualityTarget.parse accepts VIF
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    stat=_st_valid_stat,
    value=st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
)
def test_quality_target_parse_accepts_vif(stat: str, value: float) -> None:
    """Property 12: QualityTarget.parse accepts VIF.

    Validates: Requirements 7.1, 7.3
    # Feature: vif-metric-support, Property 12: QualityTarget.parse accepts VIF
    """
    result = QualityTarget.parse(f"{MetricType.VIF.value}-{stat}:{value}")
    assert result.identity.metric    == MetricType.VIF
    assert result.identity.statistic == StatisticType(stat)
    assert abs(result.value - value) < 1e-9


def test_quality_target_parse_vif_example() -> None:
    """Concrete example: 'vif-min:85.0' parses correctly (Req 7.1)."""
    result = QualityTarget.parse("vif-min:85.0")
    assert result.identity.metric    == MetricType.VIF
    assert result.identity.statistic == StatisticType.MIN
    assert result.value     == 85.0


def test_quality_target_parse_invalid_metric_raises() -> None:
    """Invalid metric names must raise ValueError (Req 7.2)."""
    with pytest.raises(ValueError):
        QualityTarget.parse("xyz-min:50")


# ---------------------------------------------------------------------------
# Property 6: parse_log structure (one value per frame, frame order)
# deadline=1000: first call imports pandas (~1-2 s on Windows cold start).
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=1000)
@given(frame_values=st.lists(_st_unit, min_size=1, max_size=50))
def test_parse_vif_structure(frame_values: list[float]) -> None:
    """Property 6: parse_log returns exactly one value per frame.

    Validates: Requirements 4.1, 4.2
    # Feature: vif-metric-support, Property 6: parse_log structure
    """
    values = MetricType.VIF.info.parse_log(_write_vif(frame_values))
    assert len(values) == len(frame_values)


# ---------------------------------------------------------------------------
# Property 7: parse_log preserves the frames' order
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(frame_values=st.lists(_st_unit, min_size=1, max_size=30))
def test_parse_vif_preserves_frame_order(frame_values: list[float]) -> None:
    """Property 7: parsed values follow the JSON frames' order (frameNum
    ascends with list position — the source frame a value belongs to).

    Validates: Requirements 4.3
    # Feature: vif-metric-support, Property 7: parse_log frame order
    """
    values = MetricType.VIF.info.parse_log(_write_vif(frame_values))
    assert values == [pytest.approx(v) for v in frame_values]


# ---------------------------------------------------------------------------
# Property 8: parse_log finite values
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(frame_values=st.lists(_st_unit, min_size=1, max_size=50))
def test_parse_vif_finite_values(frame_values: list[float]) -> None:
    """Property 8: Every parsed value is a finite float.

    Validates: Requirements 4.6
    # Feature: vif-metric-support, Property 8: parse_log finite values
    """
    for val in MetricType.VIF.info.parse_log(_write_vif(frame_values)):
        assert math.isfinite(val), f"Non-finite value: {val}"


# ---------------------------------------------------------------------------
# Property 9: parse_log combined score is average of four scales
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    s0=_st_unit, s1=_st_unit, s2=_st_unit, s3=_st_unit,
)
def test_parse_vif_combined_score(s0: float, s1: float, s2: float, s3: float) -> None:
    """Property 9: Combined VIF = average of the four integer_vif_scale values.

    Validates: Requirements 4.5
    # Feature: vif-metric-support, Property 9: parse_log combined score
    """
    frames = [{"frameNum": 0, "metrics": {
        "vmaf": 95.0,
        "integer_vif_scale0": s0,
        "integer_vif_scale1": s1,
        "integer_vif_scale2": s2,
        "integer_vif_scale3": s3,
    }}]
    _VIF_TMP2.write_text(json.dumps({"frames": frames}), encoding="utf-8")
    values = MetricType.VIF.info.parse_log(_VIF_TMP2)
    expected = (s0 + s1 + s2 + s3) / 4
    assert values[0] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Properties 10/11/14: the measured set through the public measurer
# ---------------------------------------------------------------------------


def _measure_with(
    tmp_path: Path,
    log_files: dict[str, str],
) -> MeasuredMetrics:
    """Measure through ``QualityMeasurer.measure_chunk`` with ffmpeg mocked
    at the process boundary (``run_metrics`` writes the synthetic outputs)."""
    from pyqenc.models import CropParams
    from pyqenc.utils.ffmpeg_runner import FFmpegInput, FFmpegRunResult

    measurer = QualityMeasurer(work_dir=tmp_path)

    async def _fake_run_metrics(metrics, **kwargs) -> FFmpegRunResult:
        ext        = kwargs.get("output_extension") or ".tmp"
        output_dir = Path(str(kwargs.get("cwd")))
        prefix     = kwargs.get("output_prefix", "")
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
            fps_value        = None,
            sampling         = 1,
            plot_path        = tmp_path / "plot.png",
        )
    return measurement.metrics


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(frame_values=st.lists(_st_unit, min_size=2, max_size=30))
def test_metric_pipeline_vif_normalized(tmp_path: Path, frame_values: list[float]) -> None:
    """Property 10 + 14: the parse→normalize→stats pipeline maps VIF into
    [0, 100] and the measured set carries all ten VIF statistics.

    Bug guarded: raw VIF scale is ~[0, 1.5]; a stats row leaking the raw
    scale would show min/median values far below the other metrics.

    Validates: Requirements 5.2, 5.3, 9.1
    # Feature: vif-metric-support, Property 10: pipeline VIF normalization
    """
    result = _measure_with(tmp_path, {"vmaf": json.dumps({"frames": [
        {
            "frameNum":    i,
            "metrics":     {"vmaf": 95.0, **{f"integer_vif_scale{s}": v for s in range(4)}},
        }
        for i, v in enumerate(frame_values)
    ]})})
    for statistic in StatisticType:
        key   = MetricStatKey(MetricType.VIF, statistic)
        assert key in result, f"missing measured statistic {key.sidecar_key()}"
        value = result[key]
        if statistic is StatisticType.STD and math.isnan(value):
            continue
        assert 0.0 <= value <= 100.0, f"VIF {statistic.value}={value} outside [0, 100]"


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(frame_values=st.lists(st.floats(min_value=30.0, max_value=60.0), min_size=2, max_size=30))
def test_metric_pipeline_no_vif_input_no_vif_row(tmp_path: Path, frame_values: list[float]) -> None:
    """Property 11: a PSNR-only run never produces VIF measurements.

    Validates: Requirements 5.5
    # Feature: vif-metric-support, Property 11: no VIF input, no VIF row
    """
    result = _measure_with(tmp_path, {"psnr": _write_psnr(frame_values).read_text(encoding="utf-8")})
    assert not [key for key in result if key.metric is MetricType.VIF]
    assert any(key.metric is MetricType.PSNR for key in result)


# ---------------------------------------------------------------------------
# Property 15: _generate_metrics — VIF shares vmaf_json path
# ---------------------------------------------------------------------------


def test_generate_metrics_vif_shares_vmaf_path(tmp_path: Path) -> None:
    """Property 15: vif_log and vmaf_json point to the same .tmp file.

    VIF data is embedded in the VMAF JSON, so no separate VIF process runs.

    Validates: Requirements 6.2, 12.1, 12.2
    # Feature: vif-metric-support, Property 15: _generate_metrics VIF shares vmaf path
    """
    from pyqenc.quality_metrics import MetricType as _MetricType
    from pyqenc.utils.ffmpeg_runner import FFmpegInput, FFmpegRunResult

    evaluator = QualityMeasurer(work_dir=tmp_path)

    async def _fake_run_metrics(metrics, **kwargs) -> FFmpegRunResult:
        """Fake run_metrics: touch the expected .tmp output files."""
        ext        = kwargs.get("output_extension") or ".tmp"
        output_dir = kwargs.get("cwd") or tmp_path
        for mt in (_MetricType.PSNR, _MetricType.SSIM, _MetricType.VMAF):
            (Path(str(output_dir)) / f"{kwargs.get('output_prefix', '')}{mt.value}{ext}").touch()
        return FFmpegRunResult(returncode=0, success=True, stderr_lines=[], frame_count=0)

    with patch("pyqenc.utils.visualization.run_metrics", side_effect=_fake_run_metrics):
        artifacts, run_frames = asyncio.run(
            evaluator._generate_metrics(
                encoded          = tmp_path / "encoded.mkv",
                reference        = FFmpegInput(path=tmp_path / "reference.mkv"),
                ref_crop         = __import__("pyqenc.models", fromlist=["CropParams"]).CropParams(),
                output_prefix    = str(tmp_path / "test."),
                sampling         = 1,
                cwd              = tmp_path,
            )
        )
    assert run_frames == 0

    # All paths end with .tmp
    for attr in ("psnr_log", "ssim_log", "vmaf_json", "vif_log"):
        path = getattr(artifacts, attr)
        assert path is not None, f"{attr} must not be None"
        assert path.suffix == ".tmp", f"{attr} must end with .tmp, got {path.suffix}"

    # VIF explicitly shares the VMAF path
    assert artifacts.vif_log == artifacts.vmaf_json, (
        "vif_log must point to the same file as vmaf_json"
    )


# ---------------------------------------------------------------------------
# Property 1: Normalization formula correctness
# ---------------------------------------------------------------------------


class _ProbeMetricInfo(MetricInfo):
    """A concrete descriptor with arbitrary formula parameters (the parse
    contract is irrelevant to the normalization property)."""

    def parse_log(self, path: Path) -> list[float]:
        return []


@settings(max_examples=200)
@given(
    offset=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    scale=st.floats(min_value=-1e6,  max_value=1e6, allow_nan=False, allow_infinity=False),
    raw=st.floats(min_value=-1e6,    max_value=1e6, allow_nan=False, allow_infinity=False),
    clip_lower=st.one_of(st.none(), st.floats(min_value=-1e6, max_value=0.0, allow_nan=False, allow_infinity=False)),
    clip_upper=st.one_of(st.none(), st.floats(min_value=0.0,  max_value=1e6, allow_nan=False, allow_infinity=False)),
)
def test_normalize_formula_correctness(
    offset: float,
    scale: float,
    raw: float,
    clip_lower: float | None,
    clip_upper: float | None,
) -> None:
    """Property 1: normalize(raw) == clip(offset + raw * scale, lower, upper).

    Validates: Requirements 1.3, 1.7
    # Feature: vif-metric-support, Property 1: Normalization formula correctness
    """
    test_info = _ProbeMetricInfo(
        name              = "TEST",
        higher_is_better  = True,
        _offset           = offset,
        _scale_factor     = scale,
        _clip_lower       = clip_lower,
        _clip_upper       = clip_upper,
        lossless_value    = 100.0,
        lossless_raw_repr = "100.0",
        display_unit      = "",
        plot_y_min        = 0.0,
        plot_y_max        = 103.0,
        comparison_range  = 10.0,
        acceptance_delta  = 0.2,
        subsample_via_filter = False,
        log_extension     = ".log",
        std_cutoff_max    = None,
    )
    result   = cast(float, test_info.normalize(raw))  # scalar input -> scalar result (Series stubs lack SupportsFloat)
    expected = offset + raw * scale
    if clip_lower is not None:
        expected = max(expected, clip_lower)
    if clip_upper is not None:
        expected = min(expected, clip_upper)
    if math.isfinite(expected) and math.isfinite(result):
        assert abs(result - expected) < 1e-9
    else:
        assert math.isnan(result) == math.isnan(expected)


# ---------------------------------------------------------------------------
# Property 2: VIF lossless normalization
# ---------------------------------------------------------------------------


def test_vif_normalize_lossless() -> None:
    """Property 2: normalize(1.0) == 100.0 for VIF (raw 1.0 = lossless).

    Validates: Requirements 2.4
    # Feature: vif-metric-support, Property 2: VIF lossless normalization
    """
    assert MetricType.VIF.info.normalize(1.0) == 100.0


# ---------------------------------------------------------------------------
# Property 3: VIF clip lower
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(raw=st.floats(min_value=-1e6, max_value=0.0, allow_nan=False, allow_infinity=False))
def test_vif_normalize_clip_lower(raw: float) -> None:
    """Property 3: raw <= 0 normalizes to 0.0 (clipped).

    Validates: Requirements 2.5
    # Feature: vif-metric-support, Property 3: VIF clip lower
    """
    assert MetricType.VIF.info.normalize(raw) == 0.0


# ---------------------------------------------------------------------------
# Property 4: Normalize idempotence (clipping is a projection)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(raw=_st_normalized)
def test_normalize_clip_idempotent(raw: float) -> None:
    """Property 4: For PSNR (offset=0, scale=1, clip_upper=100), normalize is
    idempotent on [0, 100]: normalize(normalize(x)) == normalize(x).

    Validates: Requirements 2.6
    # Feature: vif-metric-support, Property 4: Normalize idempotence
    """
    once  = MetricType.PSNR.info.normalize(raw)
    twice = MetricType.PSNR.info.normalize(once)
    assert once == twice


# ---------------------------------------------------------------------------
# Property 5: passes() direction correctness
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(actual=_st_normalized, target=_st_normalized)
def test_passes_direction_higher_is_better(actual: float, target: float) -> None:
    """Property 5: passes(actual, target) == (actual >= target) for higher_is_better.

    Validates: Requirements 1.7, 2.7, 2.8
    # Feature: vif-metric-support, Property 5: passes() direction correctness
    """
    for metric_type in MetricType:
        if metric_type.info.higher_is_better:
            assert metric_type.info.passes(actual, target) == (actual >= target)


# ---------------------------------------------------------------------------
# Property 13: Target judging direction-awareness
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(actual=_st_normalized, target_val=_st_normalized)
def test_vif_target_evaluation_direction(actual: float, target_val: float) -> None:
    """Property 13: VIF target met iff actual >= target_val.

    Validates: Requirements 7.4
    # Feature: vif-metric-support, Property 13: Target judging direction-awareness
    """
    assert MetricType.VIF.info.passes(actual, target_val) == (actual >= target_val)
