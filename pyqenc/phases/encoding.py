"""
Encoding phase for the quality-based encoding pipeline.

This module handles chunk encoding with iterative CRF adjustment to meet
quality targets, including parallel execution and artifact-based resumption.
"""
# CHerSun 2026

import asyncio
import json
import logging
import os
import shutil
import statistics
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as _dc_replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

import yaml
from pydantic import BaseModel, Field

from pyqenc.constants import (
    APPROXIMATE_INDICATOR_SYMBOL,
    BRACKET_LEFT,
    BRACKET_RIGHT,
    ENCODED_OUTPUT_DIR,
    ENCODING_WORKSPACE_DIR,
    FAILURE_SYMBOL_MAJOR,
    FAILURE_SYMBOL_MINOR,
    METRIC_KEY_QUALITY_MEASURE,
    NEUTRAL_INDICATOR_SYMBOL,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THRESHOLD_ATTEMPTS_WARNING,
    WARNING_SYMBOL,
)
from pyqenc.metrics import ConvergenceUpdate, MetricKey, MetricsCollector
from pyqenc.models import (
    AttemptMetadata,
    CleanupLevel,
    CropParams,
    EncodingPlan,
    PhaseOutcome,
    QualityTarget,
    Strategy,
)
from pyqenc.phase import (
    Artifact,
    FinalizeContext,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.phases.chunking import ChunkingPhase
from pyqenc.phases.job import JobPhase
from pyqenc.phases.optimization import OptimizationPhase, OptimizationPhaseResult
from pyqenc.phases.probe import ProbePhase
from pyqenc.quality import QualitySearchBase, QualitySearchV3, flatten_metric_stats
from pyqenc.state import ArtifactState, EncodingResultSidecar, MetricsSidecar
from pyqenc.stream_model import (
    DecimalYaml,
    EncodedChunk,
    ExtendedVideoStream,
    VideoStream,
    VideoStreamChunk,
    VideoStreamInfo,
)
from pyqenc.stream_model import (
    File as StreamFile,
)
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.ffmpeg_runner import FFmpegRequest, FFmpegRunResult, run_ffmpeg
from pyqenc.utils.fs import remove_stale_tmp_files, safe_stat_size
from pyqenc.utils.log_format import (
    fmt_chunk,
    fmt_chunk_attempt_result,
    fmt_chunk_attempt_start,
    fmt_chunk_final,
    fmt_chunk_start,
    fmt_metric_summary,
)
from pyqenc.utils.long_path import LongPath
from pyqenc.utils.visualization import QualityEvaluator
from pyqenc.utils.yaml_utils import load_model, save_model, write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# encoding.yaml — the replay aggregate (no keys; Req 24/61)
# ---------------------------------------------------------------------------

class LimiterSummaryRow(BaseModel):
    """One winning-limiter row of the encoding summary table (persisted form).

    Purely presentational data for ``encoding.yaml`` — rebuilt from the
    winner sidecars on every concluded encoding pass, shown verbatim on
    fully-reused runs.
    """

    limiter:     str                 # "<metric>_<statistic>" of the worst target
    passed:      int
    missed:      int
    med_deficit: float | None = None  # median deficit among the misses
    med_surplus: float | None = None  # median surplus among the passes (worst-target surplus)
    med_crf:     DecimalYaml         # median winning CRF of the row's chunks


class LimiterSummary(BaseModel):
    """One strategy group of the encoding summary table (persisted form)."""

    strategy: str
    chunks:   int
    rows:     list[LimiterSummaryRow]


class EncodingSummary(BaseModel):
    """The replay aggregate under ``encoding.yaml``'s single ``summary`` key.

    ``limiter`` is the winning-limiter table (presentational — target-gated);
    ``frames`` is the per-strategy Σ winner frame totals backing the
    frame-preservation re-assertion on fully-reused runs. Empty ``frames``
    holds only positive totals; empty (``{}``) marks unknown/skip semantics.
    """

    limiter: list[LimiterSummary] | None = None
    frames:  dict[str, int]              = Field(default_factory=dict)


class EncodingSidecar(BaseModel):
    """``encoding.yaml`` — the ONE ``summary`` block, nothing else.

    Winner currency is certified cross-phase by ``optimization.yaml``'s keys
    (Req 24): this file carries no invalidation key, so a missing file costs
    replay aggregates only and never invalidates anything. Freshness of the
    summary is guaranteed by the pending gate — any invalidated pair routes
    the run through the processing path, which rebuilds and re-saves it.
    """

    summary: EncodingSummary | None = None

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``encoding.yaml``; ``None`` when absent or unparseable."""
        return load_model(path, cls)

    def save(self, path: Path) -> None:
        """Write this sidecar to *path* atomically."""
        save_model(path, self)


def _probe_resolution(path: Path) -> str | None:
    """Return the video resolution of *path* as ``'WxH'``, or ``None`` on failure."""
    cmd: list[str | os.PathLike] = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            w, h = streams[0].get("width"), streams[0].get("height")
            if w and h:
                return f"{w}x{h}"
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.debug("Failed to probe resolution of %s: %s", path.name, e)
    return None


def _read_sidecar_yaml(sidecar_path: Path) -> dict | None:
    """Read a metrics/result sidecar YAML next to an encoded attempt.

    Both sidecar kinds share the core schema (keys: ``crf``, ``metrics``,
    ``frame_count``): the per-attempt metrics sidecar in the workspace
    (attempt facts only) and the encoding result sidecar in ``encoded/``
    (additionally carries ``targets_met`` — the winning attempt's recorded
    conclusion).

    Args:
        sidecar_path: Exact path to the sidecar ``.yaml`` file.

    Returns:
        Parsed sidecar dict, or ``None`` if it does not exist or cannot be
        parsed.
    """
    if sidecar_path.exists():
        try:
            with sidecar_path.open("r", encoding="utf-8") as fh:
                return yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as e:
            logger.debug("Failed to read metrics sidecar %s: %s", sidecar_path.name, e)

    return None


def _encoded_dir(work_dir: Path, strategy: Strategy) -> Path:
    """Finalized winners directory for *strategy*.

    Hard-linked winning attempts, result sidecars, and quality graphs live
    here; the presence of a result sidecar marks a pair as ``COMPLETE``.
    """
    return work_dir / ENCODED_OUTPUT_DIR / strategy.safe_name()


def read_winner_sidecar(work_dir: Path, winner: EncodedChunk) -> dict | None:
    """Read a winner's result sidecar by its static name (processing paths).

    The single sanctioned composition site for consumers that need a
    winner's facts (crf, resolution, targeted metrics, frame count,
    ``targets_met``) — recovery never calls this (Req 3); processing paths
    (the winner scan, the merge CRF graph, the fixed ruler) do.

    Args:
        work_dir: The run's work dir (locates ``encoded/<strategy>/``).
        winner:   The winner payload whose sidecar to read.

    Returns:
        Parsed sidecar dict, or ``None`` when absent or unparseable.
    """
    return _read_sidecar_yaml(
        _encoded_dir(work_dir, winner.strategy)
        / EncodedChunk.format_winner_sidecar_name(winner.chunk.safe_name())
    )


def _write_metrics_sidecar(
    attempt_path:     Path,
    crf:              Decimal,
    metrics:          dict[str, float],
    metrics_sampling: int,
    frame_count:      int,
    resolution:       str | None,
) -> None:
    """Atomically write a per-attempt sidecar alongside an encoded attempt.

    Uses ``write_yaml_atomic`` so a crash during writing never leaves a partial
    sidecar.  Stores ALL measured metric values (not filtered to current targets)
    so the quality history is reusable when quality targets change — facts of the
    attempt only; pass/fail is re-evaluated from ``metrics`` where decided.

    Args:
        attempt_path:     Path to the encoded attempt ``.mkv`` file.
        crf:              Quality value used.
        metrics:          ALL measured quality metrics dict (not filtered to targets).
        metrics_sampling: Frame subsampling factor used when metrics were measured.
        frame_count:      Frames of the attempt file from its encode run — a fact
                          of the file, carried over unchanged on re-measure;
                          ``0`` = could not be determined.
        resolution:       The attempt's actual output dimensions (``'WxH'``),
                          probed after the encode — the name carries no
                          resolution, so the sidecar is its durable home
                          (Req 9b).
    """
    sidecar = attempt_path.with_suffix(".yaml")
    data    = MetricsSidecar(
        crf         = crf,
        resolution  = resolution,
        metrics     = metrics,
        sampling    = metrics_sampling,
        frame_count = frame_count,
    )
    try:
        write_yaml_atomic(sidecar, data.model_dump(exclude_none=True))
    except (OSError, ValueError, yaml.YAMLError) as e:
        logger.warning("Failed to write metrics sidecar for %s: %s", attempt_path.name, e)


def _hardlink_or_copy(src: Path, dst: Path) -> None:
    """Hard-link *src* to *dst*, falling back to copy if cross-device.

    Creates parent directories as needed.

    Args:
        src: Source file path (the winning attempt ``.mkv``).
        dst: Destination path in ``encoded/<strategy>/``.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        logger.debug("Hard-linked %s → %s", src.name, dst)
    except OSError:
        # Cross-device link or other OS restriction — fall back to copy
        shutil.copy2(src, dst)
        logger.debug("Copied (cross-device fallback) %s → %s", src.name, dst)


def _write_encoding_result_sidecar(
    output_dir:      Path,
    chunk_id:        str,
    resolution:      str | None,
    crf:             Decimal,
    metrics:         dict[str, float],
    frame_count:     int,
    targets_met:     bool = True,
) -> None:
    """Atomically write the winner result sidecar when the search converges.

    Written as ``<chunk_id>.yaml`` (the winner stem swap) in the strategy
    output directory.  Its presence marks the ``(chunk_id, strategy)`` pair
    as ``COMPLETE``.  ``metrics`` must be the TARGETED subset (the caller
    filters against the judging targets — full sets live on attempt
    sidecars).

    Args:
        output_dir:      Strategy output directory.
        chunk_id:        Chunk identifier.
        resolution:      The winner's actual output resolution (``'WxH'``).
        crf:             Winning quality value.
        metrics:         The targeted metric subset for the winning attempt.
        frame_count:     Frames of the winning attempt (``0`` = could not be
                         determined).
        targets_met:     Whether quality targets were met; ``False`` when the
                         search was exhausted without a passing attempt.
    """
    sidecar_path = output_dir / EncodedChunk.format_winner_sidecar_name(chunk_id)
    data = EncodingResultSidecar(
        crf         = crf,
        resolution  = resolution,
        metrics     = metrics,
        frame_count = frame_count,
        targets_met = targets_met,
    )
    try:
        write_yaml_atomic(sidecar_path, data.model_dump(exclude_none=True))
        logger.debug(
            "Wrote winner result sidecar: %s (crf=%s, targets_met=%s)",
            sidecar_path.name, crf, targets_met,
        )
    except (OSError, ValueError, yaml.YAMLError) as e:
        logger.warning(
            "Failed to write winner result sidecar for %s: %s",
            chunk_id, e,
        )


# ---------------------------------------------------------------------------
# The per-pair ledger — one Artifact[EncodedChunk] per (chunk, strategy)
# ---------------------------------------------------------------------------

def _pair_placeholder(work_dir: Path, chunk: VideoStreamChunk, strategy: Strategy) -> EncodedChunk:
    """A placeholder payload for a pair row with no winner on disk.

    The pair identity (chunk + strategy) is real; the attempt-specific facts
    (file, resolution) are unknown until a winner exists — the row's
    ``ABSENT``/``PARTIAL`` state says so. Consumers read attempt facts from
    ``COMPLETE`` rows only. The placeholder file path uses the static winner
    name (the identity-derived destination the promotion writes to).
    """
    source_info = chunk.stream.stream.info
    winner = (
        work_dir / ENCODED_OUTPUT_DIR / strategy.safe_name()
        / EncodedChunk.format_winner_file_name(chunk.safe_name())
    )
    return EncodedChunk(
        stream = ExtendedVideoStream(
            stream      = VideoStream(
                file = StreamFile(path=winner),
                info = VideoStreamInfo(
                    track_id     = 0,
                    resolution   = source_info.resolution,
                    fps          = source_info.fps,
                    fps_fraction = source_info.fps_fraction,
                ),
            ),
            frame_count = 0,
            crop        = CropParams(),
        ),
        chunk    = chunk,
        strategy = strategy,
    )


