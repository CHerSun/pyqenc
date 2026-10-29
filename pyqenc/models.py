"""
Core data models for the quality-based encoding pipeline.

This module defines all data structures used throughout the pipeline,
including configuration, state tracking, and result objects.
All models use Pydantic BaseModel for validation and serialisation.
"""
# CHerSun 2026

import hashlib
import logging
from collections.abc import Iterable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import Enum, IntEnum
from pathlib import Path
from typing import Annotated, ClassVar, Final, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    PrivateAttr,
    field_validator,
    model_validator,
)

from pyqenc.constants import (
    DOWN_ARROW,
    FINGERPRINT_DIGEST_SIZE_BYTES,
    LEFT_ARROW,
    RIGHT_ARROW,
    TIME_SEPARATOR_MS,
    UP_ARROW,
)
from pyqenc.quality_metrics import (
    FIXED_COMPARISON_PAIRS,
    MeasuredMetrics,
    MetricStatKey,
    TargetedMetrics,
)
from pyqenc.utils.naming import sanitize_filesystem_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fingerprint — the standard identity-comparison value (spec 2026-10-07, Req 10)
# ---------------------------------------------------------------------------

_NBSP: Final = "\u00a0"
"""Non-breaking space — RENDERS like a plain space but is NOT yaml
whitespace (yaml folds U+0020 only), so storage strings built with it
dump as plain, unquoted scalars."""

_FINGERPRINT_BELT_SEPARATOR: Final = f"{_NBSP}·{_NBSP}"
"""The fingerprint storage-form separator (belt · token)."""

class Fingerprint(BaseModel):
    """An opaque identity token (token-only — no belt).

    THE standard identity-comparison mechanism: one value type for everything
    whose identity is opaque (file content, resolved config objects, id sets).
    ``token`` is authoritative — a hash of the owner-declared canonical form,
    REQUIRED at construction (derivations are total; an unreadable source is
    a fatal at the computing site, never a carried ``None``).
    ``Fingerprint(None, None)`` is unconstructible.

    Plain ``Fingerprint`` carries a token only; a belt magnitude the
    producer structurally guarantees (bytes for files, cardinality for sets)
    belongs to a belt-carrying type declared at the consuming field — never
    an optional member here.  Equality is pydantic's class-sensitive field
    equality and never crosses that line: a guaranteed belt is a semantic
    upgrade, not the same identity.  "Unknown" never occurs INSIDE the type:
    it lives one level up, at the missing-sidecar-field layer (an absent key
    on a sidecar is unknown, never a mismatch).

    Data is separate from means: this type knows nothing about derivations;
    each owning entity declares its own (e.g. ``Strategy.fingerprint``) and
    consumers persist and compare with no knowledge of either. Tokens are
    idempotency markers, never re-validated into the model they summarize
    (a fingerprint may omit required fields by construction) and never
    user-facing data. ``str()`` is the yaml storage form (see the
    ``FingerprintYaml`` aliases below) — belt first, token last.

    Attributes:
        token: The opaque hash token (hex string) — REQUIRED, non-empty.
    """

    model_config = ConfigDict(frozen=True)

    token: str

    @field_validator("token")
    @classmethod
    def _token_present(cls, v: str) -> str:
        """Reject empty tokens — the token is the identity; absent means unset."""
        if not v:
            raise ValueError(
                "Fingerprint token is required — derivations are total; an "
                "unreadable source is a fatal at the computing site."
            )
        return v

    def __str__(self) -> str:
        """The yaml storage form: empty belt part, separator, token.

        The separator (``_FINGERPRINT_BELT_SEPARATOR`` — non-breaking
        spaces around a middle dot, so yaml never quotes the scalar) is
        ALWAYS present; the belt part is empty for a token-only
        :class:`Fingerprint`.  Because the size part (digits or empty) can
        never contain the separator, parsing is a single split on its FIRST
        occurrence — an opaque token can never be mistaken for it.

        Returns:
            The compact storage string ``"<sep><token>"``.
        """
        return f"{_FINGERPRINT_BELT_SEPARATOR}{self.token}"


class SizedFingerprint(Fingerprint):
    """A fingerprint whose belt is guaranteed — the file/set identity.

    The type-level statement of a producer's guarantee: derivations whose
    belt is structural (``BlakeFingerprinter.of_file`` always stats the
    file, ``BlakeFingerprinter.of_ids`` always records cardinality) return
    this, so consumers read ``.size`` as a plain ``int`` with no dead
    None-handling.

    Equality requires token AND size to match — a ``SizedFingerprint`` is
    never equal to a plain :class:`Fingerprint` by design.

    Attributes:
        size: The belt magnitude — bytes for files, cardinality for id sets.
              Always present; required at construction.
    """

    size: int

    def __str__(self) -> str:
        """The yaml storage form: size, separator, token.

        Overrides :class:`Fingerprint`'s ``__str__`` to include the size
        (belt) in the storage string so round-trips reconstruct the correct
        concrete type.

        Returns:
            The compact storage string ``"<size><sep><token>"``.
        """
        return f"{self.size}{_FINGERPRINT_BELT_SEPARATOR}{self.token}"


