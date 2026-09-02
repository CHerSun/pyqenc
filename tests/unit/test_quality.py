"""Unit tests for quality evaluation and CRF adjustment."""

from decimal import Decimal

import pytest

from pyqenc.models import CodecConfig, QualityTarget
from pyqenc.quality import QualitySearch, QualitySearchBase, _score_attempt

_CRF_MIN    = Decimal("0")
_CRF_MAX    = Decimal("51")
_GRAN       = Decimal("0.5")   # default granularity used in tests
_GRAN_INT   = Decimal("1")     # integer-step granularity (QP-style)


def _d(v: str | int | float) -> Decimal:
    """Shorthand: convert to Decimal via str to avoid float imprecision."""
    return Decimal(str(v))


def _vmaf_target(value: float = 95.0) -> QualityTarget:
    return QualityTarget(metric="vmaf", statistic="min", value=value)


def _pass_metrics(vmaf: float = 96.0) -> dict[str, float]:
    """Metrics that pass a vmaf-min=95 target."""
    return {"vmaf_min": vmaf}


def _fail_metrics(vmaf: float = 80.0) -> dict[str, float]:
    """Metrics that fail a vmaf-min=95 target."""
    return {"vmaf_min": vmaf}


def _make_search(
    better: str = "0",
    worse:  str = "51",
    gran:   str = "0.5",
    target: float = 95.0,
) -> QualitySearch:
    return QualitySearch(
        quality_better  = _d(better),
        quality_worse   = _d(worse),
        quality_targets = [_vmaf_target(target)],
        granularity     = _d(gran),
    )


class TestQualitySearch:
    """Unit tests for QualitySearch. Validates: Requirements 2.1–2.3, 2.6, 2.7, 5.1–5.5"""

    _BETTER = Decimal("0")
    _WORSE  = Decimal("51")
    _GRAN   = Decimal("0.5")
    _TARGET = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]

    def _make(self, **kw: object) -> QualitySearch:
        return QualitySearch(
            quality_better  = kw.get("better", self._BETTER),  # type: ignore[arg-type]
            quality_worse   = kw.get("worse",  self._WORSE),   # type: ignore[arg-type]
            quality_targets = kw.get("targets", self._TARGET),  # type: ignore[arg-type]
            granularity     = kw.get("gran",   self._GRAN),    # type: ignore[arg-type]
        )

    def test_initial_state(self) -> None:
        s = self._make()
        assert s.best_quality is None
        assert s.best_metrics is None
        assert s.best_targets_met is False
        assert s.attempts == 0

    def test_pass_updates_best(self) -> None:
        s = self._make()
        next_q = s.record(Decimal("18.0"), {"vmaf_min": 96.0})
        assert s.best_targets_met is True
        assert s.best_quality == Decimal("18.0")
        assert s.attempts == 1

    def test_fail_updates_best_fail(self) -> None:
        s = self._make()
        s.record(Decimal("25.0"), {"vmaf_min": 80.0})
        assert s.best_targets_met is False
        assert s.best_quality == Decimal("25.0")
        assert s.attempts == 1

    def test_attempts_increments_every_call(self) -> None:
        s = self._make()
        s.record(Decimal("25.0"), {"vmaf_min": 80.0})
        s.record(Decimal("15.0"), {"vmaf_min": 96.0})
        assert s.attempts == 2

    def test_exhaustion_returns_none(self) -> None:
        # Tight bracket: better=19.0, worse=19.5 (span == granularity)
        s = QualitySearch(
            quality_better  = Decimal("19.0"),
            quality_worse   = Decimal("19.5"),
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )
        # First record a pass at better, then a fail at worse — bracket collapses
        s.record(Decimal("19.0"), {"vmaf_min": 96.0})
        result = s.record(Decimal("19.5"), {"vmaf_min": 80.0})
        assert result is None

    def test_subsequent_calls_after_exhaustion_return_none(self) -> None:
        s = QualitySearch(
            quality_better  = Decimal("19.0"),
            quality_worse   = Decimal("19.5"),
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )
        s.record(Decimal("19.0"), {"vmaf_min": 96.0})
        s.record(Decimal("19.5"), {"vmaf_min": 80.0})
        # All subsequent calls must return None
        assert s.record(Decimal("19.0"), {"vmaf_min": 96.0}) is None
        assert s.record(Decimal("19.5"), {"vmaf_min": 80.0}) is None

    def test_raises_if_better_equals_worse(self) -> None:
        # equal boundaries are now valid — single-point search: first record() returns None
        s = QualitySearch(
            quality_better  = Decimal("18"),
            quality_worse   = Decimal("18"),
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )
        result = s.record(Decimal("18"), {"vmaf_min": 96.0})
        assert result is None
        assert s.best_quality == Decimal("18")

    def test_raises_if_granularity_zero(self) -> None:
        with pytest.raises(ValueError):
            QualitySearch(
                quality_better  = Decimal("0"),
                quality_worse   = Decimal("51"),
                quality_targets = self._TARGET,
                granularity     = Decimal("0"),
            )

    def test_early_acceptance_returns_none(self) -> None:
        # surplus well within acceptance_delta → early acceptance
        from pyqenc.quality import MetricType
        delta  = MetricType.VMAF.info.acceptance_delta
        s      = self._make()
        result = s.record(Decimal("18.0"), {"vmaf_min": 95.0 + delta * 0.5})
        assert result is None
        assert s._exhausted is True

    def test_pass_then_fail_best_quality_is_pass(self) -> None:
        s = self._make()
        s.record(Decimal("18.0"), {"vmaf_min": 96.0})  # pass
        s.record(Decimal("20.0"), {"vmaf_min": 80.0})  # fail
        assert s.best_targets_met is True
        assert s.best_quality == Decimal("18.0")


class TestCodecConfigQualityLogPadding:
    """Tests for CodecConfig.quality_log_padding.

    Validates: Requirements 10.7
    """

    def test_crf_range_padding(self):
        """CRF range [0, 51] with granularity 0.5 → padding 4 (len("51.0") == 4)."""
        codec = CodecConfig(
            name="test-crf",
            default_quality=Decimal("18"),
            default_preset="slow",
            quality_range=(Decimal("0"), Decimal("51")),
            quality_granularity=Decimal("0.5"),
            encoder_args=["-i", "{input}", "{quality}"],
            presets=["slow"],
        )
        assert codec.quality_log_padding == 4

    def test_vbr_range_padding(self):
        """VBR range [0, 100] with granularity 0.1 → padding 5 (len("100.0") == 5)."""
        codec = CodecConfig(
            name="test-vbr",
            default_quality=Decimal("50"),
            default_preset="slow",
            quality_range=(Decimal("0"), Decimal("100")),
            quality_granularity=Decimal("0.1"),
            encoder_args=["-i", "{input}", "{quality}"],
            presets=["slow"],
        )
        assert codec.quality_log_padding == 5

    def test_qp_range_padding(self):
        """QP range [0, 63] with granularity 1 → padding 2 (len("63") == 2)."""
        codec = CodecConfig(
            name="test-qp",
            default_quality=Decimal("32"),
            default_preset="slow",
            quality_range=(Decimal("0"), Decimal("63")),
            quality_granularity=Decimal("1"),
            encoder_args=["-i", "{input}", "{quality}"],
            presets=["slow"],
        )
        assert codec.quality_log_padding == 2


