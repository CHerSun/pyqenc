"""Unit tests for quality evaluation and CRF adjustment."""

from decimal import Decimal

import pytest

from pyqenc.models import CodecConfig, QualityTarget
from pyqenc.quality import QualitySearch, _score_attempt

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
        with pytest.raises(ValueError):
            QualitySearch(
                quality_better  = Decimal("18"),
                quality_worse   = Decimal("18"),
                quality_targets = self._TARGET,
                granularity     = self._GRAN,
            )

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
        assert s.best_targets_met is True
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
        """One surplus > delta, one ≤ delta → positive (not early acceptance)."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=80.0),
            QualityTarget(metric="psnr", statistic="median", value=44.9),
        ]
        psnr_delta = MetricType.PSNR.info.acceptance_delta
        metrics = {
            "vmaf_min":    95.0,                    # large surplus > delta
            "psnr_median": 44.9 + psnr_delta * 0.5, # tiny surplus ≤ delta
        }
        score = _score_attempt(metrics, targets)
        assert score > 0.0

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
        """After sweet spot is passed, next quality is between the two bounding fails."""
        s = self._make()
        s.record(Decimal("15"), {"vmaf_min": 88.0})   # closer to target
        next_q = s.record(Decimal("25"), {"vmaf_min": 80.0})   # worse → sweet spot passed
        assert next_q is not None
        assert Decimal("15") < next_q < Decimal("25")

    def test_sweet_spot_passed_next_q_between_two_passes(self) -> None:
        """After sweet spot is passed on the pass side, next quality is between the two passes."""
        s = self._make()
        s.record(Decimal("22"), {"vmaf_min": 95.6})   # closer to sweet spot
        next_q = s.record(Decimal("18"), {"vmaf_min": 96.0})   # worse → sweet spot passed
        assert next_q is not None
        assert Decimal("18") < next_q < Decimal("22")

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
        assert s.record(Decimal("20"), self._PASS_M) is None
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
        """ValueError when quality_better == quality_worse."""
        with pytest.raises(ValueError):
            QualitySearchV2(
                quality_better  = Decimal("18"),
                quality_worse   = Decimal("18"),
                quality_targets = self._TARGET,
                granularity     = self._GRAN,
            )

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
        """QualitySearchV2 is importable from pyqenc.phases.encoding (verifies the import exists)."""
        import importlib

        import pyqenc.phases.encoding as enc_mod

        # Verify QualitySearchV2 is accessible via the module's quality import
        from pyqenc.quality import QualitySearchV2
        assert QualitySearchV2 is not None
        # Verify encoding module uses QualitySearchV2 (it's referenced in encode_chunk source)
        import inspect
        src = inspect.getsource(enc_mod.ChunkEncoder.encode_chunk)
        assert "QualitySearchV2" in src, "encode_chunk must instantiate QualitySearchV2"

    def test_qualitysearch_usable_as_drop_in(self) -> None:
        """QualitySearch satisfies QualitySearchProtocol and can be used where V2 is expected."""
        from pyqenc.models import QualityTarget
        from pyqenc.quality import QualitySearch, QualitySearchProtocol

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
        assert isinstance(s, QualitySearchProtocol)

    def test_qualitysearch_protocol_structural_compatibility(self) -> None:
        """QualitySearch is structurally compatible with QualitySearchProtocol (runtime check)."""
        from pyqenc.models import QualityTarget
        from pyqenc.quality import QualitySearch, QualitySearchProtocol

        target = QualityTarget(metric="vmaf", statistic="min", value=95.0)
        s = QualitySearch(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = Decimal("0.5"),
        )
        # Protocol is runtime_checkable — verify isinstance works.
        assert isinstance(s, QualitySearchProtocol)

    def test_qualitysearchv2_protocol_structural_compatibility(self) -> None:
        """QualitySearchV2 is structurally compatible with QualitySearchProtocol."""
        from pyqenc.models import QualityTarget
        from pyqenc.quality import QualitySearchProtocol, QualitySearchV2

        target = QualityTarget(metric="vmaf", statistic="min", value=95.0)
        s = QualitySearchV2(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = Decimal("0.5"),
        )
        assert isinstance(s, QualitySearchProtocol)