def _pair_rows(
    work_dir:   Path,
    chunks:     list[VideoStreamChunk],
    strategies: list[Strategy],
) -> list[Artifact[EncodedChunk]]:
    """Build the per-pair ledger: one row per (chunk, strategy) pair.

    States come from the shared attempt-recovery machinery — a row is
    ``COMPLETE`` only when that pair's winner actually exists on disk (result
    sidecar + winning file); such rows carry the composed winner payload.
    Rows without a winner carry the placeholder pair payload. Shared by
    OptimizationPhase (test pairs) and EncodingPhase (full set).
    """
    chunk_ids      = [c.safe_name() for c in chunks]
    strategy_names = [s.display_name() for s in strategies]
    pair_recovery  = _recover_encoding_attempts(work_dir, chunk_ids, strategies)
    chunk_by_id    = {c.safe_name(): c for c in chunks}
    strategy_by_name = {s.display_name(): s for s in strategies}

    rows: list[Artifact[EncodedChunk]] = []
    for chunk_id in chunk_ids:
        for name in strategy_names:
            # Every (chunk_id, name) combination has a row — direct index.
            pair = pair_recovery.pairs[(chunk_id, name)]
            if pair.state == ArtifactState.COMPLETE and pair.winning_file is not None:
                rows.append(Artifact(
                    payload = build_encoded_chunk(
                        chunk      = chunk_by_id[chunk_id],
                        strategy   = strategy_by_name[name],
                        path       = pair.winning_file,
                        resolution = None,  # a sidecar fact — never read at recovery (Req 3)
                        frame_count= 0,     # unknown on recovery
                    ),
                    state   = ArtifactState.COMPLETE,
                ))
            else:
                rows.append(Artifact(
                    payload = _pair_placeholder(work_dir, chunk_by_id[chunk_id], strategy_by_name[name]),
                    state   = pair.state,
                ))
    return rows


def _orphan_strategy_rows(work_dir: Path, strategies: list[Strategy]) -> list[Artifact[StreamFile]]:
    """Rows for orphaned ``encoded/<strategy>/`` directories.

    A strategy directory absent from the current selection has no
    reconstructible entity (its Strategy object is gone) — the on-disk
    product itself is the only identity left. Ledger-only rows (``wanted=
    False``): retained in place, never pending; deletion only via explicit
    cleanup.
    """
    out_dir = work_dir / ENCODED_OUTPUT_DIR
    if not out_dir.exists():
        return []
    expected = {s.safe_name() for s in strategies}
    rows: list[Artifact[StreamFile]] = []
    for strategy_dir in sorted(out_dir.iterdir()):
        if strategy_dir.is_dir() and strategy_dir.name not in expected:
            rows.append(Artifact(
                payload = StreamFile(path=LongPath(strategy_dir)),
                state   = ArtifactState.COMPLETE,
                wanted  = False,
            ))
            logger.debug(
                "encoded/%s is orphaned (strategy no longer selected) — unwanted", strategy_dir.name,
            )
    return rows


# ---------------------------------------------------------------------------
# Encoding recovery helpers
# ---------------------------------------------------------------------------

@dataclass
class _EncodingRecovery:
    """Recovery state for a ``(chunk_id, strategy)`` pair — the CRF search as a whole."""

    chunk_id:     str
    strategy:     str
    state:        ArtifactState
    winning_file: Path | None = None


@dataclass
class _PhaseRecovery:
    """Recovery result for an entire optimization or encoding phase."""

    pairs:   dict[tuple[str, str], _EncodingRecovery] = field(default_factory=dict)
    pending: list[tuple[str, str]]                    = field(default_factory=list)


def _recover_encoding_attempts(
    work_dir:  Path,
    chunk_ids: list[str],
    strategies: list[Strategy],
) -> _PhaseRecovery:
    """Classify all ``(chunk_id, strategy)`` pairs from a single directory scan per strategy.

    For each strategy, lists ``encoded/<strategy>/`` once and consumes the
    STATIC expected names: a pair is ``COMPLETE`` iff both
    ``<chunk_id>.mkv`` and ``<chunk_id>.yaml`` (the winner file and its
    result sidecar, composed by :class:`EncodedChunk`) are present in the
    listing. No name parsing, no per-pair globs, no file reads.

    CRF history for pending pairs is not pre-loaded: when the encoding worker
    actually picks a pair up, each attempt the search proposes is checked
    against the attempt workspace first — an existing file at that
    (chunk, strategy, crf) is a cache hit (``[reused]``), so replaying an
    unchanged pair re-runs the search without re-encoding.

    Args:
        work_dir:   Pipeline working directory.
        chunk_ids:  Chunk identifiers to recover.
        strategies: Strategies to recover (pairs keyed by display name).

    Returns:
        ``_PhaseRecovery`` with per-pair recovery state and pending list.
    """
    pairs:   dict[tuple[str, str], _EncodingRecovery] = {}
    pending: list[tuple[str, str]]                    = []

    complete_count = absent_count = 0

    for strategy in strategies:
        name = strategy.display_name()
        # The finalized output directory for this strategy under encoded/.
        encoded_dir = _encoded_dir(work_dir, strategy)

        # Static-name membership over one listing. Layout in encoded/<strategy>/:
        #   <chunk_id>.mkv     — the promoted winner (pure chunk identity)
        #   <chunk_id>.yaml    — the winner result sidecar (stem swap)
        #   <chunk_id>.png     — the winner quality graph (optional)
        present_names: set[str] = set()
        if encoded_dir.exists():
            present_names = {f.name for f in encoded_dir.iterdir() if f.is_file()}

        for chunk_id in chunk_ids:
            complete = (
                EncodedChunk.format_winner_file_name(chunk_id) in present_names
                and EncodedChunk.format_winner_sidecar_name(chunk_id) in present_names
            )
            if complete:
                pairs[(chunk_id, name)] = _EncodingRecovery(
                    chunk_id     = chunk_id,
                    strategy     = name,
                    state        = ArtifactState.COMPLETE,
                    winning_file = encoded_dir / EncodedChunk.format_winner_file_name(chunk_id),
                )
                complete_count += 1
            else:
                pairs[(chunk_id, name)] = _EncodingRecovery(
                    chunk_id = chunk_id,
                    strategy = name,
                    state    = ArtifactState.PARTIAL
                    if EncodedChunk.format_winner_file_name(chunk_id) in present_names
                    else ArtifactState.ABSENT,
                )
                absent_count += 1
                pending.append((chunk_id, name))

    logger.debug(
        "Attempts recovery: %d pair(s) total — %d COMPLETE, %d not-complete",
        len(pairs), complete_count, absent_count,
    )
    return _PhaseRecovery(pairs=pairs, pending=pending)


@dataclass
class ChunkEncodingResult:
    """Result of encoding a single chunk.

    Attributes:
        chunk_id:     Chunk identifier.
        strategy:     Strategy used.
        success:      Whether encoding succeeded.
        targets_met:  Whether quality targets were met; ``False`` when the search
                      was exhausted and the best non-passing attempt was accepted.
        final_crf:    Final CRF value used.
        attempts:     Number of encoding attempts.
        encoded_file: Metadata for the final encoded attempt artifact.
        frame_count:  Frames of the winning attempt's file when known — from
                      the fresh encode run, or carried from the attempt
                      sidecar on a cache hit; 0 when unknown.  Feeds the
                      winner composition; the preservation invariant sums the
                      winner result sidecars instead.
        reused:       Whether existing encoding was reused.
        error:        Error message if failed.
    """

    chunk_id:     str
    strategy:     str
    success:      bool
    targets_met:  bool                  = True
    final_crf:    Decimal        | None = None
    attempts:     int                   = 0
    encoded_file: AttemptMetadata | None = None
    frame_count:  int                     = 0
    reused:       bool                  = False
    error:        str            | None = None


@dataclass
class _WinnerScan:
    """End-of-run scan over the winner result sidecars.

    One read per winner feeds two consumers: the winning-limiter tallies
    (when targets are configured) and the frame accounting behind the
    frame-preservation invariant and its persisted aggregate.

    Attributes:
        summaries:    Per-strategy limiter summaries, or ``None`` when there
                      is nothing to show (no targets, or no readable sidecars).
        frames_known: Whether every winner contributed a frame count.
        frames:       Strategy display name -> ``[(chunk safe name, frames)]``
                      per winner with a readable sidecar — the violation-detail
                      source; partial when ``frames_known`` is ``False``.
    """

    summaries:    list[LimiterSummary] | None
    frames_known: bool
    frames:       dict[str, list[tuple[str, int]]]

    def frame_totals(self) -> dict[str, int]:
        """Per-strategy Σ winner frame counts (valid only when ``frames_known``)."""
        return {name: sum(n for _, n in pairs) for name, pairs in self.frames.items()}


@dataclass
class EncodingResult:
    """Result of encoding all chunks.

    Attributes:
        encoded_chunks: Winners grouped by strategy display name (insertion
                        order is completion order — consumers sort explicitly;
                        the payload carries its own chunk + strategy identity).
        reused_count:   Number of chunks reused from previous runs.
        encoded_count:  Number of chunks newly encoded.
        outcome:        Phase outcome.
        failed_chunks:  List of chunk IDs that failed.
        error:          Error message if pipeline failed.
    """

    encoded_chunks: dict[str, list[EncodedChunk]] = field(default_factory=dict)
    reused_count:   int                         = 0
    encoded_count:  int                         = 0
    outcome:        PhaseOutcome                = PhaseOutcome.COMPLETED
    failed_chunks:  list[str]                   = field(default_factory=list)
    error:          str | None                  = None
    winner_scan:    _WinnerScan | None          = None