def _fingerprint_from_str(value: str) -> Fingerprint | SizedFingerprint:
    """Parse the yaml storage string — the ``str()`` inverse.

    The constructed type follows belt presence: a present belt part yields
    :class:`SizedFingerprint`, an empty one a plain :class:`Fingerprint`.

    Args:
        value: ``"<size><sep><token>"`` — the separator is required; an
               empty size part means no belt; ``"0"`` is a real zero belt
               (an empty id set), never conflated with ``None``.

    Returns:
        The fingerprint (sized when the belt part is present).

    Raises:
        ValueError: On a missing separator, a non-digit size part, or an
                    empty token.
    """
    size_part, sep, token = value.partition(_FINGERPRINT_BELT_SEPARATOR)
    if not sep:
        raise ValueError(
            f"Invalid fingerprint string {value!r}: the belt separator "
            "is structural — use the str() storage form"
        )
    if not token:
        raise ValueError(f"Invalid fingerprint string {value!r}: empty token")
    if size_part == "":
        return Fingerprint(token=token)
    if not (size_part.isascii() and size_part.isdigit()):
        raise ValueError(
            f"Invalid fingerprint string {value!r}: bad size part {size_part!r}"
        )
    return SizedFingerprint(token=token, size=int(size_part))


def _fingerprint_from_yaml(value: object) -> object:
    """Accept the storage string wherever a fingerprint loads from yaml.

    ``str(Fingerprint)`` is the declared storage form; a native dict (the
    legacy nested dump) passes through to normal validation unchanged.

    Args:
        value: The raw yaml value — either the compact storage string or a
               legacy nested dict.

    Returns:
        The parsed :class:`Fingerprint` or :class:`SizedFingerprint` when
        *value* is a string; the original *value* otherwise (passed through
        to normal Pydantic validation).
    """
    if isinstance(value, str):
        return _fingerprint_from_str(value)
    return value


FingerprintYaml = Annotated[
    Fingerprint, BeforeValidator(_fingerprint_from_yaml), PlainSerializer(str)
]
"""``Fingerprint`` as it persists: the compact one-line storage string,
parsed back via :func:`_fingerprint_from_str` — the class stays explicitly
typed; only the yaml boundary changes form. For producers whose belt is
structural, use :data:`SizedFingerprintYaml` instead."""

SizedFingerprintYaml = Annotated[
    SizedFingerprint, BeforeValidator(_fingerprint_from_yaml), PlainSerializer(str)
]
"""The sized guarantee at the yaml boundary: persisted fields whose
producer structurally carries the belt (``of_file`` / ``of_ids``). A
belt-less value fails validation here — a corrupted or foreign record,
never a silent accept."""


class BlakeFingerprinter:
    """The single owner of ``Fingerprint`` derivations — the means (Req 10).

    ``Fingerprint`` stays a dumb value record; every token in the system is
    derived HERE: blake2b-128 over each thing's canonical form (Req 10d:
    canonical dumps are never persisted — debug reads the config / source
    of truth, not the sidecar). One method per input kind — no uniform
    ``from_payload``: the file derivation is sampled windows, not a
    canonical-form hash, and pretending otherwise would lie about it.
    """

    @staticmethod
    def _token(canonical: str) -> str:
        """blake2b-128 over the canonical form's UTF-8 bytes."""
        return hashlib.blake2b(
            canonical.encode("utf-8"), digest_size=FINGERPRINT_DIGEST_SIZE_BYTES,
        ).hexdigest()

    @classmethod
    def of_text(cls, canonical: str) -> Fingerprint:
        """A canonical serialization's fingerprint (token only).

        Args:
            canonical: The owner-declared canonical serialization
                       (deterministic for the same value by construction).

        Returns:
            The fingerprint (no belt — text has no meaningful magnitude).
        """
        return Fingerprint(token=cls._token(canonical))

    @classmethod
    def of_ids(cls, ids: Iterable[str]) -> SizedFingerprint:
        """The identity of a set of ids: hash over the sorted ids, ``size`` = count.

        ONE derivation, two vantages (spec 2026-10-07, Req 19/11a): optimization
        persists it as the chunk-set key (mismatch ⇒ wipe winners), and each
        merged output's sidecar carries it as the winner-set fingerprint.
        Order-independent by construction (sorted before hashing).

        Args:
            ids: The member ids in any order (chunk ids / winner ids).

        Returns:
            The set's fingerprint (``size`` = member count — structurally
            guaranteed, hence :class:`SizedFingerprint`).
        """
        ordered = sorted(ids)
        return SizedFingerprint(token=cls._token("\n".join(ordered)), size=len(ordered))

    _FILE_WINDOW_BYTES: ClassVar[int] = 1_048_576
    """Sampled-window size (~1 MiB): head + tail + two interior windows."""

    @classmethod
    def of_file(cls, path: Path) -> SizedFingerprint:
        """The source file's sampled-content fingerprint (spec Req 30).

        Reads head + tail + two interior windows (~1 MiB each, deterministic
        offsets), hashing the concatenation with blake2b-128; ``size`` is the
        file size — structurally guaranteed, hence the
        :class:`SizedFingerprint` return. Threat model — accident only: the
        digest guards against accidental wholesale replacement
        (re-mux/re-download of a same-named file) and in-window corruption;
        it does NOT promise detection of isolated mid-file bit rot
        (coverage is too thin) and NOT adversarial tampering (fixed windows
        are trivially preserved by an attacker).

        Args:
            path: The file to sample.

        Returns:
            The fingerprint (``size`` = file size in bytes).

        Raises:
            OSError: When the file cannot be read — a fatal at the computing
                     site (Job), never a carried ``None``.
        """
        size = path.stat().st_size
        digest = hashlib.blake2b(digest_size=FINGERPRINT_DIGEST_SIZE_BYTES)
        with open(path, "rb") as fh:
            for offset in cls._file_window_offsets(size):
                fh.seek(offset)
                digest.update(fh.read(cls._FILE_WINDOW_BYTES))
        return SizedFingerprint(size=size, token=digest.hexdigest())

    @classmethod
    def _file_window_offsets(cls, size: int) -> list[int]:
        """The four deterministic sample offsets: head, 1/3, 2/3, tail.

        For files smaller than the windows the reads simply overlap or come
        up empty (seeks past EOF read zero bytes) — the derivation stays
        total and deterministic for every size, including zero.
        """
        return sorted({
            0,
            size // 3,
            (size * 2) // 3,
            max(0, size - cls._FILE_WINDOW_BYTES),
        })


