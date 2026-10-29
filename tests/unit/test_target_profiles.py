"""Tests for target profiles: named bundles of quality targets.

Covers the target-profiles feature (TODO §21, 2026-10-29):

- ``--targets`` try-both dispatch: raw pairs parse first (incumbent
  grammar, unchanged behavior), a single non-raw value falls back to a
  target-profile name lookup, both failing raises one combined error
  listing the available profiles.
- ``EncodingConfig`` structural invariants: profile names exclude ``:``
  and ``,`` (the two grammars stay disjoint), profiles are non-empty,
  the default names a defined profile.
- The fixed-run guard: explicit ``--targets`` combined with a
  single-point ``-q`` is rejected at plan build; an empty ``--targets``
  is an invalid value outright (targets are the search's bar — there is
  nothing to declare with an empty one).
"""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.models import QualityTarget

_DEFAULT_CONFIG = load_app_config(default_only=True)
_DEFAULT_PROFILES = _DEFAULT_CONFIG.encoding.target_profiles

# Valid raw target strings derived from the bundled config — profile renames
# or entry edits in default_config.yaml cannot stale this alphabet.
_BUNDLED_ENTRIES = [
    entry for entries in _DEFAULT_PROFILES.values() for entry in entries
]
_raw_entry = st.sampled_from(_BUNDLED_ENTRIES)
_profile_name = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=8,
)


def _config_with(
    profiles: dict[str, list[str]] | None = None,
    default: str | None = None,
) -> AppConfig:
    """Build a default-config AppConfig with the given target-profile overrides.

    Injection goes through the supported ``model_validate`` path over a
    dumped default config; resolution happens per-call via
    ``build_encoding_plan``.
    """
    config_dict = _DEFAULT_CONFIG.model_dump()
    if profiles is not None:
        config_dict["encoding"]["target_profiles"] = profiles
    if default is not None:
        config_dict["encoding"]["default_target_profile"] = default
    return AppConfig.model_validate(config_dict)


class TestTargetProfileSelection:
    """``--targets`` accepts a profile name or raw pairs (try-both dispatch)."""

    def test_profile_name_resolves_to_its_parsed_entries(self) -> None:
        """Passing a bundled profile name by --targets resolves to that profile's targets.

        Bug condition: the profile-name fallback is never reached (raw parse
        error swallows the value), so ``--targets high`` fails instead of
        selecting the high bundle.
        """
        plan = _DEFAULT_CONFIG.build_encoding_plan(
            optimize=True, measure=True, targets=["high"],
        )
        expected = [
            QualityTarget.parse(t) for t in _DEFAULT_PROFILES["high"]
        ]
        assert plan.targets == expected

    def test_raw_pairs_resolve_unchanged(self) -> None:
        """Raw pairs keep resolving to their parse — no profile interference.

        Bug condition: the dispatch order inverts (profile lookup first) or
        raw parsing regresses, changing incumbent ``--targets`` behavior.
        """
        raw = ["vmaf-med:97.5", "vif-p10:93.0"]
        plan = _DEFAULT_CONFIG.build_encoding_plan(
            optimize=True, measure=True, targets=raw,
        )
        assert plan.targets == [QualityTarget.parse(t) for t in raw]

    def test_bare_run_uses_default_target_profile(self) -> None:
        """A run without --targets resolves default_target_profile's entries.

        Bug condition: the default profile is ignored (empty target list
        reaches the plan and a searched run fails its no-bar invariant).
        """
        plan = _DEFAULT_CONFIG.build_encoding_plan(optimize=True, measure=True)
        default = _DEFAULT_CONFIG.encoding.default_target_profile
        assert plan.targets == [
            QualityTarget.parse(t) for t in _DEFAULT_PROFILES[default]
        ]

    def test_profile_with_range_q_builds_searched_plan(self) -> None:
        """A profile combines legally with a search band -q pair.

        Bug condition: the fixed-run guard over-fires on any -q, blocking
        the legitimate narrowed-search combination.
        """
        plan = _DEFAULT_CONFIG.build_encoding_plan(
            optimize=True, measure=True,
            targets=["high"], quality=(Decimal("18"), Decimal("24")),
        )
        assert plan.fixed_quality is False
        assert len(plan.targets) > 0


class TestTargetProfileDispatchProperties:
    """Hypothesis properties of the try-both dispatch and profile round-trips."""

    @given(name=st.sampled_from(sorted(_DEFAULT_PROFILES)))
    @settings(max_examples=50)
    def test_every_bundled_profile_name_selects_that_profile(
        self, name: str,
    ) -> None:
        """For every defined profile, --targets <name> resolves its entries.

        Bug condition: the name fallback resolves a different profile (e.g.
        always the default) or drops entries from the selected bundle.
        """
        plan = _DEFAULT_CONFIG.build_encoding_plan(
            optimize=True, measure=True, targets=[name],
        )
        assert plan.targets == [
            QualityTarget.parse(t) for t in _DEFAULT_PROFILES[name]
        ]

    @given(raw=st.lists(_raw_entry, min_size=1, max_size=6))
    @settings(max_examples=100)
    def test_raw_lists_always_resolve_to_their_parse(
        self, raw: list[str],
    ) -> None:
        """Any list of valid raw entries resolves to exactly its parse.

        Bug condition: valid raw values are diverted into profile lookup or
        reordered/dropped during dispatch — incumbent --targets behavior
        would silently change.
        """
        plan = _DEFAULT_CONFIG.build_encoding_plan(
            optimize=True, measure=True, targets=list(raw),
        )
        assert plan.targets == [QualityTarget.parse(t) for t in raw]

    @given(
        profiles=st.dictionaries(
            keys=_profile_name,
            values=st.lists(_raw_entry, min_size=1, max_size=4),
            min_size=1,
            max_size=4,
        ),
    )
    @settings(max_examples=50)
    def test_custom_profiles_round_trip_and_resolve(
        self, profiles: dict[str, list[str]],
    ) -> None:
        """Arbitrary valid profile sets survive dump→validate and resolve.

        Bug condition: profiles or their raw entries are lost or mutated by
        serialisation, or the default-name wiring breaks for custom sets.
        """
        default = min(profiles)
        config = _config_with(profiles=profiles, default=default)
        dumped = config.model_dump()
        round_tripped = AppConfig.model_validate(dumped)

        assert round_tripped.encoding.target_profiles == profiles
        assert round_tripped.encoding.default_target_profile == default
        plan = round_tripped.build_encoding_plan(optimize=True, measure=True)
        assert plan.targets == [QualityTarget.parse(t) for t in profiles[default]]


