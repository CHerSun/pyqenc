"""AppConfig — layered, Pydantic-validated application configuration.

Loaded once at startup by deep-merging up to three YAML files in priority
order (bundled default < user home config < cwd config). Plain CLI overrides
are applied as direct attribute assignments after loading; the
resolution-coupled overrides (``--strategies`` / ``--targets`` / ``-q``) are
arguments to :meth:`AppConfig.resolve_encoding`, whose
:class:`~pyqenc.models.EncodingPlan` result is a volatile per-run value
passed to ``_build_registry`` — never stored here. The config is read-only
for the rest of the run.
"""

import fnmatch
import logging
from collections.abc import Sequence
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
    EncodingPlan,
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
        quality_range: Optional quality range override.  Input order is
                       free — the value is normalized to the referenced
                       codec's direction convention (``(better, worse)``) at
                       ``AppConfig`` construction time, where the codec
                       context exists.  When set, the quality search is
                       constrained to this sub-range instead of the codec's
                       full range.  Only narrowing is allowed — the profile
                       range must be a strict subset of the codec's range.
                       ``None`` means use the codec's range unchanged.
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

    The YAML parse form plus plain run flags — nothing else. ``targets`` and
    ``strategies`` stay raw strings: they are resolved to typed objects
    exactly once at the run boundary via
    :meth:`AppConfig.resolve_encoding`, whose result threads through the
    pipeline as the :class:`~pyqenc.models.EncodingPlan`. No resolved state
    lives here and no field is written after load.

    Attributes:
        targets:            Raw quality target strings (e.g. ``"vmaf-min:95"``).
        strategies:         Raw strategy pattern strings (e.g. ``"h265*"``).
        optimize:           Whether to run the strategy optimisation phase.
        concurrency:        Maximum concurrent encoding processes.
        optimize_tolerance: Tolerance percentage for strategy selection.
        visual_hash:        Whether to display emoji visual hash in chunk logs.
    """

    targets:            list[str]
    strategies:         list[str]
    optimize:           bool
    concurrency:        int
    optimize_tolerance: float
    visual_hash:        bool


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
        # Accept both shapes — the flat YAML form ({type, ...params}) and the
        # resolved/serialised form ({type, params: {...}}). The params are
        # ALWAYS re-validated against the type's model: a round-tripped
        # ``params`` dict would otherwise validate against the declared
        # ``BaseModel`` and land as a plain instance, losing the subclass.
        # extra="forbid" on the param model rejects any parameter not valid for the type.
        params_source = raw.pop("params", raw)
        params = filter_cls.params_model.model_validate(params_source)
        return {"type": type_id, "params": params}


class ChainSpec(BaseModel):
    """A named, ordered chain of filter references from ``audio.chains``.

    Applied to N matched tracks a chain produces exactly N outputs. The name
    is validated here — non-blank and filesystem-safe, since it forms the
    ``chain=<name>`` output filename suffix. Cross-field rules (references
    resolve in the palette, name uniqueness across the list, passthrough
    alone) need the owning :class:`AudioConfig` context and are enforced by
    its model-validator so failures surface as a ``ValidationError`` at
    config load.

    Attributes:
        name:    Unique, filesystem-safe chain name; the ``chain=<name>`` output
                 filename suffix.
        filters: Ordered list of filter names, each referencing an
                 ``audio.filters`` key.
    """

    name:    str
    filters: list[str]

    @field_validator("name")
    @classmethod
    def _validate_name_filesystem_safe(cls, v: str) -> str:
        """Reject blank or filesystem-unsafe names.

        Chain names form the ``chain=<name>`` output filename suffix, so the
        name must be usable in a filename verbatim — validated at the
        definition point, never sanitized at a use point.
        """
        if not v or not v.strip():
            raise ValueError("Chain name must be a non-empty, non-blank string.")
        if not is_filesystem_safe_name(v):
            bad = sorted(set(v) & (FILENAME_FORBIDDEN_CHARS | FILENAME_CONTROL_CHARS))
            raise ValueError(
                f"Chain name {v!r} contains filesystem-unsafe character(s): "
                f"{bad}. Avoid {sorted(FILENAME_FORBIDDEN_CHARS)} and control characters."
            )
        return v


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

    After the full model is assembled by Pydantic, a ``model_validator``
    normalizes and validates every declared profile ``quality_range``
    against its codec — config-file facts fail at load time. Resolution of
    the raw ``strategies`` / ``targets`` strings into typed objects is NOT
    part of loading: it happens once at the run boundary via
    :meth:`resolve_encoding`, whose :class:`~pyqenc.models.EncodingPlan`
    threads through the pipeline; the config itself is never written after
    load.

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
    def _validate_profile_ranges(self) -> Self:
        """Direction-normalize and validate every declared profile quality range.

        Called automatically by Pydantic once the entire model tree has been
        validated and constructed.

        Each profile's declared ``quality_range`` may be written in either
        input order; it is normalized here to the referenced codec's
        direction convention — the codec context is only available at this
        level. The normalized range must then satisfy the one narrowing rule
        (:meth:`_validate_quality_range`). This runs eagerly for all defined
        profiles, not just those referenced by the active strategy list, so
        config bugs are caught at load time regardless of which strategies
        are currently enabled.

        Any :class:`ValueError` raised here propagates as a Pydantic
        ``ValidationError`` — Pydantic v2 wraps ``ValueError`` from
        validators automatically.

        Returns:
            ``self`` — required by Pydantic ``mode='after'`` validators.

        Raises:
            ValidationError: If any profile quality_range violates the
                narrowing or granularity constraints.
        """
        for profile_name, profile_cfg in self.profiles.items():
            if profile_cfg.quality_range is None:
                continue
            codec = self._get_codec(profile_cfg.codec)
            profile_cfg.quality_range = self._codec_ordered_range(
                codec, profile_cfg.quality_range,
            )
            self._validate_quality_range(
                f"Profile '{profile_name}' quality_range (codec '{codec.name}')",
                profile_cfg.quality_range,
                codec,
            )
        return self

    def resolve_encoding(
        self,
        *,
        strategies: Sequence[str] | None = None,
        targets:    Sequence[str] | None = None,
        quality:    tuple[Decimal, Decimal] | None = None,
    ) -> EncodingPlan:
        """Resolve raw config (plus optional CLI overrides) into the run's plan.

        The single resolution boundary: called exactly once per run by the
        CLI, falling back to the config's own values for any argument left
        as ``None``, and returning the :class:`~pyqenc.models.EncodingPlan`
        that threads through ``JobPhaseResult``. The config itself is never
        written — overrides exist only as arguments to this call.

        The ``quality`` override (``-q``) replaces every matched profile's
        effective range: input order is free (canonical ``(lower, upper)``
        order is derived here), and each codec's own direction convention
        is applied during expansion. Because one shared number must never
        be silently reinterpreted per codec, an override also requires
        uniform quality labels across the matched strategies.

        Args:
            strategies: Strategy patterns; ``None`` → ``encoding.strategies``.
            targets:    Quality target strings; ``None`` → ``encoding.targets``.
            quality:    The ``-q`` bounds in input order; ``None`` → no override.

        Returns:
            The resolved :class:`~pyqenc.models.EncodingPlan`.

        Raises:
            ValueError: If any target string or strategy pattern is invalid,
                the override violates a matched codec's range or granularity,
                labels mix under an override, the resolved set mixes fixed
                and searched strategies, the plan resolves to no strategies,
                or a searched run carries no quality targets.
        """
        resolved_targets = [
            QualityTarget.parse(t)
            for t in (targets if targets is not None else self.encoding.targets)
        ]

        canonical_quality: tuple[Decimal, Decimal] | None = None
        if quality is not None:
            first, second = _coerce_decimal_pair(quality)
            canonical_quality = (first, second) if first <= second else (second, first)

        patterns = (
            strategies if strategies is not None else self.encoding.strategies
        )
        all_strategies: list[Strategy] = []
        for pattern in patterns:
            all_strategies.extend(
                self._expand_strategy_pattern(pattern, canonical_quality)
            )

        # Deduplicate by (preset, profile), retaining first occurrence.
        seen: set[tuple[str, str]] = set()
        unique: list[Strategy] = []
        for strategy in all_strategies:
            key = (strategy.preset, strategy.profile)
            if key not in seen:
                seen.add(key)
                unique.append(strategy)

        if canonical_quality is not None:
            labels = {s.codec.quality_label for s in unique}
            if len(labels) > 1:
                listing = ", ".join(
                    f"{s.display_name()}={s.codec.quality_label}" for s in unique
                )
                raise ValueError(
                    f"--quality shares one value across strategies, but the matched "
                    f"strategies use different quality labels: {listing}. "
                    f"A shared number would be silently reinterpreted per codec; "
                    f"use --strategies to select a single label family."
                )

        collapsed = [
            s.display_name() for s in unique
            if s.codec.quality_better == s.codec.quality_worse
        ]
        ranged = [
            s.display_name() for s in unique
            if s.codec.quality_better != s.codec.quality_worse
        ]
        if collapsed and ranged:
            raise ValueError(
                f"Mixed fixed and searched strategies: fixed (single-point range) "
                f"[{', '.join(collapsed)}], searched (ranged) "
                f"[{', '.join(ranged)}]. All strategies must be fixed (every range "
                f"collapsed, e.g. via -q <value>) or all searched — mixed sets void "
                f"the size-comparison assumptions of optimization. Adjust "
                f"--strategies or the profile quality_range settings."
            )

        return EncodingPlan(strategies=unique, targets=resolved_targets)

    def _expand_strategy_pattern(
        self,
        pattern: str,
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
            quality_range_override: Optional CLI ``-q`` override as canonical
                          ``(lower, upper)`` bounds; validated per matched codec
                          here, applied by :meth:`_effective_codec`.

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
            matching_profiles = [
                n for n in self.profiles if fnmatch.fnmatch(n, profile_part)
            ]
        else:
            if profile_part not in self.profiles:
                raise ValueError(
                    f"Unknown profile '{profile_part}'. "
                    f"Available profiles: {list(self.profiles.keys())}"
                )
            matching_profiles = [profile_part]

        if not matching_profiles:
            raise ValueError(
                f"No profiles match pattern '{profile_part}'. "
                f"Available profiles: {list(self.profiles.keys())}"
            )

        result: list[Strategy] = []
        for profile_name in matching_profiles:
            profile_cfg = self.profiles[profile_name]
            codec       = self._get_codec(profile_cfg.codec)
            if quality_range_override is not None:
                self._validate_quality_range(
                    f"Quality override for profile '{profile_name}' "
                    f"(codec '{codec.name}')",
                    quality_range_override,
                    codec,
                )
            codec = self._effective_codec(profile_cfg, codec, quality_range_override)

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

    def _validate_quality_range(
        self,
        owner: str,
        rng:    tuple[Decimal, Decimal],
        codec:  CodecConfig,
    ) -> None:
        """Raise ``ValueError`` if *rng* may not narrow *codec*'s range.

        The one rule for every layer that narrows a codec's range — a
        profile's ``quality_range`` or the CLI ``-q`` override:

        - **Subset**: only narrowing is permitted — the range must sit inside
          the codec's declared bounds. Direction-free: a numeric span is a
          subset of another iff its minimum and maximum fit inside.
        - **Endpoint alignment**: both endpoints must be exact multiples of
          the codec's ``quality_granularity`` — the search can attempt an
          endpoint verbatim, so a misaligned value would violate the
          encoder-args "already quantized" contract.

        Args:
            owner: Error-message prefix naming the range's layer
                   (e.g. ``"Profile 'h265-aq' quality_range (codec 'h265-10bit')"``).
            rng:   The range in any order.
            codec: The codec whose bounds and granularity apply.

        Raises:
            ValueError: If *rng* extends beyond the codec's range in either
                direction, or an endpoint is off the granularity grid.
        """
        lo, hi = min(rng), max(rng)
        c_lo, c_hi = min(codec.quality_range), max(codec.quality_range)
        if lo < c_lo or hi > c_hi:
            raise ValueError(
                f"{owner} [{rng[0]}, {rng[1]}] extends beyond codec "
                f"'{codec.name}' range [{codec.quality_range[0]}, "
                f"{codec.quality_range[1]}]. "
                f"Must be a subset of the codec range (only narrowing is allowed)."
            )
        for endpoint in rng:
            error = quality_alignment_error(endpoint, codec.quality_granularity, owner)
            if error is not None:
                raise ValueError(error)

    def _codec_ordered_range(
        self,
        codec: CodecConfig,
        rng:   tuple[Decimal, Decimal],
    ) -> tuple[Decimal, Decimal]:
        """Return *rng* as ``(better, worse)`` in the codec's direction convention.

        The single home of the direction convention: CRF/CQ/QP codecs keep
        ``(low, high)`` (lower is better); VBR codecs store ``(high, low)``
        (higher is better). Every range entering a ``CodecConfig`` passes
        through here.

        Args:
            codec: The codec whose convention applies.
            rng:   The range in any order.

        Returns:
            The range in the codec's ``(better, worse)`` order.
        """
        lo, hi = min(rng), max(rng)
        return (hi, lo) if codec.quality_better > codec.quality_worse else (lo, hi)

    def _effective_codec(
        self,
        profile_cfg:  ProfileConfig,
        codec:        CodecConfig,
        quality_range_override: tuple[Decimal, Decimal] | None = None,
    ) -> CodecConfig:
        """Return a ``CodecConfig`` with the effective quality settings applied.

        Range precedence: CLI override (``quality_range_override``, canonical
        ``(lower, upper)`` bounds) > profile ``quality_range`` (already
        codec-ordered at load) > codec bounds (returned unchanged, identity).
        Whatever wins is stored in the codec's own direction convention.

        Whenever the resulting effective range excludes ``default_quality`` (the
        search's starting point — nothing else clamps it into the band), the
        effective codec auto-adjusts it to the nearest range bound and logs the
        adjustment.  Auto-adjust over a loud exit: the starting point is a hint
        the search refines; in fixed runs it makes the effective settings object
        self-consistent with the pinned value.

        Range validation is **not** performed here — the callers
        (``_validate_profile_ranges`` at load, ``_expand_strategy_pattern``
        for the override) guarantee :meth:`_validate_quality_range` ran.

        Args:
            profile_cfg: Profile configuration — may carry an optional quality_range.
            codec:       Resolved codec configuration to use as the base.
            quality_range_override: Optional CLI override as canonical
                          ``(lower, upper)`` bounds.

        Returns:
            The original *codec* when neither override nor profile narrows the
            range, or a copy with the effective ``quality_range`` (and, when
            excluded, the adjusted ``default_quality``) otherwise.
        """
        if quality_range_override is not None:
            effective_range = self._codec_ordered_range(codec, quality_range_override)
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

    def _get_codec(self, name: str) -> CodecConfig:
        """Return the ``CodecConfig`` for *name*, raising ``ValueError`` if missing.

        Args:
            name:   Codec name (e.g. ``"h265-10bit"``).

        Returns:
            Matching :class:`~pyqenc.models.CodecConfig`.

        Raises:
            ValueError: If *name* is not in ``self.codecs``.
        """
        if name not in self.codecs:
            raise ValueError(
                f"Unknown codec '{name}'. Available codecs: {list(self.codecs.keys())}"
            )
        return self.codecs[name]


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
            field validation (including profile quality-range rules).
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