fingerprinter = BlakeFingerprinter()
"""The derivation surface callers use — ``fingerprinter.of_text/of_ids/of_file``."""



# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CleanupLevel(IntEnum):
    """Controls how aggressively intermediate files are removed.

    Attributes:
        NONE:         Keep all intermediate files (default — no ``--cleanup`` flag).
        INTERMEDIATE: Delete workspace files per artifact immediately after it is
                      marked ``COMPLETE`` (``--cleanup`` with no argument).
        ALL:          Superset of ``INTERMEDIATE``; also deletes remaining
                      intermediate directories after full pipeline success
                      (``--cleanup all``).
    """

    NONE         = 0
    INTERMEDIATE = 1
    ALL          = 2


class PhaseOutcome(Enum):
    """Work-state of a pipeline phase — never the run mode.

    This enum describes only *what state the phase's work is in*; it never
    encodes whether the run was a preview (dry-run) or a real execution. Run
    mode is owned by the runner via the ``dry_run`` flag it threads.

    Attributes:
        COMPLETED: Phase did real work and succeeded.
        SUBOPTIMAL: Phase did real work and every deliverable was produced,
                   but at least one item sits below its quality bar (a
                   search-mode winner accepted without meeting the targets).
                   Counts as complete for every consumer.
        REUSED:    All artifacts existed; no work performed (valid in both modes).
        PENDING:   Wanted work remains (one or more artifacts are ``ABSENT`` or
                   ``PARTIAL``), independent of run mode.
        FAILED:    Phase failed (``error`` field populated).
    """

    COMPLETED  = "completed"
    SUBOPTIMAL = "suboptimal"
    REUSED     = "reused"
    PENDING    = "pending"
    FAILED     = "failed"


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class Strategy(BaseModel):
    """Resolved encoding strategy — single owner of identity, codec, and ffmpeg args.

    Carries everything needed to identify the strategy — the uniform name pair
    (:meth:`display_name` for logs/sidecars, :meth:`safe_name` for filesystem
    paths; both passthroughs since profile/preset names are validated safe at
    config load) — and to encode with it: the codec's two argument stages and
    the ffmpeg arg generation.

    Attributes:
        preset:       FFmpeg preset (e.g. ``'slow'``, ``'veryslow'``).
        profile:      Profile name (e.g. ``'h265-aq'``, ``'h264-anime'``).
        codec:        Resolved codec configuration.
        profile_args: Resolved profile extra ffmpeg arguments (output stage).
    """

    model_config = ConfigDict(frozen=True)

    preset:       str
    profile:      str
    codec:        CodecConfig
    profile_args: list[str]

    FINGERPRINT_EXCLUDED_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "codec.default_quality",
        "codec.quality_range",
        "codec.quality_granularity",
        "codec.quality_max_step",
        "codec.name",
    })
    """Model fields excluded from the identity fingerprint (dotted paths).

    The exclusion set is the quality-PROPOSAL domain — the search's steering
    parameters, not product identity: ``default_quality`` is the starting
    point, ``quality_range`` / ``quality_granularity`` /
    ``quality_max_step`` bound and quantize which quality values get
    PROPOSED. An attempt at a given quality is the same product whatever
    proposals produced it (the quality value is the attempt's cache key),
    so a proposal-domain change — notably the ``-q`` CLI override, which
    rewrites the effective range — must not flip the fingerprint into the
    fatal band; the fixed-quality map (fixed mode's key) and the mode tag
    own quality-change invalidation. Found live 2026-10-09: a search→fixed
    ``-q`` toggle fired the per-strategy catastrophic fatal and demanded
    ``--force`` to wipe attempts that were perfectly valid products. The
    set is declared here, next to what it excludes; every other field
    participates by default (fail-safe: future fields over-invalidate
    rather than slip through)."""

    @property
    def fingerprint(self) -> Fingerprint:
        """The strategy's resolved-args identity: hash of the canonical dump
        minus :attr:`FINGERPRINT_EXCLUDED_FIELDS`.

        Opaque — the dump itself is never persisted, and the token is never
        re-validated into a ``Strategy`` (it omits ``default_quality``, a
        required field, by construction). Consumers persist it and compare
        with ``==`` — no model-shape knowledge at any comparison site.
        Joining the name family: display/safe/fingerprint.

        Returns:
            The strategy's identity fingerprint (token-only, no belt).
        """
        return fingerprinter.of_text(
            self.model_dump_json(exclude=self._fingerprint_exclude_spec()),
        )

    @classmethod
    def _fingerprint_exclude_spec(cls) -> dict[str, set[str]]:
        """Translate the dotted exclusion paths into pydantic's nested form."""
        spec: dict[str, set[str]] = {}
        for path in cls.FINGERPRINT_EXCLUDED_FIELDS:
            parent, _, leaf = path.rpartition(".")
            assert parent, f"only dotted paths are declared: {path!r}"
            spec.setdefault(parent, set()).add(leaf)
        return spec

    @field_validator("preset", "profile", mode="before")
    @classmethod
    def _sanitize_dots(cls, v: str) -> str:
        """Replace ASCII dots with ``TIME_SEPARATOR_MS`` so the strategy name is dot-free."""
        return v.replace(".", TIME_SEPARATOR_MS)

    def display_name(self) -> str:
        """Display name — the composed identity, verbatim (``'h265-aq+slow'``).

        The single generator; the safe form is :meth:`safe_name`.
        """
        return f"{self.profile}+{self.preset}"

    def safe_name(self) -> str:
        """Filesystem-safe name — passthrough sanitize (validated safe at load).

        Uniform pair with :meth:`display_name` (methods, like every named
        element): consumers touching the filesystem always take the safe
        name, no per-type thinking.
        """
        return sanitize_filesystem_text(self.display_name())

    def to_output_args(self, quality: Decimal, vf_filter: str | None = None) -> list[str]:
        """Expand the codec's ``encoder_args`` template into the output-stage args.

        Substitution rules applied to every element of ``codec.encoder_args``:

        - ``'{quality}'``     → replaced with ``str(quality)``.  The ``Decimal``
          value is already quantized to the codec's granularity, so ``str()``
          produces the correct representation (e.g. ``'18.5'``, ``'19'``).
          May appear multiple times (e.g. ``-cq:v {quality} -qmin {quality}``).
        - ``'{preset}'``      → replaced with ``self.preset``.
        - ``'{profile_args}'``→ expanded to ``self.profile_args`` in-place.
        - ``'{vf}'``          → replaced with the vf filter expression string
          (e.g. ``'crop=1920:800:0:140'``).  When ``{vf}`` is the entire
          element and *vf_filter* is empty/``None``, the element is silently
          dropped.  When ``{vf}`` is embedded inside a larger filter chain
          (e.g. ``'scale_cuda=format=p010le:{vf}'``), it is replaced with the
          filter string or with an empty string — a trailing ``:`` is left in
          place, which ffmpeg tolerates.

        Args:
            quality:   Quality parameter value as a ``Decimal`` already quantized
                       to the codec's granularity (CRF for x264/x265, CQ for nvenc, …).
            vf_filter: Optional ffmpeg video filter expression (e.g. ``'crop=1920:800:0:140'``).

        Returns:
            The expanded output-stage argument list (the runner owns ``-i``,
            ``-y`` and the ``.tmp`` muxer stage).

        Raises:
            ValueError: If ``codec.encoder_args`` still contains a legacy
                        ``'{input}'`` sentinel.
        """

        def _expand(args: list[str]) -> list[str]:
            quality_str = str(quality)
            result: list[str] = []
            for arg in args:
                if arg == "{profile_args}":
                    result.extend(self.profile_args)
                elif arg == "{vf}":
                    if vf_filter:
                        result.append(vf_filter)
                    else:
                        # Standalone {vf} with no filter — also drop the preceding -vf flag
                        if result and result[-1] == "-vf":
                            result.pop()
                else:
                    expanded = arg.replace("{quality}", quality_str).replace("{preset}", self.preset)
                    # {vf} embedded inside a larger filter chain string
                    if "{vf}" in expanded:
                        expanded = expanded.replace("{vf}", vf_filter or "")
                    result.append(expanded)
            return result

        result = _expand(self.codec.encoder_args)
        # repeat templating for profile args (they may carry sentinels too)
        result = _expand(result)

        if "{input}" in result:
            raise ValueError(
                f"Codec '{self.codec.name}' encoder_args contains a legacy "
                f"'{{input}}' sentinel — the runner owns '-i <path>'; move any "
                f"pre-input tokens to the codec's pre_input_args."
            )
        return result


