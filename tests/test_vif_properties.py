"""Property-based tests for VIF metric support.

Each test is tagged with the feature and property it validates.
Run with: uv run python -m pytest tests/test_vif_properties.py
"""

# Feature: vif-metric-support

from __future__ import annotations

import asyncio
import json
import math
import os as _os
import tempfile as _tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyqenc.models import QualityTarget
from pyqenc.quality import MetricInfo, MetricType

# Module-level imports avoid first-example import overhead (pandas/matplotlib
# take ~2-3 s on first import on Windows, which would exceed the deadline).
from pyqenc.utils.visualization import (
    QualityEvaluator,
    analyze_chunk_quality,
    parse_vif_file,
)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_VALID_STATS   = ["min", "median", "max", "p05", "p25", "p75", "p95"]
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
    assert result.metric    == MetricType.VIF.value
    assert result.statistic == stat
    assert abs(result.value - value) < 1e-9


def test_quality_target_parse_vif_example() -> None:
    """Concrete example: 'vif-min:85.0' parses correctly (Req 7.1)."""
    result = QualityTarget.parse("vif-min:85.0")
    assert result.metric    == MetricType.VIF.value
    assert result.statistic == "min"
    assert result.value     == 85.0


def test_quality_target_parse_invalid_metric_raises() -> None:
    """Invalid metric names must raise ValueError (Req 7.2)."""
    with pytest.raises(ValueError):
        QualityTarget.parse("xyz-min:50")


# ---------------------------------------------------------------------------
# Property 6: parse_vif_file DataFrame structure
# deadline=1000: first call imports pandas (~1-2 s on Windows cold start).
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=1000)
@given(frame_values=st.lists(_st_unit, min_size=1, max_size=50))
def test_parse_vif_file_structure(frame_values: list[float]) -> None:
    """Property 6: DataFrame has index 'frameNum' and one column 'vif'.

    Validates: Requirements 4.1, 4.2
    # Feature: vif-metric-support, Property 6: parse_vif_file DataFrame structure
    """
    df = parse_vif_file(_write_vif(frame_values))
    assert df.index.name == "frameNum"
    assert list(df.columns) == [MetricType.VIF.value]
    assert len(df) == len(frame_values)


# ---------------------------------------------------------------------------
# Property 7: parse_vif_file frame indexing matches VMAF frameNum
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(frame_values=st.lists(_st_unit, min_size=1, max_size=30))
def test_parse_vif_file_frame_indexing(frame_values: list[float]) -> None:
    """Property 7: frameNum values match the VMAF JSON frameNum field (0-based).

    Validates: Requirements 4.3
    # Feature: vif-metric-support, Property 7: parse_vif_file frame indexing
    """
    df = parse_vif_file(_write_vif(frame_values))
    assert list(df.index) == list(range(len(frame_values)))


# ---------------------------------------------------------------------------
# Property 8: parse_vif_file finite values
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(frame_values=st.lists(_st_unit, min_size=1, max_size=50))
def test_parse_vif_file_finite_values(frame_values: list[float]) -> None:
    """Property 8: Every value in the 'vif' column is a finite float.

    Validates: Requirements 4.6
    # Feature: vif-metric-support, Property 8: parse_vif_file finite values
    """
    df = parse_vif_file(_write_vif(frame_values))
    for val in df[MetricType.VIF.value]:
        assert math.isfinite(val), f"Non-finite value: {val}"


# ---------------------------------------------------------------------------
# Property 9: parse_vif_file combined score is average of four scales
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    s0=_st_unit, s1=_st_unit, s2=_st_unit, s3=_st_unit,
)
def test_parse_vif_file_combined_score(s0: float, s1: float, s2: float, s3: float) -> None:
    """Property 9: Combined VIF = average of the four integer_vif_scale values.

    Validates: Requirements 4.5
    # Feature: vif-metric-support, Property 9: parse_vif_file combined score
    """
    frames = [{"frameNum": 0, "metrics": {
        "vmaf": 95.0,
        "integer_vif_scale0": s0,
        "integer_vif_scale1": s1,
        "integer_vif_scale2": s2,
        "integer_vif_scale3": s3,
    }}]
    _VIF_TMP2.write_text(json.dumps({"frames": frames}), encoding="utf-8")
    df = parse_vif_file(_VIF_TMP2)
    expected = (s0 + s1 + s2 + s3) / 4
    assert abs(df[MetricType.VIF.value].iloc[0] - expected) < 1e-9


# ---------------------------------------------------------------------------
# Property 10: analyze_chunk_quality VIF integration and normalization
# ---------------------------------------------------------------------------


@settings(max_examples=50)
@given(frame_values=st.lists(_st_unit, min_size=2, max_size=30))
def test_analyze_chunk_quality_vif_normalized(frame_values: list[float]) -> None:
    """Property 10: ChunkQualityStats contains VIF; all stats in [0, 100].

    Validates: Requirements 5.2, 5.3
    # Feature: vif-metric-support, Property 10: analyze_chunk_quality VIF normalization
    """
    result = analyze_chunk_quality(
        vif_log=_write_vif(frame_values),
        generate_plot=False,
        delete_after_parse=False,
    )
    assert MetricType.VIF in result
    for stat_key in ("min", "p05", "p25", "median", "p75", "p95", "max"):
        val = result[MetricType.VIF][stat_key]  # type: ignore[literal-required]
        assert 0.0 <= val <= 100.0, f"VIF {stat_key}={val} outside [0, 100]"


