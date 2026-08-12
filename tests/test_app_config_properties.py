"""Property-based tests for AppConfig and _deep_merge.

# Feature: config-refactor
**Validates: Requirements 2.2, 2.5**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from pyqenc.app_config import _deep_merge

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Scalars: booleans, integers, floats, strings (but not dicts or lists)
_scalar = st.one_of(
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
)

# Simple flat dict: keys are short strings, values are scalars
_flat_dict = st.dictionaries(
    keys=st.text(min_size=1, max_size=20),
    values=_scalar,
    min_size=1,
    max_size=10,
)


# ---------------------------------------------------------------------------
# Property 1: Deep-merge preserves base scalar when override absent
#
# For any valid base config dict and any override dict that does NOT contain
# a given scalar key, the merged result must contain the same value for that
# key as the base.
#
# Bug this catches: if _deep_merge accidentally drops base keys whose
# counterparts do not appear in the override dict, the assertion below fails.
#
# **Validates: Requirements 2.2, 2.5**
# ---------------------------------------------------------------------------

class TestDeepMergePreservesBaseScalarWhenOverrideAbsent:
    """Property 1: base scalar keys absent from override are preserved in result.

    **Validates: Requirements 2.2, 2.5**
    """

    @given(base=_flat_dict, extra=_flat_dict, scalar_value=_scalar)
    @settings(max_examples=500)
    def test_base_scalar_preserved_when_key_absent_from_override(
        self,
        base: dict,
        extra: dict,
        scalar_value: object,
    ) -> None:
        """Merged result retains the base value for any key not present in override.

        # Feature: config-refactor, Property 1
        **Validates: Requirements 2.2, 2.5**
        """
        # Use a sentinel key guaranteed not to collide with any key in `extra`
        SENTINEL_KEY = "__pbt_sentinel_key__"

        base_with_sentinel: dict = {**base, SENTINEL_KEY: scalar_value}

        # Build override that explicitly does NOT contain the sentinel key
        override: dict = {k: v for k, v in extra.items() if k != SENTINEL_KEY}

        result = _deep_merge(base_with_sentinel, override)

        assert SENTINEL_KEY in result, (
            f"_deep_merge dropped base key {SENTINEL_KEY!r} "
            f"that was absent from override.\n"
            f"  base value : {scalar_value!r}\n"
            f"  override   : {override!r}\n"
            f"  result keys: {list(result.keys())}"
        )
        assert result[SENTINEL_KEY] == scalar_value, (
            f"_deep_merge changed the base value for key {SENTINEL_KEY!r} "
            f"even though override did not contain that key.\n"
            f"  expected : {scalar_value!r}\n"
            f"  got      : {result[SENTINEL_KEY]!r}"
        )

    @given(base=_flat_dict)
    @settings(max_examples=300)
    def test_empty_override_leaves_all_base_keys_intact(self, base: dict) -> None:
        """Merging with an empty override dict returns a copy of the entire base.

        Bug: if _deep_merge returns an empty dict (or drops any key) when
        override is {}, every base key would be silently lost.

        # Feature: config-refactor, Property 1
        **Validates: Requirements 2.2, 2.5**
        """
        result = _deep_merge(base, {})

        for key, expected_value in base.items():
            assert key in result, (
                f"_deep_merge(base, {{}}) dropped base key {key!r}.\n"
                f"  base   : {base!r}\n"
                f"  result : {result!r}"
            )
            assert result[key] == expected_value, (
                f"_deep_merge(base, {{}}) changed value for key {key!r}.\n"
                f"  expected : {expected_value!r}\n"
                f"  got      : {result[key]!r}"
            )


# ---------------------------------------------------------------------------
# Property 2: Deep-merge override wins on scalar conflict
#
# For any valid base config dict and override dict that share a scalar key
# with different values, the merged result must use the override's value.
#
# Bug this catches: if _deep_merge accidentally keeps the base value when
# override has a different scalar at the same key, the assertion below fails.
#
# **Validates: Requirements 2.1**
# ---------------------------------------------------------------------------

class TestDeepMergeOverrideWinsOnScalarConflict:
    """Property 2: when base and override share a scalar key, override value wins.

    **Validates: Requirements 2.1**
    """

    @given(
        base=_flat_dict,
        extra=_flat_dict,
        base_value=_scalar,
        override_value=_scalar,
    )
    @settings(max_examples=500)
    def test_override_scalar_wins_over_base_scalar(
        self,
        base: dict,
        extra: dict,
        base_value: object,
        override_value: object,
    ) -> None:
        """Merged result uses the override value when both dicts share a scalar key.

        Bug condition: _deep_merge returns the base value for the conflicting key
        instead of the override value — i.e. override is silently ignored.

        # Feature: config-refactor, Property 2
        **Validates: Requirements 2.1**
        """
        # Ensure the two scalar values are distinct so there is an actual conflict
        # to test. When hypothesis generates equal values the property is vacuously
        # true, so we skip those cases rather than risk a false green.
        from hypothesis import assume
        assume(base_value != override_value)

        SENTINEL_KEY = "__pbt_override_sentinel__"

        base_with_sentinel: dict     = {**base,  SENTINEL_KEY: base_value}
        override_with_sentinel: dict = {**extra, SENTINEL_KEY: override_value}

        result = _deep_merge(base_with_sentinel, override_with_sentinel)

        assert SENTINEL_KEY in result, (
            f"_deep_merge dropped the sentinel key {SENTINEL_KEY!r} entirely.\n"
            f"  base value     : {base_value!r}\n"
            f"  override value : {override_value!r}\n"
            f"  result keys    : {list(result.keys())}"
        )
        assert result[SENTINEL_KEY] == override_value, (
            f"_deep_merge kept the base value for key {SENTINEL_KEY!r} "
            f"instead of using the override value.\n"
            f"  base value     : {base_value!r}\n"
            f"  override value : {override_value!r}\n"
            f"  got            : {result[SENTINEL_KEY]!r}"
        )


# ---------------------------------------------------------------------------
# Property 3: Deep-merge recursively merges nested dicts
#
# For any base config dict and override dict where both contain the same
# dict-valued key, the merged result must contain all keys from both
# sub-dicts, with the override winning on any conflicting scalar leaf.
#
# Bug this catches: if _deep_merge does a shallow merge (override's sub-dict
# completely replaces base's sub-dict instead of recursively merging),
# base-only keys in the nested dict would be silently dropped.
#
# **Validates: Requirements 2.3**
# ---------------------------------------------------------------------------


class TestDeepMergeRecursivelyMergesNestedDicts:
    """Property 3: when both dicts share a dict-valued key, the sub-dicts are
    merged recursively rather than replaced wholesale.

    **Validates: Requirements 2.3**
    """

    @given(
        base_only=_flat_dict,
        override_only=_flat_dict,
        shared_base=_flat_dict,
        shared_override=_flat_dict,
        outer_base=_flat_dict,
        outer_override=_flat_dict,
    )
    @settings(max_examples=500)
    def test_base_only_keys_in_nested_dict_are_preserved(
        self,
        base_only: dict,
        override_only: dict,
        shared_base: dict,
        shared_override: dict,
        outer_base: dict,
        outer_override: dict,
    ) -> None:
        """Keys only in the base sub-dict survive after a deep merge.

        Bug condition: shallow merge — override's sub-dict replaces base's
        sub-dict entirely, silently dropping any key that only existed in the
        base sub-dict.

        # Feature: config-refactor, Property 3
        **Validates: Requirements 2.3**
        """
        NESTED_KEY = "__pbt_nested_key__"

        # Build sub-dicts that share some keys (with different values so there
        # is a real conflict) and each has exclusive keys.
        # Ensure exclusive keys don't collide with shared keys.
        base_sub: dict = {
            **{k: v for k, v in base_only.items() if k not in shared_base},
            **shared_base,
        }
        override_sub: dict = {
            **{k: v for k, v in override_only.items() if k not in shared_base},
            **shared_override,
        }

        base_dict: dict = {
            **{k: v for k, v in outer_base.items() if k != NESTED_KEY},
            NESTED_KEY: base_sub,
        }
        override_dict: dict = {
            **{k: v for k, v in outer_override.items() if k != NESTED_KEY},
            NESTED_KEY: override_sub,
        }

        result = _deep_merge(base_dict, override_dict)

        assert NESTED_KEY in result, (
            f"_deep_merge dropped the nested key {NESTED_KEY!r} entirely.\n"
            f"  result keys: {list(result.keys())}"
        )
        result_sub = result[NESTED_KEY]
        assert isinstance(result_sub, dict), (
            f"_deep_merge replaced the nested dict at {NESTED_KEY!r} with "
            f"a non-dict value: {result_sub!r}"
        )

        # Every key exclusive to the base sub-dict must be present.
        for key in base_sub:
            if key not in override_sub:
                assert key in result_sub, (
                    f"_deep_merge dropped base-only key {key!r} from nested "
                    f"dict at {NESTED_KEY!r}.\n"
                    f"  base sub-dict     : {base_sub!r}\n"
                    f"  override sub-dict : {override_sub!r}\n"
                    f"  result sub-dict   : {result_sub!r}"
                )
                assert result_sub[key] == base_sub[key], (
                    f"_deep_merge changed the value for base-only key "
                    f"{key!r} in nested dict.\n"
                    f"  expected : {base_sub[key]!r}\n"
                    f"  got      : {result_sub[key]!r}"
                )

    @given(
        base_only=_flat_dict,
        override_only=_flat_dict,
        outer_base=_flat_dict,
        outer_override=_flat_dict,
    )
    @settings(max_examples=500)
    def test_override_only_keys_in_nested_dict_appear_in_result(
        self,
        base_only: dict,
        override_only: dict,
        outer_base: dict,
        outer_override: dict,
    ) -> None:
        """Keys only in the override sub-dict appear in the result.

        Bug condition: shallow merge that somehow only copies the base sub-dict
        and ignores the override sub-dict's new keys.

        # Feature: config-refactor, Property 3
        **Validates: Requirements 2.3**
        """
        NESTED_KEY = "__pbt_nested_key__"

        # Ensure exclusive key sets don't overlap.
        base_sub: dict = {
            k: v for k, v in base_only.items() if k not in override_only
        }
        override_sub: dict = {
            k: v for k, v in override_only.items() if k not in base_only
        }

        base_dict: dict = {
            **{k: v for k, v in outer_base.items() if k != NESTED_KEY},
            NESTED_KEY: base_sub,
        }
        override_dict: dict = {
            **{k: v for k, v in outer_override.items() if k != NESTED_KEY},
            NESTED_KEY: override_sub,
        }

        result = _deep_merge(base_dict, override_dict)

        result_sub = result.get(NESTED_KEY)
        assert isinstance(result_sub, dict), (
            f"Expected a dict at {NESTED_KEY!r}, got: {result_sub!r}"
        )

        for key, expected_value in override_sub.items():
            assert key in result_sub, (
                f"_deep_merge dropped override-only key {key!r} from nested "
                f"dict at {NESTED_KEY!r}.\n"
                f"  base sub-dict     : {base_sub!r}\n"
                f"  override sub-dict : {override_sub!r}\n"
                f"  result sub-dict   : {result_sub!r}"
            )
            assert result_sub[key] == expected_value, (
                f"_deep_merge changed the value for override-only key "
                f"{key!r} in nested dict.\n"
                f"  expected : {expected_value!r}\n"
                f"  got      : {result_sub[key]!r}"
            )

    @given(
        shared_keys=st.lists(
            st.text(min_size=1, max_size=20),
            min_size=1, max_size=5,
            unique=True,
        ),
        base_values=st.lists(_scalar, min_size=1, max_size=5),
        override_values=st.lists(_scalar, min_size=1, max_size=5),
        outer_base=_flat_dict,
        outer_override=_flat_dict,
    )
    @settings(max_examples=500)
    def test_override_wins_on_conflicting_scalar_leaf_in_nested_dict(
        self,
        shared_keys: list[str],
        base_values: list,
        override_values: list,
        outer_base: dict,
        outer_override: dict,
    ) -> None:
        """For keys present in both sub-dicts, the override value wins.

        Bug condition: deep merge respects recursive key inclusion but keeps
        the base value for conflicting scalar leaves instead of the override
        value.

        # Feature: config-refactor, Property 3
        **Validates: Requirements 2.3**
        """
        from hypothesis import assume

        NESTED_KEY = "__pbt_nested_key__"

        # Pair up shared keys with their base/override values (zip to shortest).
        pairs = list(zip(shared_keys, base_values, override_values))
        # Only test cases where at least one key has differing values.
        differing = [(k, bv, ov) for k, bv, ov in pairs if bv != ov]
        assume(len(differing) > 0)

        base_sub: dict     = {k: bv for k, bv, _ov in pairs}
        override_sub: dict = {k: ov for k, _bv, ov in pairs}

        base_dict: dict = {
            **{k: v for k, v in outer_base.items() if k != NESTED_KEY},
            NESTED_KEY: base_sub,
        }
        override_dict: dict = {
            **{k: v for k, v in outer_override.items() if k != NESTED_KEY},
            NESTED_KEY: override_sub,
        }

        result = _deep_merge(base_dict, override_dict)

        result_sub = result.get(NESTED_KEY)
        assert isinstance(result_sub, dict), (
            f"Expected a dict at {NESTED_KEY!r}, got: {result_sub!r}"
        )

        for key, _base_val, override_val in differing:
            assert result_sub.get(key) == override_val, (
                f"_deep_merge kept the base value for conflicting key "
                f"{key!r} in nested dict instead of using the override "
                f"value.\n"
                f"  base value     : {_base_val!r}\n"
                f"  override value : {override_val!r}\n"
                f"  got            : {result_sub.get(key)!r}"
            )


# ---------------------------------------------------------------------------
# Property 4: Deep-merge fully replaces lists
#
# For any base config dict containing a list-valued key and an override dict
# containing the same key with a different list, the merged result must
# contain exactly the override's list (no elements from the base list).
#
# Additionally, a list-only-in-base survives unchanged when the override
# does not contain that key.
#
# Bug this catches: if _deep_merge merges/appends lists instead of replacing
# them, base elements would leak into the result — e.g. base list [1, 2] and
# override list [3] would produce [1, 2, 3] instead of [3].
#
# **Validates: Requirements 2.4**
# ---------------------------------------------------------------------------

# A list whose elements are drawn from scalars (heterogeneous is fine)
_scalar_list = st.lists(_scalar, min_size=0, max_size=10)


class TestDeepMergeFullyReplacesLists:
    """Property 4: when both dicts share a list-valued key, the override list
    fully replaces the base list (no concatenation / append / partial merge).

    **Validates: Requirements 2.4**
    """

    @given(
        base=_flat_dict,
        extra=_flat_dict,
        base_list=_scalar_list,
        override_list=_scalar_list,
    )
    @settings(max_examples=500)
    def test_override_list_fully_replaces_base_list(
        self,
        base: dict,
        extra: dict,
        base_list: list,
        override_list: list,
    ) -> None:
        """Merged result contains only the override list, not a blend of both.

        Bug condition: _deep_merge concatenates or otherwise mixes the base
        list with the override list, causing base elements to leak into the
        merged result.

        # Feature: config-refactor, Property 4
        **Validates: Requirements 2.4**
        """
        from hypothesis import assume

        # Require the two lists to differ so there is an observable conflict.
        assume(base_list != override_list)

        SENTINEL_KEY = "__pbt_list_sentinel__"

        base_with_sentinel: dict     = {**base,  SENTINEL_KEY: base_list}
        override_with_sentinel: dict = {**extra, SENTINEL_KEY: override_list}

        result = _deep_merge(base_with_sentinel, override_with_sentinel)

        assert SENTINEL_KEY in result, (
            f"_deep_merge dropped the list key {SENTINEL_KEY!r} entirely.\n"
            f"  base list     : {base_list!r}\n"
            f"  override list : {override_list!r}\n"
            f"  result keys   : {list(result.keys())}"
        )
        assert result[SENTINEL_KEY] == override_list, (
            f"_deep_merge did not fully replace the base list with the "
            f"override list.\n"
            f"  base list     : {base_list!r}\n"
            f"  override list : {override_list!r}\n"
            f"  got           : {result[SENTINEL_KEY]!r}"
        )

    @given(
        base=_flat_dict,
        extra=_flat_dict,
        base_list=_scalar_list,
    )
    @settings(max_examples=300)
    def test_base_list_survives_when_override_lacks_key(
        self,
        base: dict,
        extra: dict,
        base_list: list,
    ) -> None:
        """A list key present only in the base dict is preserved in the result.

        Bug condition: _deep_merge drops list-valued keys from the base when
        it only processes the override's keys, losing data the override never
        intended to remove.

        # Feature: config-refactor, Property 4
        **Validates: Requirements 2.4**
        """
        SENTINEL_KEY = "__pbt_list_base_only__"

        base_with_sentinel: dict = {**base, SENTINEL_KEY: base_list}
        # Override must NOT contain the sentinel key.
        override: dict = {k: v for k, v in extra.items() if k != SENTINEL_KEY}

        result = _deep_merge(base_with_sentinel, override)

        assert SENTINEL_KEY in result, (
            f"_deep_merge dropped base list key {SENTINEL_KEY!r} even though "
            f"the override did not contain it.\n"
            f"  base list  : {base_list!r}\n"
            f"  override   : {override!r}\n"
            f"  result keys: {list(result.keys())}"
        )
        assert result[SENTINEL_KEY] == base_list, (
            f"_deep_merge changed the base list for key {SENTINEL_KEY!r} "
            f"even though the override did not contain that key.\n"
            f"  expected : {base_list!r}\n"
            f"  got      : {result[SENTINEL_KEY]!r}"
        )


# ---------------------------------------------------------------------------
# Property 8: Layer priority ordering — three-layer merge
#
# For any three config dicts `base`, `home`, `cwd` that share a scalar key
# with three different values, `_deep_merge(_deep_merge(base, home), cwd)`
# must return the `cwd` value for that key.
#
# Additionally, when `cwd` does not contain the key, `home` must win over
# `base` in the `_deep_merge(base, home)` step.
#
# Bug this catches: wrong merge order causes base to override home/cwd, or
# home to override cwd — i.e. the layered priority is inverted or partially
# wrong.
#
# **Validates: Requirements 1.4**
# ---------------------------------------------------------------------------


class TestDeepMergeLayerPriorityOrdering:
    """Property 8: three-layer merge (base → home → cwd) follows correct priority.

    **Validates: Requirements 1.4**
    """

    @given(
        base=_flat_dict,
        home=_flat_dict,
        cwd=_flat_dict,
        base_value=_scalar,
        home_value=_scalar,
        cwd_value=_scalar,
    )
    @settings(max_examples=500)
    def test_cwd_wins_in_three_layer_merge(
        self,
        base: dict,
        home: dict,
        cwd: dict,
        base_value: object,
        home_value: object,
        cwd_value: object,
    ) -> None:
        """cwd value wins when all three layers share the same scalar key.

        Bug condition: wrong merge order (e.g. merging cwd first, then home
        on top, or merging home+cwd into base) causes cwd's value to be
        silently overridden by home or base.

        # Feature: config-refactor, Property 8
        **Validates: Requirements 1.4**
        """
        from hypothesis import assume

        # All three values must be distinct so there is an unambiguous winner.
        assume(base_value != home_value)
        assume(home_value != cwd_value)
        assume(base_value != cwd_value)

        SENTINEL_KEY = "__pbt_layer_priority_cwd__"

        base_dict: dict = {**{k: v for k, v in base.items() if k != SENTINEL_KEY}, SENTINEL_KEY: base_value}
        home_dict: dict = {**{k: v for k, v in home.items() if k != SENTINEL_KEY}, SENTINEL_KEY: home_value}
        cwd_dict:  dict = {**{k: v for k, v in cwd.items()  if k != SENTINEL_KEY}, SENTINEL_KEY: cwd_value}

        # Correct layered merge: base < home < cwd
        result = _deep_merge(_deep_merge(base_dict, home_dict), cwd_dict)

        assert SENTINEL_KEY in result, (
            f"_deep_merge dropped the sentinel key {SENTINEL_KEY!r} entirely.\n"
            f"  base value : {base_value!r}\n"
            f"  home value : {home_value!r}\n"
            f"  cwd value  : {cwd_value!r}\n"
            f"  result keys: {list(result.keys())}"
        )
        assert result[SENTINEL_KEY] == cwd_value, (
            f"_deep_merge did not give cwd priority in a three-layer merge.\n"
            f"  base value : {base_value!r}\n"
            f"  home value : {home_value!r}\n"
            f"  cwd value  : {cwd_value!r}\n"
            f"  got        : {result[SENTINEL_KEY]!r}"
        )

    @given(
        base=_flat_dict,
        home=_flat_dict,
        cwd=_flat_dict,
        base_value=_scalar,
        home_value=_scalar,
    )
    @settings(max_examples=500)
    def test_home_wins_over_base_when_cwd_absent(
        self,
        base: dict,
        home: dict,
        cwd: dict,
        base_value: object,
        home_value: object,
    ) -> None:
        """home value wins over base when cwd does not contain the key.

        Bug condition: merge order is wrong so base overrides home even when
        cwd is absent — e.g. _deep_merge(home, base) instead of
        _deep_merge(base, home) in the first stage.

        # Feature: config-refactor, Property 8
        **Validates: Requirements 1.4**
        """
        from hypothesis import assume

        assume(base_value != home_value)

        SENTINEL_KEY = "__pbt_layer_priority_home__"

        base_dict: dict = {**{k: v for k, v in base.items() if k != SENTINEL_KEY}, SENTINEL_KEY: base_value}
        home_dict: dict = {**{k: v for k, v in home.items() if k != SENTINEL_KEY}, SENTINEL_KEY: home_value}
        # cwd must NOT contain the sentinel key so home is the last layer that has it.
        cwd_dict:  dict = {k: v for k, v in cwd.items() if k != SENTINEL_KEY}

        result = _deep_merge(_deep_merge(base_dict, home_dict), cwd_dict)

        assert SENTINEL_KEY in result, (
            f"_deep_merge dropped the sentinel key {SENTINEL_KEY!r} entirely.\n"
            f"  base value : {base_value!r}\n"
            f"  home value : {home_value!r}\n"
            f"  result keys: {list(result.keys())}"
        )
        assert result[SENTINEL_KEY] == home_value, (
            f"_deep_merge did not give home priority over base when cwd lacks the key.\n"
            f"  base value : {base_value!r}\n"
            f"  home value : {home_value!r}\n"
            f"  got        : {result[SENTINEL_KEY]!r}"
        )


# ---------------------------------------------------------------------------
# Property 5: AppConfig serialization round-trip
#
# For any valid AppConfig instance, serialising it to a dict via model_dump()
# and feeding that dict back through AppConfig.model_validate() must produce
# an equivalent AppConfig (same field values throughout the model tree), with
# encoding.quality_targets and encoding.strategies preserved as their raw
# string forms so that re-validation triggers resolution again correctly.
#
# Bug this catches: if model_dump() accidentally serialises resolved private
# fields (like _resolved_targets or _resolved_strategies) back as top-level
# fields, model_validate() would fail or produce incorrect results. Also
# catches cases where scalar fields lose their values during the round-trip.
#
# **Validates: Requirements 3.1, 11.1, 11.2**
# ---------------------------------------------------------------------------

from pyqenc.app_config import AppConfig, load_app_config

# Load once at module import time — avoids per-example disk I/O when each
# Hypothesis example mutates a copy of this base config.
_BASE_CONFIG: AppConfig = load_app_config()


class TestAppConfigRoundTrip:
    """Property 5: AppConfig model_dump → model_validate round-trip preserves all field values.

    Generates mutated AppConfig instances by starting from load_app_config()
    (which is always valid) and overriding simple scalar fields via Hypothesis.
    This avoids the complexity of generating valid strategy strings from scratch.

    **Validates: Requirements 3.1, 11.1, 11.2**
    """

    @given(
        optimize=st.booleans(),
        max_parallel=st.integers(min_value=1, max_value=16),
        metrics_sampling=st.integers(min_value=1, max_value=20),
        visual_hash=st.booleans(),
        strategy_selection_tolerance=st.floats(
            min_value=0.0, max_value=50.0, allow_nan=False, allow_infinity=False
        ),
        audio_codec=st.one_of(st.none(), st.sampled_from(["aac", "libopus", "flac"])),
        audio_base_bitrate=st.one_of(st.none(), st.sampled_from(["128k", "192k", "320k"])),
        extraction_include=st.one_of(st.none(), st.text(min_size=1, max_size=30)),
        extraction_exclude=st.one_of(st.none(), st.text(min_size=1, max_size=30)),
        chunking_scene_threshold=st.floats(
            min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
        ),
        chunking_min_scene_length=st.integers(min_value=1, max_value=300),
    )
    @settings(max_examples=50)
    def test_scalar_fields_survive_round_trip(
        self,
        optimize: bool,
        max_parallel: int,
        metrics_sampling: int,
        visual_hash: bool,
        strategy_selection_tolerance: float,
        audio_codec: str | None,
        audio_base_bitrate: str | None,
        extraction_include: str | None,
        extraction_exclude: str | None,
        chunking_scene_threshold: float,
        chunking_min_scene_length: int,
    ) -> None:
        """Mutated scalar fields round-trip through model_dump/model_validate unchanged.

        Bug condition: model_dump() or model_validate() silently loses or
        corrupts scalar field values (booleans, ints, floats, optional strings)
        during serialisation → re-validation. Any such loss would mean CLI
        overrides (which are direct attribute assignments) could be silently
        dropped if the config is ever re-validated.

        # Feature: config-refactor, Property 5
        **Validates: Requirements 3.1, 11.1, 11.2**
        """
        # Start from a valid base config to avoid generating invalid strategy strings.
        config = _BASE_CONFIG.model_copy(deep=True)

        # Apply Hypothesis-generated mutations to simple scalar fields.
        config.encoding.optimize                     = optimize
        config.encoding.max_parallel                 = max_parallel
        config.encoding.metrics_sampling             = metrics_sampling
        config.encoding.visual_hash                  = visual_hash
        config.encoding.strategy_selection_tolerance = strategy_selection_tolerance
        config.audio.audio_codec                     = audio_codec
        config.audio.audio_base_bitrate              = audio_base_bitrate
        config.extraction.include                    = extraction_include
        config.extraction.exclude                    = extraction_exclude
        config.chunking.scene_threshold              = chunking_scene_threshold
        config.chunking.min_scene_length             = chunking_min_scene_length

        # Perform the round-trip.
        dumped      = config.model_dump()
        round_tripped = AppConfig.model_validate(dumped)

        # Verify every mutated scalar field survived the round-trip.
        assert round_tripped.encoding.optimize == optimize, (
            f"encoding.optimize changed during round-trip.\n"
            f"  expected : {optimize!r}\n"
            f"  got      : {round_tripped.encoding.optimize!r}"
        )
        assert round_tripped.encoding.max_parallel == max_parallel, (
            f"encoding.max_parallel changed during round-trip.\n"
            f"  expected : {max_parallel!r}\n"
            f"  got      : {round_tripped.encoding.max_parallel!r}"
        )
        assert round_tripped.encoding.metrics_sampling == metrics_sampling, (
            f"encoding.metrics_sampling changed during round-trip.\n"
            f"  expected : {metrics_sampling!r}\n"
            f"  got      : {round_tripped.encoding.metrics_sampling!r}"
        )
        assert round_tripped.encoding.visual_hash == visual_hash, (
            f"encoding.visual_hash changed during round-trip.\n"
            f"  expected : {visual_hash!r}\n"
            f"  got      : {round_tripped.encoding.visual_hash!r}"
        )
        assert round_tripped.encoding.strategy_selection_tolerance == strategy_selection_tolerance, (
            f"encoding.strategy_selection_tolerance changed during round-trip.\n"
            f"  expected : {strategy_selection_tolerance!r}\n"
            f"  got      : {round_tripped.encoding.strategy_selection_tolerance!r}"
        )
        assert round_tripped.audio.audio_codec == audio_codec, (
            f"audio.audio_codec changed during round-trip.\n"
            f"  expected : {audio_codec!r}\n"
            f"  got      : {round_tripped.audio.audio_codec!r}"
        )
        assert round_tripped.audio.audio_base_bitrate == audio_base_bitrate, (
            f"audio.audio_base_bitrate changed during round-trip.\n"
            f"  expected : {audio_base_bitrate!r}\n"
            f"  got      : {round_tripped.audio.audio_base_bitrate!r}"
        )
        assert round_tripped.extraction.include == extraction_include, (
            f"extraction.include changed during round-trip.\n"
            f"  expected : {extraction_include!r}\n"
            f"  got      : {round_tripped.extraction.include!r}"
        )
        assert round_tripped.extraction.exclude == extraction_exclude, (
            f"extraction.exclude changed during round-trip.\n"
            f"  expected : {extraction_exclude!r}\n"
            f"  got      : {round_tripped.extraction.exclude!r}"
        )
        assert round_tripped.chunking.scene_threshold == chunking_scene_threshold, (
            f"chunking.scene_threshold changed during round-trip.\n"
            f"  expected : {chunking_scene_threshold!r}\n"
            f"  got      : {round_tripped.chunking.scene_threshold!r}"
        )
        assert round_tripped.chunking.min_scene_length == chunking_min_scene_length, (
            f"chunking.min_scene_length changed during round-trip.\n"
            f"  expected : {chunking_min_scene_length!r}\n"
            f"  got      : {round_tripped.chunking.min_scene_length!r}"
        )

    @given(
        quality_targets=st.lists(
            st.sampled_from([
                "vmaf-min:92.0",
                "vmaf-p05:95.0",
                "vif-med:92.0",
                "vif-min:88.0",
                "psnr-med:45.0",
                "psnr-min:42.0",
                "ssim-med:98.0",
                "ssim-min:95.0",
            ]),
            min_size=1,
            max_size=8,
            unique=True,
        ),
        strategies=st.lists(
            st.sampled_from([
                "slow+h265",
                "slow+h265-aq",
                "slow+h265-anime",
                "veryslow+h264",
                "medium+h265",
            ]),
            min_size=1,
            max_size=5,
            unique=True,
        ),
    )
    @settings(max_examples=50)
    def test_quality_targets_and_strategies_survive_as_raw_strings(
        self,
        quality_targets: list[str],
        strategies: list[str],
    ) -> None:
        """quality_targets and strategies are stored and re-serialised as raw strings.

        Bug condition: model_dump() accidentally serialises the resolved private
        caches (_resolved_targets / _resolved_strategies) as public fields, causing
        model_validate() to receive typed objects instead of raw strings — which
        would fail validation or silently bypass re-resolution.

        # Feature: config-refactor, Property 5
        **Validates: Requirements 3.1, 11.1, 11.2**
        """
        config = _BASE_CONFIG.model_copy(deep=True)
        config.encoding.quality_targets = quality_targets
        config.encoding.strategies      = strategies

        # Force re-resolution to populate the private caches.
        config.encoding._resolved_targets    = None  # noqa: SLF001
        config.encoding._resolved_strategies = None  # noqa: SLF001
        config.encoding.resolve(config.codecs, config.profiles)

        dumped        = config.model_dump()
        round_tripped = AppConfig.model_validate(dumped)

        # quality_targets and strategies must survive as their raw string lists.
        assert round_tripped.encoding.quality_targets == quality_targets, (
            f"encoding.quality_targets did not survive round-trip as raw strings.\n"
            f"  expected : {quality_targets!r}\n"
            f"  got      : {round_tripped.encoding.quality_targets!r}"
        )
        assert round_tripped.encoding.strategies == strategies, (
            f"encoding.strategies did not survive round-trip as raw strings.\n"
            f"  expected : {strategies!r}\n"
            f"  got      : {round_tripped.encoding.strategies!r}"
        )

        # The round-tripped config must also have successfully re-resolved
        # the strategies — i.e., resolution was triggered again from raw strings
        # and did not fail.
        assert round_tripped.encoding.resolved_targets is not None, (
            "encoding.resolved_targets is None after round-trip — "
            "model_validator did not trigger re-resolution."
        )
        assert round_tripped.encoding.resolved_strategies is not None, (
            "encoding.resolved_strategies is None after round-trip — "
            "model_validator did not trigger re-resolution."
        )

    def test_default_config_round_trips_without_mutation(self) -> None:
        """load_app_config() round-trips cleanly with all defaults intact.

        Bug condition: the base config from load_app_config() fails to survive
        model_dump/model_validate due to missing required fields, unexpected
        serialisation of private attributes, or other structural issues that
        only manifest at the model boundary.

        # Feature: config-refactor, Property 5
        **Validates: Requirements 3.1, 11.1, 11.2**
        """
        config        = _BASE_CONFIG
        dumped        = config.model_dump()
        round_tripped = AppConfig.model_validate(dumped)

        assert round_tripped.encoding.quality_targets   == config.encoding.quality_targets
        assert round_tripped.encoding.strategies        == config.encoding.strategies
        assert round_tripped.encoding.optimize          == config.encoding.optimize
        assert round_tripped.encoding.max_parallel      == config.encoding.max_parallel
        assert round_tripped.encoding.metrics_sampling  == config.encoding.metrics_sampling
        assert round_tripped.encoding.visual_hash       == config.encoding.visual_hash
        assert round_tripped.extraction.include         == config.extraction.include
        assert round_tripped.extraction.exclude         == config.extraction.exclude
        assert round_tripped.chunking.mode              == config.chunking.mode
        assert round_tripped.chunking.scene_threshold   == config.chunking.scene_threshold
        assert round_tripped.chunking.min_scene_length  == config.chunking.min_scene_length
        assert round_tripped.audio.convert_filter       == config.audio.convert_filter
        assert round_tripped.audio.audio_codec          == config.audio.audio_codec
        assert round_tripped.audio.audio_base_bitrate   == config.audio.audio_base_bitrate
        assert set(round_tripped.codecs.keys())         == set(config.codecs.keys())
        assert set(round_tripped.profiles.keys())       == set(config.profiles.keys())


# ---------------------------------------------------------------------------
# Property 7: Strategy resolution is deterministic and idempotent
#
# For any AppConfig with a given encoding.strategies list and a given
# codecs/profiles map, calling encoding.resolved_strategies multiple times
# must always return the same list of Strategy objects (same count, same
# order, same content) — resolution is performed exactly once and cached.
#
# Bug this catches:
#   - If resolution is not cached, repeated calls could trigger re-expansion
#     of wildcard patterns and produce a different list each time (e.g. if
#     dict iteration order is not stable, or if expansion has side-effects).
#   - If resolve() is not idempotent (i.e. calling it a second time clears
#     and re-populates the cache), the second call could produce a different
#     result if the underlying codecs/profiles have changed in the interim.
#
# **Validates: Requirements 3.3, 3.4, 10.1, 10.2**
# ---------------------------------------------------------------------------

from pyqenc.models import Strategy

# Known valid strategy patterns from the bundled default config.
# These are concrete (non-wildcard) preset+profile pairs that are guaranteed
# to resolve to exactly one Strategy each against the bundled codecs/profiles.
_VALID_STRATEGY_PATTERNS: list[str] = [
    "slow+h265",
    "slow+h265-aq",
    "slow+h265-anime",
    "veryslow+h264",
    "medium+h265",
    "medium+h265-aq",
    "slower+h265",
]

# Hypothesis strategy: pick a non-empty subset of known valid patterns (unique,
# order preserved). Using st.lists with unique=True gives us all orderings.
_strategy_subset = st.lists(
    st.sampled_from(_VALID_STRATEGY_PATTERNS),
    min_size=1,
    max_size=len(_VALID_STRATEGY_PATTERNS),
    unique=True,
)


class TestStrategyResolutionDeterministicAndIdempotent:
    """Property 7: resolved_strategies is deterministic and idempotent.

    For any AppConfig with a given encoding.strategies list, calling
    encoding.resolved_strategies multiple times returns the same list
    every time, and calling resolve() again does not change the result.

    **Validates: Requirements 3.3, 3.4, 10.1, 10.2**
    """

    @given(strategy_patterns=_strategy_subset)
    @settings(max_examples=100)
    def test_resolved_strategies_is_stable_across_multiple_reads(
        self,
        strategy_patterns: list[str],
    ) -> None:
        """Reading resolved_strategies multiple times always returns the same list.

        Bug condition: resolution is not cached — each access to
        resolved_strategies re-expands the raw pattern strings. If expansion
        has any non-determinism (e.g. dict iteration order varies between
        calls, or state mutates during expansion), successive reads would
        return lists with a different order or content.

        # Feature: config-refactor, Property 7
        **Validates: Requirements 3.3, 3.4, 10.1, 10.2**
        """
        config = _BASE_CONFIG.model_copy(deep=True)
        config.encoding.strategies = strategy_patterns

        # Force re-resolution with the new strategy list.
        config.encoding._resolved_targets    = None  # noqa: SLF001
        config.encoding._resolved_strategies = None  # noqa: SLF001
        config.encoding.resolve(config.codecs, config.profiles)

        first_read:  list[Strategy] = config.encoding.resolved_strategies
        second_read: list[Strategy] = config.encoding.resolved_strategies
        third_read:  list[Strategy] = config.encoding.resolved_strategies

        assert len(first_read) == len(second_read) == len(third_read), (
            f"resolved_strategies returned different lengths on successive reads.\n"
            f"  first  : {len(first_read)}\n"
            f"  second : {len(second_read)}\n"
            f"  third  : {len(third_read)}\n"
            f"  patterns: {strategy_patterns!r}"
        )

        for i, (s1, s2, s3) in enumerate(zip(first_read, second_read, third_read)):
            assert (s1.preset, s1.profile) == (s2.preset, s2.profile) == (s3.preset, s3.profile), (
                f"resolved_strategies[{i}] differed between reads.\n"
                f"  first  : ({s1.preset!r}, {s1.profile!r})\n"
                f"  second : ({s2.preset!r}, {s2.profile!r})\n"
                f"  third  : ({s3.preset!r}, {s3.profile!r})\n"
                f"  patterns: {strategy_patterns!r}"
            )

        # Also verify it is literally the same list object (i.e. cached, not rebuilt).
        assert first_read is second_read, (
            "resolved_strategies returned a different list object on the second "
            "read — the cache is either missing or returning a copy each time.\n"
            f"  id(first)  : {id(first_read)}\n"
            f"  id(second) : {id(second_read)}\n"
            f"  patterns: {strategy_patterns!r}"
        )

    @given(strategy_patterns=_strategy_subset)
    @settings(max_examples=100)
    def test_resolve_called_twice_does_not_change_result(
        self,
        strategy_patterns: list[str],
    ) -> None:
        """Calling resolve() a second time leaves resolved_strategies unchanged.

        Bug condition: resolve() is not idempotent — a second call clears and
        re-populates the private cache. If the re-expansion produces a
        different list (or raises an error), the second call would silently
        corrupt the resolved result.

        # Feature: config-refactor, Property 7
        **Validates: Requirements 3.3, 3.4, 10.1, 10.2**
        """
        config = _BASE_CONFIG.model_copy(deep=True)
        config.encoding.strategies = strategy_patterns

        # First resolution (fresh cache).
        config.encoding._resolved_targets    = None  # noqa: SLF001
        config.encoding._resolved_strategies = None  # noqa: SLF001
        config.encoding.resolve(config.codecs, config.profiles)

        after_first_resolve: list[Strategy] = config.encoding.resolved_strategies

        # Second call — must be a no-op because the cache is already populated.
        config.encoding.resolve(config.codecs, config.profiles)

        after_second_resolve: list[Strategy] = config.encoding.resolved_strategies

        assert len(after_first_resolve) == len(after_second_resolve), (
            f"resolve() called twice produced a different number of strategies.\n"
            f"  after 1st resolve : {len(after_first_resolve)}\n"
            f"  after 2nd resolve : {len(after_second_resolve)}\n"
            f"  patterns: {strategy_patterns!r}"
        )

        for i, (s1, s2) in enumerate(zip(after_first_resolve, after_second_resolve)):
            assert (s1.preset, s1.profile) == (s2.preset, s2.profile), (
                f"resolve() changed resolved_strategies[{i}] on the second call.\n"
                f"  after 1st : ({s1.preset!r}, {s1.profile!r})\n"
                f"  after 2nd : ({s2.preset!r}, {s2.profile!r})\n"
                f"  patterns: {strategy_patterns!r}"
            )

        # The cache object itself must be the same (not replaced by a new list).
        assert after_first_resolve is after_second_resolve, (
            "resolve() replaced the cached list object on the second call.\n"
            f"  id after 1st : {id(after_first_resolve)}\n"
            f"  id after 2nd : {id(after_second_resolve)}\n"
            f"  patterns: {strategy_patterns!r}"
        )

    @given(strategy_patterns=_strategy_subset)
    @settings(max_examples=100)
    def test_resolved_strategies_content_matches_patterns(
        self,
        strategy_patterns: list[str],
    ) -> None:
        """resolved_strategies contains exactly the strategies matching the given patterns.

        Bug condition: resolution silently expands to too many or too few
        strategies, or returns strategies from a previous resolve() call
        (stale cache after strategies list was updated).

        # Feature: config-refactor, Property 7
        **Validates: Requirements 3.3, 3.4, 10.1, 10.2**
        """
        config = _BASE_CONFIG.model_copy(deep=True)
        config.encoding.strategies = strategy_patterns

        # Clear the cache so we resolve fresh for this specific pattern list.
        config.encoding._resolved_targets    = None  # noqa: SLF001
        config.encoding._resolved_strategies = None  # noqa: SLF001
        config.encoding.resolve(config.codecs, config.profiles)

        resolved: list[Strategy] = config.encoding.resolved_strategies

        # Every resolved strategy must carry one of the (preset, profile) pairs
        # that come from the given patterns.  Since the patterns are all explicit
        # "preset+profile" strings (no wildcards), each must map to exactly one
        # Strategy.  Build the expected (preset, profile) pairs by splitting.
        expected_pairs: list[tuple[str, str]] = [
            (pat.split("+")[0], pat.split("+")[1]) for pat in strategy_patterns
        ]

        actual_pairs: list[tuple[str, str]] = [
            (s.preset, s.profile) for s in resolved
        ]

        assert actual_pairs == expected_pairs, (
            f"resolved_strategies (preset, profile) pairs do not match the "
            f"expected pairs derived from the pattern list.\n"
            f"  expected : {expected_pairs!r}\n"
            f"  got      : {actual_pairs!r}\n"
            f"  patterns : {strategy_patterns!r}"
        )

    def test_default_config_resolved_strategies_is_stable(self) -> None:
        """The base loaded config's resolved_strategies is stable across reads.

        Bug condition: load_app_config() returns a config whose
        resolved_strategies is re-computed on every access (no caching),
        which could cause subtle ordering bugs in phases that iterate
        strategies multiple times.

        # Feature: config-refactor, Property 7
        **Validates: Requirements 3.3, 3.4, 10.1, 10.2**
        """
        config = _BASE_CONFIG

        first_read:  list[Strategy] = config.encoding.resolved_strategies
        second_read: list[Strategy] = config.encoding.resolved_strategies

        assert first_read is second_read, (
            "load_app_config() returned a config whose resolved_strategies "
            "property returns a different list object on each access — "
            "the result is not cached.\n"
            f"  id(first)  : {id(first_read)}\n"
            f"  id(second) : {id(second_read)}"
        )
        assert first_read == second_read, (
            "load_app_config() resolved_strategies changed between reads.\n"
            f"  first  : {[(s.preset, s.profile) for s in first_read]!r}\n"
            f"  second : {[(s.preset, s.profile) for s in second_read]!r}"
        )


# ---------------------------------------------------------------------------
# Property 9: Strategy deduplication by (preset, profile)
#
# For any encoding.strategies list that contains duplicate (preset, profile)
# combinations — whether from explicit repetition of the same pattern or from
# overlapping wildcard patterns that expand to the same pair — resolved_strategies
# must return a list with no duplicate (preset, profile) pairs, retaining
# only the first occurrence.
#
# Bug this catches:
#   - If deduplication is missing, duplicates appear in resolved_strategies.
#   - If deduplication uses the wrong key (e.g. only preset, only profile,
#     or codec name), combinations that are actually distinct may be wrongly
#     dropped, or true duplicates may survive.
#
# **Validates: Requirements 10.3**
# ---------------------------------------------------------------------------


class TestStrategyDeduplicationByPresetProfile:
    """Property 9: resolved_strategies contains no duplicate (preset, profile) pairs.

    **Validates: Requirements 10.3**
    """

    @given(
        patterns=st.lists(
            st.sampled_from(_VALID_STRATEGY_PATTERNS),
            min_size=2,
            max_size=len(_VALID_STRATEGY_PATTERNS) * 3,
            unique=False,   # allow repeats — that is the whole point of this property
        ),
    )
    @settings(max_examples=300)
    def test_explicit_duplicate_patterns_are_deduplicated(
        self,
        patterns: list[str],
    ) -> None:
        """Repeating the same pattern string multiple times yields no duplicate pairs.

        Bug condition: if resolve() does not deduplicate, every repetition of
        a pattern produces an extra Strategy with the same (preset, profile).
        The resolved list would then contain 2× (or more) the same pair,
        causing encoding phases to process the same strategy redundantly or
        producing incorrect selection logic.

        # Feature: config-refactor, Property 9
        **Validates: Requirements 10.3**
        """
        config = _BASE_CONFIG.model_copy(deep=True)
        config.encoding.strategies = patterns

        # Force fresh resolution with the (possibly duplicate) pattern list.
        config.encoding._resolved_targets    = None  # noqa: SLF001
        config.encoding._resolved_strategies = None  # noqa: SLF001
        config.encoding.resolve(config.codecs, config.profiles)

        resolved: list[Strategy] = config.encoding.resolved_strategies
        actual_pairs: list[tuple[str, str]] = [
            (s.preset, s.profile) for s in resolved
        ]

        # No pair may appear more than once.
        seen: set[tuple[str, str]] = set()
        for pair in actual_pairs:
            assert pair not in seen, (
                f"Duplicate (preset, profile) pair found in resolved_strategies: "
                f"{pair!r}\n"
                f"  strategy patterns : {patterns!r}\n"
                f"  resolved pairs    : {actual_pairs!r}"
            )
            seen.add(pair)

    @given(
        patterns=st.lists(
            st.sampled_from(_VALID_STRATEGY_PATTERNS),
            min_size=2,
            max_size=len(_VALID_STRATEGY_PATTERNS) * 3,
            unique=False,
        ),
    )
    @settings(max_examples=300)
    def test_deduplication_retains_first_occurrence(
        self,
        patterns: list[str],
    ) -> None:
        """When duplicates exist, only the first occurrence of each (preset, profile) is kept.

        Bug condition: deduplication keeps the last occurrence instead of the
        first, or keeps some arbitrary occurrence. The spec requires first-wins
        semantics so that earlier patterns in the list have higher priority —
        a user adding a specific pattern before a wildcard must be able to rely
        on the specific pattern's entry being the one that appears in the
        resolved list (not a later, possibly different, expansion of the same
        pair via a wildcard).

        # Feature: config-refactor, Property 9
        **Validates: Requirements 10.3**
        """
        config = _BASE_CONFIG.model_copy(deep=True)
        config.encoding.strategies = patterns

        # Force fresh resolution.
        config.encoding._resolved_targets    = None  # noqa: SLF001
        config.encoding._resolved_strategies = None  # noqa: SLF001
        config.encoding.resolve(config.codecs, config.profiles)

        resolved: list[Strategy] = config.encoding.resolved_strategies

        # Build the expected first-occurrence order: iterate through the
        # expanded pairs in the same order resolution would produce them
        # (each concrete "preset+profile" pattern expands to exactly one pair),
        # keeping only the first time each pair is seen.
        all_pairs_in_order: list[tuple[str, str]] = []
        for pat in patterns:
            # Each pattern in _VALID_STRATEGY_PATTERNS is "preset+profile" —
            # split directly to get the expected pair.
            preset_part, profile_part = pat.split("+", 1)
            all_pairs_in_order.append((preset_part, profile_part))

        seen_expected: set[tuple[str, str]] = set()
        expected_pairs: list[tuple[str, str]] = []
        for pair in all_pairs_in_order:
            if pair not in seen_expected:
                seen_expected.add(pair)
                expected_pairs.append(pair)

        actual_pairs: list[tuple[str, str]] = [
            (s.preset, s.profile) for s in resolved
        ]

        assert actual_pairs == expected_pairs, (
            "resolved_strategies did not retain first-occurrence order after "
            "deduplication.\n"
            f"  strategy patterns : {patterns!r}\n"
            f"  expected pairs    : {expected_pairs!r}\n"
            f"  got pairs         : {actual_pairs!r}"
        )

    def test_wildcard_overlap_does_not_produce_duplicates(self) -> None:
        """Overlapping wildcard patterns that expand to the same pair are deduplicated.

        Specifically, "slow+h265*" (wildcard) and "slow+h265" (exact) both
        expand to include the pair ("slow", "h265").  After resolution only
        one entry for ("slow", "h265") must appear, and it must be the one
        from the first pattern ("slow+h265*").

        Bug condition: the deduplication key is wrong (e.g. only the preset,
        or only the profile, or the codec name) — the wildcard expansion and
        the exact pattern produce Strategy objects with the same preset+profile
        but the duplicate check fails to detect the collision.

        # Feature: config-refactor, Property 9
        **Validates: Requirements 10.3**
        """
        config = _BASE_CONFIG.model_copy(deep=True)

        # "slow+h265*" expands to all profiles whose name starts with "h265":
        # e.g. h265, h265-aq, h265-anime (depending on default_config.yaml).
        # "slow+h265" also expands to exactly ("slow", "h265").
        # Any profile that matches both patterns would be a duplicate.
        config.encoding.strategies = ["slow+h265*", "slow+h265", "slow+h265-aq"]

        config.encoding._resolved_targets    = None  # noqa: SLF001
        config.encoding._resolved_strategies = None  # noqa: SLF001
        config.encoding.resolve(config.codecs, config.profiles)

        resolved: list[Strategy] = config.encoding.resolved_strategies
        actual_pairs: list[tuple[str, str]] = [
            (s.preset, s.profile) for s in resolved
        ]

        # No pair may appear more than once.
        seen: set[tuple[str, str]] = set()
        for pair in actual_pairs:
            assert pair not in seen, (
                f"Duplicate (preset, profile) pair {pair!r} produced by "
                f"overlapping wildcard and exact patterns.\n"
                f"  resolved pairs: {actual_pairs!r}"
            )
            seen.add(pair)

        # ("slow", "h265") must appear because "slow+h265*" includes it.
        assert ("slow", "h265") in seen, (
            "Expected ('slow', 'h265') to be in resolved_strategies after "
            "'slow+h265*' pattern, but it was not found.\n"
            f"  resolved pairs: {actual_pairs!r}"
        )


# ---------------------------------------------------------------------------
# Property 10: Invalid AppConfig raises ValidationError
#
# For any merged config dict that violates a field constraint — including an
# unknown strategy profile or preset name, or an unrecognised quality target
# metric or statistic — calling AppConfig.model_validate() must raise a
# ValidationError that identifies the offending field or value.
#
# Bug this catches:
#   - If AppConfig silently ignores invalid quality target strings (e.g. no
#     validation in QualityTarget.parse), encoding phases would later fail
#     with cryptic errors instead of clear load-time failures.
#   - If AppConfig silently ignores unknown strategy profile/preset names,
#     phases might receive empty strategy lists or silently skip the bad entry.
#
# **Validates: Requirements 3.2, 3.5, 3.6**
# ---------------------------------------------------------------------------

from pydantic import ValidationError  # noqa: E402


class TestValidationErrorOnInvalidStrings:
    """Property 10: AppConfig.model_validate() raises ValidationError on invalid strategy/target strings.

    **Validates: Requirements 3.2, 3.5, 3.6**
    """

    def _base_dict(self) -> dict:
        """Return a model_dump() of the valid base config as a plain dict for mutation."""
        return _BASE_CONFIG.model_dump()

    def test_invalid_quality_target_metric_raises_validation_error(self) -> None:
        """AppConfig rejects a quality_targets entry with an unknown metric name.

        Bug condition: if QualityTarget.parse() does not validate the metric name,
        an invalid string like "badmetric-min:95" would be stored silently and
        encoding phases would later fail when trying to look up the metric — or
        worse, silently produce incorrect results.

        **Validates: Requirements 3.2, 3.5**
        """
        data = self._base_dict()
        # "badmetric" is not a valid MetricType value (valid: vmaf, ssim, psnr, vif)
        data["encoding"]["quality_targets"] = ["badmetric-min:95"]

        import pytest
        with pytest.raises(ValidationError) as exc_info:
            AppConfig.model_validate(data)

        # The error must mention the offending field or value
        error_str = str(exc_info.value)
        assert "badmetric" in error_str.lower() or "quality_targets" in error_str.lower() or "encoding" in error_str.lower(), (
            "ValidationError was raised but does not identify the offending "
            f"'badmetric' metric or the quality_targets field.\n"
            f"  error: {error_str}"
        )

    def test_invalid_quality_target_statistic_raises_validation_error(self) -> None:
        """AppConfig rejects a quality_targets entry with an unknown statistic name.

        Bug condition: if QualityTarget.parse() does not validate the statistic,
        a target like "vmaf-badstat:95" would be accepted silently even though
        no phase knows how to compute "badstat" — leading to KeyError or
        AttributeError deep in the pipeline instead of a clear startup failure.

        **Validates: Requirements 3.2, 3.5**
        """
        data = self._base_dict()
        # "badstat" is not among the valid stats (min, med, median, max, p05, p25, p75, p95)
        data["encoding"]["quality_targets"] = ["vmaf-badstat:95"]

        import pytest
        with pytest.raises(ValidationError) as exc_info:
            AppConfig.model_validate(data)

        error_str = str(exc_info.value)
        assert "badstat" in error_str.lower() or "quality_targets" in error_str.lower() or "encoding" in error_str.lower(), (
            "ValidationError was raised but does not identify the offending "
            f"'badstat' statistic or the quality_targets field.\n"
            f"  error: {error_str}"
        )

    def test_unknown_strategy_profile_raises_validation_error(self) -> None:
        """AppConfig rejects a strategies entry that references an unknown profile name.

        Bug condition: if strategy resolution does not check profile names,
        "slow+nonexistent-profile" would expand to an empty list (or raise an
        unrelated AttributeError) instead of clearly identifying the missing
        profile at config load time. Encoding phases would then silently have
        fewer strategies than intended.

        **Validates: Requirements 3.2, 3.6**
        """
        data = self._base_dict()
        # "nonexistent-profile" does not exist in the bundled profiles dict
        data["encoding"]["strategies"] = ["slow+nonexistent-profile"]

        import pytest
        with pytest.raises(ValidationError) as exc_info:
            AppConfig.model_validate(data)

        error_str = str(exc_info.value)
        assert (
            "nonexistent-profile" in error_str.lower()
            or "strategies" in error_str.lower()
            or "encoding" in error_str.lower()
            or "unknown profile" in error_str.lower()
        ), (
            "ValidationError was raised but does not identify the offending "
            f"'nonexistent-profile' name or the strategies field.\n"
            f"  error: {error_str}"
        )

    def test_unknown_strategy_preset_for_valid_profile_raises_validation_error(self) -> None:
        """AppConfig rejects a strategies entry whose preset is not supported by the profile's codec.

        Bug condition: if _expand_strategy_pattern does not check preset membership
        against the codec's preset list, "badpreset+h265" would silently produce
        a Strategy with an unsupported preset, causing ffmpeg to fail at encode time
        with a confusing message rather than a clear startup error.

        **Validates: Requirements 3.2, 3.6**
        """
        data = self._base_dict()
        # "badpreset" is not in h265-10bit codec's presets list
        # "h265" is a known profile that uses the h265-10bit codec
        data["encoding"]["strategies"] = ["badpreset+h265"]

        import pytest
        with pytest.raises(ValidationError) as exc_info:
            AppConfig.model_validate(data)

        error_str = str(exc_info.value)
        assert (
            "badpreset" in error_str.lower()
            or "strategies" in error_str.lower()
            or "encoding" in error_str.lower()
            or "preset" in error_str.lower()
        ), (
            "ValidationError was raised but does not identify the offending "
            f"'badpreset' preset or the strategies field.\n"
            f"  error: {error_str}"
        )


# ---------------------------------------------------------------------------
# Task 3.10: load_app_config() with only bundled default produces valid AppConfig
#
# Calling load_app_config() without any home/cwd config files present must
# return a fully valid AppConfig with non-empty resolved_strategies,
# non-empty resolved_targets, non-empty codecs and profiles dicts, and a
# non-empty audio.convert_filter string.
#
# Bug this catches:
#   - If load_app_config() fails when optional config files are absent,
#     first-run users without ~/.config/pyqenc/config.yaml would get an
#     immediate crash instead of a working default configuration.
#   - If the bundled default_config.yaml is structurally invalid or missing
#     required keys, model_validate() would raise a ValidationError and the
#     app would be unusable out of the box.
#   - If resolution is not triggered by the model_validator, resolved_strategies
#     and resolved_targets would raise RuntimeError on first access.
#
# **Validates: Requirements 1.1, 1.2, 1.3, 11.1**
# ---------------------------------------------------------------------------


class TestLoadAppConfigWithBundledDefault:
    """Task 3.10: load_app_config() with only bundled default produces valid AppConfig.

    **Validates: Requirements 1.1, 1.2, 1.3, 11.1**
    """

    def test_returns_valid_app_config_instance(self) -> None:
        """load_app_config() returns an AppConfig without raising any exception.

        Bug condition: if load_app_config() raises FileNotFoundError (missing
        bundled default), ValidationError (invalid default_config.yaml), or
        any other exception, the application is entirely broken for new users.

        **Validates: Requirements 1.1, 1.2**
        """
        config = load_app_config()
        assert isinstance(config, AppConfig), (
            f"load_app_config() did not return an AppConfig instance; "
            f"got: {type(config)!r}"
        )

    def test_resolved_targets_is_non_empty(self) -> None:
        """load_app_config() returns a config with at least one resolved quality target.

        Bug condition: if the bundled default_config.yaml has an empty
        encoding.quality_targets list, encoding phases would silently accept
        any quality and produce maximally compressed (low quality) output
        rather than reporting the missing target configuration.

        **Validates: Requirements 1.2, 1.3**
        """
        config = load_app_config()
        targets = config.encoding.resolved_targets
        assert len(targets) > 0, (
            "load_app_config() returned a config with no resolved quality targets. "
            "encoding.resolved_targets is empty — the bundled default must define "
            "at least one quality target."
        )

    def test_resolved_strategies_is_non_empty(self) -> None:
        """load_app_config() returns a config with at least one resolved strategy.

        Bug condition: if the bundled default_config.yaml has an empty
        encoding.strategies list (or all patterns expand to nothing), encoding
        phases would have no strategies to evaluate and would produce no output
        with a confusing "no strategies" error instead of a clear configuration
        failure.

        **Validates: Requirements 1.2, 1.3**
        """
        config = load_app_config()
        strategies = config.encoding.resolved_strategies
        assert len(strategies) > 0, (
            "load_app_config() returned a config with no resolved strategies. "
            "encoding.resolved_strategies is empty — the bundled default must "
            "define at least one strategy pattern that expands to valid strategies."
        )

    def test_codecs_is_non_empty(self) -> None:
        """load_app_config() returns a config with at least one codec definition.

        Bug condition: if AppConfig.codecs is empty, strategy resolution would
        always fail (no codecs to look up profiles against), but encoding phases
        might not discover this until deep into the pipeline.

        **Validates: Requirements 1.2**
        """
        config = load_app_config()
        assert len(config.codecs) > 0, (
            "load_app_config() returned a config with no codec definitions. "
            "AppConfig.codecs must not be empty — the bundled default must "
            "define at least one codec."
        )

    def test_profiles_is_non_empty(self) -> None:
        """load_app_config() returns a config with at least one profile definition.

        Bug condition: if AppConfig.profiles is empty, all strategy patterns would
        expand to empty lists (no profiles to match against), resulting in
        resolved_strategies being empty and the encoding phase silently doing nothing.

        **Validates: Requirements 1.2**
        """
        config = load_app_config()
        assert len(config.profiles) > 0, (
            "load_app_config() returned a config with no profile definitions. "
            "AppConfig.profiles must not be empty — the bundled default must "
            "define at least one profile."
        )

    def test_audio_convert_filter_is_non_empty_string(self) -> None:
        """load_app_config() returns a config with a non-empty audio.convert_filter.

        Bug condition: if audio.convert_filter is empty or None, the audio
        conversion phase would match no files and silently skip all audio
        conversion — users would get video-only output with no indication
        that audio processing was skipped.

        **Validates: Requirements 1.2, 11.1**
        """
        config = load_app_config()
        assert isinstance(config.audio.convert_filter, str), (
            f"audio.convert_filter is not a string; got: {type(config.audio.convert_filter)!r}"
        )
        assert len(config.audio.convert_filter) > 0, (
            "load_app_config() returned a config with an empty audio.convert_filter. "
            "The bundled default must define a non-empty convert_filter regex."
        )