# ---------------------------------------------------------------------------
# Scene boundary
# ---------------------------------------------------------------------------

class SceneBoundary(BaseModel):
    """A single scene boundary detected by the scene detector.

    Attributes:
        frame:             Frame number of the boundary.
        timestamp_seconds: Timestamp in seconds of the boundary.
    """

    frame:             int
    timestamp_seconds: float


# ---------------------------------------------------------------------------
# Quality / codec configuration
# ---------------------------------------------------------------------------

class QualityTarget(BaseModel):
    """Quality target specification for encoding.

    The identity is the metric-statistic pair itself — validated against the
    closed sets at construction (a mistyped metric or statistic fails here,
    never at first use). The tolerant string boundary is :meth:`parse`.

    Attributes:
        identity: The targeted metric-statistic pair.
        value:    Target value for the metric.
    """

    identity: MetricStatKey
    value:    float

    @classmethod
    def parse(cls, target_str: str) -> Self:
        """Parse quality target from string format.

        Args:
            target_str: Target string like ``'vmaf-min:95'``, ``'ssim-med:98'``,
                        or ``'vmaf-p25:90'``.

        Returns:
            QualityTarget instance.

        Raises:
            ValueError: If target string format is invalid.
        """
        try:
            metric_stat, value_str = target_str.split(":")
            identity = MetricStatKey.parse(metric_stat)
            value = float(value_str)
            return cls(identity=identity, value=value)
        except (ValueError, AttributeError) as e:
            raise ValueError(
                f"Invalid quality target format: '{target_str}'. "
                f"Expected format: 'metric-stat:value' (e.g., 'vmaf-min:95')"
            ) from e

    def __str__(self) -> str:
        return f"{self.identity.cli_key()}≥{self.value}"


