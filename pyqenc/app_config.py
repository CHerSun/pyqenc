"""AppConfig — layered, Pydantic-validated application configuration.

Loaded once at startup by deep-merging up to three YAML files in priority
order (bundled default < user home config < cwd config). CLI overrides are
applied as direct attribute assignments after loading. Volatile per-run
parameters (source, work_dir, force, etc.) are passed separately as plain
keyword arguments to ``_build_registry`` and are never stored here.
"""

import fnmatch
import logging
from decimal import Decimal
from pathlib import Path
from typing import Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from pyqenc.audio.filters import (
    PassthroughFilter,
    get_filter_class,
    registered_type_ids,
)
from pyqenc.constants import (
    CONFIG_DIR_HOME,
    CONFIG_FILENAME_CWD,
    CONFIG_FILENAME_HOME,
    FILENAME_CONTROL_CHARS,
    FILENAME_FORBIDDEN_CHARS,
)
from pyqenc.models import (
    CodecConfig,
    QualityTarget,
    Strategy,
    _coerce_decimal_pair,
    quality_alignment_error,
)
from pyqenc.utils.naming import is_filesystem_safe_name

_logger = logging.getLogger(__name__)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* on top of *base* and return a new dict.

    Merge rules:
    - **``None`` override values are dropped**: a section present in the YAML
      but empty (only comments) parses as ``None`` and must not clobber the
      bundled default — the intent is "nothing overridden", never "delete".
    - **Scalar values** (anything that is not a ``dict`` or ``list``):
      the override value wins unconditionally.
    - **Dict values**: the two sub-dicts are merged recursively using the
      same rules — keys present only in *base* are preserved, keys present
      only in *override* are added, and conflicting keys follow these same
      rules recursively.
    - **List values**: the override list wins unconditionally; the base list
      is discarded entirely (no appending or element-wise merging).

    Neither *base* nor *override* is mutated; a new dict is always returned.

    Args:
        base:     The lower-priority config dict (e.g. bundled default).
        override: The higher-priority config dict (e.g. user home config).

    Returns:
        A new dict representing the merged result.
    """
    result: dict = dict(base)

    for key, override_value in override.items():
        if override_value is None:
            continue  # empty YAML section — "nothing overridden", never a clobber
        base_value = result.get(key)

        if isinstance(base_value, dict) and isinstance(override_value, dict):
            # Both sides are dicts — recurse.
            result[key] = _deep_merge(base_value, override_value)
        else:
            # Scalar or list: override wins unconditionally.
            result[key] = override_value

    return result


class MeasurementConfig(BaseModel):
    """Measurement phase configuration.

    Attributes:
        sampling: Frame subsampling factor for quality metric computation.
                  1 = every frame; 3 = every third frame (faster, slightly
                  less precise). Applied uniformly across encoding quality
                  checks, merge verification, and the standalone measure
                  command.
    """

    sampling: int


class ProfileConfig(BaseModel):
    """Encoding profile referencing a codec and optional FFmpeg extra arguments.

    Lives inside ``AppConfig.profiles``.

    Attributes:
        codec:         Codec reference name used by this profile
                       (must match a key in ``AppConfig.codecs``).
        description:   Human-readable description of the profile (default empty string).
        extra_args:    Additional FFmpeg arguments appended when this profile is used
                       (default empty list).
        quality_range: Optional quality range override as ``(better, worse)`` in the
                       same direction convention as the codec's range.  When set, the
                       quality search is constrained to this sub-range instead of the
                       codec's full range.  Only narrowing is allowed — the profile
                       range must be a strict subset of the codec's range.  Validated
                       at ``AppConfig`` construction time.  ``None`` means use the
                       codec's range unchanged.
    """

    codec:         str
    description:   str                             = ""
    extra_args:    list[str]                       = []
    quality_range: tuple[Decimal, Decimal] | None  = None

    @field_validator("quality_range", mode="before")
    @classmethod
    def _coerce_quality_range(
        cls, v: tuple | list | None,
    ) -> tuple[Decimal, Decimal] | None:
        """Coerce ``quality_range`` elements to ``Decimal``, preserving config order."""
        if v is None:
            return None
        return _coerce_decimal_pair(v)


class ExtractionConfig(BaseModel):
    """Stream filter configuration for the extraction phase.

    Controls which streams are selected from the source container via
    regex patterns. Both filters are optional; when omitted, the
    extraction phase applies its built-in defaults.

    Attributes:
        include: Regex pattern — only streams whose identifier matches
                 are kept. ``None`` means no include filter is applied.
        exclude: Regex pattern — streams whose identifier matches are
                 dropped. ``None`` means no exclude filter is applied.
    """

    include: str | None = None
    exclude: str | None = None


class ChunkingConfig(BaseModel):
    """Chunking phase configuration controlling scene detection.

    Attributes:
        scene_threshold:   Minimum content-change score (0.0–1.0) for the scene
                           detector to declare a scene boundary. Lower values make
                           the detector more sensitive.
        min_scene_length:  Minimum number of frames a scene must contain before it
                           is eligible to be split off as a separate chunk.
    """

    scene_threshold:  float
    min_scene_length: int


class EncodingConfig(BaseModel):
    """Encoding phase configuration: quality targets, strategies, and runtime tuning.

    Raw string fields (``targets``, ``strategies``) are stored in their
    serialisable string form and resolved to typed objects once via :meth:`resolve`.
    Resolution is triggered automatically by ``AppConfig``'s ``model_validator``
    after the full config tree has been assembled.

    Attributes:
        targets:            Raw quality target strings (e.g. ``"vmaf-min:95"``).
        strategies:         Raw strategy pattern strings (e.g. ``"h265*"``).
        optimize:           Whether to run the strategy optimisation phase.
        concurrency:        Maximum concurrent encoding processes.
        optimize_tolerance: Tolerance percentage for strategy selection.
        visual_hash:        Whether to display emoji visual hash in chunk logs.
        quality_range_override: CLI-only quality-range override (``-q/--quality``)
                            as canonical ``(lower, upper)`` bounds — never part
                            of the YAML config schema; applied by ``_build_config``
                            before the strategy re-resolve. Replaces any
                            profile-level ``quality_range`` (precedence CLI >
                            profile > codec bounds). ``None`` = no override.
    """

    targets:            list[str]
    strategies:         list[str]
    optimize:           bool
    concurrency:        int
    optimize_tolerance: float
    visual_hash:        bool
    quality_range_override: tuple[Decimal, Decimal] | None = None

    @field_validator("quality_range_override", mode="before")
    @classmethod
    def _coerce_quality_range_override(
        cls, v: tuple | list | None,
    ) -> tuple[Decimal, Decimal] | None:
        """Coerce the override bounds to ``Decimal``, normalised to (lower, upper).

        Direction-agnostic storage: each codec's own convention (reversed for
        VBR-style codecs) is applied later, at ``_effective_codec``.
        """
        if v is None:
            return None
        first, second = _coerce_decimal_pair(v)
        return (first, second) if first <= second else (second, first)

    # Private resolved caches — not persisted, populated by resolve().
    _resolved_targets:    list[QualityTarget] | None = PrivateAttr(default=None)
    _resolved_strategies: list[Strategy]     | None = PrivateAttr(default=None)

    model_config = ConfigDict(validate_assignment=True)

    @model_validator(mode="after")
    def _invalidate_resolved_cache_on_mutation(self) -> Self:
        """Invalidate the resolved caches whenever a field is assigned.

        ``AppConfig`` resolves eagerly at validation time and ``resolve()`` is
        idempotent — so without this, a post-load assignment (e.g. the CLI
        applying ``--strategies`` / ``--targets`` overrides) would silently
        leave the previously resolved defaults in place. Clearing on every
        assignment is cheap: the caller re-resolves once, at most.

        Runs on initial validation too (caches are ``None`` then — no-op) and
        on assignments of non-input fields (``optimize`` etc.) — also a
        harmless no-op beyond a single re-resolve.
        """
        self._resolved_targets    = None
        self._resolved_strategies = None
        return self

    def resolve(
        self,
        codecs:   dict[str, CodecConfig],
        profiles: dict[str, ProfileConfig],
    ) -> None:
        """Resolve raw strings to typed objects and cache the results.

        Idempotent: if already resolved (private fields are not ``None``), returns
        immediately without re-resolving.

        Args:
            codecs:   Codec config map from ``AppConfig.codecs``.
            profiles: Profile config map from ``AppConfig.profiles``.

        Raises:
            ValueError: If any quality target string or strategy pattern is invalid.
        """
        if self._resolved_targets is not None and self._resolved_strategies is not None:
            return

        # --- resolve quality targets ---
        self._resolved_targets = [
            QualityTarget.parse(t) for t in self.targets
        ]

        # --- resolve strategies ---
        all_strategies: list[Strategy] = []
        for pattern in self.strategies:
            all_strategies.extend(
                _expand_strategy_pattern(
                    pattern, codecs, profiles, self.quality_range_override,
                )
            )

        # Deduplicate by (preset, profile), retaining first occurrence.
        seen: set[tuple[str, str]] = set()
        unique: list[Strategy] = []
        for strategy in all_strategies:
            key = (strategy.preset, strategy.profile)
            if key not in seen:
                seen.add(key)
                unique.append(strategy)

        self._resolved_strategies = unique

    @property
    def resolved_targets(self) -> list[QualityTarget]:
        """Resolved ``QualityTarget`` objects; populated after :meth:`resolve` is called.

        Raises:
            AssertionError: If :meth:`resolve` has not been called yet.
        """
        assert self._resolved_targets is not None, (
            "EncodingConfig.resolve() must be called before accessing "
            "resolved_targets"
        )
        return self._resolved_targets

    @property
    def resolved_strategies(self) -> list[Strategy]:
        """Resolved ``Strategy`` objects; populated after :meth:`resolve` is called.

        Raises:
            AssertionError: If :meth:`resolve` has not been called yet.
        """
        assert self._resolved_strategies is not None, (
            "EncodingConfig.resolve() must be called before accessing "
            "resolved_strategies"
        )
        return self._resolved_strategies

    @property
    def fixed_quality(self) -> bool:
        """Whether the run pins the quality knob: every resolved strategy's
        effective range is a single point (``quality_better == quality_worse``).

        Derived, never declared or persisted — a collapsed config profile and
        the ``-q`` override produce the same value here. Re-derived on every
        read from the resolved strategies.

        Raises:
            AssertionError: If :meth:`resolve` has not been called yet.
        """
        strategies = self.resolved_strategies
        if not strategies:
            return False
        return all(
            s.codec.quality_better == s.codec.quality_worse for s in strategies
        )


class FilterInstance(BaseModel):
    """A validated config-side filter definition from ``audio.filters``.

    A raw filter def in YAML is ``{type: <id>, ...params}``. This model resolves
    ``type`` against the filter-type **registry** (``pyqenc.audio.filters``) — the
    single authority on which types exist — and validates the remaining fields
    against that type's ``params_model`` (whose ``extra="forbid"`` rejects unknown
    params). No filter-type ids are enumerated here; adding a filter
    type never touches this model.

    The runnable filter is constructed from an instance via
    ``get_filter_class(inst.type)(inst.params)``.

    Attributes:
        type:   The filter-type id, guaranteed present in the registry.
        params: The validated parameter model instance for that type.
    """

    model_config = ConfigDict(frozen=True)

    type:   str
    # ``SerializeAsAny``: ``params`` must serialise by its runtime concrete
    # type — the declared ``BaseModel`` would dump ``{}`` and silently drop
    # every filter param.
    params: SerializeAsAny[BaseModel]

    @model_validator(mode="before")
    @classmethod
    def _resolve_and_validate(cls, data: object) -> object:
        """Resolve ``type`` via the registry and validate the remaining params.

        Args:
            data: The raw filter def — a mapping ``{type: <id>, ...params}``.

        Returns:
            A dict ``{"type": <id>, "params": <validated model>}`` ready for
            field validation.

        Raises:
            ValueError: If ``type`` is missing, unknown to the registry (message
                lists the registered ids), or the params fail the type's model.
        """
        if not isinstance(data, dict):
            return data
        # Already in resolved form (e.g. model_dump round-trip) — pass through.
        if "params" in data and "type" in data and not (set(data) - {"type", "params"}):
            return data

        raw = dict(data)
        type_id = raw.pop("type", None)
        if type_id is None:
            raise ValueError("Filter definition is missing the required 'type' field.")
        try:
            filter_cls = get_filter_class(type_id)
        except KeyError:
            raise ValueError(
                f"Unknown filter type {type_id!r}. "
                f"Registered filter types: {registered_type_ids()}."
            ) from None
        # Validate the remaining fields against the type's param model.
        # extra="forbid" on the param model rejects any parameter not valid for the type.
        params = filter_cls.params_model(**raw)
        return {"type": type_id, "params": params}


class ChainSpec(BaseModel):
    """A named, ordered chain of filter references from ``audio.chains``.

    Applied to N matched tracks a chain produces exactly N outputs. Referential
    integrity (each name resolves in the palette), name uniqueness across the
    list, passthrough-alone, and filename-safe name are all enforced by
    :class:`AudioConfig`'s model-validator so failures surface as a
    ``ValidationError`` at config load.

    Attributes:
        name:    Unique, filesystem-safe chain name; the ``chain=<name>`` output
                 filename suffix.
        filters: Ordered list of filter names, each referencing an
                 ``audio.filters`` key.
    """

    name:    str
    filters: list[str]


class SelectEntry(BaseModel):
    """One entry of the ``audio.select`` track-selection tree.

    Attributes:
        for_:    Regex gate (YAML key ``for``) matched against a track's
                 conventional string; matching tracks are candidates.
        exclude: Optional regex; candidate tracks matching it are dropped.
        prefer:  Optional ordered regex tiers; the first tier matching ≥1
                 candidate wins and contributes all its matches, else the
                 implicit fallback contributes all candidates.
    """

    model_config = ConfigDict(populate_by_name=True)

    for_:    str             = Field(alias="for")
    exclude: str | None      = None
    prefer:  list[str]       = []


class AudioConfig(BaseModel):
    """Audio processing configuration: filter palette, chains, and track select.

    The three pieces are:

    - ``filters`` — a **palette** of named, reusable filter definitions, each
      validated through the filter-type registry (:class:`FilterInstance`).
      Uses **dict-merge** across config layers, so a later layer may add or tune
      a named filter without redefining the whole palette.
    - ``chains`` — ordered recipes referencing filter names. Uses **list-replace**
      across layers.
    - ``select`` — an ordered track-selection tree; empty means "all extracted
      tracks". Uses **list-replace** across layers.

    Cross-field integrity (chain→filter references, name uniqueness, passthrough
    alone, filename-safe names) is enforced by :meth:`_validate_chains` and
    surfaces as a ``ValidationError`` at config load.

    Attributes:
        filters: Palette: filter name → validated :class:`FilterInstance`.
        chains:  Ordered list of :class:`ChainSpec` recipes.
        select:  Ordered list of :class:`SelectEntry`; empty = all tracks.
    """

    filters: dict[str, FilterInstance]
    chains:  list[ChainSpec]
    select:  list[SelectEntry]         = []

    @model_validator(mode="after")
    def _validate_chains(self) -> Self:
        """Enforce chain integrity: references, uniqueness, passthrough-alone, safe names.

        Returns:
            ``self`` — required by Pydantic ``mode='after'`` validators.

        Raises:
            ValueError: If a chain references an unknown filter, two chains share
                a name, a ``passthrough`` filter is not alone in its chain, or a
                chain name contains filesystem-unsafe characters. Pydantic wraps
                this as a ``ValidationError`` at load.
        """
        seen_names: set[str] = set()
        for chain in self.chains:
            if chain.name in seen_names:
                raise ValueError(f"Duplicate chain name {chain.name!r} in audio.chains.")
            seen_names.add(chain.name)

            _validate_chain_name_filesystem_safe(chain.name)

            for filter_name in chain.filters:
                if filter_name not in self.filters:
                    raise ValueError(
                        f"Chain {chain.name!r} references unknown filter "
                        f"{filter_name!r}. Defined filters: {sorted(self.filters)}."
                    )

            has_passthrough = any(
                self.filters[name].type == PassthroughFilter.type_id
                for name in chain.filters
            )
            if has_passthrough and len(chain.filters) != 1:
                raise ValueError(
                    f"Chain {chain.name!r} combines {PassthroughFilter.type_id!r} with "
                    f"other filters; a passthrough filter must be the only filter in its chain."
                )
        return self


def _validate_chain_name_filesystem_safe(name: str) -> None:
    """Raise ``ValueError`` if *name* is unsafe for use in an output filename.

    Rejects empty/whitespace-only names and any filesystem-unsafe character
    (via :func:`pyqenc.utils.naming.is_filesystem_safe_name`). Chain names
    form the ``chain=<name>`` filename suffix, so they must be
    filesystem-safe.

    Args:
        name: The chain name to validate.

    Raises:
        ValueError: If *name* is empty/blank or contains a forbidden character.
    """
    if not name or not name.strip():
        raise ValueError("Chain name must be a non-empty, non-blank string.")
    if not is_filesystem_safe_name(name):
        bad = sorted(set(name) & (FILENAME_FORBIDDEN_CHARS | FILENAME_CONTROL_CHARS))
        raise ValueError(
            f"Chain name {name!r} contains filesystem-unsafe character(s): "
            f"{bad}. Avoid {sorted(FILENAME_FORBIDDEN_CHARS)} and control characters."
        )


class AppConfig(BaseModel):
    """Top-level application configuration — layered-loaded, Pydantic-validated.

    Assembled once at startup from up to three YAML files merged in ascending
    priority order (bundled default → user home config → cwd config), then
    optionally mutated by CLI overrides (direct attribute assignment) before
    being treated as read-only for the rest of the run.

    All volatile per-run parameters (source video path, work directory, force
    flag, cleanup level, etc.) are **not** stored here — they are passed as
    plain keyword arguments to ``_build_registry`` and only forwarded to
    ``JobPhase``, which stores them as typed fields on ``JobPhaseResult``.

    All fields are required — the bundled ``default_config.yaml`` is the
    single source of truth for operational defaults. A missing or empty YAML
    raises a ``ValidationError`` immediately at startup rather than silently
    using a stale Python-level fallback.

    Attributes:
        extraction:  Stream filter settings for the extraction phase.
        chunking:    Chunking strategy and scene-detection tuning.
        encoding:    Quality targets, strategy selection, and encoding tuning.
        audio:       Audio processing settings (filter palette, chains, select).
        measurement: Measurement phase settings (sampling factor).
        codecs:      Map of codec name → :class:`~pyqenc.models.CodecConfig`.
        profiles:    Map of profile name → :class:`ProfileConfig`.

    After the full model is assembled by Pydantic, a ``model_validator`` calls
    :meth:`~EncodingConfig.resolve` so that ``encoding.resolved_targets`` and
    ``encoding.resolved_strategies`` are immediately available without any
    lazy-initialisation guard on the call site.  If the ``strategies`` or
    ``targets`` strings are invalid, the ``ValueError`` raised by
    ``resolve()`` is automatically re-raised by Pydantic v2 as a
    :class:`~pydantic.ValidationError`, making invalid configs fail at
    load time before any phase runs.

    The private ``_source_paths`` attribute is populated by
    :func:`load_app_config` after construction.  It records which YAML files
    were actually loaded (in priority order) so that ``_cmd_config`` can
    display them without calling a separate discovery function.
    """

    extraction:  ExtractionConfig
    chunking:    ChunkingConfig
    encoding:    EncodingConfig
    audio:       AudioConfig
    measurement: MeasurementConfig
    codecs:      dict[str, CodecConfig]
    profiles:    dict[str, ProfileConfig]

    # Populated by load_app_config() after model construction; not serialised.
    # Holds the paths of all config files that were actually loaded, in
    # priority order (bundled default first, then home config, then cwd config).
    # Used by _cmd_config to report which files are active.
    _source_paths: list[Path] = PrivateAttr(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _inject_codec_names(cls, data: object) -> object:
        """Inject the dict key as the ``name`` field for each codec entry.

        The YAML/dict representation stores codecs as ``{codec_name: {fields}}``
        where the codec name is the dict key, not a field value.  Pydantic needs
        ``name`` to be present inside each codec sub-dict, so this validator
        injects it before field validation runs.

        Also validates that neither codec names nor profile names contain ``'+'``,
        since ``'+'`` is the delimiter in strategy pattern syntax and its presence
        in a name would make pattern parsing ambiguous.

        Additionally rejects profile and preset names containing filesystem-unsafe
        characters: a strategy name (``profile[preset]``) is embedded
        verbatim in strategy directory and merge output names, so its parts are
        safe by construction — validated at the definition point, never
        sanitized at a use point.

        Args:
            data: Raw input data (typically a ``dict``).

        Returns:
            The same ``data`` dict with ``name`` injected into each codec entry,
            or ``data`` unchanged if it is not a ``dict`` (Pydantic will handle
            the type error downstream).

        Raises:
            ValueError: If any codec or profile name contains ``'+'``, or any
                        profile/preset name is filesystem-unsafe.
        """
        if isinstance(data, dict):
            codecs = data.get("codecs")
            if isinstance(codecs, dict):
                for codec_name in codecs:
                    if "+" in codec_name:
                        raise ValueError(
                            f"Codec name '{codec_name}' contains '+', which is reserved "
                            f"as the delimiter in strategy pattern syntax. "
                            f"Rename the codec to remove '+'."
                        )
                for codec_name, codec_data in codecs.items():
                    if isinstance(codec_data, dict):
                        for preset in codec_data.get("presets", []):
                            if not is_filesystem_safe_name(str(preset)):
                                raise ValueError(
                                    f"Preset name '{preset}' of codec '{codec_name}' "
                                    f"contains filesystem-unsafe characters — strategy "
                                    f"names are embedded verbatim in filesystem paths. "
                                    f"Rename the preset."
                                )
                patched: dict[str, object] = {}
                for codec_name, codec_data in codecs.items():
                    if isinstance(codec_data, dict) and "name" not in codec_data:
                        codec_data = {**codec_data, "name": codec_name}
                    patched[codec_name] = codec_data
                data = {**data, "codecs": patched}

            profiles = data.get("profiles")
            if isinstance(profiles, dict):
                for profile_name in profiles:
                    if "+" in profile_name:
                        raise ValueError(
                            f"Profile name '{profile_name}' contains '+', which is reserved "
                            f"as the delimiter in strategy pattern syntax. "
                            f"Rename the profile to remove '+'."
                        )
                    if not is_filesystem_safe_name(profile_name):
                        raise ValueError(
                            f"Profile name '{profile_name}' contains filesystem-unsafe "
                            f"characters — strategy names are embedded verbatim in "
                            f"filesystem paths. Rename the profile."
                        )
        return data

    @model_validator(mode="after")
    def _resolve_encoding(self) -> Self:
        """Validate profile quality ranges and trigger strategy/target resolution.

        Called automatically by Pydantic once the entire model tree has been
        validated and constructed.

        First, every profile that declares a ``quality_range`` is checked
        against its referenced codec to ensure the range only narrows (never
        extends) the codec's bounds.  This runs eagerly for all defined
        profiles, not just those referenced by the active strategy list, so
        config bugs are caught at load time regardless of which strategies
        are currently enabled.

        Then delegates to :meth:`EncodingConfig.resolve`, passing the codec
        and profile maps so that wildcard strategy patterns can be expanded
        correctly.

        Any :class:`ValueError` raised here or by ``resolve()`` (e.g. unknown
        profile name, out-of-range quality bound, unrecognised quality target
        metric) propagates as a Pydantic ``ValidationError`` — Pydantic v2
        wraps ``ValueError`` from validators automatically.

        Returns:
            ``self`` — required by Pydantic ``mode='after'`` validators.

        Raises:
            ValidationError: If any profile quality_range violates the
                narrowing constraint, or if any strategy pattern or quality
                target string is invalid (wraps the underlying ``ValueError``).
        """
        for profile_name, profile_cfg in self.profiles.items():
            if profile_cfg.quality_range is not None:
                codec = _get_codec(profile_cfg.codec, self.codecs)
                _validate_profile_quality_range(profile_name, profile_cfg.quality_range, codec)

        self.encoding.resolve(self.codecs, self.profiles)
        return self


def _expand_strategy_pattern(
    pattern:  str,
    codecs:   dict[str, CodecConfig],
    profiles: dict[str, ProfileConfig],
    quality_range_override: tuple[Decimal, Decimal] | None = None,
) -> list[Strategy]:
    """Expand a single strategy pattern string into a list of ``Strategy`` objects.

    **Pattern syntax:** ``<profile>[+<preset>]``

    The profile part is mandatory and comes first; the preset part is optional
    and follows a ``'+'`` separator.  ``'+'`` is reserved — codec and profile
    names must not contain it (validated at config load time).

    Supported formats:

    - ``"h265-aq"``     — specific profile, codec's ``default_preset``
    - ``"h265*"``       — profile wildcard, each codec's ``default_preset``
    - ``"h265-aq+slow"``— specific profile, specific preset
    - ``"h265*+slow"``  — profile wildcard, specific preset
    - ``"h265*+*"``     — profile wildcard, all presets
    - ``"*"``           — all profiles, each codec's ``default_preset``
    - ``"*+*"``         — all profiles, all presets

    An empty profile part (e.g. ``""``, ``"+*"``, ``"+slow"``) is always an error.

    Args:
        pattern:  Raw strategy pattern string.
        codecs:   Codec config map.
        profiles: Profile config map (``ProfileConfig`` instances).
        quality_range_override: Optional CLI ``-q`` override as canonical
                      ``(lower, upper)`` bounds; validated per matched codec
                      here, applied by :func:`_effective_codec`.

    Returns:
        Expanded list of :class:`~pyqenc.models.Strategy` instances.

    Raises:
        ValueError: If the profile part is empty, no profiles match, the
            requested preset is not supported by the codec, or the override
            violates the subset / granularity rules of a matched codec.
    """
    # Split on first '+' to get (profile_part, preset_part | None).
    if "+" in pattern:
        profile_part, preset_part = pattern.split("+", 1)
    else:
        profile_part = pattern
        preset_part  = None   # absent → use default_preset per codec

    if not profile_part:
        raise ValueError(
            f"Strategy pattern '{pattern}' has an empty profile part — "
            f"the profile is required. "
            f"Use '*' to match all profiles with their default presets, "
            f"or '*+*' for all profiles with all presets."
        )

    # Resolve matching profile names.
    if "*" in profile_part:
        matching_profiles = [n for n in profiles if fnmatch.fnmatch(n, profile_part)]
    else:
        if profile_part not in profiles:
            raise ValueError(
                f"Unknown profile '{profile_part}'. "
                f"Available profiles: {list(profiles.keys())}"
            )
        matching_profiles = [profile_part]

    if not matching_profiles:
        raise ValueError(
            f"No profiles match pattern '{profile_part}'. "
            f"Available profiles: {list(profiles.keys())}"
        )

    result: list[Strategy] = []
    for profile_name in matching_profiles:
        profile_cfg = profiles[profile_name]
        codec       = _get_codec(profile_cfg.codec, codecs)
        if quality_range_override is not None:
            _validate_override_quality_range(quality_range_override, profile_name, codec)
        codec = _effective_codec(profile_name, profile_cfg, codec, quality_range_override)

        if preset_part is None:
            # No preset specified — use the codec's default_preset.
            presets_to_use = [codec.default_preset]
        elif preset_part == "*":
            # Explicit wildcard — expand to all presets.
            presets_to_use = list(codec.presets)
        else:
            # Specific preset — validate it exists.
            if preset_part not in codec.presets:
                raise ValueError(
                    f"Preset '{preset_part}' not supported by codec '{codec.name}'. "
                    f"Supported presets: {codec.presets}"
                )
            presets_to_use = [preset_part]

        for preset in presets_to_use:
            result.append(Strategy(
                preset       = preset,
                profile      = profile_name,
                codec        = codec,
                profile_args = profile_cfg.extra_args,
            ))

    return result


def _validate_profile_quality_range(
    profile_name:  str,
    profile_range: tuple[Decimal, Decimal],
    codec:         CodecConfig,
) -> None:
    """Raise ``ValueError`` if *profile_range* is invalid for *codec*.

    Two rules:

    - **Subset**: only narrowing is permitted — a profile may restrict the
      search band but must never extend it beyond the codec's declared bounds.
      Direction is inferred from the codec: for CRF/CQ/QP codecs
      ``better < worse`` (lower is better); for VBR codecs ``better > worse``
      (higher is better).
    - **Endpoint alignment**: both endpoints must be exact multiples of the
      codec's ``quality_granularity`` — the search can attempt an endpoint
      verbatim, so a misaligned value would violate the encoder-args
      "already quantized" contract.

    Args:
        profile_name:  Profile key, used in the error message.
        profile_range: The ``(better, worse)`` tuple from the profile config.
        codec:         The resolved ``CodecConfig`` the profile references.

    Raises:
        ValueError: If ``profile_range`` exceeds the codec's range in either
            direction, or an endpoint is off the granularity grid.
    """
    p_better, p_worse = profile_range
    c_better, c_worse = codec.quality_better, codec.quality_worse

    if codec.quality_range[0] > codec.quality_range[1]:
        # VBR: better > worse (e.g. [99.5, 0.5] Mbit/s).
        # Profile better must not exceed codec better; profile worse must not go below codec worse.
        out_of_range = p_better > c_better or p_worse < c_worse
    else:
        # CRF/CQ/QP: better < worse (e.g. [6, 30]).
        # Profile better must not go below codec better; profile worse must not exceed codec worse.
        out_of_range = p_better < c_better or p_worse > c_worse

    if out_of_range:
        raise ValueError(
            f"Profile '{profile_name}' quality_range [{p_better}, {p_worse}] "
            f"extends beyond codec '{codec.name}' range [{c_better}, {c_worse}]. "
            f"Profile quality_range must be a subset of the codec range (only narrowing is allowed)."
        )

    for endpoint in profile_range:
        error = quality_alignment_error(
            endpoint, codec.quality_granularity,
            f"Profile '{profile_name}' quality_range (codec '{codec.name}')",
        )
        if error is not None:
            raise ValueError(error)


def _validate_override_quality_range(
    override:     tuple[Decimal, Decimal],
    profile_name: str,
    codec:        CodecConfig,
) -> None:
    """Raise ``ValueError`` if the CLI ``-q`` override is invalid for *codec*.

    The CLI-override sibling of :func:`_validate_profile_quality_range` — the
    same two rules (subset of the codec range, endpoints on the granularity
    grid), evaluated per matched profile so a shared ``-q`` value must satisfy
    every matched codec (across codecs the granularity intersection applies:
    the coarsest step wins).

    Args:
        override:     The override as canonical ``(lower, upper)`` bounds.
        profile_name: Profile key, used in the error message.
        codec:        The resolved ``CodecConfig`` the profile references.

    Raises:
        ValueError: If the override exceeds the codec's range in either
            direction, or an endpoint is off the granularity grid.
    """
    o_lower, o_upper = override
    c_better, c_worse = codec.quality_better, codec.quality_worse
    c_lower, c_upper = (
        (c_worse, c_better) if c_better > c_worse else (c_better, c_worse)
    )

    if o_lower < c_lower or o_upper > c_upper:
        raise ValueError(
            f"Quality override [{o_lower}, {o_upper}] for profile '{profile_name}' "
            f"extends beyond codec '{codec.name}' range [{c_better}, {c_worse}]. "
            f"The -q override must be a subset of every matched codec's range "
            f"(only narrowing is allowed)."
        )

    for endpoint in override:
        error = quality_alignment_error(
            endpoint, codec.quality_granularity,
            f"Quality override (profile '{profile_name}', codec '{codec.name}')",
        )
        if error is not None:
            raise ValueError(error)


def _effective_codec(
    profile_name: str,
    profile_cfg:  ProfileConfig,
    codec:        CodecConfig,
    quality_range_override: tuple[Decimal, Decimal] | None = None,
) -> CodecConfig:
    """Return a ``CodecConfig`` with the effective quality settings applied.

    Range precedence: CLI override (``quality_range_override``, canonical
    ``(lower, upper)`` bounds re-ordered per the codec's own direction
    convention) > profile ``quality_range`` (config order, already
    directional) > codec bounds (returned unchanged, identity).

    Whenever the resulting effective range excludes ``default_quality`` (the
    search's starting point — nothing else clamps it into the band), the
    effective codec auto-adjusts it to the nearest range bound and logs the
    adjustment.  Auto-adjust over a loud exit: the starting point is a hint
    the search refines; in fixed runs it makes the effective settings object
    self-consistent with the pinned value.

    Range validation is **not** performed here — it is the caller's
    responsibility to ensure ``_validate_profile_quality_range`` /
    ``_validate_override_quality_range`` already ran (which
    ``AppConfig._resolve_encoding`` and ``_expand_strategy_pattern``
    guarantee).

    Args:
        profile_name: Profile key (unused at runtime; kept for symmetry with validators).
        profile_cfg:  Profile configuration — may carry an optional quality_range.
        codec:        Resolved codec configuration to use as the base.
        quality_range_override: Optional CLI override as canonical
                      ``(lower, upper)`` bounds.

    Returns:
        The original *codec* when neither override nor profile narrows the
        range, or a copy with the effective ``quality_range`` (and, when
        excluded, the adjusted ``default_quality``) otherwise.
    """
    if quality_range_override is not None:
        o_lower, o_upper = quality_range_override
        effective_range = (
            (o_upper, o_lower) if codec.quality_better > codec.quality_worse
            else (o_lower, o_upper)
        )
    elif profile_cfg.quality_range is not None:
        effective_range = profile_cfg.quality_range
    else:
        return codec

    updates: dict[str, Decimal | tuple[Decimal, Decimal]] = {
        "quality_range": effective_range,
    }
    lower, upper = min(effective_range), max(effective_range)
    default_quality = codec.default_quality
    if default_quality < lower or default_quality > upper:
        clamped = lower if default_quality < lower else upper
        updates["default_quality"] = clamped
        _logger.info(
            "Codec '%s': default_quality %s lies outside the effective range "
            "[%s, %s] — adjusted to %s",
            codec.name, default_quality, effective_range[0], effective_range[1], clamped,
        )
    return codec.model_copy(update=updates)


def _get_codec(name: str, codecs: dict[str, CodecConfig]) -> CodecConfig:
    """Return the ``CodecConfig`` for *name*, raising ``ValueError`` if missing.

    Args:
        name:   Codec name (e.g. ``"h265-10bit"``).
        codecs: Codec config map.

    Returns:
        Matching :class:`~pyqenc.models.CodecConfig`.

    Raises:
        ValueError: If *name* is not in *codecs*.
    """
    if name not in codecs:
        raise ValueError(
            f"Unknown codec '{name}'. Available codecs: {list(codecs.keys())}"
        )
    return codecs[name]


def load_app_config(*, default_only: bool = False) -> AppConfig:
    """Discover, merge, and validate the layered YAML configuration.

    Loads up to three YAML files in ascending priority order and deep-merges
    them before validating the result as an :class:`AppConfig`.  The
    ``_source_paths`` private attribute on the returned instance is populated
    with the paths of all files that were actually found and loaded (in
    priority order), so that ``_cmd_config`` can display them without
    re-discovering sources.

    Priority order (lowest → highest):

    1. Bundled ``default_config.yaml`` shipped with the package — always required.
    2. User home config at ``~/.config/pyqenc/config.yaml`` — optional.
    3. CWD config at ``./pyqenc.yaml`` — optional.

    Args:
        default_only: When ``True``, load only the bundled ``default_config.yaml``
            and skip the home and CWD layers entirely.  Useful in tests that
            must not be affected by the developer's local configuration.

    Returns:
        A fully validated :class:`AppConfig` instance with ``_source_paths``
        populated.

    Raises:
        FileNotFoundError: If the bundled ``default_config.yaml`` is missing
            (indicates a broken installation).
        pydantic.ValidationError: If the merged config dict fails Pydantic
            field validation or strategy / quality-target resolution.
    """
    bundled_default = Path(__file__).parent / "default_config.yaml"
    if not bundled_default.exists():
        raise FileNotFoundError(
            f"Bundled default config not found: {bundled_default}. "
            "This indicates a broken package installation."
        )

    with bundled_default.open(encoding="utf-8") as fh:
        merged: dict = yaml.safe_load(fh) or {}

    source_paths: list[Path] = [bundled_default]
    _logger.debug("Loaded bundled default config: %s", bundled_default)

    if not default_only:
        home_config = Path.home() / CONFIG_DIR_HOME / CONFIG_FILENAME_HOME
        if home_config.exists():
            with home_config.open(encoding="utf-8") as fh:
                home_data: dict = yaml.safe_load(fh) or {}
            merged = _deep_merge(merged, home_data)
            source_paths.append(home_config)
            _logger.debug("Loaded home config: %s", home_config)
        else:
            _logger.debug("Home config absent (skipped): %s", home_config)

        cwd_config = Path.cwd() / CONFIG_FILENAME_CWD
        if cwd_config.exists():
            with cwd_config.open(encoding="utf-8") as fh:
                cwd_data: dict = yaml.safe_load(fh) or {}
            merged = _deep_merge(merged, cwd_data)
            source_paths.append(cwd_config)
            _logger.debug("Loaded CWD config: %s", cwd_config)
        else:
            _logger.debug("CWD config absent (skipped): %s", cwd_config)

    config = AppConfig.model_validate(merged)
    config._source_paths = source_paths
    return config