class ChunkEncoder:
    """Handles encoding of individual chunks with CRF adjustment.

    This class manages the iterative encoding process for a single chunk,
    adjusting CRF values until quality targets are met.
    """

    def __init__(
        self,
        quality_evaluator: QualityEvaluator,
        work_dir:          Path,
        collector:         MetricsCollector,
        crop_params:       CropParams | None = None,
        cleanup_level:     CleanupLevel      = CleanupLevel.NONE,
        visual_hash:       bool              = True,
        metrics_sampling:  int               = 3,
        metric_prefix:     MetricKey         = MetricKey.ENCODING,
        measure_attempts:  bool              = True,
    ):
        """Initialize chunk encoder.

        Args:
            quality_evaluator: Quality evaluator for metric calculation.
            work_dir:          Working directory for artifacts.
            collector:         Metrics collector for per-attempt timing.
            crop_params:       Optional crop parameters to apply to every chunk attempt.
            cleanup_level:     Controls deletion of intermediate attempt files after
                               a pair converges.
            visual_hash:       When ``True``, prepend a deterministic emoji to every
                               chunk log line for visual distinction in parallel output.
            metrics_sampling:  Frame subsampling factor for quality metric generation.
            metric_prefix:     Top-level key prefixing this encoder's dotted timing
                               keys — ``<prefix>.<strategy>`` per ffmpeg encode and
                               ``<prefix>.quality_measure`` per quality evaluation.
                               EncodingPhase uses the default (``encoding``);
                               OptimizationPhase passes ``optimization`` so its test
                               encodes are attributed to the owning phase.
            measure_attempts:  When ``True`` (default) every encoded attempt gets a
                               quality evaluation. When ``False``, attempts are
                               encoded and promoted without measurement — the seam
                               exists for measurement cost control (TODO §83 owns
                               default-flipping and the metrics-absence tolerance);
                               callers exercising it must run a single-point quality
                               domain where the search never consumes metrics.
        """
        self.quality_evaluator = quality_evaluator
        self.work_dir          = work_dir
        self._collector        = collector
        self._crop_params      = crop_params
        self._cleanup_level    = cleanup_level
        self._visual_hash      = visual_hash
        self._metrics_sampling = metrics_sampling
        self._metric_prefix    = metric_prefix
        self._measure_attempts = measure_attempts

    def _get_output_dir(self, strategy: Strategy) -> Path:
        """Get the CRF search workspace directory for *strategy*.

        Attempt files (intermediate) are written here during the CRF search.
        On convergence the winning attempt is hard-linked into ``_get_encoded_dir``.

        Args:
            strategy: Encoding strategy.

        Returns:
            Path to ``<work_dir>/encoding/<safe_strategy>/``.
        """
        return self.work_dir / ENCODING_WORKSPACE_DIR / strategy.safe_name()

    def _get_encoded_dir(self, strategy: Strategy) -> Path:
        """Get the finalized output directory for *strategy*.

        Hard-linked winning attempts, result sidecars, and quality graphs are
        written here.  The presence of a result sidecar marks a pair as
        ``COMPLETE``.

        Args:
            strategy: Encoding strategy.

        Returns:
            Path to ``<work_dir>/encoded/<safe_strategy>/``.
        """
        return _encoded_dir(self.work_dir, strategy)

    def _get_attempt_path(
        self,
        chunk_id: str,
        strategy: Strategy,
        crf: Decimal,
    ) -> Path:
        """The attempt path for a (chunk, quality) pair — the exact cache address.

        Naming pattern: ``<chunk_id>.q<quality>.mkv`` — the quality is the
        search's cache key, and the name is fully known before encoding
        begins (no resolution component, no post-encode rename — Req 9/9b).

        Args:
            chunk_id: Chunk identifier (e.g. ``'00꞉00꞉00․000-00꞉05꞉20․000'``).
            strategy: Encoding strategy.
            crf:      Quality value used for this attempt (already quantized
                      to the codec's granularity).

        Returns:
            Path to the attempt file.
        """
        output_dir = self._get_output_dir(strategy)
        return output_dir / EncodedChunk.format_attempt_file_name(chunk_id, crf)

    def _check_existing_encoding(
        self,
        chunk_id:   str,
        strategy:   Strategy,
        crf:        Decimal,
    ) -> AttemptMetadata | None:
        """Check if a complete encoded attempt already exists at the exact address.

        Composes the attempt's static name for the proposed quality and
        checks existence — no globbing, no name parsing (Req 9). Resolution
        matching no longer participates: it is a sidecar fact, verified with
        the rest of the sidecar at pick-up.

        Args:
            chunk_id: Chunk identifier.
            strategy: Encoding strategy.
            crf:      Quality value to look for.

        Returns:
            ``AttemptMetadata`` if the attempt file exists, ``None`` otherwise.
        """
        candidate = self._get_attempt_path(chunk_id, strategy, crf)
        try:
            size = candidate.stat().st_size
        except OSError:
            return None
        if size == 0:
            return None
        return AttemptMetadata(
            path            = candidate,
            chunk_id        = chunk_id,
            strategy        = strategy.display_name(),
            crf             = crf,
            resolution      = "",
            file_size_bytes = size,
        )

    def _finalize_winning_attempt(
        self,
        strategy:        Strategy,
        chunk_id:        str,
        resolution:      str | None,
        winning_attempt: Path,
        crf:             Decimal,
        metrics:         dict[str, float],
        frame_count:     int,
        quality_targets: list[QualityTarget],
        targets_met:     bool = True,
    ) -> None:
        """Promote the winning attempt to the static winner name and write the sidecar.

        On quality-search convergence:
        1. Hard-link the winning ``.mkv`` from ``encoding/<strategy>/`` into
           ``encoded/<strategy>/<chunk_id>.mkv`` — the statically composed
           winner name (pure chunk identity, no quality/resolution — Req 1/2).
        2. Hard-link the winning ``.png`` quality graph (if present) alongside
           it under the winner stem.
        3. Write the winner result sidecar ``<chunk_id>.yaml`` into
           ``encoded/<strategy>/`` — its presence marks the pair as
           ``COMPLETE``.  Its metrics are the TARGETED subset (filtered here
           against the judging targets; full sets stay on attempt sidecars).

        Args:
            strategy:        Encoding strategy.
            chunk_id:        Chunk identifier.
            resolution:      The winner's actual output resolution (``'WxH'``).
            winning_attempt: Path to the winning attempt ``.mkv`` in ``encoding/``.
            crf:             Winning quality value.
            metrics:         All measured metric values for the winning attempt.
            frame_count:     Frames of the winning attempt — the winning encode
                             run's count, or the attempt sidecar's count for a
                             cache-hit winner; ``0`` = could not be determined.
            quality_targets: The targets that judged the pair (the metrics filter).
            targets_met:     Whether quality targets were met; ``False`` when the
                             search was exhausted without a passing attempt.
        """
        encoded_dir = self._get_encoded_dir(strategy)
        encoded_dir.mkdir(parents=True, exist_ok=True)

        dst_mkv = encoded_dir / EncodedChunk.format_winner_file_name(chunk_id)
        if not dst_mkv.exists():
            _hardlink_or_copy(winning_attempt, dst_mkv)

        src_graph = winning_attempt.with_suffix(".png")
        if src_graph.exists():
            dst_graph = encoded_dir / f"{chunk_id}.png"
            if not dst_graph.exists():
                _hardlink_or_copy(src_graph, dst_graph)

        targeted_keys = {f"{t.metric}_{t.statistic}" for t in quality_targets}
        targeted_metrics = {k: v for k, v in metrics.items() if k in targeted_keys}
        _write_encoding_result_sidecar(
            output_dir  = encoded_dir,
            chunk_id    = chunk_id,
            resolution  = resolution,
            crf         = crf,
            metrics     = targeted_metrics,
            frame_count = frame_count,
            targets_met = targets_met,
        )

        # Intermediate cleanup: delete all attempt files for this pair from
        # encoding/ — only after the promotion and sidecar are safely written.
        if self._cleanup_level >= CleanupLevel.INTERMEDIATE:
            encoding_dir = self._get_output_dir(strategy)
            if encoding_dir.exists():
                # The attempt's exact q values are workspace facts — sweep the
                # pair's attempts by prefix (deletion, not identity parsing).
                for attempt_file in list(encoding_dir.glob(f"{chunk_id}.q*.mkv")):
                    # Delete the attempt .mkv, its per-attempt sidecar, and its graph
                    for related in (
                        attempt_file,
                        attempt_file.with_suffix(".yaml"),
                        attempt_file.with_suffix(".png"),
                    ):
                        if related.exists():
                            try:
                                related.unlink()
                                logger.debug("Intermediate cleanup: deleted %s", related.name)
                            except OSError as exc:
                                logger.warning(
                                    "Intermediate cleanup: could not delete %s: %s",
                                    related.name, exc,
                                )
                    # Also remove the per-attempt metrics subfolder if present
                    metrics_dir = encoding_dir / attempt_file.stem
                    if metrics_dir.is_dir():
                        try:
                            shutil.rmtree(metrics_dir)
                            logger.debug(
                                "Intermediate cleanup: deleted metrics dir %s", metrics_dir.name,
                            )
                        except OSError as exc:
                            logger.warning(
                                "Intermediate cleanup: could not delete metrics dir %s: %s",
                                metrics_dir.name, exc,
                            )

    def _encode_with_ffmpeg(
        self,
        chunk:       VideoStreamChunk,
        strategy:    Strategy,
        crf:         Decimal,
        output_file: Path,
    ) -> FFmpegRunResult | None:
        """Encode the chunk window from the source via the runner.

        Direct-from-source: the input is ``chunk.as_input()`` (the source
        file + ``-map`` selector + the window's input-side ``-ss``/``-t``),
        merged with the codec's pre-input stage; the output stage comes from
        the strategy template. The runner owns ``-i``, ``-y`` and the
        ``.tmp``-then-rename protocol.

        Args:
            chunk:       The chunk window to encode.
            strategy:    Encoding strategy (provides both argument stages).
            crf:         CRF value to use.
            output_file: Intended final output path.

        Returns:
            The run's ``FFmpegRunResult`` (its ``frame_count`` feeds the
            preservation invariant), or ``None`` on failure.
        """
        output_file.parent.mkdir(parents=True, exist_ok=True)

        vf_filter = (
            self._crop_params.to_ffmpeg_filter()
            if self._crop_params and not self._crop_params.is_empty()
            else None
        )

        request = FFmpegRequest(
            inputs      = [
                _dc_replace(
                    chunk.as_input(),
                    pre_input_args = tuple(strategy.codec.pre_input_args),
                ),
            ],
            output_args = tuple(strategy.to_output_args(crf, vf_filter=vf_filter)),
            output      = output_file,
        )

        try:
            result = run_ffmpeg(request)

            if not result.success:
                logger.error(
                    "FFmpeg encoding failed with code %d for chunk %s",
                    result.returncode, chunk.safe_name(),
                )
                return None

            return result

        except (OSError, RuntimeError) as e:
            logger.error("Exception during encoding: %s", e)
            return None

    def encode_chunk(
        self,
        chunk:            VideoStreamChunk,
        strategy:         Strategy,
        quality_targets:  list[QualityTarget],
        initial_crf:      Decimal,
        force:            bool  = False,
    ) -> ChunkEncodingResult:
        """Encode the chunk window, adjusting CRF until quality targets met.

        The window reads the source directly (``chunk.as_input()``); quality
        compares the attempt against the same window (per-side crop: empty for
        the attempt, the detected crop for the source window).

        Args:
            chunk:           The chunk window to encode.
            strategy:        Encoding strategy.
            quality_targets: Quality targets to meet.
            initial_crf:     Initial CRF value (if no history available).
            force:           If ``False``, reuse existing encoding that meets targets.

        Returns:
            ChunkEncodingResult with encoding outcome.
        """
        logger.debug(fmt_chunk_start(strategy.display_name(), chunk.safe_name(), self._visual_hash))

        search = QualitySearchV3(
            quality_better   = strategy.codec.quality_better,
            quality_worse    = strategy.codec.quality_worse,
            quality_targets  = quality_targets,
            granularity      = strategy.codec.quality_granularity,
            quality_max_step = strategy.codec.quality_max_step,
        )
        current_q      = initial_crf
        attempt_number = 0
        final_attempt:      AttemptMetadata | None = None
        best_fail_attempt:  AttemptMetadata | None = None
        last_frame_count:   int                    = 0
        frame_counts:  dict[Path, int]             = {}
        """Attempt file path → its frame count when known this session (fresh
        encode run, or carried from the attempt sidecar on a cache hit /
        re-measure; 0 = could not be determined).  Feeds the winner sidecar
        and the result's frame_count."""
        all_metrics_by_path: dict[Path, dict[str, float]] = {}
        """Attempt file path → its full measured metrics (every metric, not
        target-filtered) — same data the attempt sidecar persists.  Feeds the
        winner result sidecar, whose metrics contract is "all measured
        metric values of the winning attempt"."""
        _any_real_work: bool                       = False

        while True:
            attempt_number += 1

            if attempt_number == THRESHOLD_ATTEMPTS_WARNING:
                logger.warning(
                    fmt_chunk(strategy.display_name(), chunk.safe_name(),
                              f"reached {THRESHOLD_ATTEMPTS_WARNING} attempts without meeting targets — "
                              "continuing search",
                              self._visual_hash)
                )

            logger.debug(fmt_chunk_attempt_start(strategy.display_name(), chunk.safe_name(), attempt_number, current_q, strategy.codec.quality_label, self._visual_hash, strategy.codec.quality_log_padding))

            # The attempt's final name is fully known before encoding begins
            # (quality = the cache key; no resolution component — Req 9b), so
            # there is no post-encode rename: the encoded output lands at its
            # final address and the probed resolution becomes a sidecar fact.
            output_dir = self._get_output_dir(strategy)
            output_dir.mkdir(parents=True, exist_ok=True)

            # Check for an existing attempt at the exact proposed address
            # (exact-name compose, no scan — Req 9).
            goto_eval   = False
            output_file: Path | None = None
            attempt_frames: int = 0
            attempt_resolution: str = ""
            if not force:
                existing = self._check_existing_encoding(
                    chunk.safe_name(), strategy, current_q
                )
                if existing is not None:
                    sidecar = _read_sidecar_yaml(existing.path.with_suffix(".yaml"))
                    # Validate sidecar contains all required metric keys.
                    required_keys        = {f"{t.metric}_{t.statistic}" for t in quality_targets}
                    raw_sidecar_sampling = sidecar.get("sampling") if sidecar is not None else None
                    sidecar_sampling     = int(raw_sidecar_sampling) if raw_sidecar_sampling is not None else None
                    sampling_stale       = (
                        sidecar_sampling is not None
                        and sidecar_sampling != self._metrics_sampling
                    )
                    sidecar_valid = (
                        sidecar is not None
                        and not sampling_stale
                        and required_keys.issubset(sidecar.get("metrics", {}).keys())
                    )
                    # The file's frame count survives regardless of sidecar
                    # staleness — re-measure never changes the encoded file
                    # (0 = not on record).
                    file_frames = int(sidecar.get("frame_count") or 0) if sidecar is not None else 0
                    if sidecar_valid and sidecar is not None:
                        # Full cache hit — no real work performed.
                        frame_counts[existing.path] = file_frames
                        all_sidecar_metrics: dict[str, float] = {
                            k: float(v) for k, v in sidecar.get("metrics", {}).items()
                        }
                        all_metrics_by_path[existing.path] = all_sidecar_metrics
                        targets_set_reused = {f"{t.metric}_{t.statistic}" for t in quality_targets}
                        metrics_dict: dict[str, float] = {
                            k: v for k, v in all_sidecar_metrics.items() if k in targets_set_reused
                        }
                        targets_met: bool = all(
                            all_sidecar_metrics.get(f"{t.metric}_{t.statistic}", 0.0) >= t.value
                            for t in quality_targets
                        )
                        _worst         = QualitySearchBase.find_worst_target(metrics_dict, quality_targets)
                        metric_summary = fmt_metric_summary(
                            metrics_dict,
                            worst_key    = f"{_worst[0].metric}_{_worst[0].statistic}" if _worst else None,
                            worst_passed = (_worst[1] >= 0) if _worst else True,
                        )
                        pass_fail      = (
                            f"{SUCCESS_SYMBOL_MINOR} pass"
                            if targets_met
                            else f"{FAILURE_SYMBOL_MINOR} miss"
                        )
                        prev_best = search.best_quality
                        next_q    = search.record(existing.crf, metrics_dict)

                        best_string = ""
                        if search.best_targets_met and (prev_best is None or search.best_quality != prev_best):
                            best_string   = " NEW BEST"
                            final_attempt = existing
                        elif not search.best_targets_met and search.best_quality == existing.crf:
                            best_fail_attempt = existing

                        summary_suffix = f" ({metric_summary})" if metric_summary else ""
                        logger.info(
                            fmt_chunk_attempt_result(
                                strategy.display_name(), chunk.safe_name(), attempt_number,
                                f"{pass_fail} with {strategy.codec.quality_label} {str(existing.crf).rjust(strategy.codec.quality_log_padding)}{summary_suffix}{best_string} [reused]",
                                self._visual_hash,
                            )
                        )

                        if next_q is None:
                            break
                        current_q = next_q
                        continue
                    else:
                        # File exists but sidecar is missing, incomplete, or stale — re-measure.
                        reason = (
                            f"sampling changed ({sidecar_sampling} → {self._metrics_sampling})"
                            if sampling_stale
                            else "sidecar missing or incomplete"
                        )
                        logger.info(
                            fmt_chunk(strategy.display_name(), chunk.safe_name(),
                                f"existing attempt ({strategy.codec.quality_label.lower()}={str(existing.crf).rjust(strategy.codec.quality_log_padding)}) — re-evaluating metrics ({reason})",
                                self._visual_hash),
                        )
                        _any_real_work  = True
                        output_file     = existing.path
                        attempt_frames  = file_frames
                        attempt_resolution = str(sidecar.get("resolution") or "") if sidecar is not None else ""
                        goto_eval       = True

            if not goto_eval:
                # Encode — real work.
                _any_real_work = True
                output_file    = self._get_attempt_path(
                    chunk.safe_name(), strategy, crf=current_q
                )
                with self._collector.time(self._metric_prefix, strategy.display_name()):
                    run_result = self._encode_with_ffmpeg(
                        chunk, strategy, current_q, output_file
                    )

                if run_result is None:
                    error_msg = f"Encoding failed for chunk {chunk.safe_name()}"
                    logger.error(error_msg)
                    return ChunkEncodingResult(
                        chunk_id    = chunk.safe_name(),
                        strategy    = strategy.display_name(),
                        success     = False,
                        targets_met = False,
                        attempts    = attempt_number,
                        error       = error_msg,
                    )

                # Preservation invariant: attempts of the same chunk
                # must encode the same frames — a differing count is an error.
                attempt_frames = run_result.frame_count or 0
                if attempt_frames > 0:
                    if last_frame_count > 0 and attempt_frames != last_frame_count:
                        logger.critical(
                            "Frame count disagreement between attempts of chunk %s: "
                            "%d vs %d — the same window must encode the same frames",
                            chunk.safe_name(), last_frame_count, attempt_frames,
                        )
                        return ChunkEncodingResult(
                            chunk_id    = chunk.safe_name(),
                            strategy    = strategy.display_name(),
                            success     = False,
                            targets_met = False,
                            attempts    = attempt_number,
                            error       = f"Attempt frame count mismatch for {chunk.safe_name()}",
                        )
                    last_frame_count = attempt_frames
                    # Vocal cross-check vs the detector-derived chunk count:
                    # ±1 boundary disagreement is an expected
                    # artifact of seek-target rounding, not a lost frame.
                    if chunk.frame_count > 0 and attempt_frames != chunk.frame_count:
                        logger.warning(
                            "Chunk %s: attempt encoded %d frame(s) vs detector-derived %d "
                            "(boundaries [%s, %s)) — seek rounding may shift a boundary frame; "
                            "the invariant sums remain the hard verification",
                            chunk.safe_name(), attempt_frames, chunk.frame_count,
                            chunk.start_timestamp, chunk.end_timestamp,
                        )

                # Probe the actual output resolution (crop may change the
                # dimensions) — a sidecar fact; the file's name is already
                # final, so there is no rename (Req 9b).
                attempt_resolution = _probe_resolution(output_file) or ""

            assert output_file is not None

            # Evaluate quality — raw metric logs/stats go into a per-attempt subfolder;
            # the plot and YAML sidecar stay next to the .mkv. The measure_attempts
            # seam off-path (TODO §83) skips evaluation entirely: no metrics, no
            # verdict — single-point domains accept the attempt unconditionally.
            if self._measure_attempts:
                with self._collector.time(self._metric_prefix, METRIC_KEY_QUALITY_MEASURE):
                    evaluation = self.quality_evaluator.evaluate_chunk(
                        encoded              = output_file,
                        reference            = chunk.as_input(),
                        ref_crop             = self._crop_params or CropParams(),
                        output_dir           = output_file.parent,
                        duration_seconds     = chunk.duration_seconds,
                        fps_value            = chunk.stream.stream.info.fps_fraction,
                        subsample_factor     = self._metrics_sampling,
                        plot_path            = output_file.parent / f"{output_file.stem}.png",
                        chunk_start_seconds  = chunk.start_timestamp,
                    )
                all_metrics         = flatten_metric_stats(evaluation.metrics)
                # Live comparator verdict — a DECISION after measurement
                # (Req 53), never persisted (the attempt sidecar stores
                # facts only).
                attempt_targets_met = not QualitySearchBase.failed_targets(
                    all_metrics, quality_targets,
                )
            else:
                all_metrics         = {}
                attempt_targets_met = True

            # Collect ALL measured metrics (not filtered to current targets) for the sidecar
            # so the quality history is reusable when quality targets change.
            # Targeted metrics subset (for search and convergence decisions).
            targets_set  = {f"{t.metric}_{t.statistic}" for t in quality_targets}
            metrics_dict = {k: v for k, v in all_metrics.items() if k in targets_set}

            # Record the attempt's facts and write the per-attempt sidecar
            # atomically (resolution included — the name carries none).
            frame_counts[output_file] = attempt_frames
            all_metrics_by_path[output_file] = all_metrics
            _write_metrics_sidecar(
                output_file, current_q, all_metrics,
                self._metrics_sampling, attempt_frames,
                resolution=attempt_resolution or None,
            )

            # Build AttemptMetadata for this attempt.
            attempt_meta = AttemptMetadata(
                path            = output_file,
                chunk_id        = chunk.safe_name(),
                strategy        = strategy.display_name(),
                crf             = current_q,
                resolution      = attempt_resolution,
                file_size_bytes = output_file.stat().st_size,
            )

            prev_best = search.best_quality
            next_q    = search.record(current_q, metrics_dict)

            best_string = ""
            if search.best_targets_met and (prev_best is None or search.best_quality != prev_best):
                best_string   = " NEW BEST"
                final_attempt = attempt_meta
            elif not search.best_targets_met and search.best_quality == current_q:
                best_fail_attempt = attempt_meta

            _worst         = QualitySearchBase.find_worst_target(metrics_dict, quality_targets)
            metric_summary = fmt_metric_summary(
                metrics_dict,
                worst_key    = f"{_worst[0].metric}_{_worst[0].statistic}" if _worst else None,
                worst_passed = (_worst[1] >= 0) if _worst else True,
            )
            pass_fail      = (
                f"{SUCCESS_SYMBOL_MINOR} pass"
                if attempt_targets_met
                else f"{FAILURE_SYMBOL_MINOR} miss"
            )
            summary_suffix = f" ({metric_summary})" if metric_summary else ""
            logger.info(
                fmt_chunk_attempt_result(
                    strategy.display_name(), chunk.safe_name(), attempt_number,
                    f"{pass_fail} with {strategy.codec.quality_label} {str(current_q).rjust(strategy.codec.quality_log_padding)}{summary_suffix}{best_string}",
                    self._visual_hash,
                )
            )

            if next_q is None:
                break

            logger.debug("Adjusting %s from %s to %s", strategy.codec.quality_label, current_q, next_q)
            current_q = next_q

        # --- Post-loop: finalize ---

        if search.best_targets_met and final_attempt is not None:
            assert search.best_quality is not None, "a passing attempt implies a measured quality"
            worst      = QualitySearchBase.find_worst_target(search.best_metrics, quality_targets) if search.best_metrics else None
            limited_by = f"{worst[0].metric}_{worst[0].statistic}" if worst is not None else None
            logger.info(fmt_chunk_final(
                strategy.display_name(), chunk.safe_name(), search.best_quality, attempt_number,
                strategy.codec.quality_label, self._visual_hash, strategy.codec.quality_log_padding,
                limited_by,
            ))
            self._finalize_winning_attempt(
                strategy        = strategy,
                chunk_id        = chunk.safe_name(),
                resolution      = final_attempt.resolution,
                winning_attempt = final_attempt.path,
                crf             = search.best_quality,
                metrics         = all_metrics_by_path.get(final_attempt.path, {}),
                frame_count     = frame_counts.get(final_attempt.path, 0),
                quality_targets = quality_targets,
                targets_met     = True,
            )
        elif not search.best_targets_met and best_fail_attempt is not None:
            assert search.best_quality is not None, "a surviving attempt implies a measured quality"
            # Every accepted winner logs an acceptance line in the uniform
            # success shape, with severity-carrying symbols: a single-point
            # (fixed) domain misses the ruler by construction — a soft ≈ at
            # INFO (the anchor is an approximation, not a user target); a
            # ranged domain that exhausted without passing is a real — if
            # bypassable — problem: ❌ at WARNING, distinct from per-attempt
            # ✘ misses.
            worst = (
                QualitySearchBase.find_worst_target(search.best_metrics, quality_targets)
                if search.best_metrics else None
            )
            limited_by = f"{worst[0].metric}_{worst[0].statistic}" if worst is not None else None
            if strategy.codec.quality_better == strategy.codec.quality_worse:
                status, label, emit = (
                    f"miss {APPROXIMATE_INDICATOR_SYMBOL}",
                    strategy.codec.quality_label, logger.info,
                )
            else:
                status, label, emit = (
                    f"exhausted {FAILURE_SYMBOL_MAJOR}",
                    f"best {strategy.codec.quality_label}", logger.warning,
                )
            emit(fmt_chunk_final(
                strategy.display_name(), chunk.safe_name(), search.best_quality, attempt_number,
                quality_label    = label,
                use_visual_hash  = self._visual_hash,
                quality_padding  = strategy.codec.quality_log_padding,
                limited_by       = limited_by,
                status           = status,
            ))
            self._finalize_winning_attempt(
                strategy        = strategy,
                chunk_id        = chunk.safe_name(),
                resolution      = best_fail_attempt.resolution,
                winning_attempt = best_fail_attempt.path,
                crf             = search.best_quality,
                metrics         = all_metrics_by_path.get(best_fail_attempt.path, {}),
                frame_count     = frame_counts.get(best_fail_attempt.path, 0),
                quality_targets = quality_targets,
                targets_met     = False,
            )
            final_attempt = best_fail_attempt
        elif search.best_quality is None:
            logger.warning(
                "%s search space exhausted for chunk %s strategy %s after %d attempts",
                strategy.codec.quality_label, chunk.safe_name(), strategy.display_name(), attempt_number,
            )

        # Progress bar advance — after the loop so ETA reflects actual encode time.
        if not _any_real_work:
            # All cache hits — chunk was fully recovered from existing artifacts.
            # Same winner rule as the fresh path: a passing attempt when one exists,
            # otherwise the best failing attempt (still the best recovered state).
            winner = final_attempt if final_attempt is not None else best_fail_attempt
            return ChunkEncodingResult(
                chunk_id     = chunk.safe_name(),
                strategy     = strategy.display_name(),
                success      = True,
                targets_met  = search.best_targets_met,
                final_crf    = search.best_quality,
                attempts     = attempt_number,
                encoded_file = winner,
                frame_count  = frame_counts.get(winner.path, 0) if winner is not None else 0,
                reused       = True,
            )

        if final_attempt is not None or best_fail_attempt is not None:
            winning = final_attempt if final_attempt is not None else best_fail_attempt
            assert winning is not None, "the branch condition guarantees a winner"
            return ChunkEncodingResult(
                chunk_id     = chunk.safe_name(),
                strategy     = strategy.display_name(),
                success      = True,
                targets_met  = search.best_targets_met,
                final_crf    = search.best_quality,
                attempts     = attempt_number,
                encoded_file = winning,
                frame_count  = frame_counts.get(winning.path, 0),
                reused       = False,
            )
        else:
            error_msg = f"Failed to meet quality targets after {attempt_number} attempts"
            logger.error("Chunk %s: %s", chunk.safe_name(), error_msg)
            return ChunkEncodingResult(
                chunk_id    = chunk.safe_name(),
                strategy    = strategy.display_name(),
                success     = False,
                targets_met = False,
                attempts    = attempt_number,
                error       = error_msg,
            )