def targets_as_strings(targets: list[QualityTarget]) -> list[str]:
    """Serialise quality targets to ``"metric-statistic:value"`` strings.

    The canonical persisted form of a target set (``optimization.yaml``,
    ``merge.yaml``): sorted so the list is order-independent — two runs with
    the same targets in different config order serialise identically.

    Args:
        targets: Quality targets from an :class:`EncodingPlan`.

    Returns:
        Sorted list of strings like ``["vmaf-min:93.0"]``.
    """
    return sorted(f"{t.identity.cli_key()}:{t.value}" for t in targets)


class EncodingPlan(BaseModel):
    """The run's resolved encoding inputs — derived once, consumed everywhere.

    Produced by :meth:`pyqenc.app_config.AppConfig.build_encoding_plan` at the
    run boundary (the CLI) from the raw config plus any CLI overrides, then
    threaded through ``JobPhaseResult`` like every other volatile per-run
    parameter. Phases consume this object; nobody re-resolves
    ``encoding.strategies`` downstream, and the config is never written
    after load.

    Attributes:
        strategies: Resolved strategies (identity + effective codec each).
        targets:    Resolved quality targets.
        optimize:   Whether the run size-selects strategies via the
                    optimization phase (``--no-optimize`` encodes every
                    strategy full-length instead).
        measure:    Whether reporting measurements are kept (``--no-measure``
                    opts out: merged-output measurement always; attempt
                    measurement when the fixed run also opted out of
                    optimization).
    """

    model_config = ConfigDict(frozen=True)

    strategies: list[Strategy]
    targets:    list[QualityTarget]
    optimize:   bool
    measure:    bool

    # Whether the run pins the quality knob — derived once at construction
    # in the after-validator (plain private-attr assignment works there
    # even on frozen models; verified on pydantic 2.12.5). Storage is
    # private so it can never be provided as input and stays outside the
    # schema (model_dump / equality never see it); the property is the
    # public read contract.
    _fixed_quality: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _derive_fixed_quality(self) -> Self:
        self._fixed_quality = bool(self.strategies) and all(
            s.codec.quality_better == s.codec.quality_worse
            for s in self.strategies
        )
        return self

    @model_validator(mode="after")
    def _validate_invariants(self) -> Self:
        """Construction-time plan invariants (typed — no dict plumbing).

        - No strategies, no plan: the plan IS the video encoding work; an
          empty strategy set is a configuration error, failed loudly at
          the run boundary instead of minutes into the pipeline.
        - A searched (ranged) run must carry quality targets — without a
          judging reference the search degenerates into a single
          default-quality encode. Fixed runs legitimately omit targets: the
          fixed knob is the reference and config targets drive nothing
          there.
        """
        if not self.strategies:
            raise ValueError(
                "Encoding plan has no strategies — there is nothing to encode. "
                "Check encoding.strategies / --strategies."
            )
        if not self._fixed_quality and not self.targets:
            raise ValueError(
                "Searched run has no quality targets — the search has no bar "
                "to meet. Provide targets via --targets (a target profile "
                "name or raw metric-stat:value pairs; defined profiles live "
                "in config encoding.target_profiles) "
                "(fixed runs via -q / collapsed profiles may omit targets)."
            )
        return self

    @property
    def fixed_quality(self) -> bool:
        """Whether the run pins the quality knob: every strategy's effective
        range is a single point (``quality_better == quality_worse``).

        Derived, never an input — a collapsed config profile and the
        ``-q`` override produce the same value here. Computed once at
        construction; this property is the read contract.
        """
        return self._fixed_quality

    @property
    def measures_attempts(self) -> bool:
        """Whether encoding attempts get quality measurements.

        The search always steers by measurements; a fixed run measures its
        attempts only when an elimination-driven comparison judges them
        (optimize on, multiple strategies) — without the optimization phase
        there is no anchor to judge against, so a measurement-opted-out run
        with nothing to eliminate measures nothing.
        """
        return (
            (not self._fixed_quality)
            or self.measure
            or (self.optimize and len(self.strategies) > 1)
        )

    def targeted(self, metrics: MeasuredMetrics) -> TargetedMetrics:
        """The mode-aware full→targeted narrowing front.

        A searched plan narrows to the configured targets' identities
        (guaranteed present by the construction invariants); a fixed plan
        narrows to the fixed comparison pairs. Delegates to
        ``metrics.narrowed_to`` — the mode picks the pair set, the primitive
        filters.

        Args:
            metrics: The full measured set of one measurement.

        Returns:
            The targeted subset for this plan's mode.
        """
        if self.fixed_quality:
            return metrics.narrowed_to(FIXED_COMPARISON_PAIRS)
        return metrics.narrowed_to(t.identity for t in self.targets)