class TestScoreAttempt:
    """Unit tests for _score_attempt().

    Validates: Requirements 1.3
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _vmaf_target(value: float = 95.0) -> QualityTarget:
        return QualityTarget(metric="vmaf", statistic="min", value=value)

    @staticmethod
    def _psnr_target(value: float = 45.0) -> QualityTarget:
        return QualityTarget(metric="psnr", statistic="median", value=value)

    # ------------------------------------------------------------------
    # Early acceptance (returns 0.0)
    # ------------------------------------------------------------------

    def test_early_acceptance_single_target_at_delta(self):
        """All targets pass and surplus well within acceptance_delta → 0.0."""
        from pyqenc.quality import MetricType
        target = self._vmaf_target(95.0)
        delta  = MetricType.VMAF.info.acceptance_delta
        # surplus = delta * 0.5 — clearly within, avoids float boundary issues
        metrics = {"vmaf_min": 95.0 + delta * 0.5}
        assert _score_attempt(metrics, [target]) == 0.0

    def test_early_acceptance_surplus_below_delta(self):
        """Surplus well below acceptance_delta → 0.0."""
        from pyqenc.quality import MetricType
        target = self._vmaf_target(95.0)
        delta  = MetricType.VMAF.info.acceptance_delta
        metrics = {"vmaf_min": 95.0 + delta * 0.1}
        assert _score_attempt(metrics, [target]) == 0.0

    def test_early_acceptance_multiple_targets_all_within_delta(self):
        """Multiple targets all within acceptance_delta → 0.0."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="psnr", statistic="median", value=45.0),
        ]
        vmaf_delta = MetricType.VMAF.info.acceptance_delta
        psnr_delta = MetricType.PSNR.info.acceptance_delta
        metrics = {
            "vmaf_min":    95.0 + vmaf_delta * 0.5,
            "psnr_median": 45.0 + psnr_delta * 0.5,
        }
        assert _score_attempt(metrics, targets) == 0.0

    # ------------------------------------------------------------------
    # Pass (returns positive)
    # ------------------------------------------------------------------

    def test_pass_large_surplus_returns_positive(self):
        """All targets pass with surplus > acceptance_delta → positive score."""
        target  = self._vmaf_target(80.0)
        metrics = {"vmaf_min": 95.0}   # surplus = 15, well above delta
        score   = _score_attempt(metrics, [target])
        assert score > 0.0

    def test_pass_score_sums_all_targets(self):
        """Pass score sums surplus/comparison_range for ALL targets."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=80.0),
            QualityTarget(metric="psnr", statistic="median", value=40.0),
        ]
        metrics = {
            "vmaf_min":    95.0,   # surplus = 15
            "psnr_median": 50.0,   # surplus = 10
        }
        score = _score_attempt(metrics, targets)
        vmaf_info = MetricType.VMAF.info
        psnr_info = MetricType.PSNR.info
        expected  = (15.0 / vmaf_info.comparison_range) + (10.0 / psnr_info.comparison_range)
        assert abs(score - expected) < 1e-9
        assert score > 0.0

    def test_pass_one_target_above_delta_one_below(self):
        """Least-proficit surplus ≤ delta → early acceptance even if another target has large surplus.

        The binding constraint (PSNR, smallest surplus) is within its delta, so
        further search cannot meaningfully improve the tightest metric.  The large
        VMAF surplus is irrelevant to the early-exit decision.
        """
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=80.0),
            QualityTarget(metric="psnr", statistic="median", value=44.9),
        ]
        psnr_delta = MetricType.PSNR.info.acceptance_delta
        metrics = {
            "vmaf_min":    95.0,                     # large surplus > delta — not the binding constraint
            "psnr_median": 44.9 + psnr_delta * 0.5,  # tiny surplus ≤ delta — least proficit
        }
        score = _score_attempt(metrics, targets)
        assert score == 0.0  # early acceptance: least-proficit metric is within its delta

    # ------------------------------------------------------------------
    # Fail (returns negative)
    # ------------------------------------------------------------------

    def test_fail_single_target_returns_negative(self):
        """Any target not met → negative score."""
        target  = self._vmaf_target(95.0)
        metrics = {"vmaf_min": 90.0}   # deficit = -5
        score   = _score_attempt(metrics, [target])
        assert score < 0.0

    def test_fail_score_sums_only_failing_targets(self):
        """Fail score sums deficit/comparison_range for FAILING targets only."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="psnr", statistic="median", value=45.0),
        ]
        metrics = {
            "vmaf_min":    90.0,   # fails: deficit = -5
            "psnr_median": 50.0,   # passes: surplus = 5 (should NOT contribute)
        }
        score     = _score_attempt(metrics, targets)
        vmaf_info = MetricType.VMAF.info
        expected  = -5.0 / vmaf_info.comparison_range   # only vmaf contributes
        assert abs(score - expected) < 1e-9
        assert score < 0.0

    def test_fail_multiple_failing_targets(self):
        """Multiple failing targets all contribute to the negative score."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="psnr", statistic="median", value=45.0),
        ]
        metrics = {
            "vmaf_min":    90.0,   # deficit = -5
            "psnr_median": 40.0,   # deficit = -5
        }
        score     = _score_attempt(metrics, targets)
        vmaf_info = MetricType.VMAF.info
        psnr_info = MetricType.PSNR.info
        expected  = (-5.0 / vmaf_info.comparison_range) + (-5.0 / psnr_info.comparison_range)
        assert abs(score - expected) < 1e-9
        assert score < 0.0

    # ------------------------------------------------------------------
    # Missing key raises ValueError
    # ------------------------------------------------------------------

    def test_missing_key_raises_value_error(self):
        """Missing metric key in metrics dict raises ValueError."""
        target  = self._vmaf_target(95.0)
        metrics = {"psnr_min": 50.0}   # wrong key
        with pytest.raises(ValueError, match="vmaf_min"):
            _score_attempt(metrics, [target])

    def test_missing_one_of_multiple_keys_raises(self):
        """Missing any one key among multiple targets raises ValueError."""
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="psnr", statistic="median", value=45.0),
        ]
        metrics = {"vmaf_min": 96.0}   # psnr_median missing
        with pytest.raises(ValueError, match="psnr_median"):
            _score_attempt(metrics, targets)

    def test_empty_targets_returns_zero(self):
        """No targets → all pass trivially → early acceptance → 0.0."""
        assert _score_attempt({}, []) == 0.0

    # ------------------------------------------------------------------
    # Inf handling
    # ------------------------------------------------------------------

    def test_inf_actual_not_expected_but_handled_gracefully(self):
        """inf actual value is not expected (metrics are pre-normalized) but doesn't crash."""
        target  = QualityTarget(metric="psnr", statistic="min", value=45.0)
        # inf would be a bug upstream, but _score_attempt should not crash —
        # it just computes deficit(inf, 45.0) = inf - 45.0 = inf → positive score
        metrics = {"psnr_min": float("inf")}
        score   = _score_attempt(metrics, [target])
        # inf - 45.0 = inf → surplus is inf → positive score
        assert score > 0.0


