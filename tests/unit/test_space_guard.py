"""Free-space floor behavior: size parsing, guard accounting, and the audio size bound.

The floor's contract (TODO §156 rework, 2026-11): no produced item — attempt,
audio output, merged video — may start when it would leave less than
``min_free_space`` bytes free. The guard coordinates concurrent productions by
an in-memory per-filesystem ledger (accounting, not reservation); the parse
accepts loose human sizes loosely and yields exact bytes.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pyqenc.audio.chain import ResolvedChain, estimate_output_bytes, resolve_chain
from pyqenc.audio.layout import ChannelLayout
from pyqenc.stream_model import AudioStream, AudioStreamInfo, File
from pyqenc.utils.disk_space import InsufficientSpaceError, SpaceGuard
from pyqenc.utils.sizes import parse_size

# ---------------------------------------------------------------------------
# parse_size — loose in, exact bytes out
# ---------------------------------------------------------------------------

_UNIT_MULTIPLIERS = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


class TestParseSizeHappy:
    def test_every_loose_form_yields_the_same_exact_bytes(self) -> None:
        """Unit spelling freedom is display convenience only: '2G', '2 G',
        '2g', '2GB', '2 GiB' are one value, and a bare int is bytes."""
        assert parse_size("2G") == 2 * 1024**3
        assert parse_size("2 G") == 2 * 1024**3
        assert parse_size("2g") == 2 * 1024**3
        assert parse_size("2GB") == 2 * 1024**3
        assert parse_size("2 GiB") == 2 * 1024**3
        assert parse_size("2000M") == 2000 * 1024**2
        assert parse_size("512 K") == 512 * 1024
        assert parse_size("1t") == 1024**4
        assert parse_size(1073741824) == 1073741824
        assert parse_size(0) == 0

    def test_default_config_floor_is_one_gib(self) -> None:
        """The bundled default states the floor as '1 GiB' — the loaded
        config exposes it as exact bytes."""
        from pyqenc.app_config import load_app_config

        assert load_app_config(default_only=True).min_free_space == 1024**3


class TestParseSizeProperties:
    @given(
        number=st.integers(min_value=0, max_value=10**7),
        unit=st.sampled_from(sorted(_UNIT_MULTIPLIERS)),
    )
    def test_every_unit_combination_multiplies_exactly(self, number: int, unit: str) -> None:
        """Any digits + any unit parses to digits × the binary multiplier."""
        assert parse_size(f"{number}{unit}") == number * _UNIT_MULTIPLIERS[unit]

    @given(number=st.integers(min_value=0, max_value=10**9))
    def test_bare_integers_pass_through_unchanged(self, number: int) -> None:
        assert parse_size(number) == number


class TestParseSizeErrors:
    @pytest.mark.parametrize("bad", ["", "abc", "1.5G", "-5", "1z", "1 P", "G", "1 2", 1.5, True, None])
    def test_unparsable_values_are_refused(self, bad: object) -> None:
        """A size the user states must be whole digits plus a known unit:
        fractional, negative, unknown-unit, and non-numeric inputs raise —
        pyqenc never rounds or guesses a size for them."""
        with pytest.raises(ValueError):
            parse_size(bad)  # ty: ignore[invalid-argument-type] — refusal is the test's subject


# ---------------------------------------------------------------------------
# SpaceGuard — the floor and the ledger
# ---------------------------------------------------------------------------


def _guard(free: int, min_free: int = 0) -> SpaceGuard:
    """A guard over a fake filesystem always reporting *free* bytes."""
    return SpaceGuard(min_free, probe=lambda _dir: free)


class TestSpaceGuardHappy:
    def test_admits_when_floor_survives_the_item(self, tmp_path: Path) -> None:
        """free - need >= min_free admits the item and releases the ledger
        on exit — the next check sees the filesystem unchanged."""
        guard = _guard(free=1000, min_free=100)
        with guard.ensure(tmp_path, need=500):
            pass
        with guard.ensure(tmp_path, need=900):
            pass

    def test_concurrent_needs_account_collectively(self, tmp_path: Path) -> None:
        """The bug this guards against: N workers each passing an individual
        check cannot collectively overshoot — an in-flight need stays on the
        ledger and is charged to the next item's check."""
        guard = _guard(free=1000, min_free=100)
        # Contexts enter in order: 600 reserved, then the 400-need item is
        # refused (600 outstanding + 400 needed leaves 0 < 100 floor).
        with (
            guard.ensure(tmp_path, need=600),
            pytest.raises(InsufficientSpaceError),
            guard.ensure(tmp_path, need=400),
        ):
            pass