def _coerce_decimal_pair(v: tuple | list) -> tuple[Decimal, Decimal]:
    """Coerce a two-element quality range to ``(Decimal, Decimal)``.

    The shared ``quality_range`` before-validator body: each element passes
    through ``str`` first so floats and YAML strings both land on the exact
    same ``Decimal`` value.  Config order is preserved — the caller's
    convention (first = better end) decides meaning, not the helper.

    Args:
        v: The two-element range (ints, floats, or strings).

    Returns:
        The pair as ``Decimal`` values.
    """
    return Decimal(str(v[0])), Decimal(str(v[1]))


def quality_alignment_error(
    value:      Decimal,
    granularity: Decimal,
    owner:      str,
) -> str | None:
    """Return the misalignment error line for *value*, or ``None`` when aligned.

    An effective-range endpoint must be an exact multiple of the codec's
    quality granularity at every layer that supplied it (codec, profile,
    CLI override): the search treats range boundaries as sentinel candidates
    and can attempt an endpoint verbatim, so a misaligned value would reach
    ``to_output_args`` — whose contract requires already-quantized values —
    and feed fractional values to integer-step encoders.

    Args:
        value:       The endpoint value to check.
        granularity: The codec's quality granularity (the required step).
        owner:       Error-message prefix naming the violating layer
                     (e.g. ``"Codec 'av1'"``).

    Returns:
        The full error line, or ``None`` when *value* sits on the grid.
    """
    if value % granularity == 0:
        return None
    below = (value / granularity).to_integral_value(ROUND_FLOOR) * granularity
    above = (value / granularity).to_integral_value(ROUND_CEILING) * granularity
    return (
        f"{owner}: quality value {value} is not an exact multiple of the "
        f"granularity {granularity}; nearest aligned values are {below} and {above}. "
        f"Range endpoints are attempted verbatim by the quality search, so they "
        f"must sit on the granularity grid."
    )