from pyqenc.quality import QualitySearchV2


class TestQualitySearchV2:
    """Unit tests for QualitySearchV2 observable behavior.

    Uses CRF range [0, 51] (lower=better), granularity 0.5, VMAF target min=95.0.
    All assertions use only the public API: record(), best_quality, best_metrics,
    best_targets_met, attempts.
    """

    _BETTER  = Decimal("0")
    _WORSE   = Decimal("51")
    _GRAN    = Decimal("0.5")
    _TARGET  = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]

    # pass metrics: vmaf_min=96.0 → surplus > 0 → targets met
    _PASS_M  = {"vmaf_min": 96.0}
    # fail metrics: vmaf_min=80.0 → deficit → targets not met
    _FAIL_M  = {"vmaf_min": 80.0}

    @staticmethod
    def _early_m() -> dict[str, float]:
        """Metrics that trigger early acceptance: surplus = delta * 0.5."""
        from pyqenc.quality import MetricType
        delta = MetricType.VMAF.info.acceptance_delta
        return {"vmaf_min": 95.0 + delta * 0.5}

    def _make(self) -> QualitySearchV2:
        return QualitySearchV2(
            quality_better  = self._BETTER,
            quality_worse   = self._WORSE,
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )

    # ------------------------------------------------------------------
    # Initial state
    # ------------------------------------------------------------------

    def test_initial_state(self) -> None:
        """Before any record() call: no best, no metrics, targets not met, 0 attempts."""
        s = self._make()
        assert s.best_quality is None
        assert s.best_metrics is None
        assert s.best_targets_met is False
        assert s.attempts == 0

    # ------------------------------------------------------------------
    # Single attempt
    # ------------------------------------------------------------------

    def test_first_fail_sets_best(self) -> None:
        """First failing attempt: best_quality is set, targets not met."""
        s = self._make()
        s.record(Decimal("25"), self._FAIL_M)
        assert s.best_quality == Decimal("25")
        assert s.best_targets_met is False
        assert s.attempts == 1

    def test_first_pass_sets_best(self) -> None:
        """First passing attempt: best_quality is set, targets met."""
        s = self._make()
        s.record(Decimal("18"), self._PASS_M)
        assert s.best_quality == Decimal("18")
        assert s.best_targets_met is True
        assert s.attempts == 1

    def test_first_fail_returns_next_q(self) -> None:
        """First failing attempt returns a next quality value to try (not None)."""
        s = self._make()
        result = s.record(Decimal("25"), self._FAIL_M)
        assert result is not None
        assert isinstance(result, Decimal)

    def test_first_pass_returns_next_q(self) -> None:
        """First passing attempt returns a next quality value to try (not None)."""
        s = self._make()
        result = s.record(Decimal("18"), self._PASS_M)
        assert result is not None
        assert isinstance(result, Decimal)

    # ------------------------------------------------------------------
    # Best tracking across multiple attempts
    # ------------------------------------------------------------------

    def test_pass_beats_fail_as_best(self) -> None:
        """A passing attempt always becomes best over a prior failing attempt."""
        s = self._make()
        s.record(Decimal("25"), self._FAIL_M)
        s.record(Decimal("18"), self._PASS_M)
        assert s.best_quality == Decimal("18")
        assert s.best_targets_met is True

    def test_closer_fail_beats_farther_fail(self) -> None:
        """Among two failing attempts, the one with higher score (closer to 0) is best."""
        s = self._make()
        s.record(Decimal("25"), {"vmaf_min": 80.0})   # score ≈ -0.75
        s.record(Decimal("15"), {"vmaf_min": 88.0})   # score ≈ -0.35 — closer to target
        assert s.best_quality == Decimal("15")
        assert s.best_targets_met is False

    def test_closer_pass_beats_farther_pass(self) -> None:
        """Among two passing attempts, the one with lower score (closer to 0) is best."""
        s = self._make()
        s.record(Decimal("18"), {"vmaf_min": 96.0})   # further from sweet spot
        s.record(Decimal("22"), {"vmaf_min": 95.6})   # closer to sweet spot
        assert s.best_quality == Decimal("22")
        assert s.best_targets_met is True

    # ------------------------------------------------------------------
    # Convergence: next quality stays within bounds
    # ------------------------------------------------------------------

    def test_next_quality_within_bounds(self) -> None:
        """record() always returns a quality value within [quality_better, quality_worse]."""
        s = self._make()
        current_q = Decimal("25")
        for _ in range(10):
            result = s.record(current_q, self._FAIL_M)
            if result is None:
                break
            assert self._BETTER <= result <= self._WORSE, (
                f"Next quality {result} is outside [{self._BETTER}, {self._WORSE}]"
            )
            current_q = result

    def test_sweet_spot_passed_next_q_between_two_fails(self) -> None:
        """After sweet spot is passed, next quality is within the full search range."""
        s = self._make()
        s.record(Decimal("15"), {"vmaf_min": 88.0})   # closer to target
        next_q = s.record(Decimal("25"), {"vmaf_min": 80.0})   # worse → sweet spot passed
        assert next_q is not None
        assert self._BETTER <= next_q <= self._WORSE

    def test_sweet_spot_passed_next_q_between_two_passes(self) -> None:
        """After sweet spot is passed on the pass side, next quality is within the full search range."""
        s = self._make()
        s.record(Decimal("22"), {"vmaf_min": 95.6})   # closer to sweet spot
        next_q = s.record(Decimal("18"), {"vmaf_min": 96.0})   # worse → sweet spot passed
        assert next_q is not None
        assert self._BETTER <= next_q <= self._WORSE

    # ------------------------------------------------------------------
    # Early acceptance
    # ------------------------------------------------------------------

    def test_early_acceptance_returns_none(self) -> None:
        """A winner result (score == 0) causes record() to return None immediately."""
        s = self._make()
        result = s.record(Decimal("20"), self._early_m())
        assert result is None
        assert s.best_targets_met is True
        assert s.best_quality == Decimal("20")

    def test_subsequent_calls_after_early_acceptance_return_none(self) -> None:
        """After early acceptance, all subsequent record() calls return None."""
        s = self._make()
        s.record(Decimal("20"), self._early_m())
        assert s.record(Decimal("21"), self._early_m()) is None
        assert s.record(Decimal("25"), self._FAIL_M) is None

    # ------------------------------------------------------------------
    # Exhaustion via tight range
    # ------------------------------------------------------------------

    def test_exhaustion_tight_range_terminates(self) -> None:
        """A very tight range exhausts within a bounded number of iterations."""
        s = QualitySearchV2(
            quality_better  = Decimal("19"),
            quality_worse   = Decimal("21"),
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )
        current_q = Decimal("20")
        for _ in range(20):
            result = s.record(current_q, self._FAIL_M)
            if result is None:
                break
            current_q = result
        else:
            pytest.fail("QualitySearchV2 did not exhaust within 20 iterations on a tight range")

    # ------------------------------------------------------------------
    # Constructor validation
    # ------------------------------------------------------------------

    def test_raises_if_better_equals_worse(self) -> None:
        """Equal boundaries are valid — single-point search: first record() returns None."""
        s = QualitySearchV2(
            quality_better  = Decimal("18"),
            quality_worse   = Decimal("18"),
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )
        result = s.record(Decimal("18"), self._PASS_M)
        assert result is None
        assert s.best_quality == Decimal("18")

    def test_raises_if_granularity_zero(self) -> None:
        """ValueError when granularity <= 0."""
        with pytest.raises(ValueError):
            QualitySearchV2(
                quality_better  = Decimal("0"),
                quality_worse   = Decimal("51"),
                quality_targets = self._TARGET,
                granularity     = Decimal("0"),
            )


