"""Unit tests for the -q/--quality override, its validation, and derived fixed mode.

Covers the fixed-quality spec's config layer (Req 1–4):

- ``-q`` parsing: single value, pair separators (``:``, ``-``, ``..``),
  unordered input, error forms.
- Override application through ``AppConfig.resolve_encoding``: per-codec
  direction (CRF and VBR), precedence over profile ranges,
  ``default_quality`` auto-adjust.
- Validation at the shared range-validation site: subset-of-codec and endpoint
  granularity alignment for the CLI override, profile-declared ranges, and
  codec-declared ranges (multi-codec intersection included).
- Derived run mode: ``EncodingPlan.fixed_quality`` derivation and the
  mixed-mode / uniform-label loud exits in ``resolve_encoding``.
- Searched configs (no override) resolve unchanged.
"""

import argparse
import logging
from decimal import Decimal

import pytest
from pydantic import ValidationError

from pyqenc.app_config import (
    AppConfig,
    load_app_config,
)
from pyqenc.cli import _parse_quality_override
from pyqenc.models import EncodingPlan

_DEFAULT_CONFIG = load_app_config(default_only=True)


def _config_with_strategies(*patterns: str) -> AppConfig:
    """Build a default-config ``AppConfig`` with the given strategy patterns.

    Strategies are injected into the dumped default config and re-validated,
    so the config is constructed through the supported ``model_validate``
    path; resolution itself happens per-call via ``resolve_encoding``.
    """
    config_dict = _DEFAULT_CONFIG.model_dump()
    config_dict["encoding"]["strategies"] = list(patterns)
    return AppConfig.model_validate(config_dict)


def _resolve_with_override(
    patterns: list[str],
    override: tuple[Decimal, Decimal] | None,
) -> EncodingPlan:
    """Resolve the given patterns under a ``-q`` override; returns the plan."""
    config = _config_with_strategies(*patterns)
    return config.resolve_encoding(quality=override)


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

class TestParseQualityOverride:
    """``_parse_quality_override``: all accepted forms and error forms."""

    def test_none_when_flag_absent(self) -> None:
        assert _parse_quality_override(None) is None

    def test_single_value_becomes_fixed_pair(self) -> None:
        assert _parse_quality_override("18") == (Decimal("18"), Decimal("18"))

    def test_single_decimal_value(self) -> None:
        assert _parse_quality_override("18.5") == (Decimal("18.5"), Decimal("18.5"))

    def test_colon_pair(self) -> None:
        assert _parse_quality_override("18:24") == (Decimal("18"), Decimal("24"))

    def test_dash_pair(self) -> None:
        assert _parse_quality_override("18-24") == (Decimal("18"), Decimal("24"))

    def test_dotdot_pair(self) -> None:
        assert _parse_quality_override("18..24") == (Decimal("18"), Decimal("24"))

    def test_pair_returned_in_input_order(self) -> None:
        """The parser does not canonicalize — order normalization is owned
        solely by ``AppConfig.resolve_encoding`` (pinned there)."""
        assert _parse_quality_override("24:18") == (Decimal("24"), Decimal("18"))

    def test_whitespace_tolerated(self) -> None:
        assert _parse_quality_override(" 18 : 24 ") == (Decimal("18"), Decimal("24"))

    def test_decimal_pair_with_separator(self) -> None:
        assert _parse_quality_override("18.5-22.5") == (Decimal("18.5"), Decimal("22.5"))

    @pytest.mark.parametrize("bad", ["", "   ", "abc", "18:24:30", "18:aa", "a-b", ":"])
    def test_invalid_values_raise(self, bad: str) -> None:
        with pytest.raises(ValueError, match="quality"):
            _parse_quality_override(bad)


# ---------------------------------------------------------------------------
# Override application through resolve_encoding()
# ---------------------------------------------------------------------------