class TestSpaceGuardProperties:
    @given(
        free=st.integers(min_value=0, max_value=10**9),
        need=st.integers(min_value=0, max_value=10**9),
        min_free=st.integers(min_value=0, max_value=10**9),
    )
    def test_refusal_is_exactly_the_broken_floor(self, free: int, need: int, min_free: int) -> None:
        """ensure raises iff free - need < min_free — the floor is the whole
        decision, with no hidden margin or rounding."""
        guard = _guard(free, min_free)
        if free - need < min_free:
            with pytest.raises(InsufficientSpaceError), guard.ensure(Path("."), need=need):
                pass
        else:
            with guard.ensure(Path("."), need=need):
                pass


class TestSpaceGuardErrors:
    def test_refusal_names_the_knob_and_keeps_the_ledger_clean(self, tmp_path: Path) -> None:
        """A refused item reserves nothing and its message teaches the
        config knob — the user reading the stop can act on it."""
        guard = _guard(free=300, min_free=100)
        with pytest.raises(InsufficientSpaceError) as excinfo, guard.ensure(tmp_path, need=500):
            pass
        assert "min_free_space" in str(excinfo.value)
        # Nothing was reserved: a fitting item still starts.
        with guard.ensure(tmp_path, need=200):
            pass

    def test_failed_item_releases_its_reservation(self, tmp_path: Path) -> None:
        """An item crashing mid-production releases its ledger entry — the
        reservation tracks production, not the item's fate."""
        guard = _guard(free=1000, min_free=0)
        with pytest.raises(RuntimeError), guard.ensure(tmp_path, need=800):
            raise RuntimeError("item crashed")
        with guard.ensure(tmp_path, need=800):
            pass


# ---------------------------------------------------------------------------
# The audio size bound
# ---------------------------------------------------------------------------


def _stream(tmp_path: Path, layout: str, duration: float | None) -> AudioStream:
    return AudioStream(
        file = File(path=tmp_path / "movie.mkv"),
        info = AudioStreamInfo(
            track_id=2, codec_name="flac",
            duration_seconds=duration,
            layout=ChannelLayout.parse(layout),
        ),
    )


def _chain(filters: list[str], bitrate: str = "96k") -> ResolvedChain:
    from pyqenc.app_config import ChainSpec
    from pyqenc.audio.filters import EncodeInstance, PeakNormInstance

    palette = {
        "peaknorm": PeakNormInstance(target_dbfs=-1.0),
        "aac": EncodeInstance(codec="aac", bitrate_per_channel=bitrate, extension="m4a"),
    }
    return resolve_chain(ChainSpec(name="t", filters=filters), palette)


class TestAudioSizeBound:
    def test_lossy_bound_is_bitrate_times_source_channels_times_duration(self, tmp_path: Path) -> None:
        """The bound uses the SOURCE channel count — never below the
        downmixed output — so a downmixing chain cannot under-bound."""
        stream = _stream(tmp_path, "5.1(side)", duration=100.0)
        bound = estimate_output_bytes(_chain(["peaknorm", "aac"]), stream)
        assert bound == 96_000 * 6 * 100 // 8

    def test_flac_bound_is_the_documented_pcm_worst_case(self, tmp_path: Path) -> None:
        """xiph documents FLAC at up to ~100% of the input PCM rate: the
        bound is 48 kHz × 4 bytes × source channels × duration."""
        stream = _stream(tmp_path, "stereo", duration=100.0)
        bound = estimate_output_bytes(_chain(["peaknorm"]), stream)
        assert bound == 48_000 * 4 * 2 * 100

    def test_unbounded_facts_degrade_to_a_plain_floor_check(self, tmp_path: Path) -> None:
        """A track without a container duration, or an unparsable bitrate,
        cannot be bounded — 0 need, never a block on a guess."""
        no_duration = _stream(tmp_path, "stereo", duration=None)
        assert estimate_output_bytes(_chain(["peaknorm", "aac"]), no_duration) == 0
        stream = _stream(tmp_path, "stereo", duration=100.0)
        broken = _chain(["peaknorm", "aac"], bitrate="?k?")
        assert estimate_output_bytes(broken, stream) == 0