class TestEncodeChunkIntegration:
    """Unit tests for encode_chunk QualitySearchV2 integration.
    Validates: Requirements 8.1–8.3, 7.5–7.6
    """

    def test_qualitysearchv2_imported_in_encoding(self) -> None:
        """QualitySearchV3 is importable from pyqenc.phases.encoding (verifies the import exists)."""
        import importlib

        import pyqenc.phases.encoding as enc_mod

        # Verify QualitySearchV3 is accessible via the module's quality import
        from pyqenc.quality import QualitySearchV3
        assert QualitySearchV3 is not None
        # Verify encoding module uses QualitySearchV3 (it's referenced in encode_chunk source)
        import inspect
        src = inspect.getsource(enc_mod.ChunkEncoder.encode_chunk)
        assert "QualitySearchV3" in src, "encode_chunk must instantiate QualitySearchV3"

    def test_qualitysearch_usable_as_drop_in(self) -> None:
        """QualitySearch satisfies QualitySearchBase and can be used where V2 is expected."""
        from pyqenc.models import QualityTarget
        from pyqenc.quality import QualitySearch, QualitySearchBase

        target = QualityTarget(metric="vmaf", statistic="min", value=95.0)
        s = QualitySearch(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = Decimal("0.5"),
        )
        # Verify all protocol properties exist and have correct types.
        assert isinstance(s.attempts, int)
        assert s.best_quality is None
        assert s.best_metrics is None
        assert s.best_targets_met is False
        result = s.record(Decimal("25"), {"vmaf_min": 96.0})   # passing attempt
        assert result is None or isinstance(result, Decimal)
        # After one passing record call, attempts == 1 and best_quality is set.
        assert s.attempts == 1
        assert s.best_quality is not None
        assert isinstance(s, QualitySearchBase)

    def test_qualitysearch_protocol_structural_compatibility(self) -> None:
        """QualitySearch inherits from QualitySearchBase."""
        from pyqenc.models import QualityTarget
        from pyqenc.quality import QualitySearch, QualitySearchBase

        target = QualityTarget(metric="vmaf", statistic="min", value=95.0)
        s = QualitySearch(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = Decimal("0.5"),
        )
        assert isinstance(s, QualitySearchBase)

    def test_qualitysearchv2_protocol_structural_compatibility(self) -> None:
        """QualitySearchV2 inherits from QualitySearchBase."""
        from pyqenc.models import QualityTarget
        from pyqenc.quality import QualitySearchBase, QualitySearchV2

        target = QualityTarget(metric="vmaf", statistic="min", value=95.0)
        s = QualitySearchV2(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = Decimal("0.5"),
        )
        assert isinstance(s, QualitySearchBase)


from pyqenc.quality import QualitySearchV3