class CodecConfig(BaseModel):
    """Configuration for a video codec.

    Attributes:
        name:            Codec identifier (e.g., ``'h264-8bit'``, ``'h265-10bit'``).
        default_quality: Default quality parameter value for this codec.
        default_preset:  Default preset used when a strategy pattern omits the preset part
                         (e.g. ``'h265*'`` expands using each codec's ``default_preset``).
                         Must be one of the values in ``presets``.
        quality_range:   Valid quality range as ``(first, last)`` tuple stored in config order.
                         ``quality_range[0]`` is always the *better* end (lower CRF = better quality,
                         higher bitrate = better quality).  For CRF/CQ/QP codecs use ``[0, 51]``
                         (0 = lossless, 51 = worst).  For VBR bitrate codecs use ``[99, 0]``
                         (99 Mbit/s = best, 0 = worst).  Order is preserved as-is from config.
        quality_label:       Human-readable label for the quality parameter used in logs
                             and plots (e.g. ``'CRF'``, ``'CQ'``).
        quality_granularity: Step size for the quality search algorithm.  CRF/CQ codecs
                             typically use ``0.5``; QP-based codecs prefer ``1.0`` (integer
                             steps).  The search result is rounded to the nearest multiple
                             of this value.
        pre_input_args:      Input-stage tokens emitted before the input's
                             window flags and ``-i`` (e.g. ``-hwaccel`` /
                             ``-init_hw_device`` vulkan setup). The single
                             source of truth for the default is here in the
                             bundled config.
        encoder_args:        The output-stage ffmpeg argument template (the
                         ``"-i", "{input}"`` pair is gone — the runner owns
                         ``-i <path>``).  Sentinels:

                         - ``'{quality}'`` — replaced with the quality value; may appear
                           multiple times (e.g. ``-cq:v {quality} -qmin {quality}``).
                         - ``'{preset}'`` — replaced with the strategy preset name.
                         - ``'{profile_args}'`` — expanded to the profile's extra args.
                         - ``'{vf}'`` — replaced with the vf filter expression when
                           active (e.g. crop), or silently dropped when standalone
                           and no filter is set.  Can be embedded inside a larger
                           filter chain: ``'scale_cuda=format=p010le:{vf}'``.
        presets:         List of presets supported by this encoder.
    """

    name:                str
    quality_label:       str
    quality_granularity: Decimal
    quality_range:       tuple[Decimal, Decimal]
    default_quality:     Decimal
    default_preset:      str
    quality_max_step:    Decimal|None   = None
    presets:             list[str]      = Field(default_factory=list)
    pre_input_args:      list[str]      = Field(default_factory=list)
    encoder_args:        list[str]      = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_default_preset(self) -> Self:
        """Ensure ``default_preset`` is a member of ``presets``.

        Raises:
            ValueError: If ``default_preset`` is not in the ``presets`` list.
        """
        if self.default_preset not in self.presets:
            raise ValueError(
                f"Codec '{self.name}': default_preset '{self.default_preset}' "
                f"is not in the presets list {self.presets}."
            )
        return self

    @model_validator(mode="after")
    def _validate_range_endpoint_alignment(self) -> Self:
        """Require both ``quality_range`` endpoints on the granularity grid.

        Raises:
            ValueError: If either endpoint is not an exact multiple of
                ``quality_granularity``.
        """
        for endpoint in self.quality_range:
            error = quality_alignment_error(
                endpoint, self.quality_granularity, f"Codec '{self.name}' quality_range",
            )
            if error is not None:
                raise ValueError(error)
        return self

    @property
    def quality_better(self) -> Decimal:
        """The quality value representing the *better* end of the range (``quality_range[0]``)."""
        return self.quality_range[0]

    @property
    def quality_worse(self) -> Decimal:
        """The quality value representing the *worse* end of the range (``quality_range[1]``)."""
        return self.quality_range[1]

    @field_validator("quality_range", mode="before")
    @classmethod
    def _normalise_quality_range(
        cls, v: tuple[Decimal | float | str, Decimal | float | str] | list,
    ) -> tuple[Decimal, Decimal]:
        """Convert ``quality_range`` elements to ``Decimal``, preserving config order.

        ``quality_range[0]`` is always the *better* end as specified in config.
        """
        return _coerce_decimal_pair(v)

    @property
    def quality_log_padding(self) -> int:
        """Computed column width for quality parameter log formatting.

        Derives the correct padding width from the codec's own range and granularity
        so log columns align correctly for any codec (e.g. CRF 0–51 with gran 0.5 → 4 chars;
        VBR 0–100 with gran 0.1 → 5 chars; QP 0–63 with gran 1 → 2 chars).
        """
        max_val = max(abs(self.quality_better), abs(self.quality_worse))
        return len(str(Decimal(str(max_val)).quantize(self.quality_granularity)))

    @field_validator("default_quality", "quality_granularity", "quality_max_step", mode="before")
    @classmethod
    def _to_decimal(cls, v: Decimal | float | str | None) -> Decimal | None:
        """Coerce numeric config values to ``Decimal`` for exact arithmetic."""
        if v is None:
            return None
        return Decimal(str(v))




class AttemptMetadata(BaseModel):
    """Metadata about a completed encoded chunk attempt artifact on disk.

    Facts of the attempt as known at its exact cache address: the path is
    the statically composed attempt name (``<chunk>.q<q>.mkv`` — Req 9; no
    resolution component ever — Req 9b).

    Attributes:
        path:            Path to the encoded attempt file.
        chunk_id:        Chunk identifier (the composed name's identity part).
        strategy:        Encoding strategy display name.
        quality:         Quality value used for this attempt.
        file_size_bytes: File size in bytes.
    """

    path:            Path
    chunk_id:        str
    strategy:        str
    quality:         Decimal
    file_size_bytes: int


# ---------------------------------------------------------------------------
# Resolution — the derivable dimension value
# ---------------------------------------------------------------------------