class TestTargetProfileConfigValidation:
    """Structural invariants of ``encoding.target_profiles`` at config load."""

    @pytest.mark.parametrize("bad_name", ["high:med", "a,b", ""])
    def test_ambiguous_or_empty_profile_name_rejected(self, bad_name: str) -> None:
        """A profile name containing ':'/',' (or empty) fails validation.

        Bug condition: such a name could also be read as raw target pairs,
        making a --targets value ambiguous under the try-both dispatch.
        """
        profiles = {**_DEFAULT_PROFILES, bad_name: ["vmaf-med:96.0"]}
        with pytest.raises(ValidationError):
            _config_with(profiles=profiles, default="high")

    def test_empty_profile_rejected(self) -> None:
        """A profile with no entries fails validation.

        Bug condition: selecting it would silently produce a searched run
        with no bar, surfacing as a downstream plan error instead of a
        config-time failure naming the profile.
        """
        profiles = {**_DEFAULT_PROFILES, "hollow": []}
        with pytest.raises(ValidationError):
            _config_with(profiles=profiles, default="hollow")

    def test_undefined_default_profile_rejected(self) -> None:
        """A default naming no defined profile fails validation.

        Bug condition: every bare run would crash at plan build with a
        KeyError instead of the config failing loudly at load.
        """
        with pytest.raises(ValidationError):
            _config_with(default="nonexistent")


class TestTargetProfileErrors:
    """Error surfaces of the dispatch and profile parsing."""

    def test_unknown_name_lists_available_profiles(self) -> None:
        """A value that is neither raw pairs nor a profile names both failures.

        Bug condition: the raw-parse error alone is surfaced, hiding the
        profile alternative and the available names (the discovery hook).
        """
        with pytest.raises(ValueError, match="available profiles.*high"):
            _DEFAULT_CONFIG.build_encoding_plan(
                optimize=True, measure=True, targets=["hig"],
            )

    def test_two_names_rejected(self) -> None:
        """A value naming two profiles is rejected — one bar per run.

        Bug condition: multi-element non-raw values silently resolve to
        nothing or to the first name.
        """
        with pytest.raises(ValueError, match="not valid target pairs"):
            _DEFAULT_CONFIG.build_encoding_plan(
                optimize=True, measure=True, targets=["high", "medium"],
            )

    def test_invalid_profile_entry_error_names_the_profile(self) -> None:
        """An unparseable entry inside a profile errors naming that profile.

        Bug condition: the bare QualityTarget.parse error surfaces without
        the profile name — the user cannot tell which bundle is broken.
        """
        config = _config_with(
            profiles={**_DEFAULT_PROFILES, "broken": ["vmaf-med:notanumber"]},
            default="high",
        )
        with pytest.raises(ValueError, match="broken"):
            config.build_encoding_plan(optimize=True, measure=True,
                                       targets=["broken"])


class TestFixedRunTargetGuard:
    """The explicit-bar + fixed-knob contradiction guard at plan build."""

    def test_point_q_with_explicit_targets_rejected(self) -> None:
        """A single-point -q together with explicit --targets fails loudly.

        Bug condition: the run proceeds as fixed with the bar silently dead
        in the plan — a fixed run judges by its elected anchor, so the user
        believes targets steer quality while nothing consumes them.
        """
        with pytest.raises(ValueError, match="single-point"):
            _DEFAULT_CONFIG.build_encoding_plan(
                optimize=True, measure=True,
                targets=["high"], quality=(Decimal("18"), Decimal("18")),
            )

    def test_empty_targets_rejected(self) -> None:
        """An empty explicit target list is an invalid value, not a shape.

        Bug condition: an empty --targets is accepted as "no bar" — targets
        are the search's acceptance bar, and nothing (a fixed run included)
        has a use for an explicitly empty one; the user meant to omit the
        flag and use default_target_profile.
        """
        with pytest.raises(ValueError, match="not a valid bar"):
            _DEFAULT_CONFIG.build_encoding_plan(
                optimize=True, measure=True, targets=[],
            )

    def test_default_profile_with_point_q_stays_silent(self) -> None:
        """-q alone with the config-default profile is a normal fixed run.

        Bug condition: the guard fires on config state (not explicit CLI
        intent), making every default-config fixed run require --targets ''.
        """
        plan = _DEFAULT_CONFIG.build_encoding_plan(
            optimize=True, measure=True,
            quality=(Decimal("18"), Decimal("18")),
        )
        assert plan.fixed_quality is True