def build_encoded_chunk(
    chunk:        VideoStreamChunk,
    strategy:     Strategy,
    path:         Path,
    resolution:   str | None,
    frame_count:  int,
) -> EncodedChunk:
    """Compose the winning attempt as an :class:`EncodedChunk`.

    The attempt's own video stream: crop empty by construction (applied
    during the encode), frame count from the encode run, info from the
    post-encode probe (resolution) plus the source fps — path and size are
    read through ``stream.file``, never duplicated. The winning quality is
    deliberately NOT composed: it lives on the winner sidecar as a fact for
    processing-path consumers only (Req 4).

    Args:
        chunk:        The source window the attempt encodes.
        strategy:     The strategy used.
        path:         The winner file.
        resolution:   The winner's actual output resolution (``None`` when
                      unknown — e.g. recovery-composed rows).
        frame_count:  Frames from the winning encode run (0 = unknown).

    Returns:
        The composed :class:`~pyqenc.stream_model.EncodedChunk`.
    """
    file_size_bytes = safe_stat_size(path)
    source_info = chunk.stream.stream.info
    attempt_info = VideoStreamInfo(
        track_id     = 0,
        resolution   = resolution or None,
        fps          = source_info.fps,
        fps_fraction = source_info.fps_fraction,
    )
    return EncodedChunk(
        stream = ExtendedVideoStream(
            stream      = VideoStream(
                file = StreamFile(path=path, file_size_bytes=file_size_bytes),
                info = attempt_info,
            ),
            frame_count = frame_count,
            crop        = CropParams(),
        ),
        chunk    = chunk,
        strategy = strategy,
    )


