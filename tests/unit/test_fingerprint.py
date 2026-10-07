"""Unit tests for the ``Fingerprint`` identity mechanism (spec 2026-10-07, Req 10–11).

One value type for every opaque identity comparison in the pipeline: the
required token, the belt size, uniform matching, and the owning entities'
derivations (``Strategy``, id sets, ``File`` sampled content). Each test names
the bug it guards.
"""
# CHerSun 2026

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from pyqenc.models import (
    CodecConfig,
    Fingerprint,
    Strategy,
    fingerprint_token,
    id_set_fingerprint,
)
from pyqenc.stream_model import File


def _codec(**overrides) -> CodecConfig:
    """A minimal valid codec config; ``overrides`` vary identity-bearing args."""
    params: dict = {
        "name": "h265-10bit",
        "default_quality": Decimal(20),
        "default_preset": "slow",
        "quality_range": (Decimal(0), Decimal(51)),
        "presets": ["slow"],
        "encoder_args": ["-c:v", "libx265", "-crf", "{quality}"],
    }
    params.update(overrides)
    return CodecConfig(**params)


def _strategy(**codec_overrides) -> Strategy:
    """A minimal resolved strategy over ``_codec``."""
    return Strategy(
        preset="slow", profile="h265", codec=_codec(**codec_overrides), profile_args=[],
    )


class TestFingerprintType:
    """The value type's construction contract (Req 10)."""

    def test_token_is_required(self) -> None:
        """``Fingerprint(None, None)`` (or a missing/empty token) is unconstructible.

        Bug: an Optional token would let a derivation failure travel as a
        carried None and compare "equal" to another unset fingerprint —
        derivations are total; an unreadable source is a fatal at the
        computing site, never a None inside the type.
        """
        with pytest.raises((ValidationError, TypeError)):
            Fingerprint(None, None)  # ty: ignore[missing-argument, too-many-positional-arguments] — rejection is the test's subject
        with pytest.raises((ValidationError, TypeError)):
            Fingerprint()  # ty: ignore[missing-argument] — rejection is the test's subject
        with pytest.raises(ValidationError):
            Fingerprint(token="")

    def test_token_is_authoritative_size_is_a_belt(self) -> None:
        """Matching: token decides; size participates only when present on
        both sides.

        Bug guarded: a size-only comparison (cheap pre-check promoted to the
        identity) would treat different content of equal length as the same
        thing; conversely a sizeless owner must never mismatch against a
        sized one.
        """
        base   = Fingerprint(token="aa", size=10)
        same   = Fingerprint(token="aa", size=10)
        other  = Fingerprint(token="bb", size=10)
        belt   = Fingerprint(token="aa", size=11)   # token equal, size contradicts
        bare   = Fingerprint(token="aa")            # sizeless owner

        assert base.matches(same)
        assert not base.matches(other)
        assert not base.matches(belt), "differing sizes on both sides = collision, not a match"
        assert base.matches(bare)
        assert bare.matches(base)

    def test_round_trip_through_yaml_dump(self) -> None:
        """The ``{token, size?}`` YAML form round-trips with size omitted when
        absent (the codebase's exclude_none convention).

        Bug: an explicit ``size: null`` key would leak into every sidecar for
        sizeless owners (Strategy, ResolvedChain) and drift from the declared
        sidecar layouts.
        """
        sized  = Fingerprint(token="ab", size=5)
        sizeless = Fingerprint(token="cd")

        assert sized.model_dump(exclude_none=True) == {"token": "ab", "size": 5}
        assert sizeless.model_dump(exclude_none=True) == {"token": "cd"}

        assert Fingerprint.model_validate(sized.model_dump()) == sized
        assert Fingerprint.model_validate(sizeless.model_dump()) == sizeless


class TestTokenDerivation:
    """The shared hash primitive and the id-set derivation (Req 10c, 11a)."""

    def test_fingerprint_token_deterministic_and_sensitive(self) -> None:
        """Same canonical form → same token; any difference → different token.

        Bug: a non-deterministic or insensitive hash would make every
        fingerprint comparison meaningless.
        """
        assert fingerprint_token("abc") == fingerprint_token("abc")
        assert fingerprint_token("abc") != fingerprint_token("abd")

    def test_id_set_fingerprint_is_order_insensitive(self) -> None:
        """Set identity does not depend on iteration order.

        Bug: the chunk-set key would spuriously invalidate winners whenever
        the chunking result arrived in a different order.
        """
        assert id_set_fingerprint(["b", "a", "c"]) == id_set_fingerprint(["c", "a", "b"])

    def test_id_set_fingerprint_size_is_the_count(self) -> None:
        """``size`` carries the cardinality (Req 11a: count + hash).

        Bug: without the count, two different sets could collide only via
        hash; the belt is the cheap pre-check.
        """
        fp = id_set_fingerprint(["a", "b"])
        assert fp.size == 2

    def test_id_set_fingerprint_detects_membership_change(self) -> None:
        """Adding/removing an id changes the fingerprint.

        Bug guarded (Req 19): a re-chunk must wipe winners via this key.
        """
        base = id_set_fingerprint(["a", "b"])
        assert base.matches(id_set_fingerprint(["a", "b"]))
        assert not base.matches(id_set_fingerprint(["a", "b", "c"]))
        assert not base.matches(id_set_fingerprint(["a"]))