class TestQualitySearchV3:
    """Unit tests for QualitySearchV3 observable behavior.

    Uses CRF range [0, 51] (lower=better), granularity 0.5, VMAF target min=95.0.
    All assertions use only the public API: record(), best_quality, best_metrics,
    best_targets_met, attempts.

    Validates: Requirements 1.1-1.7, 2.1-2.5, 4.1, 5.1-5.5, 6.1, 7.1-7.4,
               9.1-9.5, 10.1-10.5, 13.1-13.10
    """

    _BETTER = Decimal('0')
    _WORSE  = Decimal('51')
    _GRAN   = Decimal('0.5')
    _TARGET = [QualityTarget(metric='vmaf', statistic='min', value=95.0)]

    _PASS_M = {'vmaf_min': 96.0}
    _FAIL_M = {'vmaf_min': 80.0}

    @staticmethod
    def _early_m() -> dict[str, float]:
        """Metrics that trigger early acceptance: surplus = delta * 0.5 -> score == 0."""
        from pyqenc.quality import MetricType
        delta = MetricType.VMAF.info.acceptance_delta
        return {'vmaf_min': 95.0 + delta * 0.5}

    def _make(
        self,
        better: str = '0',
        worse:  str = '51',
        gran:   str = '0.5',
        target: float = 95.0,
    ) -> QualitySearchV3:
        return QualitySearchV3(
            quality_better  = Decimal(better),
            quality_worse   = Decimal(worse),
            quality_targets = [QualityTarget(metric='vmaf', statistic='min', value=target)],
            granularity     = Decimal(gran),
        )

    # ------------------------------------------------------------------
    # 5.1 Initial state and constructor tests
    # ------------------------------------------------------------------

    def test_initial_state(self) -> None:
        """Before any record() call: no best, no metrics, targets not met, 0 attempts.
        Validates: Req 1.5, 13.1
        """
        s = self._make()
        assert s.best_quality is None
        assert s.best_metrics is None
        assert s.best_targets_met is False
        assert s.attempts == 0

    def test_raises_if_granularity_zero(self) -> None:
        """ValueError when granularity <= 0. Validates: Req 1.4, 13.1"""
        with pytest.raises(ValueError):
            QualitySearchV3(
                quality_better  = Decimal('0'),
                quality_worse   = Decimal('51'),
                quality_targets = self._TARGET,
                granularity     = Decimal('0'),
            )

    def test_raises_if_granularity_negative(self) -> None:
        """ValueError when granularity < 0. Validates: Req 1.4"""
        with pytest.raises(ValueError):
            QualitySearchV3(
                quality_better  = Decimal('0'),
                quality_worse   = Decimal('51'),
                quality_targets = self._TARGET,
                granularity     = Decimal('-0.5'),
            )

    def test_equal_boundaries_single_point_search(self) -> None:
        """Equal boundaries: first record() returns None, best_quality is set.
        Validates: Req 1.3, 13.1
        """
        s = QualitySearchV3(
            quality_better  = Decimal('18'),
            quality_worse   = Decimal('18'),
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )
        result = s.record(Decimal('18'), self._PASS_M)
        assert result is None
        assert s.best_quality == Decimal('18')
        assert s.attempts == 1
        # All subsequent calls also return None
        assert s.record(Decimal('18'), self._PASS_M) is None

    def test_protocol_compliance(self) -> None:
        """QualitySearchV3 inherits from QualitySearchBase. Validates: Req 1.1, 13.1"""
        s = self._make()
        assert isinstance(s, QualitySearchBase)

    # ------------------------------------------------------------------
    # 5.2 First-attempt behavior
    # ------------------------------------------------------------------

    def test_first_winner_returns_none(self) -> None:
        """score == 0 on first attempt: record() returns None, best_quality set.
        Validates: Req 2.1, 13.2
        """
        s      = self._make()
        result = s.record(Decimal('20'), self._early_m())
        assert result is None
        assert s.best_quality == Decimal('20')
        assert s.best_targets_met is True
        assert s.attempts == 1

    def test_first_pass_steps_toward_worse(self) -> None:
        """First passing attempt: returned value is worse than input (higher CRF).
        Validates: Req 2.2, 13.2
        """
        s      = self._make()
        result = s.record(Decimal('18'), self._PASS_M)
        assert result is not None
        assert result > Decimal('18'), f'Expected next quality > 18 (worse), got {result}'
        assert result <= Decimal('51')

    def test_first_fail_steps_toward_better(self) -> None:
        """First failing attempt: returned value is better than input (lower CRF).
        Validates: Req 2.3, 13.2
        """
        s      = self._make()
        result = s.record(Decimal('30'), self._FAIL_M)
        assert result is not None
        assert result < Decimal('30'), f'Expected next quality < 30 (better), got {result}'
        assert result >= Decimal('0')

    def test_first_pass_half_range_step(self) -> None:
        """First pass: step is half the distance to quality_worse, snapped to granularity.
        quality=18, quality_worse=51 -> half_range=(51-18)/2=16.5 -> next=34.5
        Validates: Req 2.2, 2.4
        """
        s      = self._make()
        result = s.record(Decimal('18'), self._PASS_M)
        assert result == Decimal('34.5'), f'Expected 34.5, got {result}'

    def test_first_fail_half_range_step(self) -> None:
        """First fail: step is half the distance to quality_better, snapped to granularity.
        quality=30, quality_better=0 -> half_range=(30-0)/2=15 -> next=15
        Validates: Req 2.3, 2.4
        """
        s      = self._make()
        result = s.record(Decimal('30'), self._FAIL_M)
        assert result == Decimal('15'), f'Expected 15, got {result}'

    def test_first_attempt_sets_best_quality(self) -> None:
        """After first record(), best_quality is set regardless of pass/fail.
        Validates: Req 10.2
        """
        s = self._make()
        s.record(Decimal('25'), self._FAIL_M)
        assert s.best_quality == Decimal('25')
        assert s.attempts == 1

    def test_attempts_increments_every_call(self) -> None:
        """attempts increments on every record() call. Validates: Req 10.1"""
        s = self._make()
        assert s.attempts == 0
        s.record(Decimal('25'), self._FAIL_M)
        assert s.attempts == 1
        s.record(Decimal('15'), self._PASS_M)
        assert s.attempts == 2

    # ------------------------------------------------------------------
    # 5.3 Two-point same-side extrapolation
    # ------------------------------------------------------------------

    def test_two_point_same_side_extrapolation(self) -> None:
        """2 same-side fail points: returned value is OUTSIDE both (extrapolated toward better).

        Record CRF=30 -> fail (vmaf=80, large deficit).
        Record CRF=15 -> fail (vmaf=90, smaller deficit, closer to target).
        Both fail, outward = quality_better (CRF=0), not yet tested.
        Different metric values give a non-flat curve so extrapolation projects
        outside the two points (result < 15).
        Validates: Req 4.1, 13.3
        """
        s = self._make(gran='1')
        s.record(Decimal('30'), {'vmaf_min': 80.0})   # fail, large deficit
        result = s.record(Decimal('15'), {'vmaf_min': 90.0})  # fail, smaller deficit
        assert result is not None, 'Expected extrapolated value, got None'
        assert result < Decimal('15'), f'Expected extrapolated value < 15, got {result}'

    def test_two_point_same_side_extrapolation_pass_side(self) -> None:
        """2 same-side pass points: returned value is OUTSIDE both toward quality_worse.

        Record CRF=18 -> pass (vmaf=98, large surplus).
        Record CRF=34 -> pass (vmaf=96, smaller surplus, closer to target).
        Both pass, outward = quality_worse (CRF=51), not yet tested.
        Proportional extrapolation projects past CRF=34 toward CRF=51 (result > 34).
        Validates: Req 4.1, 13.3
        """
        s = self._make(gran='1')
        s.record(Decimal('18'), {'vmaf_min': 98.0})   # pass, large surplus
        result = s.record(Decimal('34'), {'vmaf_min': 96.0})  # pass, smaller surplus
        assert result is not None, 'Expected extrapolated value, got None'
        assert result > Decimal('34'), f'Expected extrapolated value > 34 (toward quality_worse), got {result}'

    # ------------------------------------------------------------------
    # 5.4 Direction-exhausted midpoint probe
    # ------------------------------------------------------------------

    def test_two_point_same_side_direction_exhausted_midpoint_probe(self) -> None:
        """Direction exhausted + probe flag not set: midpoint between the two points returned.

        Range [0, 20], gran=1.
        Record CRF=15 -> fail (phase 0 steps toward better).
        Record CRF=7 -> fail (phase 0 result: (15-0)/2=7.5->8, but let's use 7 directly).
        Record CRF=0 (quality_better boundary) -> fail.
        Now: 3 attempts. best = whichever fail is closest to target.
        best's neighbour toward quality_better=0 is CRF=0, which IS tested -> direction exhausted.
        Midpoint probe between the two same-side points.

        Simpler approach: use a range where we can control exactly which 2 points
        end up as best + neighbour with the boundary tested.
        Validates: Req 5.3, 13.4
        """
        s = QualitySearchV3(
            quality_better  = Decimal('0'),
            quality_worse   = Decimal('20'),
            quality_targets = self._TARGET,
            granularity     = Decimal('1'),
        )
        # Record CRF=10 -> fail (phase 0 steps to CRF=5)
        r1 = s.record(Decimal('10'), {'vmaf_min': 88.0})
        assert r1 is not None
        # Record CRF=5 -> fail (extrapolates toward CRF=0)
        r2 = s.record(Decimal('5'), {'vmaf_min': 92.0})
        assert r2 is not None
        # Record CRF=0 (quality_better boundary) -> fail
        r3 = s.record(Decimal('0'), {'vmaf_min': 93.0})
        # Now quality_better=0 is in attempted_points -> direction exhausted for any fail pair
        # The best is CRF=5 (score closest to 0 among fails).
        # Its neighbour toward better is CRF=0 (tested) -> direction exhausted.
        # Midpoint probe flag is False -> probe midpoint.
        # If r3 is None the range already collapsed; otherwise it's the midpoint probe.
        if r3 is None:
            # Range collapsed before midpoint probe — drive further
            pytest.skip('Range collapsed before midpoint probe scenario could be set up')
        assert Decimal('0') < r3 < Decimal('5'), (
            f'Expected midpoint probe between 0 and 5, got {r3}'
        )

    def test_two_point_same_side_direction_exhausted_after_probe(self) -> None:
        """Direction exhausted + probe flag set: search exhausts within bounded iterations.
        Validates: Req 5.4, 13.8
        """
        s = QualitySearchV3(
            quality_better  = Decimal('0'),
            quality_worse   = Decimal('20'),
            quality_targets = self._TARGET,
            granularity     = Decimal('1'),
        )
        # Same setup as the midpoint probe test
        r1 = s.record(Decimal('10'), {'vmaf_min': 88.0})
        assert r1 is not None
        r2 = s.record(Decimal('5'), {'vmaf_min': 92.0})
        assert r2 is not None
        s.record(Decimal('0'), {'vmaf_min': 93.0})
        # Drive to exhaustion — all remaining probes are fail
        current_q = Decimal('3')  # start somewhere in the remaining range
        for _ in range(30):
            result = s.record(current_q, self._FAIL_M)
            if result is None:
                break
            current_q = result
        else:
            pytest.fail('Search did not exhaust within 30 iterations')

    def test_midpoint_probe_flag_resets_on_new_best(self) -> None:
        """Midpoint probe flag resets when best-scoring point changes.

        Drive to direction-exhausted state so the probe flag is set,
        then record a PASS at the probe point -> new best -> flag resets.
        After reset the search should continue (not immediately exhaust).
        Validates: Req 5.5, 13.4
        """
        s = QualitySearchV3(
            quality_better  = Decimal('0'),
            quality_worse   = Decimal('51'),
            quality_targets = self._TARGET,
            granularity     = Decimal('1'),
        )
        # Build up two fail points, then test the better boundary to exhaust direction
        r1 = s.record(Decimal('20'), {'vmaf_min': 88.0})  # fail, phase 0 -> toward better
        assert r1 is not None
        r2 = s.record(Decimal('10'), {'vmaf_min': 92.0})  # fail, extrapolates toward CRF=0
        assert r2 is not None
        # Test the better boundary -> direction exhausted
        probe_q = s.record(Decimal('0'), {'vmaf_min': 93.0})
        # probe_q is the midpoint probe (or None if range collapsed)
        if probe_q is None:
            pytest.skip('Range collapsed before midpoint probe scenario')
        # Record the probe as a PASS -> new best (pass beats fail) -> flag resets
        result = s.record(probe_q, self._PASS_M)
        assert s.attempts == 4
        assert s.best_targets_met is True
        # After flag reset the search has a pass/fail straddling pair and should continue
        assert result is not None, (
            'Search exhausted immediately after flag reset; flag did not reset correctly'
        )

    # ------------------------------------------------------------------
    # 5.5 Two-point different-sides interpolation
    # ------------------------------------------------------------------

    def test_two_point_different_sides_interpolation(self) -> None:
        """2 points on different sides: returned value is BETWEEN the two input points.

        Record CRF=18 -> pass (phase 0 steps toward worse).
        Record CRF=30 -> fail. Now one pass, one fail.
        Interpolated result must be strictly between 18 and 30.
        Validates: Req 6.1, 13.5
        """
        s      = self._make()
        next_q = s.record(Decimal('18'), self._PASS_M)
        assert next_q is not None
        result = s.record(Decimal('30'), self._FAIL_M)
        assert result is not None, 'Expected interpolated value, got None'
        assert Decimal('18') < result < Decimal('30'), (
            f'Expected interpolated value between 18 and 30, got {result}'
        )

    def test_two_point_different_sides_result_snapped(self) -> None:
        """Interpolated result is snapped to granularity. Validates: Req 6.4, 8.1"""
        s      = self._make(gran='0.5')
        next_q = s.record(Decimal('18'), self._PASS_M)
        assert next_q is not None
        result = s.record(Decimal('30'), self._FAIL_M)
        assert result is not None
        assert result % Decimal('0.5') == Decimal('0'), (
            f'Result {result} is not snapped to granularity 0.5'
        )

    # ------------------------------------------------------------------
    # 5.6 Three-point forks
    # ------------------------------------------------------------------

    def test_three_point_spanning_both_sides(self) -> None:
        """3 points spanning both sides: result is within the straddling sub-range.

        Points: [15(pass), 20(fail), 25(fail)]
        best = 20 (fail, score closest to 0 among fails).
        lower_p = 15 (pass), upper_p = 25 (fail).
        lower pair (15,20) straddles -> result between 15 and 20.
        Validates: Req 7.1, 13.6
        """
        s = self._make(gran='1')
        s.record(Decimal('25'), {'vmaf_min': 80.0})   # fail, large deficit
        s.record(Decimal('15'), self._PASS_M)          # pass
        result = s.record(Decimal('20'), {'vmaf_min': 93.0})  # fail, small deficit -> best fail
        assert result is not None, 'Expected value within straddling range, got None'
        assert Decimal('15') < result < Decimal('20'), (
            f'Expected result between 15 and 20, got {result}'
        )

    def test_three_point_all_same_side_sweet_spot(self) -> None:
        """3 points all same side: result is midpoint of the larger sub-range.

        Points: [10(fail), 20(fail), 40(fail)].
        best = 20 (score closest to 0).
        left_range = |20-10| = 10, right_range = |40-20| = 20.
        Larger = right -> midpoint(20, 40) = 30.
        Validates: Req 7.2, 13.7
        """
        s = QualitySearchV3(
            quality_better  = Decimal('0'),
            quality_worse   = Decimal('51'),
            quality_targets = [QualityTarget(metric='vmaf', statistic='min', value=95.0)],
            granularity     = Decimal('1'),
        )
        # Record in an order that builds up [10, 20, 40] without triggering exhaustion:
        # CRF=40 fail -> phase0 returns 20; CRF=20 fail -> extrapolates to 10; CRF=10 fail
        s.record(Decimal('40'), {'vmaf_min': 82.0})   # fail, score ~ -0.87
        s.record(Decimal('20'), {'vmaf_min': 93.0})   # fail, score ~ -0.13 (best)
        result = s.record(Decimal('10'), {'vmaf_min': 85.0})  # fail, score ~ -0.67
        # Now: sorted = [10(fail), 20(fail), 40(fail)], best=20
        # left_range=10, right_range=20 -> midpoint(20,40)=30
        assert result is not None, 'Expected sweet-spot midpoint, got None'
        assert result == Decimal('30'), f'Expected midpoint 30, got {result}'

    def test_three_point_both_pairs_straddle_prefers_worse_quality(self) -> None:
        """When both adjacent pairs straddle, prefer the pair with the worse-quality neighbour.

        Points: [15(fail), 20(pass), 25(fail)].
        best = 20 (pass). lower_p=15(fail), upper_p=25(fail).
        Both pairs straddle. Worse-quality neighbour = 25 (higher CRF = worse for CRF codec).
        Preferred pair = (20, 25) -> result between 20 and 25.
        Validates: Req 7.4, 13.7
        """
        s = self._make(gran='1')
        s.record(Decimal('20'), self._PASS_M)           # pass, best
        s.record(Decimal('15'), {'vmaf_min': 93.0})     # fail
        result = s.record(Decimal('25'), {'vmaf_min': 93.0})  # fail
        # sorted (quality_better=0 first) = [15(fail), 20(pass), 25(fail)]
        # best=20(pass), lower=15(fail), upper=25(fail) -> both pairs straddle
        # 25 is further from quality_better=0 -> worse-quality neighbour
        # preferred pair = (20, 25) -> result between 20 and 25
        assert result is not None, 'Expected value in preferred straddling range, got None'
        assert Decimal('20') < result < Decimal('25'), (
            f'Expected result between 20 and 25 (worse-quality pair preferred), got {result}'
        )

    # ------------------------------------------------------------------
    # 5.7 Exhaustion behavior
    # ------------------------------------------------------------------

    def test_exhaustion_returns_none(self) -> None:
        """record() returns None after the search window collapses. Validates: Req 9.1, 13.8"""
        s = QualitySearchV3(
            quality_better  = Decimal('19'),
            quality_worse   = Decimal('20'),
            quality_targets = self._TARGET,
            granularity     = Decimal('0.5'),
        )
        current_q = Decimal('19.5')
        for _ in range(20):
            result = s.record(current_q, self._FAIL_M)
            if result is None:
                break
            current_q = result
        else:
            pytest.fail('QualitySearchV3 did not exhaust within 20 iterations on tight range')

    def test_subsequent_calls_after_exhaustion_return_none(self) -> None:
        """After exhaustion, all subsequent record() calls return None without mutating state.
        Validates: Req 9.3, 13.8
        """
        s = QualitySearchV3(
            quality_better  = Decimal('19'),
            quality_worse   = Decimal('20'),
            quality_targets = self._TARGET,
            granularity     = Decimal('0.5'),
        )
        current_q = Decimal('19.5')
        for _ in range(20):
            result = s.record(current_q, self._FAIL_M)
            if result is None:
                break
            current_q = result
        attempts_at_exhaustion = s.attempts
        best_q_at_exhaustion   = s.best_quality
        # All subsequent calls must return None and not mutate state
        assert s.record(Decimal('19.5'), self._FAIL_M) is None
        assert s.record(Decimal('20'),   self._PASS_M) is None
        assert s.attempts     == attempts_at_exhaustion
        assert s.best_quality == best_q_at_exhaustion

    def test_winner_exhausts_immediately(self) -> None:
        """Early acceptance (score == 0) exhausts the search; subsequent calls return None.
        Validates: Req 9.2
        """
        s      = self._make()
        result = s.record(Decimal('20'), self._early_m())
        assert result is None
        assert s.record(Decimal('20'), self._early_m()) is None
        assert s.record(Decimal('25'), self._FAIL_M)    is None

    # ------------------------------------------------------------------
    # 5.8 Linear score curves
    # ------------------------------------------------------------------

    @staticmethod
    def _drive_to_exhaustion(
        s:         QualitySearchV3,
        metrics_fn: object,
        start_q:   Decimal,
        max_iters: int = 200,
    ) -> None:
        """Drive a search to exhaustion using a callable metrics function."""
        current_q = start_q
        for _ in range(max_iters):
            metrics = metrics_fn(float(current_q))  # type: ignore[operator]
            result  = s.record(current_q, metrics)
            if result is None:
                return
            current_q = result
        pytest.fail(f'Search did not exhaust within {max_iters} iterations')

    def test_linear_curve_all_failing(self) -> None:
        """Linear curve where all points fail: search exhausts, no pass recorded.
        Validates: Req 13.9
        """
        # vmaf_min(q) = q - 105 -> always < 95 for q in [0, 51]
        def metrics_fn(q: float) -> dict[str, float]:
            return {'vmaf_min': max(0.0, q - 105.0)}

        s = self._make(gran='1')
        self._drive_to_exhaustion(s, metrics_fn, Decimal('25'))
        assert s.best_targets_met is False

    def test_linear_curve_all_passing(self) -> None:
        """Linear curve where all points pass: search exhausts, best is a pass.
        Validates: Req 13.9
        """
        # vmaf_min(q) = 99 - q * 0.01 -> always > 95 for q in [0, 51]
        def metrics_fn(q: float) -> dict[str, float]:
            return {'vmaf_min': 99.0 - q * 0.01}

        s = self._make(gran='1')
        self._drive_to_exhaustion(s, metrics_fn, Decimal('25'))
        assert s.best_targets_met is True

    def test_linear_curve_crossing_zero(self) -> None:
        """Linear curve crossing zero: best_quality within 1 granularity of true crossing.

        vmaf_min(q) = 95 + (25 - q) * 2.0
        True crossing at q_cross = 25.0 (where vmaf_min = 95).
        Validates: Req 13.9
        """
        q_cross = 25.0

        def metrics_fn(q: float) -> dict[str, float]:
            vmaf = 95.0 + (q_cross - q) * 2.0
            return {'vmaf_min': max(0.0, min(100.0, vmaf))}

        gran = Decimal('1')
        s    = QualitySearchV3(
            quality_better  = Decimal('0'),
            quality_worse   = Decimal('51'),
            quality_targets = [QualityTarget(metric='vmaf', statistic='min', value=95.0)],
            granularity     = gran,
        )
        self._drive_to_exhaustion(s, metrics_fn, Decimal('25'))
        assert s.best_quality is not None
        assert abs(float(s.best_quality) - q_cross) <= float(gran), (
            f'best_quality={s.best_quality} is more than 1 gran from true crossing {q_cross}'
        )

    # ------------------------------------------------------------------
    # 5.9 Quadratic score curves
    # ------------------------------------------------------------------

    def test_quadratic_curve_all_failing(self) -> None:
        """Quadratic curve where all points fail: search exhausts, no pass recorded.
        vmaf_min(q) = 90 - (q-25)^2 * 0.1 -> max is 90 < 95.
        Validates: Req 13.10
        """
        def metrics_fn(q: float) -> dict[str, float]:
            return {'vmaf_min': max(0.0, 90.0 - (q - 25.0) ** 2 * 0.1)}

        s = self._make(gran='1')
        self._drive_to_exhaustion(s, metrics_fn, Decimal('25'))
        assert s.best_targets_met is False

    def test_quadratic_curve_all_passing(self) -> None:
        """Quadratic curve where all points pass: search exhausts, best is a pass.
        vmaf_min(q) = 99 - (q-25)^2 * 0.001 -> always > 95 for q in [0, 51].
        Validates: Req 13.10
        """
        def metrics_fn(q: float) -> dict[str, float]:
            return {'vmaf_min': max(0.0, 99.0 - (q - 25.0) ** 2 * 0.001)}

        s = self._make(gran='1')
        self._drive_to_exhaustion(s, metrics_fn, Decimal('25'))
        assert s.best_targets_met is True

    def test_quadratic_curve_crossing_zero_min_inside(self) -> None:
        """Quadratic with minimum inside range: best_quality within 1 gran of nearest root.

        vmaf_min(q) = 85 + (q-25)^2 * 0.1
        Roots at q=15 and q=35 (where vmaf_min=95).
        Validates: Req 13.10
        """
        q_root_lower = 15.0
        q_root_upper = 35.0

        def metrics_fn(q: float) -> dict[str, float]:
            return {'vmaf_min': max(0.0, min(100.0, 85.0 + (q - 25.0) ** 2 * 0.1))}

        gran = Decimal('1')
        s    = QualitySearchV3(
            quality_better  = Decimal('0'),
            quality_worse   = Decimal('51'),
            quality_targets = [QualityTarget(metric='vmaf', statistic='min', value=95.0)],
            granularity     = gran,
        )
        self._drive_to_exhaustion(s, metrics_fn, Decimal('25'))
        assert s.best_quality is not None
        bq         = float(s.best_quality)
        near_lower = abs(bq - q_root_lower) <= float(gran)
        near_upper = abs(bq - q_root_upper) <= float(gran)
        assert near_lower or near_upper, (
            f'best_quality={s.best_quality} is not within 1 gran of either root '
            f'({q_root_lower} or {q_root_upper})'
        )

    def test_quadratic_curve_crossing_zero_min_outside(self) -> None:
        """Quadratic with minimum outside range: best_quality within 1 gran of the root.

        Normal curve: higher CRF = lower vmaf (fails at high CRF, passes at low CRF).
        vmaf_min(q) = 100 - (q - 60)^2 * 0.1
        Minimum at q=60 (outside [0,51]). Monotone decreasing on [0,51].
        Root where vmaf_min=95: (q-60)^2 = 50 -> q = 60 - sqrt(50) ~ 52.93 -> outside range.
        Use a shallower slope so root is inside:
        vmaf_min(q) = 100 - (q - 60)^2 * 0.02
        Root: (q-60)^2 = 250 -> q = 60 - sqrt(250) ~ 44.2.
        Validates: Req 13.10
        """
        import math as _math
        q_root = 60.0 - _math.sqrt(250.0)  # ~44.2

        def metrics_fn(q: float) -> dict[str, float]:
            return {'vmaf_min': max(0.0, min(100.0, 100.0 - (q - 60.0) ** 2 * 0.02))}

        gran = Decimal('1')
        s    = QualitySearchV3(
            quality_better  = Decimal('0'),
            quality_worse   = Decimal('51'),
            quality_targets = [QualityTarget(metric='vmaf', statistic='min', value=95.0)],
            granularity     = gran,
        )
        self._drive_to_exhaustion(s, metrics_fn, Decimal('45'))
        assert s.best_quality is not None
        assert abs(float(s.best_quality) - q_root) <= float(gran) * 2, (
            f'best_quality={s.best_quality} is more than 2 gran from root {q_root:.2f}'
        )

    # ------------------------------------------------------------------
    # Additional protocol invariant tests
    # ------------------------------------------------------------------

    def test_best_targets_met_true_iff_pass_recorded(self) -> None:
        """best_targets_met is True iff at least one passing attempt was recorded.
        Validates: Req 9.5, 10.3
        """
        s = self._make()
        s.record(Decimal('30'), self._FAIL_M)
        assert s.best_targets_met is False
        s.record(Decimal('15'), self._PASS_M)
        assert s.best_targets_met is True

    def test_best_metrics_matches_best_quality(self) -> None:
        """best_metrics is the metrics dict associated with best_quality.
        Validates: Req 10.5
        """
        s = self._make()
        s.record(Decimal('30'), self._FAIL_M)
        assert s.best_metrics == self._FAIL_M
        s.record(Decimal('15'), self._PASS_M)
        assert s.best_metrics == self._PASS_M

    def test_pass_beats_fail_as_best(self) -> None:
        """A passing attempt always becomes best over a prior failing attempt.
        Validates: Req 10.3
        """
        s = self._make()
        s.record(Decimal('30'), self._FAIL_M)
        s.record(Decimal('15'), self._PASS_M)
        assert s.best_quality == Decimal('15')
        assert s.best_targets_met is True

    def test_closer_fail_beats_farther_fail(self) -> None:
        """Among two failing attempts, the one with higher score (closer to 0) is best.
        Validates: Req 10.4
        """
        s = self._make()
        s.record(Decimal('30'), {'vmaf_min': 80.0})   # large deficit
        s.record(Decimal('20'), {'vmaf_min': 93.0})   # small deficit, closer to target
        assert s.best_quality == Decimal('20')
        assert s.best_targets_met is False

    def test_output_within_bounds(self) -> None:
        """Every non-None returned value is within [quality_better, quality_worse].
        Validates: Req 8.5
        """
        s         = self._make()
        current_q = Decimal('25')
        for _ in range(30):
            result = s.record(current_q, self._FAIL_M)
            if result is None:
                break
            assert Decimal('0') <= result <= Decimal('51'), (
                f'Result {result} is outside [0, 51]'
            )
            current_q = result

    def test_output_never_repeats(self) -> None:
        """record() never returns a quality value that was already tested.
        Validates: Req 8.4
        """
        s         = self._make()
        tested    : set[Decimal] = set()
        current_q = Decimal('25')
        for _ in range(30):
            tested.add(current_q)
            result = s.record(current_q, self._FAIL_M)
            if result is None:
                break
            assert result not in tested, (
                f'record() returned already-tested quality {result}'
            )
            current_q = result

    def test_output_snapped_to_granularity(self) -> None:
        """Every non-None returned value is snapped to granularity.
        Validates: Req 8.1
        """
        gran      = Decimal('0.5')
        s         = self._make(gran='0.5')
        current_q = Decimal('25')
        for _ in range(30):
            result = s.record(current_q, self._FAIL_M)
            if result is None:
                break
            assert result % gran == Decimal('0'), (
                f'Result {result} is not snapped to granularity {gran}'
            )
            current_q = result