class TestOverrideApplication:
    """The override threads into strategy resolution as the effective range."""

    def test_crf_direction_better_lower(self) -> None:
        plan = _resolve_with_override(["h265-aq"], (Decimal("18"), Decimal("24")))
        (strategy,) = plan.strategies
        assert strategy.codec.quality_better == Decimal("18")
        assert strategy.codec.quality_worse == Decimal("24")

    def test_crf_unordered_input_normalized(self) -> None:
        plan = _resolve_with_override(["h265-aq"], (Decimal("24"), Decimal("18")))
        (strategy,) = plan.strategies
        assert strategy.codec.quality_better == Decimal("18")
        assert strategy.codec.quality_worse == Decimal("24")

    def test_vbr_direction_better_higher(self) -> None:
        # VBR codec range is [99.5, 0.5] (higher = better): the canonical
        # (lower, upper) override must be re-ordered into that convention.
        plan = _resolve_with_override(
            ["nvenc-h265-10bit-vbr"], (Decimal("10"), Decimal("40")),
        )
        (strategy,) = plan.strategies
        assert strategy.codec.quality_better == Decimal("40")
        assert strategy.codec.quality_worse == Decimal("10")

    def test_override_replaces_profile_range(self) -> None:
        config_dict = _DEFAULT_CONFIG.model_dump()
        config_dict["profiles"]["h265-aq"]["quality_range"] = [12.0, 20.0]
        config = AppConfig.model_validate(config_dict)
        plan = config.resolve_encoding(
            strategies = ["h265-aq"],
            quality    = (Decimal("22"), Decimal("26")),
        )
        (strategy,) = plan.strategies
        # CLI wins over the profile band entirely.
        assert (strategy.codec.quality_better, strategy.codec.quality_worse) == (
            Decimal("22"), Decimal("26"),
        )

    def test_fixed_quality_derivation_single_point(self) -> None:
        plan = _resolve_with_override(["h265-aq", "h264"], (Decimal("18"), Decimal("18")))
        assert plan.fixed_quality is True
        assert all(
            s.codec.quality_better == s.codec.quality_worse == Decimal("18")
            for s in plan.strategies
        )

    def test_fixed_quality_false_for_ranged_override(self) -> None:
        plan = _resolve_with_override(["h265-aq"], (Decimal("18"), Decimal("24")))
        assert plan.fixed_quality is False

    def test_default_quality_auto_adjusted_to_override(self, caplog: pytest.LogCaptureFixture) -> None:
        # h265 default_quality is 18.0; pinning to 20 excludes it → nearest
        # bound (20) with a recorded adjustment.
        with caplog.at_level(logging.INFO, logger="pyqenc.app_config"):
            plan = _resolve_with_override(["h265-aq"], (Decimal("20"), Decimal("20")))
        (strategy,) = plan.strategies
        assert strategy.codec.default_quality == Decimal("20")
        assert "default_quality" in caplog.text and "adjusted" in caplog.text

    def test_default_quality_untouched_when_inside_override(self) -> None:
        plan = _resolve_with_override(["h265-aq"], (Decimal("16"), Decimal("22")))
        (strategy,) = plan.strategies
        assert strategy.codec.default_quality == Decimal("18.0")

    def test_default_quality_auto_adjusted_for_profile_narrowing(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The same auto-adjust applies retroactively to profile-declared
        # narrowing (pre-existing gap closed by the spec).
        config_dict = _DEFAULT_CONFIG.model_dump()
        config_dict["profiles"]["h265-aq"]["quality_range"] = [20.0, 24.0]
        config = AppConfig.model_validate(config_dict)
        with caplog.at_level(logging.INFO, logger="pyqenc.app_config"):
            plan = config.resolve_encoding(strategies=["h265-aq"])
        (strategy,) = plan.strategies
        assert strategy.codec.default_quality == Decimal("20.0")
        assert "default_quality" in caplog.text

    def test_vbr_default_quality_adjusted_to_lower_bound(self) -> None:
        # VBR default 20.0; override [30, 60] excludes it → nearest bound is 30.
        plan = _resolve_with_override(
            ["nvenc-h265-10bit-vbr"], (Decimal("30"), Decimal("60")),
        )
        (strategy,) = plan.strategies
        assert strategy.codec.default_quality == Decimal("30")


class TestSearchedRunsUnchanged:
    """Without the override, resolution is identical to today's behavior."""

    def test_no_override_resolves_default_ranges(self) -> None:
        plan = _resolve_with_override(["h265-aq", "h264"], None)
        default_plan = _config_with_strategies("h265-aq", "h264").resolve_encoding()
        assert [
            (s.codec.quality_better, s.codec.quality_worse) for s in plan.strategies
        ] == [
            (s.codec.quality_better, s.codec.quality_worse)
            for s in default_plan.strategies
        ]
        assert plan.fixed_quality is False

    def test_override_clearing_restores_original_ranges(self) -> None:
        config = _config_with_strategies("h265-aq")
        original = [
            (s.codec.quality_better, s.codec.quality_worse)
            for s in config.resolve_encoding().strategies
        ]
        pinned = config.resolve_encoding(quality=(Decimal("18"), Decimal("18")))
        assert pinned.fixed_quality is True
        restored = config.resolve_encoding()
        assert [
            (s.codec.quality_better, s.codec.quality_worse)
            for s in restored.strategies
        ] == original


# ---------------------------------------------------------------------------
# Validation: subset + endpoint granularity alignment
# ---------------------------------------------------------------------------

class TestOverrideValidation:
    """The override lives at the shared range-validation site (Req 2)."""

    def test_subset_below_rejected(self) -> None:
        with pytest.raises(ValueError, match="subset"):
            _resolve_with_override(["h265-aq"], (Decimal("4"), Decimal("24")))

    def test_subset_above_rejected(self) -> None:
        with pytest.raises(ValueError, match="subset"):
            _resolve_with_override(["h265-aq"], (Decimal("18"), Decimal("31")))

    def test_vbr_subset_rejected(self) -> None:
        with pytest.raises(ValueError, match="subset"):
            _resolve_with_override(
                ["nvenc-h265-10bit-vbr"], (Decimal("0.2"), Decimal("24")),
            )

    def test_single_point_granularity_rejected(self) -> None:
        # av1 granularity is 1 (integer steps): -q 18.5 must exit loudly
        # naming the codec and the nearest aligned values.
        with pytest.raises(ValueError, match="av1.*18 and 19"):
            _resolve_with_override(["av1"], (Decimal("18.5"), Decimal("18.5")))

    def test_pair_granularity_rejected(self) -> None:
        # h265 granularity 0.5: fractional quarters are off the grid.
        with pytest.raises(ValueError, match="granularity 0.5"):
            _resolve_with_override(["h265-aq"], (Decimal("22.3"), Decimal("28.7")))

    def test_multi_codec_intersection(self) -> None:
        # h265 (0.5) + AV1 (1.0) matched by one override: the coarsest step
        # wins — 18.5 is legal for h265 but must fail naming the AV1 side.
        with pytest.raises(ValueError, match="av1.*18 and 19"):
            _resolve_with_override(["h265-aq", "av1"], (Decimal("18.5"), Decimal("18.5")))

    def test_aligned_pair_accepted_across_codecs(self) -> None:
        plan = _resolve_with_override(["h265-aq", "av1"], (Decimal("18"), Decimal("24")))
        assert len(plan.strategies) == 2


class TestProfileAndCodecAlignment:
    """Endpoint alignment applies retroactively to profile- and codec-declared ranges."""

    def test_profile_range_misaligned_rejected_at_load(self) -> None:
        config_dict = _DEFAULT_CONFIG.model_dump()
        config_dict["profiles"]["h265-aq"]["quality_range"] = [18.5, 24.0]
        # av1 codec granularity is 1 — an av1 profile with a fractional bound
        # must fail validation at config load.
        config_dict["profiles"]["av1"]["quality_range"] = [18.5, 24.0]
        with pytest.raises(ValidationError, match="granularity 1"):
            AppConfig.model_validate(config_dict)

    def test_profile_range_aligned_accepted(self) -> None:
        config_dict = _DEFAULT_CONFIG.model_dump()
        config_dict["profiles"]["av1"]["quality_range"] = [18.0, 24.0]
        config = AppConfig.model_validate(config_dict)
        plan = config.resolve_encoding(strategies=["av1"])
        (strategy,) = plan.strategies
        assert strategy.codec.quality_better == Decimal("18.0")

    def test_codec_range_misaligned_rejected(self) -> None:
        config_dict = _DEFAULT_CONFIG.model_dump()
        config_dict["codecs"]["av1-10bit"]["quality_range"] = [4.5, 30]
        with pytest.raises(ValidationError, match="not an exact multiple"):
            AppConfig.model_validate(config_dict)


# ---------------------------------------------------------------------------
# resolve_encoding mode checks (exercised through _build_config)
# ---------------------------------------------------------------------------

def _build_args(**overrides: object) -> argparse.Namespace:
    """A minimal namespace carrying only the probed override attributes."""
    defaults: dict[str, object] = {
        "include": None, "exclude": None,
        "scene_threshold": None, "min_scene_length": None,
        "targets": None, "strategies": None, "quality": None,
        "no_optimize": False, "concurrency": None,
        "metrics_sampling": None, "no_visual_hash": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _build_config_isolated(
    monkeypatch: pytest.MonkeyPatch,
    args: argparse.Namespace,
    *,
    base_config: AppConfig | None = None,
) -> EncodingPlan:
    """``_build_config`` pinned to the bundled default (no home/CWD layers).

    Returns the resolved ``EncodingPlan`` (the mode-check surface); the
    module-level default is deep-copied so it stays pristine for other tests.
    """
    config = (base_config if base_config is not None else _DEFAULT_CONFIG).model_copy(deep=True)
    monkeypatch.setattr("pyqenc.cli.load_app_config", lambda: config)
    from pyqenc.cli import _build_config as build_config
    _, plan = build_config(args)
    return plan


class TestBuildConfigModeChecks:
    """Post-resolve loud exits in ``resolve_encoding`` via ``_build_config`` (Req 3.2, Req 4)."""

    def test_uniform_label_required_under_q(self, monkeypatch: pytest.MonkeyPatch) -> None:
        args = _build_args(strategies="h265-aq,vulkan-h265-10bit-qp", quality="18")
        with pytest.raises(ValueError, match="different quality labels.*CRF.*QP"):
            _build_config_isolated(monkeypatch, args)

    def test_same_label_families_accepted_under_q(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # h265-aq and av1 both carry the default "CRF" label — string equality
        # passes; knob-scale incomparability is the banner's message, not a stop.
        args = _build_args(strategies="h265-aq,av1", quality="18")
        plan = _build_config_isolated(monkeypatch, args)
        assert plan.fixed_quality is True

    def test_mixed_fixed_and_searched_stops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dict = _DEFAULT_CONFIG.model_dump()
        config_dict["profiles"]["h265-aq"]["quality_range"] = [18.0, 18.0]
        base = AppConfig.model_validate(config_dict)
        args = _build_args(strategies="h265-aq,h264")
        with pytest.raises(ValueError, match="Mixed fixed and searched"):
            _build_config_isolated(monkeypatch, args, base_config=base)

    def test_q_pinning_all_strategies_avoids_mixed_stop(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        args = _build_args(strategies="h265-aq,h264", quality="20")
        plan = _build_config_isolated(monkeypatch, args)
        assert plan.fixed_quality is True

    def test_default_config_builds_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The default searched configuration must build identically (no -q).
        plan = _build_config_isolated(monkeypatch, _build_args())
        assert plan.fixed_quality is False

    def test_invalid_q_exits_with_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        args = _build_args(strategies="h265-aq,av1", quality="18.5")
        with pytest.raises(ValueError, match="granularity 1"):
            _build_config_isolated(monkeypatch, args)


class TestPlanConstructionInvariants:
    """The plan's own construction-time invariants (typed after-validators)."""

    def test_no_strategies_is_loud(self) -> None:
        """An empty strategy set is a configuration error at the run boundary,
        not a phase failure minutes into the pipeline."""
        config = _config_with_strategies()
        config.encoding.strategies = []
        with pytest.raises(ValueError, match="no strategies.*nothing to encode"):
            config.resolve_encoding()

    def test_searched_run_without_targets_is_loud(self) -> None:
        """Without targets a searched run degenerates into a single
        default-quality encode — no bar means no search."""
        config = _config_with_strategies("h265-aq", "h264")
        with pytest.raises(ValueError, match="Searched run has no quality targets"):
            config.resolve_encoding(targets=[])

    def test_fixed_run_without_targets_allowed(self) -> None:
        """Fixed runs legitimately omit targets: the pinned knob is the bar
        and config targets drive nothing there."""
        config = _config_with_strategies("h265-aq")
        plan = config.resolve_encoding(
            strategies=["h265-aq"], targets=[], quality=(Decimal("18"), Decimal("18")),
        )
        assert plan.fixed_quality is True
        assert plan.targets == []