class TestStrategyFingerprint:
    """The strategy's resolved-args identity (Req 10a)."""

    def test_same_strategy_same_token(self) -> None:
        """Identical resolved strategies produce identical tokens.

        Bug: drift would spuriously invalidate attempts on every run.
        """
        assert _strategy().fingerprint == _strategy().fingerprint

    def test_default_quality_is_excluded(self) -> None:
        """``codec.default_quality`` does not change the fingerprint — it is
        the search's starting point, not product identity (Req 10a).

        Bug guarded: tuning the starting CRF would demand wiping attempts
        that are still perfectly valid products of the same strategy.
        """
        base = _strategy(default_quality=Decimal(20))
        tuned = _strategy(default_quality=Decimal(28))
        assert base.fingerprint.matches(tuned.fingerprint)

    def test_encoder_args_change_is_detected(self) -> None:
        """A deep codec-args edit changes the token (Req 39: catastrophic
        per-strategy invalidation keys off this).

        Bug guarded (TODO §68/O-5): codec config edits were invisible —
        attempts of the old args would be silently reused.
        """
        base = _strategy()
        edited = _strategy(encoder_args=["-c:v", "libx265", "-crf", "{quality}", "-tune", "grain"])
        assert not base.fingerprint.matches(edited.fingerprint)

    def test_preset_change_is_detected(self) -> None:
        """The strategy's own preset participates in its identity."""
        base = _strategy()
        other = Strategy(
            preset="fast", profile="h265", codec=_codec(), profile_args=[],
        )
        assert not base.fingerprint.matches(other.fingerprint)


class TestSourceFingerprint:
    """``File.sampled_fingerprint`` — the source identity derivation (Req 30)."""

    def test_same_content_same_token_regardless_of_path(self) -> None:
        """Identity is content, not location (Req 34's locator split).

        Bug: hashing the path would fatal on a moved source instead of the
        sanctioned locator rewrite.
        """
        data = b"x" * (3 * 1024 * 1024)  # 3 MiB: all four windows land in content
        a = Path("D:/_encoding/tmp/fp_a.mkv")
        b = Path("D:/_encoding/tmp/fp_b.bin")
        for p in (a, b):
            p.write_bytes(data)
        try:
            fp_a = File.sampled_fingerprint(a)
            fp_b = File.sampled_fingerprint(b)
            assert fp_a.matches(fp_b)
            assert fp_a.size == fp_b.size == len(data)
        finally:
            a.unlink(missing_ok=True)
            b.unlink(missing_ok=True)

    def test_changed_content_changes_token(self) -> None:
        """A wholesale replacement of equal size is detected (the threat
        model's headline case: re-mux/re-download).

        Bug: keying on size only (today's job identity) accepts a replaced
        source of the same length.
        """
        size = 3 * 1024 * 1024
        p = Path("D:/_encoding/tmp/fp_c.mkv")
        try:
            p.write_bytes(b"a" * size)
            original = File.sampled_fingerprint(p)
            p.write_bytes(b"b" * size)
            replaced = File.sampled_fingerprint(p)
            assert not original.matches(replaced)
        finally:
            p.unlink(missing_ok=True)

    def test_tiny_file_derivation_is_total(self) -> None:
        """Zero- and few-byte files hash deterministically (windows overlap
        or read empty) — the derivation never fails or carries None.

        Bug: an edge-case crash or a None token on small sources would break
        the "derivations are total" contract.
        """
        p = Path("D:/_encoding/tmp/fp_d.bin")
        try:
            p.write_bytes(b"")
            empty = File.sampled_fingerprint(p)
            assert empty.size == 0
            assert empty.token

            p.write_bytes(b"abc")
            tiny = File.sampled_fingerprint(p)
            assert tiny.size == 3
            assert tiny.token
            assert not tiny.matches(empty)
        finally:
            p.unlink(missing_ok=True)
