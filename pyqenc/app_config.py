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
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from pyqenc.constants import (
    CONFIG_DIR_HOME,
    CONFIG_FILENAME_CWD,
    CONFIG_FILENAME_HOME,
    DEFAULT_MAX_PARALLEL,
    DEFAULT_METRICS_SAMPLING,
)
from pyqenc.models import (
    ChunkingMode,
    CodecConfig,
    CropParams,
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


class AudioConversionProfile(BaseModel):
    """Codec/bitrate/extension profile for the final audio delivery conversion step.

    Attributes:
        codec:     FFmpeg audio codec name (e.g. ``"aac"``).
        bitrate:   Target bitrate string (e.g. ``"192k"``).
        extension: Output file extension (e.g. ``"m4a"``).
    """

    codec:     str
    bitrate:   str
    extension: str


class AudioConfig(BaseModel):
    """Audio output / conversion phase configuration.

    Controls which processed audio files are passed to the ``ConversionStrategy``
    finalizer (via ``convert_filter``), what codec/bitrate/extension profile is
    used per channel layout, and optional CLI-level overrides that override the
    per-layout profile values for a single run.

    Attributes:
        convert_filter:    Regex pattern; processed audio files whose name matches
                           are passed to the ``ConversionStrategy`` finalizer.
                           Required — no built-in default.
        profiles:          Map of channel-layout key (e.g. ``"2.0"``, ``"5.1"``) to
                           :class:`AudioConversionProfile`.  Defaults to empty dict.
        audio_codec:       When set, overrides the ``codec`` field of every profile
                           for this run (e.g. ``"aac"``).  ``None`` means use each
                           profile's own codec.  CLI-settable.
        audio_base_bitrate: When set, overrides the ``bitrate`` field of every
                           profile for this run (e.g. ``"192k"``).  ``None`` means
                           use each profile's own bitrate.  CLI-settable.
    """

    convert_filter:     str
    profiles:           dict[str, AudioConversionProfile] = Field(default_factory=dict)
    audio_codec:        str | None = None
    audio_base_bitrate: str | None = None


class ProfileConfig(BaseModel):
    """Encoding profile referencing a codec and optional FFmpeg extra arguments.

    Lives inside ``AppConfig.profiles``.

    Attributes:
        codec:       Codec reference name used by this profile
                     (must match a key in ``AppConfig.codecs``).
        description: Human-readable description of the profile (default empty string).
        extra_args:  Additional FFmpeg arguments appended when this profile is used
                     (default empty list).
    """

    codec:       str
    description: str       = ""
    extra_args:  list[str] = Field(default_factory=list)


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
                           to FFV1 all-intra for frame-perfect boundaries (default);
                           ``REMUX`` stream-copies for speed at the cost of snapping
                           to the nearest I-frame.
        scene_threshold:   Minimum content-change score (0.0–1.0) for the scene
                           detector to declare a scene boundary.  Lower values make
                           the detector more sensitive.  Default ``0.3``.
        min_scene_length:  Minimum number of frames a scene must contain before it
                           is eligible to be split off as a separate chunk.
                           Default ``24``.
    """

    mode:             ChunkingMode = ChunkingMode.LOSSLESS
    scene_threshold:  float        = 0.3
    min_scene_length: int          = 24


class EncodingConfig(BaseModel):
    """Encoding phase configuration: quality targets, strategies, and runtime tuning.

    Raw string fields (``quality_targets``, ``strategies``) are stored in their
    serialisable string form and resolved to typed objects once via :meth:`resolve`.
    Resolution is triggered automatically by ``AppConfig``'s ``model_validator``
    after the full config tree has been assembled.

    Attributes:
        quality_targets:              Raw quality target strings (e.g. ``"vmaf-min:95"``).
        strategies:                   Raw strategy pattern strings (e.g. ``"slow+h265*"``).
        optimize:                     Whether to run the strategy optimisation phase.
        max_parallel:                 Maximum concurrent encoding processes.
        metrics_sampling:             Frame subsampling factor for metric computation.
        visual_hash:                  Whether to display emoji visual hash in chunk logs.
        strategy_selection_tolerance: Tolerance percentage for strategy selection.
        crop_params:                  Manual crop parameters; ``None`` means auto-detect.
    """

    quality_targets:              list[str]        = Field(default_factory=list)
    strategies:                   list[str]        = Field(default_factory=list)
    optimize:                     bool             = True
    max_parallel:                 int              = DEFAULT_MAX_PARALLEL
    metrics_sampling:             int              = DEFAULT_METRICS_SAMPLING
    visual_hash:                  bool             = True
    strategy_selection_tolerance: float            = 5.0
    crop_params:                  CropParams | None = None

    # Private resolved caches — not persisted, populated by resolve().
    _resolved_targets:    list[QualityTarget] | None = PrivateAttr(default=None)
    _resolved_strategies: list[Strategy]     | None = PrivateAttr(default=None)

    def resolve(
        self,
        codecs:   dict[str, CodecConfig],
        profiles: dict[str, "ProfileConfig"],
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
            QualityTarget.parse(t) for t in self.quality_targets
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


def _expand_strategy_pattern(
    pattern:  str,
    codecs:   dict[str, CodecConfig],
    profiles: dict[str, "ProfileConfig"],
) -> list[Strategy]:
    """Expand a single strategy pattern string into a list of ``Strategy`` objects.

    Mirrors the legacy strategy expansion logic but operates on the Pydantic
    ``ProfileConfig`` / ``CodecConfig`` dicts.

    Supported formats:

    - ``"slow+h265-aq"`` — specific preset + profile
    - ``"slow+h265*"``   — preset with profile wildcard
    - ``"slow"``         — preset only (all compatible profiles)
    - ``"+h265*"``       — profile pattern, all presets
    - ``"+h265-aq"``     — specific profile, all presets
    - ``""``             — all preset+profile combinations

    Args:
        pattern:  Raw strategy pattern string.
        codecs:   Codec config map.
        profiles: Profile config map (``ProfileConfig`` instances).

    Returns:
        Expanded list of :class:`~pyqenc.models.Strategy` instances.

    Raises:
        ValueError: If an unrecognised preset, profile, or pattern is supplied.
    """
    if pattern == "":
        # All combinations.
        result: list[Strategy] = []
        for profile_name, profile_cfg in profiles.items():
            codec = _get_codec(profile_cfg.codec, codecs)
            for preset in codec.presets:
                result.append(Strategy(
                    preset       = preset,
                    profile      = profile_name,
                    codec        = codec,
                    profile_args = profile_cfg.extra_args,
                ))
        return result

    if "+" in pattern:
        preset_part, profile_part = pattern.split("+", 1)
    else:
        preset_part  = pattern
        profile_part = "*"

    if not preset_part:
        # Profile-only: "+h265*" or "+h265-aq"
        return _expand_profile_pattern(None, profile_part, codecs, profiles)

    if profile_part == "*":
        # Preset only: all compatible profiles.
        result = []
        for profile_name, profile_cfg in profiles.items():
            codec = _get_codec(profile_cfg.codec, codecs)
            if preset_part in codec.presets:
                result.append(Strategy(
                    preset       = preset_part,
                    profile      = profile_name,
                    codec        = codec,
                    profile_args = profile_cfg.extra_args,
                ))
        if not result:
            raise ValueError(
                f"Preset '{preset_part}' is not supported by any codec. "
                f"Available codecs: {list(codecs.keys())}"
            )
        return result

    return _expand_profile_pattern(preset_part, profile_part, codecs, profiles)


def _expand_profile_pattern(
    preset:          str | None,
    profile_pattern: str,
    codecs:          dict[str, CodecConfig],
    profiles:        dict[str, "ProfileConfig"],
) -> list[Strategy]:
    """Expand a profile pattern (with optional preset) into ``Strategy`` objects.

    Args:
        preset:          Preset name, or ``None`` for all presets supported by the codec.
        profile_pattern: Profile name or glob pattern (e.g. ``"h265*"``).
        codecs:          Codec config map.
        profiles:        Profile config map.

    Returns:
        List of resolved :class:`~pyqenc.models.Strategy` instances.

    Raises:
        ValueError: If no profiles match, or if the preset is not supported.
    """
    if "*" in profile_pattern:
        matching = [n for n in profiles if fnmatch.fnmatch(n, profile_pattern)]
    else:
        if profile_pattern not in profiles:
            raise ValueError(
                f"Unknown profile '{profile_pattern}'. "
                f"Available profiles: {list(profiles.keys())}"
            )
        matching = [profile_pattern]

    if not matching:
        raise ValueError(
            f"No profiles match pattern '{profile_pattern}'. "
            f"Available profiles: {list(profiles.keys())}"
        )

    result: list[Strategy] = []
    for profile_name in matching:
        profile_cfg = profiles[profile_name]
        codec       = _get_codec(profile_cfg.codec, codecs)

        if preset is None:
            for p in codec.presets:
                result.append(Strategy(
                    preset       = p,
                    profile      = profile_name,
                    codec        = codec,
                    profile_args = profile_cfg.extra_args,
                ))
        else:
            if preset not in codec.presets:
                raise ValueError(
                    f"Preset '{preset}' not supported by codec '{codec.name}'. "
                    f"Supported presets: {codec.presets}"
                )
            result.append(Strategy(
                preset       = preset,
                profile      = profile_name,
                codec        = codec,
                profile_args = profile_cfg.extra_args,
            ))
    return result


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

    Attributes:
        extraction: Stream filter settings for the extraction phase.
        chunking:   Chunking strategy and scene-detection tuning.
        encoding:   Quality targets, strategy selection, and encoding tuning.
        audio:      Audio conversion profiles and per-run overrides.
        codecs:     Map of codec name → :class:`~pyqenc.models.CodecConfig`.
                    Defaults to empty dict (bundled default YAML always supplies
                    at least one codec in practice).
        profiles:   Map of profile name → :class:`ProfileConfig`.
                    Defaults to empty dict (same note as ``codecs``).

    After the full model is assembled by Pydantic, a ``model_validator`` calls
    :meth:`~EncodingConfig.resolve` so that ``encoding.resolved_targets`` and
    ``encoding.resolved_strategies`` are immediately available without any
    lazy-initialisation guard on the call site.  If the ``strategies`` or
    ``quality_targets`` strings are invalid, the ``ValueError`` raised by
    ``resolve()`` is automatically re-raised by Pydantic v2 as a
    :class:`~pydantic.ValidationError`, making invalid configs fail at
    load time before any phase runs.

    The private ``_source_paths`` attribute is populated by
    :func:`load_app_config` after construction.  It records which YAML files
    were actually loaded (in priority order) so that ``_cmd_config`` can
    display them without calling a separate discovery function.
    """

    extraction: ExtractionConfig
    chunking:   ChunkingConfig
    encoding:   EncodingConfig
    audio:      AudioConfig
    codecs:     dict[str, CodecConfig]   = Field(default_factory=dict)
    profiles:   dict[str, ProfileConfig] = Field(default_factory=dict)

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

        Args:
            data: Raw input data (typically a ``dict``).

        Returns:
            The same ``data`` dict with ``name`` injected into each codec entry,
            or ``data`` unchanged if it is not a ``dict`` (Pydantic will handle
            the type error downstream).
        """
        if isinstance(data, dict):
            codecs = data.get("codecs")
            if isinstance(codecs, dict):
                patched: dict[str, object] = {}
                for codec_name, codec_data in codecs.items():
                    if isinstance(codec_data, dict) and "name" not in codec_data:
                        codec_data = {**codec_data, "name": codec_name}
                    patched[codec_name] = codec_data
                data = {**data, "codecs": patched}
        return data

    @model_validator(mode="after")
    def _resolve_encoding(self) -> "AppConfig":
        """Trigger strategy and quality-target resolution after full assembly.

        Called automatically by Pydantic once the entire model tree has been
        validated and constructed.  Delegates to
        :meth:`EncodingConfig.resolve`, passing the codec and profile maps so
        that wildcard strategy patterns can be expanded correctly.

        Any :class:`ValueError` raised by ``resolve()`` (e.g. unknown profile
        name, unrecognised quality target metric) propagates as a Pydantic
        ``ValidationError`` — Pydantic v2 wraps ``ValueError`` from validators
        automatically.

        Returns:
            ``self`` — required by Pydantic ``mode='after'`` validators.

        Raises:
            ValidationError: If any strategy pattern or quality target string
                is invalid (wraps the underlying ``ValueError``).
        """
        self.encoding.resolve(self.codecs, self.profiles)
        return self


def load_app_config() -> AppConfig:
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
        _logger.debug("Loaded cwd config: %s", cwd_config)
    else:
        _logger.debug("CWD config absent (skipped): %s", cwd_config)

    config = AppConfig.model_validate(merged)
    config._source_paths = source_paths  # noqa: SLF001  (intentional post-init population)
    return config