class Resolution(BaseModel):
    """Pixel dimensions as a mathematical value: ``width x height``.

    The output resolution of any encoded product is fully DERIVABLE — the
    encode's only dimension-changing filter is the crop
    (``crop=iw-{l+r}:ih-{t+b}``), so ``output = source - crop`` BY
    CONSTRUCTION (found live 2026-10-09: 1920x1080 − ↑22↓22 = 1920x1036,
    exactly what the legacy post-encode ffprobe returned). Nothing probes
    or persists per-attempt resolutions anymore; the source fact lives in
    Extraction's ``VideoStreamInfo.resolution``, the crop in Probe's facet,
    and this operator is the single derivation.

    Human-facing only: derived for display (probe info line,
    ``probe.yaml``) — never an invalidation key, never a completeness
    input.

    Attributes:
        width:  Pixel width.
        height: Pixel height.
    """

    model_config = ConfigDict(frozen=True)

    width:  int
    height: int

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse a ``'WxH'`` string (the stream-info convention).

        Args:
            value: The resolution string (e.g. ``'1920x1080'``).

        Returns:
            The parsed dimensions.

        Raises:
            ValueError: If the string is not ``WxH`` with positive ints.
        """
        width_s, _, height_s = value.partition("x")
        try:
            width, height = int(width_s), int(height_s)
        except ValueError as exc:
            raise ValueError(f"Not a resolution string: {value!r}") from exc
        if width <= 0 or height <= 0:
            raise ValueError(f"Not a resolution string: {value!r}")
        return cls(width=width, height=height)

    def __sub__(self, crop: CropParams) -> Resolution:
        """Apply crop margins: ``source - crop = output dimensions``.

        The mathematical form of the crop filter's dimension arithmetic —
        margins subtract from the corresponding axis.

        Args:
            crop: The crop margins to subtract.

        Returns:
            The cropped dimensions.
        """
        return Resolution(
            width  = self.width - crop.left - crop.right,
            height = self.height - crop.top - crop.bottom,
        )

    def __str__(self) -> str:
        return f"{self.width}x{self.height}"


# ---------------------------------------------------------------------------
# Crop parameters
# ---------------------------------------------------------------------------

class CropParams(BaseModel):
    """Black border crop parameters.

    Attributes:
        top:    Pixels to crop from top.
        bottom: Pixels to crop from bottom.
        left:   Pixels to crop from left.
        right:  Pixels to crop from right.
    """

    top:    int = 0
    bottom: int = 0
    left:   int = 0
    right:  int = 0

    def is_empty(self) -> bool:
        """Return ``True`` if no cropping is needed."""
        return not (self.top or self.bottom or self.left or self.right)

    def to_ffmpeg_filter(self) -> str:
        """Convert to ffmpeg crop filter string.

        Returns:
            FFmpeg crop filter like ``'crop=1920:800:0:140'``.
        """
        return (
            f"crop=iw-{self.left + self.right}:ih-{self.top + self.bottom}"
            f":{self.left}:{self.top}"
        )

    def __str__(self) -> str:
        """String representation for storage and display."""
        return f"{self.top},{self.bottom},{self.left},{self.right}"

    def display(self) -> str:
        """String representation for display."""
        return f"{UP_ARROW}{self.top} {DOWN_ARROW}{self.bottom} {LEFT_ARROW}{self.left} {RIGHT_ARROW}{self.right}"

    @classmethod
    def parse(cls, crop_str: str) -> Self:
        """Parse from comma-separated string format.

        Accepts 2 or 4 comma-separated values:

        - 2 values: ``top,bottom`` (left and right default to 0)
        - 4 values: ``top,bottom,left,right``

        Args:
            crop_str: Crop string like ``"140,140"`` or ``"140,140,0,0"``.

        Returns:
            CropParams instance.

        Raises:
            ValueError: If format is invalid.

        Examples:
            >>> CropParams.parse("140,140")
            CropParams(top=140, bottom=140, left=0, right=0)
            >>> CropParams.parse("140,140,0,0")
            CropParams(top=140, bottom=140, left=0, right=0)
        """
        parts = crop_str.split(",")
        if len(parts) == 2:
            return cls(top=int(parts[0]), bottom=int(parts[1]), left=0, right=0)
        elif len(parts) == 4:
            return cls(
                top=int(parts[0]),
                bottom=int(parts[1]),
                left=int(parts[2]),
                right=int(parts[3]),
            )
        else:
            raise ValueError(
                f"Invalid crop format: '{crop_str}'. Expected 2 or 4 comma-separated values "
                f"(e.g., '140,140' or '140,140,0,0')"
            )


def _crop_from_yaml(value: object) -> object:
    """Accept the model's storage string wherever a CropParams loads from yaml.

    ``str(CropParams)`` is the declared storage form ("top,bottom,left,right"
    — always canonical four values); a native dict (the model's own default
    dump) passes through to normal validation unchanged.
    """
    if isinstance(value, str):
        return CropParams.parse(value)
    return value


CropYaml = Annotated[CropParams, BeforeValidator(_crop_from_yaml), PlainSerializer(str)]
"""``CropParams`` as it persists: the compact one-line storage string
(``crop: 22,22,0,0``), parsed back via ``CropParams.parse`` — the class
stays explicitly typed; only the yaml boundary changes form."""