class ChunkQueue:
    """Manages queue of chunks for parallel encoding.

    Prioritizes completing started chunks before starting new ones.
    """

    def __init__(self, chunks: list[VideoStreamChunk], strategies: list[Strategy]):
        """Initialize chunk queue.

        Args:
            chunks:     List of chunks to encode.
            strategies: List of strategies to apply.
        """
        self.chunks     = chunks
        self.strategies = strategies
        self._pending:     list[tuple[VideoStreamChunk, Strategy]] = []
        self._in_progress: set[tuple[str, str]]                 = set()

        # Build initial queue (all chunk+strategy combinations)
        for chunk in chunks:
            for strategy in strategies:
                self._pending.append((chunk, strategy))

    def get_next(self) -> tuple[VideoStreamChunk, Strategy] | None:
        """Get next chunk+strategy to encode.

        Prioritizes completing started chunks before starting new ones.

        Returns:
            Tuple of (chunk, strategy) or None if queue empty.
        """
        if not self._pending:
            return None

        # Check if any in-progress chunks have other strategies pending
        for chunk, strategy in self._pending:
            if any((chunk.safe_name(), s.display_name()) in self._in_progress for s in self.strategies):
                # This chunk has work in progress, prioritize it
                self._pending.remove((chunk, strategy))
                self._in_progress.add((chunk.safe_name(), strategy.display_name()))
                return (chunk, strategy)

        # No in-progress chunks, take first pending
        chunk, strategy = self._pending.pop(0)
        self._in_progress.add((chunk.safe_name(), strategy.display_name()))
        return (chunk, strategy)

    def mark_complete(self, chunk_id: str, strategy: Strategy) -> None:
        """Mark chunk+strategy as complete.

        Args:
            chunk_id: Chunk identifier.
            strategy: Encoding strategy.
        """
        self._in_progress.discard((chunk_id, strategy.display_name()))

    def mark_failed(self, chunk_id: str, strategy: Strategy) -> None:
        """Mark chunk+strategy as failed.

        Args:
            chunk_id: Chunk identifier.
            strategy: Encoding strategy.
        """
        self._in_progress.discard((chunk_id, strategy.display_name()))

    def is_empty(self) -> bool:
        """Check if queue is empty.

        Returns:
            True if no more work to do.
        """
        return len(self._pending) == 0 and len(self._in_progress) == 0


