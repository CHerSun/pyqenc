"""AppConfig — layered, Pydantic-validated application configuration.

Loaded once at startup by deep-merging up to three YAML files in priority
order (bundled default < user home config < cwd config). CLI overrides are
applied as direct attribute assignments after loading. Volatile per-run
parameters (source, work_dir, force, etc.) are passed separately as plain
keyword arguments to ``_build_registry`` and are never stored here.
"""

from __future__ import annotations

import fnmatch
import logging
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, PrivateAttr, field_validator, model_validator

from pyqenc.constants import (
    CONFIG_DIR_HOME,
    CONFIG_FILENAME_CWD,
    CONFIG_FILENAME_HOME,
)
from pyqenc.models import (
    ChunkingMode,
    CodecConfig,
    QualityTarget,
    Strategy,
)

_logger = logging.getLogger(__name__)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* on top of *base* and return a new dict.

    Merge rules:
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
        a, b = Decimal(str(v[0])), Decimal(str(v[1]))
        return a, b


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
    """Chunking phase configuration controlling split strategy and scene detection.

    Attributes:
        mode:              Chunking strategy — ``LOSSLESS`` re-encodes each chunk
                           to FFV1 all-intra for frame-perfect boundaries;
                           ``REMUX`` stream-copies for speed at the cost of snapping
                           to the nearest I-frame.
        scene_threshold:   Minimum content-change score (0.0–1.0) for the scene
                           detector to declare a scene boundary. Lower values make
                           the detector more sensitive.
        min_scene_length:  Minimum number of frames a scene must contain before it
                           is eligible to be split off as a separate chunk.
    """

    mode:             ChunkingMode
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
        strategies:         Raw strategy pattern strings (e.g. ``"slow+h265*"``).
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

    # Private resolved caches — not persisted, populated by resolve().
    _resolved_targets:    list[QualityTarget] | None = PrivateAttr(default=None)
    _resolved_strategies: list[Strategy]     | None = PrivateAttr(default=None)

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
                _expand_strategy_pattern(pattern, codecs, profiles)
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
            RuntimeError: If :meth:`resolve` has not been called yet.
        """
        if self._resolved_targets is None:
            raise RuntimeError(
                "EncodingConfig.resolve() has not been called — "
                "resolved_targets is not available."
            )
        return self._resolved_targets

    @property
    def resolved_strategies(self) -> list[Strategy]:
        """Resolved ``Strategy`` objects; populated after :meth:`resolve` is called.

        Raises:
            RuntimeError: If :meth:`resolve` has not been called yet.
        """
        if self._resolved_strategies is None:
            raise RuntimeError(
                "EncodingConfig.resolve() has not been called — "
                "resolved_strategies is not available."
            )
        return self._resolved_strategies


class AudioConfig(BaseModel):
    """Audio output / conversion phase configuration.

    All fields are required — values are supplied by the bundled YAML and
    any user overrides. No Python-level defaults are set; a missing YAML
    key raises a ``ValidationError`` at startup.

    Attributes:
        convert_pattern:     Regex pattern; processed audio files whose name
                             matches are passed to the ``ConversionStrategy``
                             finalizer.
        codec:               FFmpeg audio codec name (e.g. ``"aac"``).
        bitrate_per_channel: Per-channel bitrate string (e.g. ``"96k"``).
                             Scaled at runtime by channel count:
                             2.0/stereo → ×2, 5.1 → ×6, 7.1 → ×8.
        extension:           Output file extension (e.g. ``".m4a"``).
        peak_target_dbfs:    Target peak level in dBFS for normalisation passes
                             (e.g. ``-1.0``). Applied uniformly to all
                             ``NormStrategy`` and downmix normalisation passes.
    """

    convert_pattern:     str
    codec:               str
    bitrate_per_channel: str
    extension:           str
    peak_target_dbfs:    float


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
        audio:       Audio conversion settings (flat; no per-layout profiles).
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

        Args:
            data: Raw input data (typically a ``dict``).

        Returns:
            The same ``data`` dict with ``name`` injected into each codec entry,
            or ``data`` unchanged if it is not a ``dict`` (Pydantic will handle
            the type error downstream).

        Raises:
            ValueError: If any codec or profile name contains ``'+'``.
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
        return data

    @model_validator(mode="after")
    def _resolve_encoding(self) -> AppConfig:
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
) -> list[Strategy]:
    """Expand a single strategy pattern string into a list of ``Strategy`` objects.

    **Pattern syntax:** ``<profile>[+<preset>]``

    The profile part is mandatory and comes first; the preset part is optional
    and follows a ``'+'`` separator.  ``'+'`` is reserved — codec and profile
    names must not contain it (validated at config load time).

    Supported formats:

    - ``"h265-aq"``     — specific profile, codec's ``default_preset``
    - ``"h265*"``       — profile wildcard, each matching codec's ``default_preset``
    - ``"h265-aq+slow"``— specific profile, specific preset
    - ``"h265*+slow"``  — profile wildcard, specific preset
    - ``"h265*+*"``     — profile wildcard, all presets for each codec
    - ``"*"``           — all profiles, each codec's ``default_preset``
    - ``"*+*"``         — all profiles, all presets

    An empty profile part (e.g. ``""``, ``"+*"``, ``"+slow"``) is always an error.

    Args:
        pattern:  Raw strategy pattern string.
        codecs:   Codec config map.
        profiles: Profile config map (``ProfileConfig`` instances).

    Returns:
        Expanded list of :class:`~pyqenc.models.Strategy` instances.

    Raises:
        ValueError: If the profile part is empty, no profiles match, or the
            requested preset is not supported by the codec.
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
        codec       = _effective_codec(profile_name, profile_cfg, _get_codec(profile_cfg.codec, codecs))

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
    """Raise ``ValueError`` if *profile_range* is not a subset of the codec's range.

    Only narrowing is permitted — a profile may restrict the search band but
    must never extend it beyond the codec's declared bounds.  Direction is
    inferred from the codec: for CRF/CQ/QP codecs ``better < worse`` (lower
    is better); for VBR codecs ``better > worse`` (higher is better).

    Args:
        profile_name:  Profile key, used in the error message.
        profile_range: The ``(better, worse)`` tuple from the profile config.
        codec:         The resolved ``CodecConfig`` the profile references.

    Raises:
        ValueError: If ``profile_range`` exceeds the codec's range in either direction.
    """
    p_better, p_worse = profile_range
    c_better, c_worse = codec.quality_better, codec.quality_worse

    if codec.quality_higher_is_better:
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


def _effective_codec(
    profile_name: str,
    profile_cfg:  ProfileConfig,
    codec:        CodecConfig,
) -> CodecConfig:
    """Return a ``CodecConfig`` with the profile's quality range applied, if set.

    When the profile declares no ``quality_range`` the original *codec* is
    returned unchanged.  When it does, a shallow copy of *codec* is returned
    with ``quality_range`` replaced by the profile's narrowed range.

    Validation is **not** repeated here — it is the caller's responsibility
    to ensure ``_validate_profile_quality_range`` was already called (which
    ``AppConfig._resolve_encoding`` guarantees for every profile at load time).

    Args:
        profile_name: Profile key (unused at runtime; kept for symmetry with validators).
        profile_cfg:  Profile configuration — may carry an optional quality_range.
        codec:        Resolved codec configuration to use as the base.

    Returns:
        The original *codec* when ``profile_cfg.quality_range`` is ``None``,
        or a copy with the overridden ``quality_range`` otherwise.
    """
    if profile_cfg.quality_range is None:
        return codec
    return codec.model_copy(update={"quality_range": profile_cfg.quality_range})


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
    config._source_paths = source_paths  # noqa: SLF001  (intentional post-init population)
    return config