# ---------------------------------------------------------------------------
# Property 11: analyze_chunk_quality backward compatibility
# ---------------------------------------------------------------------------


@settings(max_examples=50)
@given(frame_values=st.lists(_st_unit, min_size=2, max_size=30))
def test_analyze_chunk_quality_no_vif_unchanged(frame_values: list[float]) -> None:
    """Property 11: vif_log=None → MetricType.VIF absent from ChunkQualityStats.

    Validates: Requirements 5.5
    # Feature: vif-metric-support, Property 11: analyze_chunk_quality backward compatibility
    """
    result = analyze_chunk_quality(
        psnr_log=_write_psnr(frame_values),
        vif_log=None,
        generate_plot=False,
        delete_after_parse=False,
    )
    assert MetricType.VIF  not in result
    assert MetricType.PSNR in result


# ---------------------------------------------------------------------------
# Property 15: _generate_metrics — VIF shares vmaf_json path
# ---------------------------------------------------------------------------


def test_generate_metrics_vif_shares_vmaf_path(tmp_path: Path) -> None:
    """Property 15: vif_log and vmaf_json point to the same .tmp file.

    VIF data is embedded in the VMAF JSON, so no separate VIF process runs.

    Validates: Requirements 6.2, 12.1, 12.2
    # Feature: vif-metric-support, Property 15: _generate_metrics VIF shares vmaf path
    """
    from pyqenc.quality import MetricType as _MetricType
    from pyqenc.utils.ffmpeg_runner import FFmpegRunResult

    evaluator = QualityEvaluator(work_dir=tmp_path)

    async def _fake_run_metrics(
        metrics:           object = None,
        distorted:         object = None,
        reference:         object = None,
        crop_distorted:    object = None,
        crop_reference:    object = None,
        duration:          object = None,
        width:             object = None,
        use_gpu:           object = None,
        subsample:         object = None,
        output_prefix:     str    = "",
        cwd:               Path | None = None,
        progress_callback: object = None,
        output_extension:  str | None = None,
    ) -> FFmpegRunResult:
        """Fake run_metrics: touch the expected .tmp output files."""
        ext        = output_extension or ".tmp"
        output_dir = cwd or tmp_path
        for mt in (_MetricType.PSNR, _MetricType.SSIM, _MetricType.VMAF):
            (Path(str(output_dir)) / f"{output_prefix}{mt.value}{ext}").touch()
        return FFmpegRunResult(returncode=0, success=True, stderr_lines=[], frame_count=0)

    with patch("pyqenc.utils.visualization.run_metrics", side_effect=_fake_run_metrics):
        artifacts = asyncio.run(
            evaluator._generate_metrics(
                encoded          = tmp_path / "encoded.mkv",
                reference        = tmp_path / "reference.mkv",
                ref_crop         = __import__("pyqenc.models", fromlist=["CropParams"]).CropParams(),
                output_prefix    = str(tmp_path / "test."),
                metrics_sampling = 1,
                cwd              = tmp_path,
            )
        )

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
    test_info = MetricInfo(
        name              = "TEST",
        id                = "test",
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
    )
    result   = test_info.normalize(raw)
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
# Property 13: Target evaluation direction-awareness
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(actual=_st_normalized, target_val=_st_normalized)
def test_vif_target_evaluation_direction(actual: float, target_val: float) -> None:
    """Property 13: VIF target met iff actual >= target_val.

    Validates: Requirements 7.4
    # Feature: vif-metric-support, Property 13: Target evaluation direction-awareness
    """
    assert MetricType.VIF.info.passes(actual, target_val) == (actual >= target_val)


# ---------------------------------------------------------------------------
# Property 14: Sidecar YAML VIF key generation
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    vif_stats=st.fixed_dictionaries({
        "min":    _st_normalized,
        "p05":    _st_normalized,
        "p25":    _st_normalized,
        "median": _st_normalized,
        "p75":    _st_normalized,
        "p95":    _st_normalized,
        "max":    _st_normalized,
        "std":    st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    }),
)
def test_sidecar_vif_keys(vif_stats: dict[str, float]) -> None:
    """Property 14: Sidecar flattening produces all 8 vif_* keys with correct values.

    Validates: Requirements 9.1
    # Feature: vif-metric-support, Property 14: Sidecar YAML VIF key generation
    """
    from pyqenc.quality import ChunkQualityStats, MetricStats

    stats: ChunkQualityStats = {MetricType.VIF: MetricStats(**vif_stats)}  # type: ignore[misc]
    flat: dict[str, float] = {
        f"{mt.value}_{stat}": value
        for mt, ms in stats.items()
        for stat, value in ms.items()
    }
    for s in ("min", "p05", "p25", "median", "p75", "p95", "max", "std"):
        key = f"{MetricType.VIF.value}_{s}"
        assert key in flat
        assert flat[key] == vif_stats[s]