async def _encode_chunk_async(
    encoder:         ChunkEncoder,
    chunk:           VideoStreamChunk,
    strategy:        Strategy,
    quality_targets: list[QualityTarget],
    initial_crf:     Decimal,
    force:           bool,
) -> ChunkEncodingResult:
    """Async wrapper for chunk encoding.

    Args:
        encoder:         ChunkEncoder instance.
        chunk:           The chunk window to encode.
        strategy:        Encoding strategy.
        quality_targets: Quality targets.
        initial_crf:     Initial CRF value.
        force:           Whether to force re-encoding.

    Returns:
        ChunkEncodingResult
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        encoder.encode_chunk,
        chunk,
        strategy,
        quality_targets,
        initial_crf,
        force,
    )


async def _encode_chunks_parallel(
    encoder:          ChunkEncoder,
    chunks:           list[VideoStreamChunk],
    strategies:       list[Strategy],
    quality_targets:  list[QualityTarget],
    max_parallel:     int,
    force:            bool,
    collector:        MetricsCollector,
    phase_recovery:   _PhaseRecovery | None                                  = None,
    advance:          Callable[[int | float, AdvanceState], None] | None = None,
    metric_prefix:    MetricKey                                                  = MetricKey.ENCODING,
) -> EncodingResult:
    """Encode chunks in parallel with semaphore control.

    Args:
        encoder:        ChunkEncoder instance.
        chunks:         List of chunk windows to encode.
        strategies:     List of strategies to use.
        quality_targets: Quality targets to meet.
        max_parallel:   Maximum concurrent encodings.
        force:          Whether to force re-encoding.
        collector:      Metrics collector for timing and convergence tracking.
                        Per-attempt timing is recorded inside ``ChunkEncoder``:
                        ``encoding.<strategy>`` for each ffmpeg encode and
                        ``encoding.quality_measure`` for each quality evaluation.
                        ``step(MetricKey.ENCODING, convergence_update=...)`` is
                        called after each chunk/strategy pair converges.
        phase_recovery: Optional recovery state from ``recover_attempts``; when
                        provided, ``COMPLETE`` pairs are skipped and ``PARTIAL``
                        pairs resume from their recovered ``QualitySearch`` state.
        advance:        Optional advance callable from ``ProgressBar``; called with
                        chunk duration in seconds and an ``AdvanceState`` on each
                        chunk completion.

    Returns:
        EncodingResult with all encoding outcomes.
    """
    result    = EncodingResult()
    semaphore = asyncio.Semaphore(max_parallel)

    # Pre-populate result with COMPLETE pairs from recovery (skip them in the queue)
    complete_pairs: set[tuple[str, str]] = set()
    if phase_recovery is not None:
        for chunk in chunks:
            for strategy in strategies:
                pair_recovery = phase_recovery.pairs[(chunk.safe_name(), strategy.display_name())]
                if pair_recovery.state == ArtifactState.COMPLETE:
                    logger.debug(
                        "Skipping COMPLETE pair %s/%s (encoding result sidecar valid)",
                        chunk.safe_name(), strategy.display_name(),
                    )
                    # COMPLETE recovery rows always carry their winning file.
                    assert pair_recovery.winning_file is not None, (
                        f"winning file guaranteed for COMPLETE pair "
                        f"{chunk.safe_name()}/{strategy.display_name()}"
                    )
                    result.encoded_chunks.setdefault(
                        strategy.display_name(), [],
                    ).append(build_encoded_chunk(
                        chunk        = chunk,
                        strategy     = strategy,
                        path         = pair_recovery.winning_file,
                        resolution   = None,  # a sidecar fact — never read at recovery (Req 3)
                        frame_count  = 0,     # unknown on recovery
                    ))
                    result.reused_count += 1
                    complete_pairs.add((chunk.safe_name(), strategy.display_name()))

    queue = ChunkQueue(chunks, strategies)
    # Remove already-complete pairs from the queue
    queue._pending = [
        (c, s) for (c, s) in queue._pending
        if (c.safe_name(), s.display_name()) not in complete_pairs
    ]

    async def encode_worker() -> None:
        """Worker coroutine for encoding chunks."""
        while not queue.is_empty():
            next_item = queue.get_next()
            if next_item is None:
                break

            chunk, strategy = next_item

            async with semaphore:
                # Encode chunk using the codec's default quality as the fixed starting point.
                # Predictable initial quality = predictable recovery path when parameters change.
                gran = strategy.codec.quality_granularity
                chunk_initial_crf = strategy.codec.default_quality.quantize(gran)

                # Encode chunk — timing is recorded inside encode_chunk:
                #   encoding.<strategy>       for each ffmpeg encode attempt
                #   encoding.quality_measure  for each quality evaluation
                chunk_result = await _encode_chunk_async(
                    encoder,
                    chunk,
                    strategy,
                    quality_targets,
                    chunk_initial_crf,
                    force,
                )

                # Update result
                if chunk_result.success:
                    # Success implies a built winner (encode_chunk's contract).
                    assert chunk_result.encoded_file is not None and chunk_result.final_crf is not None
                    result.encoded_chunks.setdefault(
                        strategy.display_name(), [],
                    ).append(build_encoded_chunk(
                        chunk        = chunk,
                        strategy     = strategy,
                        path         = chunk_result.encoded_file.path,
                        resolution   = chunk_result.encoded_file.resolution or None,
                        frame_count  = chunk_result.frame_count,
                    ))

                    if chunk_result.reused:
                        result.reused_count += 1
                        if advance is not None:
                            advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.SKIPPED)
                    else:
                        result.encoded_count += 1
                        if advance is not None:
                            advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.SUCCESS)
                        # Record convergence for this chunk/strategy pair
                        collector.step(
                            metric_prefix,
                            convergence_update=ConvergenceUpdate(
                                strategy      = strategy.display_name(),
                                attempt_count = chunk_result.attempts,
                            ),
                        )

                    queue.mark_complete(chunk.safe_name(), strategy)
                else:
                    queue.mark_failed(chunk.safe_name(), strategy)
                    result.failed_chunks.append(chunk.safe_name())
                    if advance is not None:
                        advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)

    # Start worker tasks
    workers = [asyncio.create_task(encode_worker()) for _ in range(max_parallel)]

    # No top-level timing span here: the owning phase's template run() wraps
    # _execute() under its own top-level key (encoding / optimization). Only
    # the dotted sub-action spans are recorded by the encoder machinery.
    await asyncio.gather(*workers)

    _assert_one_winner_per_chunk(result, chunks, strategies)

    return result


def _assert_one_winner_per_chunk(
    result:     EncodingResult,
    chunks:     list[VideoStreamChunk],
    strategies: list[Strategy],
) -> None:
    """Contract guard: every non-failed chunk has exactly one winner per strategy.

    The span ``(start_timestamp, end_timestamp)`` is the compared identity —
    the chunk→winner seam is transitional data between phases, and this is the
    earliest point where a lost, duplicated, or foreign winner is localizable
    (the frame-count invariant at merge fires far too late to point anywhere).
    Two checks per strategy, each catching what the other cannot: the span
    set comparison catches lost and foreign winners (a frozenset would
    silently coalesce duplicates), and the count comparison catches
    same-span duplicates (invisible to set equality). Spans of failed pairs
    are excluded: a failed pair legitimately has no winner and is reported
    through ``failed_chunks``.

    Args:
        result:     The concluded encode result.
        chunks:     The chunk set the run was asked to encode.
        strategies: The strategies the run was asked to encode with.

    Raises:
        AssertionError: When any strategy's winner count or spans diverge
            from the non-failed chunks.
    """
    failed_ids = set(result.failed_chunks)
    expected = frozenset(
        (c.start_timestamp, c.end_timestamp) for c in chunks
        if c.safe_name() not in failed_ids
    )
    for strategy in strategies:
        winners = result.encoded_chunks.get(strategy.display_name(), [])
        actual = frozenset(
            (w.chunk.start_timestamp, w.chunk.end_timestamp) for w in winners
        )
        assert actual == expected, (
            f"Winner spans diverge from chunk spans for strategy "
            f"{strategy.display_name()}: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
        assert len(winners) == len(expected), (
            f"Winner count diverges from chunk count for strategy "
            f"{strategy.display_name()}: {len(winners)} winners for "
            f"{len(expected)} non-failed chunks (duplicate same-span winners?)"
        )


# ---------------------------------------------------------------------------
# Winning-limiter distribution summary
# ---------------------------------------------------------------------------


@dataclass
class _LimiterTally:
    """Accumulation over winning attempts sharing one limiter metric.

    Misses collect deficits (how far the unreachable target was); passes
    collect surpluses (how far above the bar the worst target sits — the
    fixed-mode anchor ruler makes this meaningful, search mode gets
    uniformity); CRFs over every winner in the row feed the med CRF column.
    """

    passed:    int           = 0
    missed:    int           = 0
    deficits:  list[float]   = field(default_factory=list)
    surpluses: list[float]   = field(default_factory=list)
    crfs:      list[Decimal] = field(default_factory=list)


def _scan_winner_sidecars(
    work_dir:        Path,
    encoded_chunks:  dict[str, list[EncodedChunk]],
    strategy_order:  list[str],
    quality_targets: list[QualityTarget],
) -> _WinnerScan:
    """Scan every winner's result sidecar once: limiter tallies + frame counts.

    Derived from the encoding-phase result artifacts
    (``encoded/<strategy>/<chunk_id>.<res>.yaml``), written for every
    concluded pair — so the scan is identical on fresh, resumed, and
    recovered runs, and survives intermediate cleanup of the attempt
    workspace.  Winners with a missing
    sidecar, or no persisted frame count are excluded from the affected
    output and mark ``frames_known=False`` (skip semantics).

    Args:
        work_dir:        Work dir root (locates ``encoded/<strategy>/``).
        encoded_chunks:  Winners grouped by strategy display name.
        strategy_order:  Strategy display names in pipeline order.
        quality_targets: Targets the winning attempts are judged against
                         (empty → ``summaries`` stays ``None``; frame
                         accounting is target-independent).

    Returns:
        The :class:`_WinnerScan`.
    """
    if not encoded_chunks:
        return _WinnerScan(summaries=None, frames_known=True, frames={})

    tallies:     dict[str, dict[str, _LimiterTally]] = {}
    frames:      dict[str, list[tuple[str, int]]]    = {}
    frames_known = True

    for winners in encoded_chunks.values():
        for winner in winners:
            strategy_name = winner.strategy.display_name()
            chunk_id = winner.chunk.safe_name()
            sidecar = read_winner_sidecar(work_dir, winner)
            if sidecar is None:
                logger.debug(
                    "Winner %s has no result sidecar — excluded from the winner scan",
                    winner.stream.stream.file.path.name,
                )
                frames_known = False
                continue

            frames_value = int(sidecar.get("frame_count") or 0)
            if frames_value > 0:
                frames.setdefault(strategy_name, []).append(
                    (chunk_id, frames_value)
                )
            else:
                frames_known = False

            if not quality_targets:
                continue
            metrics     = {k: float(v) for k, v in sidecar.get("metrics", {}).items()}
            targets_met = bool(sidecar.get("targets_met", False))
            worst       = QualitySearchBase.find_worst_target(metrics, quality_targets)
            if worst is None:
                continue
            tally = tallies.setdefault(strategy_name, {}).setdefault(
                f"{worst[0].metric}_{worst[0].statistic}", _LimiterTally(),
            )
            tally.crfs.append(Decimal(str(sidecar.get("crf"))))
            if targets_met:
                tally.passed += 1
                tally.surpluses.append(worst[1])
            else:
                tally.missed += 1
                tally.deficits.append(worst[1])

    summaries: list[LimiterSummary] | None = None
    if quality_targets:
        ordered = [name for name in strategy_order if name in tallies]
        ordered += sorted(set(tallies) - set(strategy_order))
        if ordered:
            summaries = []
            for strategy_name in ordered:
                rows_map = tallies[strategy_name]
                total    = sum(t.passed + t.missed for t in rows_map.values())
                summaries.append(LimiterSummary(
                    strategy = strategy_name,
                    chunks   = total,
                    rows     = [
                        LimiterSummaryRow(
                            limiter     = key,
                            passed      = t.passed,
                            missed      = t.missed,
                            med_deficit = statistics.median(t.deficits) if t.deficits else None,
                            med_surplus = statistics.median(t.surpluses) if t.surpluses else None,
                            med_crf     = statistics.median(t.crfs),
                        )
                        for key, t in sorted(rows_map.items(), key=lambda kv: (-kv[1].passed - kv[1].missed, kv[0]))
                    ],
                ))
    return _WinnerScan(summaries=summaries, frames_known=frames_known, frames=frames)


def encode_all_chunks(
    chunks:            list[VideoStreamChunk],
    strategies:        list[Strategy],
    quality_targets:   list[QualityTarget],
    work_dir:          Path,
    collector:         MetricsCollector,
    max_parallel:    int,
    force:           bool              = False,
    dry_run:         bool              = False,
    crop_params:     CropParams | None = None,
    encoding_yaml:   Path | None       = None,
    cleanup_level:   CleanupLevel      = CleanupLevel.NONE,
    visual_hash:     bool              = True,
    metrics_sampling: int              = 10,
    metric_prefix:   MetricKey         = MetricKey.ENCODING,
    measure_attempts: bool             = True,
) -> EncodingResult:
    """Encode all chunks with quality-targeted CRF adjustment.

    This is the main entry point for the encoding phase. It handles:
    - Classifying all ``(chunk, strategy)`` pairs via
      ``_recover_encoding_attempts``
    - Skipping ``COMPLETE`` pairs and resuming pending pairs
    - Parallel encoding of chunks that need work

    ``encoding.yaml`` persistence and probe-mismatch validation are owned by
    ``EncodingPhase``.

    Args:
        chunks:            List of chunk windows to encode.
        strategies:        List of resolved ``Strategy`` objects to use.
        quality_targets:   Quality targets to meet.
        work_dir:          Working directory for artifacts.
        collector:         Metrics collector for timing and convergence tracking.
        max_parallel:      Maximum concurrent encoding processes
        force:             If False, reuse existing encodings that meet current targets
        dry_run:           If True, only report what would be done without encoding
        crop_params:       Crop parameters to apply uniformly to every chunk attempt.
                           When ``None``, no cropping is applied.
        encoding_yaml:     Unused — ``encoding.yaml`` persistence is owned by
                           ``EncodingPhase``.
        cleanup_level:     Controls deletion of intermediate attempt files after each
                           pair converges.
        metrics_sampling:  Frame subsampling factor for quality metric generation.
                           Passed through to ``ChunkEncoder`` and then to
                           ``QualityEvaluator.evaluate_chunk``.
        measure_attempts:  Whether encoded attempts get quality evaluations
                           (the ``ChunkEncoder`` seam; default = measure).

    Returns:
        EncodingResult with paths to encoded chunks and statistics
    """
    logger.debug(
        "Encoding phase: %d chunks, %d strategies, %d quality targets",
        len(chunks), len(strategies), len(quality_targets),
    )

    # No stale-.tmp scan here: the sole caller (``EncodingPhase._execute``)
    # always follows ``EncodingPhase._recover()`` — which already cleaned the
    # identical ``encoding/`` tree — and nothing writes ``.tmp`` under it
    # between the two (the only intervening write is ``encoding.yaml`` at the
    # work-dir root, outside this tree).

    # Artifact recovery: classify every (chunk, strategy) pair.
    chunk_ids      = [c.safe_name() for c in chunks]
    strategy_names = [s.display_name() for s in strategies]
    phase_recovery = _recover_encoding_attempts(work_dir, chunk_ids, strategies)

    if dry_run:
        pending_count  = len(phase_recovery.pending)
        complete_count = len(chunk_ids) * len(strategy_names) - pending_count
        logger.info("[DRY-RUN] Encoding recovery: %d COMPLETE, %d pending", complete_count, pending_count)
        if pending_count == 0:
            logger.info("[DRY-RUN] Status: Complete (all chunks already encoded)")
        else:
            logger.info("[DRY-RUN] Status: Needs work (%d pair(s) pending)", pending_count)
        result = EncodingResult()
        result.reused_count = complete_count
        return result

    # Create encoder
    encoder = ChunkEncoder(
        quality_evaluator = QualityEvaluator(work_dir),
        work_dir          = work_dir,
        collector         = collector,
        crop_params       = crop_params,
        cleanup_level     = cleanup_level,
        visual_hash       = visual_hash,
        metrics_sampling  = metrics_sampling,
        metric_prefix     = metric_prefix,
        measure_attempts  = measure_attempts,
    )

    # Run parallel encoding — COMPLETE pairs are skipped inside _encode_chunks_parallel
    logger.debug("Starting parallel encoding with %d workers", max_parallel)
    total_seconds = sum(c.end_timestamp - c.start_timestamp for c in chunks) * len(strategies)
    with ProgressBar(total_seconds, title="Encoding", total_count=len(chunks) * len(strategies)) as advance:
        # Update the bar for completed chunks
        chunks_by_id = {c.safe_name(): c for c in chunks}
        for r in phase_recovery.pairs.values():
            if r.state == ArtifactState.COMPLETE:
                advance((chunks_by_id[r.chunk_id].end_timestamp - chunks_by_id[r.chunk_id].start_timestamp), AdvanceState.SKIPPED)

        # Run parallel encoding
        result = asyncio.run(
            _encode_chunks_parallel(
                encoder         = encoder,
                chunks          = chunks,
                strategies      = strategies,
                quality_targets = quality_targets,
                max_parallel    = max_parallel,
                force           = force,
                phase_recovery  = phase_recovery,
                advance         = advance,
                collector       = collector,
                metric_prefix   = metric_prefix,
            )
        )
        advance(0, AdvanceState.COMPLETE)

    # Log summary
    status_message = f"{SUCCESS_SYMBOL_MINOR} Encoding complete" if not result.failed_chunks and result.outcome == PhaseOutcome.COMPLETED else \
                    f"{WARNING_SYMBOL} Encoding complete with {len(result.failed_chunks)} failures" if result.failed_chunks and result.outcome == PhaseOutcome.COMPLETED else \
                    f"{FAILURE_SYMBOL_MINOR} Encoding failed with {len(result.failed_chunks)} failures"
    logger.info(
         status_message + ": %d newly encoded, %d reused, %d failed",
        result.encoded_count, result.reused_count, len(result.failed_chunks),
    )

    if result.failed_chunks:
        # Duplicate to ERROR level on failed chunks
        logger.error("Failed chunks: %s", ", ".join(result.failed_chunks))

    result.winner_scan = _scan_winner_sidecars(
        work_dir,
        result.encoded_chunks,
        [s.display_name() for s in strategies],
        quality_targets,
    )

    return result

# ---------------------------------------------------------------------------
# EncodingPhase — Phase object
# ---------------------------------------------------------------------------

@dataclass
class EncodingPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying encoding-specific payload.

    Attributes:
        winners:        The winning attempts — one ``Artifact[EncodedChunk]``
                        per (chunk, selected strategy); consumed by Merge.
        quality_labels: Strategy display name -> quality label (settings —
                        consumed by Merge to label plots).
        presentation_anchor: The fixed-mode presentation anchor's display
                        name (deltas basis) — optimization's due anchor, or
                        the scan-elected fallback on uncompared runs. NEVER
                        a persisted key, a selection input, or merge's
                        anchor basis (Req 59).
    """

    winners:             list[Artifact[EncodedChunk]] = field(default_factory=list)
    quality_labels:      dict[str, str]               = field(default_factory=dict)
    presentation_anchor: str | None                   = None

    @property
    def encoded_chunks(self) -> dict[str, list[EncodedChunk]]:
        """Derived grouping over the winners: strategy display name -> the
        composed payloads (path via ``stream.file.path``)."""
        lookup: dict[str, list[EncodedChunk]] = {}
        for row in self.winners:
            payload = row.payload
            lookup.setdefault(
                payload.strategy.display_name(), [],
            ).append(payload)
        return lookup


class EncodingPhase(Phase[EncodingPhaseResult]):
    """Phase object for CRF-search chunk encoding.

    Owns artifact enumeration, recovery, invalidation, execution, and logging
    for the encoding phase.  Execution delegates to the ``encode_all_chunks``
    helper. The uniform run footprint is inherited from :class:`Phase`.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name:        str       = "encoding"
    SIDECAR_NAME = "encoding.yaml"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (
        JobPhase, ProbePhase, ChunkingPhase, OptimizationPhase,
    )
    _METRIC_KEY: MetricKey = MetricKey.ENCODING

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry,
        *,
        collector: MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        self._persisted:    EncodingSidecar | None    = None
        """The replay aggregate loaded from ``encoding.yaml`` during recovery
        (``None`` when absent) — the fast-exit source for the summary."""
        self.quality_labels: dict[str, str]           = {}
        """Maps strategy name → quality_label (e.g. ``'CRF'``, ``'CQ'``) for all
        strategies resolved during the last ``run()`` call.  Empty until ``run()``
        completes.  Used by downstream phases (e.g. ``MergePhase``) to label plots."""

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _recovery_unit(self) -> str:
        """The recovery summary counts (chunk, strategy) pairs."""
        return "pair"

    def _log_key_params(self) -> None:
        """Log chunks, strategies, crop, and targets (key parameters)."""
        logger.info("Scanning for existing artifacts...")

        probe_result = self._deps[ProbePhase]
        crop         = probe_result.crop

        strategies = self._deps[OptimizationPhase].selected_strategies
        chunks     = self._deps[ChunkingPhase].chunks
        logger.info("Chunks:      %d", len(chunks))
        logger.info("Strategies:  %s", ", ".join(s.display_name() for s in strategies) if strategies else "none")
        if crop:
            logger.info("Crop:        %s", crop)
        plan = self._deps[ProbePhase].plan

        logger.info("Targets:     %s", ", ".join(f"{t.metric}-{t.statistic}≥{t.value}" for t in plan.targets))

    def _log_limiter_summary(self, summaries: list[LimiterSummary]) -> None:
        """Emit the winning-limiter distribution table at INFO.

        Args:
            summaries: Per-strategy groups (rows sorted by chunk count desc).
        """
        # Column widths
        LIMIT_WIDTH = 14
        PASS_WIDTH  = 13
        MISS_WIDTH  = 12
        SHARE_WIDTH = 7
        CRF_WIDTH   = 8

        logger.info("")
        logger.info("Winning-limiter distribution")
        header = (
            f"  {'Limiter':<{LIMIT_WIDTH}}   {f'pass {NEUTRAL_INDICATOR_SYMBOL}':>{PASS_WIDTH}}   "
            f"{f'miss {FAILURE_SYMBOL_MINOR}':>{MISS_WIDTH}}   {'share':>{SHARE_WIDTH}}   {'med CRF':>{CRF_WIDTH}}"
        )
        for summary in summaries:
            logger.info("-"*10)
            logger.info("%s — %d chunks", f"{BRACKET_LEFT}{summary.strategy}{BRACKET_RIGHT}", summary.chunks)
            logger.info(header)
            for row in summary.rows:
                pass_cell = (
                    f"{row.passed} (+{row.med_surplus:.1f})"
                    if row.passed and row.med_surplus is not None
                    else f"{row.passed}"
                )
                miss_cell = f"{row.missed} ({row.med_deficit:.1f})" if row.missed else "0"
                share     = 100.0 * (row.passed + row.missed) / summary.chunks if summary.chunks else 0.0
                logger.info(
                    f"  {row.limiter:<{LIMIT_WIDTH}}   {pass_cell:>{PASS_WIDTH}}   {miss_cell:>{MISS_WIDTH}}   "
                    f"{share:>{SHARE_WIDTH - 1}.1f}%   {row.med_crf:{CRF_WIDTH}.1f}"
                )
            passed_total = sum(r.passed for r in summary.rows)
            missed_total = sum(r.missed for r in summary.rows)
            if summary.chunks:
                logger.info(
                    f"  Passed {100.0 * passed_total / summary.chunks:.1f}% · missed {100.0 * missed_total / summary.chunks:.1f}%"
                )

    def finalize(self, ctx: FinalizeContext) -> None:
        """Perform end-of-run housekeeping for the encoding phase.

        When ``ctx.deep_cleanup`` is ``True``, deletes this phase's own
        ``encoding/`` (CRF-search attempt workspace) and ``encoded/`` (finalized
        winning-attempt) directories. Both are consumables the merge phase has
        already drawn from and are reproducible from the chunks. ``encoding.yaml``
        is a recovery sidecar and is left in place. Each directory is deleted
        independently, guarded by an existence check; any ``OSError`` is caught
        per directory and logged as a warning so a cleanup failure never fails
        the run.

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        if not ctx.deep_cleanup:
            return
        work_dir = self._deps[JobPhase].work_dir
        for target in (work_dir / ENCODING_WORKSPACE_DIR, work_dir / ENCODED_OUTPUT_DIR):
            if target.exists():
                try:
                    shutil.rmtree(target)
                    logger.debug("deep cleanup: deleted %s", target)
                except OSError as exc:
                    logger.warning("deep cleanup: could not delete %s: %s", target, exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recover(self) -> Recovery:
        """Classification-only recovery over the shared namespace.

        Steps:
        1. Clean up leftover ``.tmp`` files.
        2. Classify all (chunk, strategy) pairs via the shared pair-ledger
           builders (static-name consumption over one listing per strategy
           dir).
        3. Consumption-triggered curation (winner-layer policy): orphaned
           ``encoded/<strategy>/`` directories (strategy no longer selected)
           and names no pair consumed (old-chunk-id winners, stray sidecars)
           are DELETED automatically — ``encoded/`` stays merge-ready and
           human-clean (Req 13/16/17). The attempt workspace ``encoding/``
           is the investment layer: never classified, never auto-deleted.

        Encoding carries NO invalidation of its own (Req 38): the shared
        namespace's keys all live at optimization; winner currency is
        certified cross-phase by ``optimization.yaml``.

        Returns:
            The :class:`Recovery` single source of truth.

        Raises:
            RecoveryError: When chunking/optimization produced no chunks /
                strategies.
        """
        job_result = self._deps[JobPhase]
        work_dir   = job_result.work_dir
        enc_dir    = work_dir / ENCODING_WORKSPACE_DIR
        yaml_path  = work_dir / EncodingPhase.SIDECAR_NAME

        # The replay aggregate — loaded here for the fast-exit resurface;
        # its absence costs display only, never invalidation (Req 24).
        self._persisted = EncodingSidecar.load(yaml_path)

        # Step 1: clean up .tmp files
        remove_stale_tmp_files(enc_dir)

        # Step 2: get chunks and strategies from dependencies
        chunking_result     = self._deps[ChunkingPhase]
        optimization_result = self._deps[OptimizationPhase]

        chunks: list[VideoStreamChunk] = [a.payload for a in chunking_result.chunks]
        strategies = optimization_result.selected_strategies

        if not chunks:
            raise RecoveryError("No chunks available from ChunkingPhase")
        if not strategies:
            raise RecoveryError("No strategies available from OptimizationPhase")

        # Step 3: the per-pair ledger (winning attempt per chunk x strategy,
        # presence-based) plus the winner-layer curation.
        rows: list[Artifact] = _pair_rows(work_dir, chunks, strategies)
        rows += self._curate_winner_layer(work_dir, strategies, chunks)
        return Recovery.from_artifacts(rows)

    def _curate_winner_layer(
        self,
        work_dir:   Path,
        strategies: list[Strategy],
        chunks:     list[VideoStreamChunk],
    ) -> list[Artifact]:
        """Winner-layer curation: delete what no pair consumed (Req 13/17).

        Two leftovers classes, both deleted automatically (the winner layer
        re-derives from the attempt substrate):

        - orphan strategy directories (a strategy removed from the
          selection — its attempts stay in ``encoding/``);
        - names inside selected strategy dirs that no expected pair
          consumed (old-chunk-id winners after a re-chunk, stray sidecars).

        Curation rows (``wanted=False``) surface what was deleted for the
        recovery line; nothing is retained in place — that policy belongs
        to the deliverable layer.
        """
        curated: list[Artifact] = []
        expected_dirs = {s.safe_name() for s in strategies}
        chunk_ids     = {c.safe_name() for c in chunks}

        out_dir = work_dir / ENCODED_OUTPUT_DIR
        if not out_dir.exists():
            return curated

        for strategy_dir in sorted(out_dir.iterdir()):
            if not strategy_dir.is_dir():
                continue
            if strategy_dir.name not in expected_dirs:
                logger.info(
                    "encoded/%s is orphaned (strategy no longer selected) — "
                    "deleting the winner directory (attempts stay in encoding/)",
                    strategy_dir.name,
                )
                shutil.rmtree(strategy_dir, ignore_errors=True)
                curated.append(Artifact(
                    payload = StreamFile(path=LongPath(strategy_dir)),
                    state   = ArtifactState.ABSENT,
                    wanted  = False,
                ))
                continue

            expected_names = set()
            for chunk_id in chunk_ids:
                expected_names.add(EncodedChunk.format_winner_file_name(chunk_id))
                expected_names.add(EncodedChunk.format_winner_sidecar_name(chunk_id))
                expected_names.add(f"{chunk_id}.png")
            for path in sorted(strategy_dir.iterdir()):
                if path.name.endswith(TEMP_SUFFIX) or path.name in expected_names:
                    continue
                try:
                    path.unlink()
                    logger.debug("Deleted unconsumed winner-layer name: %s", path.name)
                    curated.append(Artifact(
                        payload = StreamFile(path=LongPath(path)),
                        state   = ArtifactState.ABSENT,
                        wanted  = False,
                    ))
                except OSError as exc:
                    logger.warning("Could not delete %s: %s", path, exc)
        return curated

    def _reused_result(self, wanted: list[Artifact], message: str) -> EncodingPhaseResult:
        """Resurface the persisted aggregates on a fully-reused run.

        Mirrors ``OptimizationPhase``: the winning-limiter table is shown from
        the stash loaded during recovery, and the persisted winners frame
        totals are re-asserted against the probe's frame count — no
        winner-sidecar reads on the fast path.  Freshness of both is
        guaranteed by the pending gate: any invalidated pair routes the run
        through the processing path, which rebuilds and re-saves them.

        Args:
            wanted:   Wanted artifact rows (COMPLETE pairs carry winners).
            message:  Reuse message from the template.

        Returns:
            The reused ``EncodingPhaseResult``.
        """
        result = super()._reused_result(wanted, message)
        if (
            self._persisted is not None
            and self._persisted.summary is not None
            and self._persisted.summary.limiter is not None
        ):
            self._log_limiter_summary(self._persisted.summary.limiter)
        else:
            logger.debug("No persisted limiter summary — table skipped on reused run")
        self._reassert_frame_preservation(self._persisted)
        return result

    def _reassert_frame_preservation(self, persisted: EncodingSidecar | None) -> None:
        """Re-assert frame preservation from the persisted aggregate (fast exit).

        Reads the single ``summary.frames`` map — never per-winner sidecars —
        and compares each strategy's total against the probe's in-memory
        frame count.  A disagreement is surfaced as a warning (the hook
        contract keeps the REUSED outcome); the merge-time frame verification
        remains the hard backstop.  Empty totals (unknown) keep the skip
        semantics and stay silent.
        """
        if persisted is None or persisted.summary is None:
            return
        totals = persisted.summary.frames
        if not totals:
            return
        probe_stream = self._deps[ProbePhase].stream
        if probe_stream is None or probe_stream.payload.frame_count <= 0:
            return
        source_total = probe_stream.payload.frame_count
        mismatched = {name: total for name, total in totals.items() if total != source_total}
        if mismatched:
            logger.warning(
                "Persisted winners frame totals disagree with the source frame count "
                "(source=%d): %s — merge-time verification remains the backstop",
                source_total,
                ", ".join(f"{name}={total}" for name, total in mismatched.items()),
            )

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[Artifact[EncodedChunk]],
        message:   str,
        presentation_anchor: str | None = None,
    ) -> EncodingPhaseResult:
        """Assemble an ``EncodingPhaseResult`` from the pair rows.

        Args:
        outcome:   The phase outcome.
            artifacts: The wanted pair rows (the winners field takes the
                       complete ones).
            message:   Human-readable summary — on ``FAILED``, the error
                       description (count plus identifiers).
            presentation_anchor: The elected presentation anchor (execute
                       path only).

        Returns:
            The populated result.
        """
        return EncodingPhaseResult(
            outcome             = outcome,
            message             = message,
            winners             = [
                r for r in artifacts
                if isinstance(r.payload, EncodedChunk) and r.state == ArtifactState.COMPLETE
            ],
            quality_labels      = dict(self.quality_labels),
            presentation_anchor = presentation_anchor,
        )

    def _execute(
        self,
        wanted:  list[Artifact[EncodedChunk]],
        dry_run: bool,
    ) -> EncodingPhaseResult:
        """Encode all pending ``(chunk, strategy)`` pairs.

        The top-level ``encoding`` span belongs to the template and therefore
        covers everything here — the ``encoding.yaml`` write, the parallel
        encode, and the post-encode re-scan. ``dry_run`` is never ``True``
        here (encoding is not a readonly-execute phase; the template previews
        instead).

        Args:
            wanted:  Wanted artifact list from ``_recover()``.
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``EncodingPhaseResult`` after encoding.
        """
        work_dir = self._deps[JobPhase].work_dir
        probe_result = self._deps[ProbePhase]
        crop         = probe_result.crop

        # Resolve chunks and strategies from dependencies
        chunking_result     = self._deps[ChunkingPhase]
        optimization_result = self._deps[OptimizationPhase]

        chunks: list[VideoStreamChunk] = [a.payload for a in chunking_result.chunks]
        strategies = optimization_result.selected_strategies

        if not chunks:
            err = "No chunks available from ChunkingPhase"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err)

        if not strategies:
            err = "No strategies available from OptimizationPhase"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err)

        # Cache quality labels for downstream phases (e.g. MergePhase CRF plot)
        self.quality_labels = {s.display_name(): s.codec.quality_label for s in strategies}

        # Presentation targets — what winner sidecars and the limiter-style
        # output are judged against. Fixed compared runs use the anchor's
        # synthetic set (config targets are search-tuned vocabulary and would
        # read as all-miss noise); uncompared fixed runs have no ruler
        # (absolute values, no verdicts — the limiter table self-extinguishes
        # on empty targets); searched runs use the config targets, unchanged.
        plan = self._deps[ProbePhase].plan

        if plan.fixed_quality:
            presentation_targets = optimization_result.synthetic_targets
        else:
            presentation_targets = plan.targets

        # encoding.yaml carries the replay aggregate ONLY (no keys — Req 24):
        # a single post-success write; there is no crash-safe early write
        # because there is no key to certify mid-run.
        encoding_yaml = work_dir / EncodingPhase.SIDECAR_NAME

        # Run encoding via the existing encode_all_chunks function
        enc_result = encode_all_chunks(
            chunks           = chunks,
            strategies       = strategies,
            quality_targets  = presentation_targets,
            work_dir         = work_dir,
            collector        = self._collector,
            max_parallel     = self._config.encoding.concurrency,
            force            = False,  # attempt reuse is never bypassed by --force (permission, not a command)
            dry_run          = False,
            crop_params      = crop,
            encoding_yaml    = None,
            cleanup_level    = self._deps[JobPhase].cleanup,
            visual_hash      = self._config.encoding.visual_hash,
            metrics_sampling = self._config.measurement.sampling,
            measure_attempts = True,  # default-flipping is TODO §83's decision
        )

        if enc_result.outcome == PhaseOutcome.FAILED:
            err = enc_result.error or "Encoding failed"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err)

        # Winners come from the fresh encode result — every complete pair,
        # with freshly measured payloads (frame counts from the run itself).
        # Sorted by (chunk start, strategy) for a deterministic winners order —
        # start timestamps are the quantity itself; sorting on the formatted
        # safe name would depend on zero-padded rendering.
        winners = [
            Artifact(payload=payload, state=ArtifactState.COMPLETE)
            for payload in sorted(
                (p for ps in enc_result.encoded_chunks.values() for p in ps),
                key=lambda p: (p.chunk.start_timestamp, p.strategy.display_name()),
            )
        ]
        complete_pairs = {
            (p.chunk.safe_name(), p.strategy.display_name()) for p in
            (row.payload for row in winners)
        }
        failed_pairs = [
            f"{c.safe_name()}/{st.display_name()}"
            for c in chunks for st in strategies
            if (c.safe_name(), st.display_name()) not in complete_pairs
        ]

        # Log phase summary
        complete_count = len(winners)

        if failed_pairs:
            final_rows = _pair_rows(work_dir, chunks, strategies)
            return self._make_result(
                PhaseOutcome.FAILED,
                [r for r in final_rows if r.wanted],
                f"{len(failed_pairs)} pair(s) failed: {', '.join(failed_pairs[:5])}",
            )

        # Preservation invariant: each strategy's winners tile the source —
        # per-strategy Σ winner frame counts must equal the probe count.  The
        # sums come from the end-of-run winner-sidecar scan (uniform for
        # fresh, seeded, and cache-hit winners — the composition's 0 sentinel
        # for recovered pairs is never consulted here).  A winner without a
        # persisted count (sidecar predates the field, re-measured attempt)
        # skips the check with a warning — the final-merge verification
        # remains the hard backstop.
        # The dependency walk guarantees a completed probe with a resolved stream.
        assert probe_result.stream is not None, "probe guaranteed complete by the dependency walk"
        source_total = probe_result.stream.payload.frame_count
        scan         = enc_result.winner_scan
        if source_total > 0 and scan is not None:
            if not scan.frames_known:
                logger.warning(
                    "Frame-preservation check skipped: some winning attempts have "
                    "no known frame count"
                )
            else:
                violated = [
                    (name, total) for name, total in scan.frame_totals().items()
                    if total != source_total
                ]
                if violated:
                    totals  = ", ".join(f"{name}={total}" for name, total in violated)
                    details = "; ".join(
                        f"{name}: " + ", ".join(f"{cid}={n}" for cid, n in scan.frames[name])
                        for name, _ in violated
                    )
                    err = (
                        f"Frame preservation violated: per-strategy Σ winning attempts "
                        f"({totals}) != source ({source_total}). Per-chunk: {details}"
                    )
                    logger.critical(err)
                    return self._make_result(PhaseOutcome.FAILED, [], err)
                logger.debug(
                    "Frame preservation verified: per-strategy Σ winning attempts "
                    "== source == %d",
                    source_total,
                )

        # Anchor presentation (Req 59): optimization's due anchor is the
        # ruler basis; when NONE was due (the all-strategies path — no test
        # work, no measurements at optimization), the concluded scan elects
        # a presentation anchor here: the smallest-total-size strategy with
        # measured metrics. Presentation-only — never persisted as a key,
        # never a selection input, never merge's anchor basis. When an
        # anchor WAS due but no strategy carried measurements, the
        # optimization error surfaces loudly instead of a silent election.
        presentation_anchor = self._resolve_presentation_anchor(
            optimization_result, scan, plan,
            winner_sizes={
                name: sum(
                    p.stream.stream.file.file_size_bytes or 0
                    for p in payloads
                )
                for name, payloads in enc_result.encoded_chunks.items()
            },
        )

        # The single post-success write: the winning-limiter table and the
        # winners frame totals persist so fully-reused runs resurface both
        # without re-reading every winner sidecar.  Only on full success — a
        # failed pair leaves the phase pending on rerun, which rebuilds them
        # via the processing path.
        if scan is not None:
            if scan.summaries is not None:
                self._log_limiter_summary(scan.summaries)
            EncodingSidecar(summary=EncodingSummary(
                limiter = scan.summaries,
                frames  = scan.frame_totals() if scan.frames_known else {},
            )).save(encoding_yaml)

        outcome = PhaseOutcome.COMPLETED if enc_result.encoded_count > 0 else PhaseOutcome.REUSED
        return self._make_result(
            outcome, winners, f"{complete_count} pair(s) complete",
            presentation_anchor=presentation_anchor,
        )

    def _resolve_presentation_anchor(
        self,
        optimization_result: OptimizationPhaseResult,
        scan:                _WinnerScan | None,
        plan:                EncodingPlan,
        winner_sizes:        dict[str, int],
    ) -> str | None:
        """The presentation anchor for delta display (Req 59) — never a key.

        Args:
            optimization_result: The optimization phase's result (the due
                                anchor, when one was elected).
            scan:                The concluded winner scan (the measured
                                population for the fallback election).
            plan:                The run's encoding plan (compared-vs-not).
            winner_sizes:        Per-strategy Σ winner file sizes (the
                                election's ordering basis).

        Returns:
            The presentation anchor's display name, or ``None`` (searched
            runs elect nothing — their reference row is the config targets).
        """
        if not plan.fixed_quality:
            return None
        if optimization_result.anchor is not None:
            return optimization_result.anchor

        # No anchor from optimization: DUE only when the run was compared
        # (optimize on, more than one strategy) — then a missing anchor with
        # no measured strategy is an optimization error surfaced loudly,
        # never silently elected around. The uncompared case (all-strategies
        # path — no test work, no measurements at optimization) elects here:
        # the smallest-total-size strategy with measured metrics.
        compared = self._config.encoding.optimize and len(plan.strategies) > 1
        measured = [
            name for name, rows in (scan.frames if scan is not None else {}).items()
            if rows
        ]
        if not measured:
            if compared:
                logger.error(
                    "An anchor was due (compared fixed run) but no strategy "
                    "carried measurements — the ruler is missing (optimization "
                    "error); no silent election."
                )
            return None
        anchor = min(measured, key=lambda name: winner_sizes.get(name, 0))
        logger.info("Presentation anchor (elected at the winner scan): %s", anchor)
        return anchor

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
