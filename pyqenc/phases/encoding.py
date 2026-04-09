"""
Encoding phase for the quality-based encoding pipeline.

This module handles chunk encoding with iterative CRF adjustment to meet
quality targets, including parallel execution and artifact-based resumption.
"""
# CHerSun 2026

import asyncio
import logging
import os
import shutil as _shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import dataclass as _dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, cast

from alive_progress import config_handler

from pyqenc.config import ConfigManager
from pyqenc.constants import (
    CHUNKS_DIR,
    DEFAULT_METRICS_SAMPLING,
    ENCODED_ATTEMPT_GLOB_PATTERN,
    ENCODED_ATTEMPT_NAME_PATTERN,
    ENCODED_OUTPUT_DIR,
    ENCODING_WORKSPACE_DIR,
    FAILURE_SYMBOL_MINOR,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THICK_LINE,
    THRESHOLD_ATTEMPTS_WARNING,
)
from pyqenc.models import (
    AttemptMetadata,
    ChunkMetadata,
    CleanupLevel,
    CropParams,
    PhaseOutcome,
    QualityTarget,
    Strategy,
    VideoMetadata,
)
from pyqenc.phase import Artifact, Phase, PhaseResult
from pyqenc.quality import QualitySearchV2
from pyqenc.state import (
    ArtifactState,
    EncodingParams,
    EncodingResultSidecar,
    MetricsSidecar,
)
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.ffmpeg_runner import run_ffmpeg
from pyqenc.utils.log_format import (
    emit_phase_banner,
    fmt_chunk,
    fmt_chunk_attempt_result,
    fmt_chunk_attempt_start,
    fmt_chunk_final,
    fmt_chunk_start,
    fmt_metric_summary,
    fmt_metric_value,
    log_recovery_line,
)
from pyqenc.utils.visualization import QualityEvaluator
from pyqenc.utils.yaml_utils import write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.metrics import MetricsCollector
    from pyqenc.models import PipelineConfig
    from pyqenc.phases.chunking import ChunkingPhase, ChunkingPhaseResult
    from pyqenc.phases.job import JobPhase, JobPhaseResult
    from pyqenc.phases.optimization import OptimizationPhase, OptimizationPhaseResult

_ENCODING_YAML = "encoding.yaml"

config_handler.set_global(enrich_print=False) # type: ignore
logger = logging.getLogger(__name__)


def _probe_resolution(path: Path) -> str | None:
    """Return the video resolution of *path* as ``'WxH'``, or ``None`` on failure."""
    import json as _json
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        data = _json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            w, h = streams[0].get("width"), streams[0].get("height")
            if w and h:
                return f"{w}x{h}"
    except Exception:
        pass
    return None


def _read_metrics_sidecar(attempt_path: Path) -> dict | None:
    """Read a per-attempt metrics sidecar for an encoded attempt.

    Tries the new YAML format (``.yaml``) first; falls back to the legacy
    JSON format (``.metrics.json``) for backward compatibility (Req 8.2).

    Args:
        attempt_path: Path to the encoded attempt ``.mkv`` file.

    Returns:
        Parsed sidecar dict (keys: ``targets_met``, ``crf``, ``metrics``),
        or ``None`` if no sidecar exists or it cannot be parsed.
    """
    import yaml as _yaml

    yaml_sidecar = attempt_path.with_suffix(".yaml")
    if yaml_sidecar.exists():
        try:
            with yaml_sidecar.open("r", encoding="utf-8") as fh:
                return _yaml.safe_load(fh)
        except Exception:
            pass

    # Legacy fallback
    json_sidecar = attempt_path.with_suffix(".metrics.json")
    if json_sidecar.exists():
        import json as _json
        try:
            with json_sidecar.open("r", encoding="utf-8") as fh:
                return _json.load(fh)
        except Exception:
            pass

    return None


def _write_metrics_sidecar(
    attempt_path:     Path,
    targets_met:      bool,
    crf:              Decimal,
    metrics:          dict[str, float],
    metrics_sampling: int,
) -> None:
    """Atomically write a per-attempt metrics sidecar alongside an encoded attempt.

    Uses ``write_yaml_atomic`` so a crash during writing never leaves a partial
    sidecar.  Stores ALL measured metric values (not filtered to current targets)
    so the CRF history is reusable when quality targets change (Req 6a.1).

    Args:
        attempt_path:     Path to the encoded attempt ``.mkv`` file.
        targets_met:      Whether quality targets were met (for human inspection only).
        crf:              CRF value used for this attempt.
        metrics:          ALL measured quality metrics dict (not filtered to targets).
        metrics_sampling: Frame subsampling factor used when metrics were measured.
    """
    sidecar = attempt_path.with_suffix(".yaml")
    data    = MetricsSidecar(
        crf              = crf,
        targets_met      = targets_met,
        metrics          = metrics,
        metrics_sampling = metrics_sampling,
    )
    try:
        write_yaml_atomic(sidecar, data.to_yaml_dict())
    except Exception as e:
        logger.warning("Failed to write metrics sidecar for %s: %s", attempt_path.name, e)


def _hardlink_or_copy(src: Path, dst: Path) -> None:
    """Hard-link *src* to *dst*, falling back to copy if cross-device.

    Creates parent directories as needed.

    Args:
        src: Source file path (the winning attempt ``.mkv``).
        dst: Destination path in ``encoded/<strategy>/``.
    """
    import os
    import shutil

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
    resolution:      str,
    winning_attempt: Path,
    crf:             Decimal,
    metrics:         dict[str, float],
    targets_met:     bool = True,
) -> None:
    """Atomically write an encoding result sidecar when CRF search converges.

    Written as ``<chunk_id>.<res>.yaml`` in the strategy output directory.
    Its presence marks the ``(chunk_id, strategy)`` pair as ``COMPLETE``
    (Req 6b.1, 6b.2).

    Args:
        output_dir:      Strategy output directory.
        chunk_id:        Chunk identifier.
        resolution:      Output resolution string (e.g. ``'1920x800'``).
        winning_attempt: Path to the winning encoded attempt ``.mkv``.
        crf:             Winning CRF value.
        metrics:         All measured metric values for the winning attempt.
        targets_met:     Whether quality targets were met; ``False`` when the
                         search was exhausted without a passing attempt.
    """
    sidecar_path = output_dir / f"{chunk_id}.{resolution}.yaml"
    data = EncodingResultSidecar(
        winning_attempt = winning_attempt.name,
        crf             = crf,
        metrics         = metrics,
        targets_met     = targets_met,
    )
    try:
        write_yaml_atomic(sidecar_path, data.to_yaml_dict())
        logger.debug(
            "Wrote encoding result sidecar: %s (crf=%s, targets_met=%s)",
            sidecar_path.name, crf, targets_met,
        )
    except Exception as e:
        logger.warning(
            "Failed to write encoding result sidecar for %s/%s: %s",
            chunk_id, resolution, e,
        )


# ---------------------------------------------------------------------------
# Encoding recovery helpers (moved from recovery.py — Req 4.3)
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


def _enc_encoded_strategy_dir(work_dir: Path, strategy: str) -> Path:
    """Return the finalized output directory for *strategy* under ``encoded/``."""
    safe = strategy.replace("+", "_").replace(":", "_")
    return work_dir / ENCODED_OUTPUT_DIR / safe


def _recover_encoding_attempts(
    work_dir:  Path,
    chunk_ids: list[str],
    strategies: list[str],
) -> _PhaseRecovery:
    """Classify all ``(chunk_id, strategy)`` pairs from a single directory scan per strategy.

    For each strategy, lists ``encoded/<strategy>/`` once to build a set of
    chunk_ids that have a result sidecar (``<chunk_id>.*.yaml``).  Pairs whose
    chunk_id appears in that set are ``COMPLETE``; all others are ``ABSENT``.
    No per-pair globs and no file reads are performed during recovery.

    CRF history for pending pairs is loaded lazily by the encoding worker via
    ``_load_history_from_sidecars`` when the pair is actually picked up.

    Args:
        work_dir:   Pipeline working directory.
        chunk_ids:  Chunk identifiers to recover.
        strategies: Strategy names to recover.

    Returns:
        ``_PhaseRecovery`` with per-pair recovery state and pending list.
    """
    pairs:   dict[tuple[str, str], _EncodingRecovery] = {}
    pending: list[tuple[str, str]]                    = []

    complete_count = absent_count = 0

    for strategy in strategies:
        encoded_dir = _enc_encoded_strategy_dir(work_dir, strategy)

        # Build index: chunk_id -> winning .mkv path, from a single directory listing.
        # Layout in encoded/<strategy>/:
        #   <chunk_id>.<res>.q<N>.mkv   — winning attempt file
        #   <chunk_id>.<res>.yaml        — result sidecar (NO quality in name)
        #   <chunk_id>.<res>.q<N>.png    — quality graph (optional)
        # A pair is COMPLETE when both the winning .mkv and its result sidecar exist.
        complete_index: dict[str, Path] = {}
        if encoded_dir.exists():
            # Collect mkv paths and result-sidecar chunk_ids in one pass
            mkv_by_chunk:     dict[str, Path] = {}
            sidecar_chunk_ids: set[str]        = set()

            for f in encoded_dir.iterdir():
                if f.suffix == ".mkv":
                    m = ENCODED_ATTEMPT_NAME_PATTERN.match(f.name)
                    if m:
                        mkv_by_chunk[m.group("chunk_id")] = f
                elif f.suffix == ".yaml":
                    # Result sidecar: <chunk_id>.<res>.yaml — stem has exactly one dot-separated
                    # resolution component at the end, no "crf" segment.
                    stem_parts = f.stem.rsplit(".", 1)
                    if len(stem_parts) == 2:
                        res = stem_parts[-1]
                        if "x" in res and res.replace("x", "").isdigit():
                            sidecar_chunk_ids.add(stem_parts[0])

            for chunk_id_candidate, mkv_path in mkv_by_chunk.items():
                if chunk_id_candidate in sidecar_chunk_ids:
                    complete_index[chunk_id_candidate] = mkv_path

        for chunk_id in chunk_ids:
            if chunk_id in complete_index:
                pairs[(chunk_id, strategy)] = _EncodingRecovery(
                    chunk_id     = chunk_id,
                    strategy     = strategy,
                    state        = ArtifactState.COMPLETE,
                    winning_file = complete_index[chunk_id],
                )
                complete_count += 1
            else:
                pairs[(chunk_id, strategy)] = _EncodingRecovery(
                    chunk_id = chunk_id,
                    strategy = strategy,
                    state    = ArtifactState.ABSENT,
                )
                absent_count += 1
                pending.append((chunk_id, strategy))

    logger.debug(
        "Attempts recovery: %d pair(s) total — %d COMPLETE, %d ABSENT",
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
    reused:       bool                  = False
    error:        str            | None = None


@dataclass
class EncodingResult:
    """Result of encoding all chunks.

    Attributes:
        encoded_chunks: Mapping of chunk_id -> strategy -> encoded file path.
        reused_count:   Number of chunks reused from previous runs.
        encoded_count:  Number of chunks newly encoded.
        outcome:        Phase outcome.
        failed_chunks:  List of chunk IDs that failed.
        error:          Error message if pipeline failed.
    """

    encoded_chunks: dict[str, dict[str, Path]] = field(default_factory=dict)
    reused_count:   int                         = 0
    encoded_count:  int                         = 0
    outcome:        PhaseOutcome                = PhaseOutcome.COMPLETED
    failed_chunks:  list[str]                   = field(default_factory=list)
    error:          str | None                  = None


class ChunkEncoder:
    """Handles encoding of individual chunks with CRF adjustment.

    This class manages the iterative encoding process for a single chunk,
    adjusting CRF values until quality targets are met.
    """

    def __init__(
        self,
        quality_evaluator: QualityEvaluator,
        work_dir:          Path,
        crop_params:       CropParams | None = None,
        cleanup_level:     CleanupLevel      = CleanupLevel.NONE,
        visual_hash:       bool              = True,
        metrics_sampling:  int               = DEFAULT_METRICS_SAMPLING,
    ):
        """Initialize chunk encoder.

        Args:
            quality_evaluator: Quality evaluator for metric calculation.
            work_dir:          Working directory for artifacts.
            crop_params:       Optional crop parameters to apply to every chunk attempt.
            cleanup_level:     Controls deletion of intermediate attempt files after
                               a pair converges (Req 12.3).
            visual_hash:       When ``True``, prepend a deterministic emoji to every
                               chunk log line for visual distinction in parallel output.
            metrics_sampling:  Frame subsampling factor for quality metric generation.
        """
        self.quality_evaluator = quality_evaluator
        self.work_dir          = work_dir
        self._crop_params      = crop_params
        self._cleanup_level    = cleanup_level
        self._visual_hash      = visual_hash
        self._metrics_sampling = metrics_sampling

    def _get_output_dir(self, strategy: Strategy) -> Path:
        """Get the CRF search workspace directory for *strategy*.

        Attempt files (intermediate) are written here during the CRF search.
        On convergence the winning attempt is hard-linked into ``_get_encoded_dir``.

        Args:
            strategy: Encoding strategy.

        Returns:
            Path to ``<work_dir>/encoding/<safe_strategy>/``.
        """
        return self.work_dir / ENCODING_WORKSPACE_DIR / strategy.safe_name

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
        return self.work_dir / ENCODED_OUTPUT_DIR / strategy.safe_name

    def _get_attempt_path(
        self,
        chunk_id:   str,
        strategy:   Strategy,
        resolution: str | None = None,
        crf:        Decimal | None = None,
    ) -> Path:
        """Get the final path for a CRF-only encoded attempt.

        Naming pattern: ``<chunk_id>.<width>x<height>.q{CRF}.mkv``

        Falls back to a simpler name when resolution or CRF are not yet known.

        Args:
            chunk_id:   Chunk identifier (e.g. ``'00꞉00꞉00․000-00꞉05꞉20․000'``).
            strategy:   Encoding strategy.
            resolution: Output resolution string (e.g. ``'1920x800'``).
            crf:        CRF value used for this attempt.

        Returns:
            Path to encoded file for this attempt.
        """
        output_dir = self._get_output_dir(strategy)
        if resolution and crf is not None:
            filename = f"{chunk_id}.{resolution}.q{crf}.mkv"
        else:
            filename = f"{chunk_id}.mkv"
        return output_dir / filename

    def _check_existing_encoding(
        self,
        chunk_id:   str,
        strategy:   Strategy,
        resolution: str | None,
        crf:        Decimal,
    ) -> AttemptMetadata | None:
        """Check if a complete encoded attempt already exists on disk.

        Scans the strategy output directory for a file matching
        ``ENCODED_ATTEMPT_NAME_PATTERN`` with the correct ``chunk_id``,
        ``resolution``, and ``crf``.  No progress-tracker lookup is performed.

        Args:
            chunk_id:   Chunk identifier.
            strategy:   Encoding strategy.
            resolution: Expected resolution string (e.g. ``'1920x800'``).
                        When ``None`` any resolution is accepted.
            crf:        CRF value to look for.

        Returns:
            ``AttemptMetadata`` if a matching file exists, ``None`` otherwise.
        """
        output_dir = self._get_output_dir(strategy)
        if not output_dir.exists():
            return None

        for candidate in output_dir.glob(f"{chunk_id}.*.q*.mkv"):
            m = ENCODED_ATTEMPT_NAME_PATTERN.match(candidate.name)
            if m is None:
                continue
            if m.group("chunk_id") != chunk_id:
                continue
            try:
                file_crf = Decimal(str(m.group("quality"))).quantize(strategy.codec.quality_granularity)
            except ValueError:
                continue
            if abs(file_crf - crf) > Decimal("0.05"):
                continue
            file_resolution = m.group("resolution")
            if resolution is not None and file_resolution != resolution:
                continue
            # Found a matching file
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue
            return AttemptMetadata(
                path=candidate,
                chunk_id=chunk_id,
                strategy=strategy.name,
                crf=file_crf,
                resolution=file_resolution,
                file_size_bytes=size,
            )
        return None

    def _finalize_winning_attempt(
        self,
        strategy:        Strategy,
        chunk_id:        str,
        resolution:      str,
        winning_attempt: Path,
        crf:             Decimal,
        metrics:         dict[str, float],
        targets_met:     bool = True,
    ) -> None:
        """Hard-link the winning attempt into ``encoded/`` and write the result sidecar.

        On CRF search convergence:
        1. Hard-link the winning ``.mkv`` from ``encoding/<strategy>/`` into
           ``encoded/<strategy>/`` (same filename).
        2. Hard-link the winning ``.png`` quality graph (if present) alongside it.
        3. Write the encoding result sidecar ``<chunk_id>.<res>.yaml`` into
           ``encoded/<strategy>/`` — its presence marks the pair as ``COMPLETE``.

        Args:
            strategy:        Encoding strategy.
            chunk_id:        Chunk identifier.
            resolution:      Output resolution string (e.g. ``'1920x800'``).
            winning_attempt: Path to the winning attempt ``.mkv`` in ``encoding/``.
            crf:             Winning CRF value.
            metrics:         All measured metric values for the winning attempt.
            targets_met:     Whether quality targets were met; ``False`` when the
                             search was exhausted without a passing attempt.
        """
        encoded_dir = self._get_encoded_dir(strategy)
        encoded_dir.mkdir(parents=True, exist_ok=True)

        # 1. Hard-link the winning .mkv
        dst_mkv = encoded_dir / winning_attempt.name
        if not dst_mkv.exists():
            _hardlink_or_copy(winning_attempt, dst_mkv)

        # 2. Hard-link the winning quality graph (.png) if it exists
        src_graph = winning_attempt.with_suffix(".png")
        if src_graph.exists():
            dst_graph = encoded_dir / src_graph.name
            if not dst_graph.exists():
                _hardlink_or_copy(src_graph, dst_graph)

        # 3. Write the encoding result sidecar into encoded/
        _write_encoding_result_sidecar(
            output_dir  = encoded_dir,
            chunk_id    = chunk_id,
            resolution  = resolution,
            winning_attempt = dst_mkv,
            crf         = crf,
            metrics     = metrics,
            targets_met = targets_met,
        )

        # 4. Intermediate cleanup: delete all attempt files for this pair from encoding/
        #    (Req 6.6, 12.3) — only after the hard-link and sidecar are safely written.
        if self._cleanup_level >= CleanupLevel.INTERMEDIATE:
            encoding_dir = self._get_output_dir(strategy)
            if encoding_dir.exists():
                for attempt_file in list(encoding_dir.glob(f"{chunk_id}.*.q*.mkv")):
                    m = ENCODED_ATTEMPT_NAME_PATTERN.match(attempt_file.name)
                    if m and m.group("chunk_id") == chunk_id:
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
                            import shutil as _shutil
                            try:
                                _shutil.rmtree(metrics_dir)
                                logger.debug(
                                    "Intermediate cleanup: deleted metrics dir %s", metrics_dir.name
                                )
                            except OSError as exc:
                                logger.warning(
                                    "Intermediate cleanup: could not delete metrics dir %s: %s",
                                    metrics_dir.name, exc,
                                )

    def _encode_with_ffmpeg(
        self,
        chunk:       ChunkMetadata,
        strategy:    Strategy,
        crf:         Decimal,
        output_file: Path,
    ) -> bool:
        """Encode chunk with FFmpeg using the runner's ``.tmp``-then-rename protocol.

        Args:
            chunk:       Chunk information.
            strategy:    Encoding strategy (provides ffmpeg args).
            crf:         CRF value to use.
            output_file: Intended final output path.

        Returns:
            ``True`` if encoding succeeded, ``False`` otherwise.
        """
        output_file.parent.mkdir(parents=True, exist_ok=True)

        vf_filter = (
            self._crop_params.to_ffmpeg_filter()
            if self._crop_params and not self._crop_params.is_empty()
            else None
        )
        ffmpeg_args = strategy.to_ffmpeg_args(crf, vf_filter=vf_filter)

        # Replace the {input} sentinel with the actual input path.
        # The preceding "-i" flag is already in the template; everything before
        # "-i" is pre-input args (e.g. -hwaccel), everything after is output-side.
        i_pos = ffmpeg_args.index("{input}")
        cmd: list[str | os.PathLike] = [
            "ffmpeg",
            "-y",
            *ffmpeg_args[:i_pos],
            chunk.path,
            *ffmpeg_args[i_pos + 1:],
            "-f", "matroska",
            output_file,
        ]

        logger.debug("Encoding command: %s", " ".join(str(a) for a in cmd))

        try:
            result = run_ffmpeg(cmd, output_file=output_file)

            if not result.success:
                logger.error(
                    "FFmpeg encoding failed with code %d for chunk %s",
                    result.returncode, chunk.chunk_id,
                )
                return False

            return True

        except Exception as e:
            logger.error("Exception during encoding: %s", e)
            return False

    def encode_chunk(
        self,
        chunk:            ChunkMetadata,
        reference:        VideoMetadata,
        strategy:         Strategy,
        quality_targets:  list[QualityTarget],
        initial_crf:      Decimal,
        force:            bool  = False,
        max_attempts:     int   = 10,
    ) -> ChunkEncodingResult:
        """Encode single chunk, adjusting CRF until quality targets met.

        Args:
            chunk:           Chunk information.
            reference:       Reference chunk for quality comparison.
            strategy:        Encoding strategy.
            quality_targets: Quality targets to meet.
            initial_crf:     Initial CRF value (if no history available).
            force:           If ``False``, reuse existing encoding that meets targets.
            max_attempts:    Unused; kept for API compatibility.

        Returns:
            ChunkEncodingResult with encoding outcome.
        """
        logger.debug(fmt_chunk_start(strategy.name, chunk.chunk_id, self._visual_hash))

        search = QualitySearchV2(
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
        _any_real_work: bool                       = False

        while True:
            attempt_number += 1

            if attempt_number == THRESHOLD_ATTEMPTS_WARNING:
                logger.warning(
                    fmt_chunk(strategy.name, chunk.chunk_id,
                              f"reached {THRESHOLD_ATTEMPTS_WARNING} attempts without meeting targets — "
                              "continuing search",
                              self._visual_hash)
                )

            logger.debug(fmt_chunk_attempt_start(strategy.name, chunk.chunk_id, attempt_number, current_q, strategy.codec.quality_label, self._visual_hash, strategy.codec.quality_log_padding))

            # Determine the final output path for this CRF (resolution unknown yet)
            # We'll encode to a temp file, probe resolution, then rename to final path.
            output_dir = self._get_output_dir(strategy)
            output_dir.mkdir(parents=True, exist_ok=True)

            # Probe resolution from the source chunk to build the final path before encoding
            # (so we can check for an existing file first)
            if chunk._resolution is not None:
                resolution = chunk._resolution
            else:
                resolution = chunk.resolution  # triggers lazy probe

            # When crop is active the encoded output resolution differs from the source chunk
            # resolution, so we cannot use the source resolution to match existing files.
            # Pass None to accept any resolution for the given chunk_id + crf.
            check_resolution = None if (self._crop_params and not self._crop_params.is_empty()) else resolution

            # Check for existing encoding at this quality value (filesystem scan, no tracker).
            goto_eval   = False
            output_file: Path | None = None
            if not force:
                existing = self._check_existing_encoding(
                    chunk.chunk_id, strategy, check_resolution, current_q
                )
                if existing is not None:
                    sidecar = _read_metrics_sidecar(existing.path)
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
                    if sidecar_valid and sidecar is not None:
                        # Full cache hit — no real work performed.
                        all_sidecar_metrics: dict[str, float] = {
                            k: float(v) for k, v in sidecar.get("metrics", {}).items()
                        }
                        targets_set_reused = {f"{t.metric}_{t.statistic}" for t in quality_targets}
                        metrics_dict: dict[str, float] = {
                            k: v for k, v in all_sidecar_metrics.items() if k in targets_set_reused
                        }
                        targets_met: bool = all(
                            all_sidecar_metrics.get(f"{t.metric}_{t.statistic}", 0.0) >= t.value
                            for t in quality_targets
                        )
                        metric_summary = fmt_metric_summary(metrics_dict, quality_targets)
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

                        logger.info(
                            fmt_chunk_attempt_result(
                                strategy.name, chunk.chunk_id, attempt_number,
                                f"{pass_fail} with {strategy.codec.quality_label} {str(existing.crf).rjust(strategy.codec.quality_log_padding)} ({metric_summary}){best_string} [reused]",
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
                            fmt_chunk(strategy.name, chunk.chunk_id,
                                f"existing attempt ({strategy.codec.quality_label.lower()}={str(existing.crf).rjust(strategy.codec.quality_log_padding)}) — re-evaluating metrics ({reason})",
                                self._visual_hash),
                        )
                        _any_real_work = True
                        output_file    = existing.path
                        goto_eval      = True

            if not goto_eval:
                # Encode — real work.
                _any_real_work = True
                output_file    = self._get_attempt_path(
                    chunk.chunk_id, strategy, resolution=resolution, crf=current_q
                )
                encode_success = self._encode_with_ffmpeg(
                    chunk, strategy, current_q, output_file
                )

                if not encode_success:
                    error_msg = f"Encoding failed for chunk {chunk.chunk_id}"
                    logger.error(error_msg)
                    return ChunkEncodingResult(
                        chunk_id    = chunk.chunk_id,
                        strategy    = strategy.name,
                        success     = False,
                        targets_met = False,
                        attempts    = attempt_number,
                        error       = error_msg,
                    )

                # Update resolution from actual output (crop may change dimensions).
                actual_resolution = _probe_resolution(output_file)
                if actual_resolution and actual_resolution != resolution:
                    correct_path = self._get_attempt_path(
                        chunk.chunk_id, strategy, resolution=actual_resolution, crf=current_q
                    )
                    try:
                        output_file.replace(correct_path)
                    except OSError:
                        output_file.rename(correct_path)
                    output_file = correct_path
                    resolution  = actual_resolution

            assert output_file is not None

            # Evaluate quality — raw metric logs/stats go into a per-attempt subfolder;
            # the plot and YAML sidecar stay next to the .mkv.
            attempt_metrics_dir = output_file.parent / output_file.stem
            attempt_plot_path   = output_file.parent / f"{output_file.stem}.png"
            evaluation = self.quality_evaluator.evaluate_chunk(
                encoded              = output_file,
                reference            = reference.path,
                ref_crop             = self._crop_params or CropParams(),
                targets              = quality_targets,
                output_dir           = attempt_metrics_dir,
                subsample_factor     = self._metrics_sampling,
                plot_path            = attempt_plot_path,
                chunk_start_seconds  = chunk.start_timestamp,
            )

            # Collect ALL measured metrics (not filtered to current targets) for the sidecar
            # so the quality history is reusable when quality targets change.
            all_metrics: dict[str, float] = {
                f"{metric.value}_{stat}": float(value)
                for metric, stats in evaluation.metrics.items()
                for stat, value in stats.items()
            }

            # Targeted metrics subset (for search and convergence decisions).
            targets_set  = {f"{t.metric}_{t.statistic}" for t in quality_targets}
            metrics_dict = {k: v for k, v in all_metrics.items() if k in targets_set}

            # Write per-attempt metrics sidecar atomically.
            _write_metrics_sidecar(output_file, evaluation.targets_met, current_q, all_metrics, self._metrics_sampling)

            # Build AttemptMetadata for this attempt.
            attempt_meta = AttemptMetadata(
                path            = output_file,
                chunk_id        = chunk.chunk_id,
                strategy        = strategy.name,
                crf             = current_q,
                resolution      = resolution or "",
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

            metric_summary = fmt_metric_summary(metrics_dict, quality_targets)
            pass_fail      = (
                f"{SUCCESS_SYMBOL_MINOR} pass"
                if evaluation.targets_met
                else f"{FAILURE_SYMBOL_MINOR} miss"
            )
            logger.info(
                fmt_chunk_attempt_result(
                    strategy.name, chunk.chunk_id, attempt_number,
                    f"{pass_fail} with {strategy.codec.quality_label} {str(current_q).rjust(strategy.codec.quality_log_padding)} ({metric_summary}){best_string}",
                    self._visual_hash,
                )
            )

            if next_q is None:
                break

            logger.debug("Adjusting %s from %s to %s", strategy.codec.quality_label, current_q, next_q)
            current_q = next_q

        # --- Post-loop: finalize ---

        if search.best_targets_met and final_attempt is not None:
            logger.info(fmt_chunk_final(
                strategy.name, chunk.chunk_id, search.best_quality, attempt_number,
                strategy.codec.quality_label, self._visual_hash, strategy.codec.quality_log_padding,
            ))
            self._finalize_winning_attempt(
                strategy        = strategy,
                chunk_id        = chunk.chunk_id,
                resolution      = final_attempt.resolution,
                winning_attempt = final_attempt.path,
                crf             = search.best_quality,  # type: ignore[arg-type]
                metrics         = search.best_metrics or {},
                targets_met     = True,
            )
        elif not search.best_targets_met and best_fail_attempt is not None:
            logger.warning(
                "%s search space exhausted for chunk %s strategy %s after %d attempts — accepting best attempt (%s=%s)",
                strategy.codec.quality_label, chunk.chunk_id, strategy.name, attempt_number,
                strategy.codec.quality_label, search.best_quality,
            )
            self._finalize_winning_attempt(
                strategy        = strategy,
                chunk_id        = chunk.chunk_id,
                resolution      = best_fail_attempt.resolution,
                winning_attempt = best_fail_attempt.path,
                crf             = search.best_quality,  # type: ignore[arg-type]
                metrics         = search.best_metrics or {},
                targets_met     = False,
            )
            final_attempt = best_fail_attempt
        elif search.best_quality is None:
            logger.warning(
                "%s search space exhausted for chunk %s strategy %s after %d attempts",
                strategy.codec.quality_label, chunk.chunk_id, strategy.name, attempt_number,
            )

        # Progress bar advance — after the loop so ETA reflects actual encode time.
        if not _any_real_work:
            # All cache hits — chunk was fully recovered from existing artifacts.
            return ChunkEncodingResult(
                chunk_id     = chunk.chunk_id,
                strategy     = strategy.name,
                success      = True,
                targets_met  = search.best_targets_met,
                final_crf    = search.best_quality,
                attempts     = attempt_number,
                encoded_file = final_attempt,
                reused       = True,
            )

        if final_attempt is not None or best_fail_attempt is not None:
            winning = final_attempt if final_attempt is not None else best_fail_attempt
            return ChunkEncodingResult(
                chunk_id     = chunk.chunk_id,
                strategy     = strategy.name,
                success      = True,
                targets_met  = search.best_targets_met,
                final_crf    = search.best_quality,
                attempts     = attempt_number,
                encoded_file = winning,
                reused       = False,
            )
        else:
            error_msg = f"Failed to meet quality targets after {attempt_number} attempts"
            logger.error("Chunk %s: %s", chunk.chunk_id, error_msg)
            return ChunkEncodingResult(
                chunk_id    = chunk.chunk_id,
                strategy    = strategy.name,
                success     = False,
                targets_met = False,
                attempts    = attempt_number,
                error       = error_msg,
            )



class ChunkQueue:
    """Manages queue of chunks for parallel encoding.

    Prioritizes completing started chunks before starting new ones.
    """

    def __init__(self, chunks: list[ChunkMetadata], strategies: list[Strategy]):
        """Initialize chunk queue.

        Args:
            chunks:     List of chunks to encode.
            strategies: List of strategies to apply.
        """
        self.chunks     = chunks
        self.strategies = strategies
        self._pending:     list[tuple[ChunkMetadata, Strategy]] = []
        self._in_progress: set[tuple[str, str]]                 = set()
        self._completed:   set[tuple[str, str]]                 = set()

        # Build initial queue (all chunk+strategy combinations)
        for chunk in chunks:
            for strategy in strategies:
                self._pending.append((chunk, strategy))

    def get_next(self) -> tuple[ChunkMetadata, Strategy] | None:
        """Get next chunk+strategy to encode.

        Prioritizes completing started chunks before starting new ones.

        Returns:
            Tuple of (chunk, strategy) or None if queue empty.
        """
        if not self._pending:
            return None

        # Check if any in-progress chunks have other strategies pending
        for chunk, strategy in self._pending:
            if any((chunk.chunk_id, s.name) in self._in_progress for s in self.strategies):
                # This chunk has work in progress, prioritize it
                self._pending.remove((chunk, strategy))
                self._in_progress.add((chunk.chunk_id, strategy.name))
                return (chunk, strategy)

        # No in-progress chunks, take first pending
        chunk, strategy = self._pending.pop(0)
        self._in_progress.add((chunk.chunk_id, strategy.name))
        return (chunk, strategy)

    def mark_complete(self, chunk_id: str, strategy: Strategy) -> None:
        """Mark chunk+strategy as complete.

        Args:
            chunk_id: Chunk identifier.
            strategy: Encoding strategy.
        """
        self._in_progress.discard((chunk_id, strategy.name))
        self._completed.add((chunk_id, strategy.name))

    def mark_failed(self, chunk_id: str, strategy: Strategy) -> None:
        """Mark chunk+strategy as failed.

        Args:
            chunk_id: Chunk identifier.
            strategy: Encoding strategy.
        """
        self._in_progress.discard((chunk_id, strategy.name))

    def is_empty(self) -> bool:
        """Check if queue is empty.

        Returns:
            True if no more work to do.
        """
        return len(self._pending) == 0 and len(self._in_progress) == 0

    def get_progress(self) -> tuple[int, int]:
        """Get current progress.

        Returns:
            Tuple of (completed, total).
        """
        total     = len(self.chunks) * len(self.strategies)
        completed = len(self._completed)
        return (completed, total)


async def _encode_chunk_async(
    encoder:         ChunkEncoder,
    chunk:           ChunkMetadata,
    reference:       VideoMetadata,
    strategy:        Strategy,
    quality_targets: list[QualityTarget],
    initial_crf:     Decimal,
    force:           bool,
) -> ChunkEncodingResult:
    """Async wrapper for chunk encoding.

    Args:
        encoder:         ChunkEncoder instance.
        chunk:           Chunk to encode.
        reference:       Reference chunk.
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
        reference,
        strategy,
        quality_targets,
        initial_crf,
        force,
        10,  # max_attempts (unused, kept for API compat)
    )


async def _encode_chunks_parallel(
    encoder:         ChunkEncoder,
    chunks:          list[ChunkMetadata],
    reference_dir:   Path,
    strategies:      list[Strategy],
    quality_targets: list[QualityTarget],
    max_parallel:    int,
    force:           bool,
    collector:       "MetricsCollector",
    phase_recovery:  "_PhaseRecovery | None"                                  = None,
    advance:         Callable[[int | float, AdvanceState | None], None] | None = None,
) -> EncodingResult:
    """Encode chunks in parallel with semaphore control.

    Args:
        encoder:        ChunkEncoder instance.
        chunks:         List of chunks to encode.
        reference_dir:  Directory containing reference chunks.
        strategies:     List of strategies to use.
        quality_targets: Quality targets to meet.
        max_parallel:   Maximum concurrent encodings.
        force:          Whether to force re-encoding.
        phase_recovery: Optional recovery state from ``recover_attempts``; when
                        provided, ``COMPLETE`` pairs are skipped and ``ARTIFACT_ONLY``
                        pairs resume from their recovered ``QualitySearch`` state.
        advance:        Optional advance callable from ``ProgressBar``; called with
                        chunk duration in seconds and an ``AdvanceState`` on each
                        chunk completion.
        collector:      Metrics collector for timing and convergence tracking;
                        wraps the entire encoding loop with
                        ``time(TimeKey.ENCODING_MAIN)`` and calls
                        ``step(TimeKey.ENCODING_MAIN, convergence_update=...)``
                        after each chunk/strategy pair converges.

    Returns:
        EncodingResult with all encoding outcomes.
    """
    result    = EncodingResult()
    semaphore = asyncio.Semaphore(max_parallel)
    counter_failed = 0

    # Pre-populate result with COMPLETE pairs from recovery (skip them in the queue)
    complete_pairs: set[tuple[str, str]] = set()
    if phase_recovery is not None:
        for chunk in chunks:
            for strategy in strategies:
                pair_recovery = phase_recovery.pairs.get((chunk.chunk_id, strategy.name))
                if pair_recovery is not None and pair_recovery.state == ArtifactState.COMPLETE:
                    logger.debug(
                        "Skipping COMPLETE pair %s/%s (encoding result sidecar valid)",
                        chunk.chunk_id, strategy.name,
                    )
                    if chunk.chunk_id not in result.encoded_chunks:
                        result.encoded_chunks[chunk.chunk_id] = {}
                    if pair_recovery.winning_file is None:
                        raise ValueError(f"Winning file not found for {chunk.chunk_id}/{strategy.name}")
                    result.encoded_chunks[chunk.chunk_id][strategy.name] = pair_recovery.winning_file
                    result.reused_count += 1
                    complete_pairs.add((chunk.chunk_id, strategy.name))

    queue = ChunkQueue(chunks, strategies)
    # Remove already-complete pairs from the queue
    queue._pending = [
        (c, s) for (c, s) in queue._pending
        if (c.chunk_id, s.name) not in complete_pairs
    ]
    queue._completed = complete_pairs.copy()

    async def encode_worker() -> None:
        """Worker coroutine for encoding chunks."""
        nonlocal counter_failed
        while not queue.is_empty():
            next_item = queue.get_next()
            if next_item is None:
                break

            chunk, strategy = next_item

            async with semaphore:
                # Find reference chunk
                reference = reference_dir / chunk.path.name
                if not reference.exists():
                    logger.error("Reference chunk not found: %s", reference)
                    queue.mark_failed(chunk.chunk_id, strategy)
                    result.failed_chunks.append(chunk.chunk_id)
                    if advance is not None:
                        advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)
                    continue

                # Encode chunk using the codec's default quality as the fixed starting point.
                # Predictable initial quality = predictable recovery path when parameters change.
                gran = strategy.codec.quality_granularity
                chunk_initial_crf = strategy.codec.default_quality.quantize(gran)

                # Encode chunk
                chunk_result = await _encode_chunk_async(
                    encoder,
                    chunk,
                    VideoMetadata(path=reference),
                    strategy,
                    quality_targets,
                    chunk_initial_crf,
                    force,
                )

                # Update result
                if chunk_result.success:
                    if chunk.chunk_id not in result.encoded_chunks:
                        result.encoded_chunks[chunk.chunk_id] = {}
                    encoded_path = chunk_result.encoded_file.path if chunk_result.encoded_file else None
                    result.encoded_chunks[chunk.chunk_id][strategy.name] = encoded_path

                    if chunk_result.reused:
                        result.reused_count += 1
                        if advance is not None:
                            advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.SKIPPED)
                    else:
                        result.encoded_count += 1
                        if advance is not None:
                            advance(chunk.end_timestamp - chunk.start_timestamp)
                        # Record convergence for this chunk/strategy pair
                        from pyqenc.metrics import ConvergenceUpdate, TimeKey
                        collector.step(
                            TimeKey.ENCODING_MAIN,
                            convergence_update=ConvergenceUpdate(
                                strategy      = strategy.name,
                                attempt_count = chunk_result.attempts,
                            ),
                        )

                    queue.mark_complete(chunk.chunk_id, strategy)
                else:
                    queue.mark_failed(chunk.chunk_id, strategy)
                    result.failed_chunks.append(chunk.chunk_id)
                    counter_failed += 1
                    if advance is not None:
                        advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)

    # Start worker tasks
    workers = [asyncio.create_task(encode_worker()) for _ in range(max_parallel)]

    # Wait for all workers to complete
    from pyqenc.metrics import TimeKey
    async with collector.time(TimeKey.ENCODING_MAIN):
        await asyncio.gather(*workers)

    return result


def encode_all_chunks(
    chunks:            list[ChunkMetadata],
    reference_dir:     Path,
    strategies:        list[str],
    quality_targets:   list[QualityTarget],
    work_dir:          Path,
    collector:         "MetricsCollector",
    max_parallel:    int               = 2,
    force:           bool              = False,
    dry_run:         bool              = False,
    crop_params:     CropParams | None = None,
    encoding_yaml:   Path | None       = None,
    cleanup_level:   CleanupLevel      = CleanupLevel.NONE,
    visual_hash:     bool              = True,
    metrics_sampling: int              = 10,
) -> EncodingResult:
    """Encode all chunks with quality-targeted CRF adjustment.

    This is the main entry point for the encoding phase. It handles:
    - Pre-validating crop params against ``encoding.yaml`` (Req 3.5)
    - Writing ``encoding.yaml`` with current crop params (Req 2.4)
    - Calling ``recover_attempts`` to classify all ``(chunk, strategy)`` pairs
    - Skipping ``COMPLETE`` pairs and resuming ``ARTIFACT_ONLY`` pairs
    - Parallel encoding of chunks that need work

    Args:
        chunks:            List of chunks to encode.
        reference_dir:     Directory containing reference chunks (already cropped).
        strategies:        List of encoding strategy name strings to use.
        quality_targets:   Quality targets to meet.
        work_dir:          Working directory for artifacts.
        collector:         Metrics collector for timing and convergence tracking.
        max_parallel:      Maximum concurrent encoding processes
        force:             If False, reuse existing encodings that meet current targets
        dry_run:           If True, only report what would be done without encoding
        crop_params:       Crop parameters to apply uniformly to every chunk attempt.                           When ``None``, no cropping is applied.
                           When ``None``, no cropping is applied.
        encoding_yaml:     Optional path to ``encoding.yaml`` for crop pre-validation
                           and persistence.  When provided, crop pre-validation
                           and ``encoding.yaml`` persistence are enabled.
        cleanup_level:     Controls deletion of intermediate attempt files after each
                           pair converges (Req 6.6, 12.3).
        collector:         Metrics collector; passed through to
                           ``_encode_chunks_parallel`` for timing and convergence tracking.
        metrics_sampling:  Frame subsampling factor for quality metric generation.
                           Passed through to ``ChunkEncoder`` and then to
                           ``QualityEvaluator.evaluate_chunk``.

    Returns:
        EncodingResult with paths to encoded chunks and statistics
    """
    logger.debug(
        "Encoding phase: %d chunks, %d strategies, %d quality targets",
        len(chunks), len(strategies), len(quality_targets),
    )

    # --- Step 1: Crop pre-validation against encoding.yaml (Req 3.5) ---
    if encoding_yaml is not None:
        persisted_enc = EncodingParams.load(encoding_yaml)
        if persisted_enc is not None:
            if persisted_enc.crop != crop_params:
                if force:
                    logger.warning(
                        "Crop params changed since last encoding run "
                        "(persisted=%s, current=%s) — --force: deleting all encoded attempt artifacts",
                        persisted_enc.crop, crop_params,
                    )
                    import shutil as _shutil
                    for _dir in (work_dir / ENCODING_WORKSPACE_DIR, work_dir / ENCODED_OUTPUT_DIR):
                        if _dir.exists():
                            _shutil.rmtree(_dir)
                            logger.debug("Deleted directory: %s", _dir)
                else:
                    logger.critical(
                        "Crop params changed since last encoding run "
                        "(persisted=%s, current=%s). "
                        "Re-run with --force to delete stale encoded artifacts and continue.",
                        persisted_enc.crop, crop_params,
                    )
                    return EncodingResult(
                        outcome=PhaseOutcome.FAILED,
                        error="Crop params mismatch — aborting. Use --force to override.",
                    )

    # --- Stale .tmp cleanup (Req 7.7) ---
    encoding_base = work_dir / ENCODING_WORKSPACE_DIR
    if encoding_base.exists():
        for tmp_file in encoding_base.rglob(f"*{TEMP_SUFFIX}"):
            logger.warning("Removing stale temp file from previous run: %s", tmp_file.name)
            try:
                tmp_file.unlink()
            except OSError as e:
                logger.warning("Could not remove stale temp file %s: %s", tmp_file, e)

    # --- Step 2: Write encoding.yaml with current crop params (Req 2.4) ---
    if encoding_yaml is not None and not dry_run:
        EncodingParams(crop=crop_params).save(encoding_yaml)
        logger.debug("Wrote encoding.yaml (crop=%s)", crop_params)

    # --- Step 3: Artifact recovery via _recover_encoding_attempts (Req 3.6) ---
    chunk_ids      = [c.chunk_id for c in chunks]
    strategy_names = [s if isinstance(s, str) else s.name for s in strategies]
    phase_recovery = _recover_encoding_attempts(work_dir, chunk_ids, strategy_names)

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

    # Resolve Strategy objects (with codec) for the parallel worker
    resolved_strategies: list[Strategy] = ConfigManager().resolve_strategies(strategy_names)

    # Create encoder
    encoder = ChunkEncoder(
        quality_evaluator = QualityEvaluator(work_dir),
        work_dir          = work_dir,
        crop_params       = crop_params,
        cleanup_level     = cleanup_level,
        visual_hash       = visual_hash,
        metrics_sampling  = metrics_sampling,
    )

    # Run parallel encoding — COMPLETE pairs are skipped inside _encode_chunks_parallel
    logger.debug("Starting parallel encoding with %d workers", max_parallel)
    total_seconds = sum(c.end_timestamp - c.start_timestamp for c in chunks) * len(resolved_strategies)
    with ProgressBar(total_seconds, title="Encoding", total_count=len(chunks) * len(resolved_strategies)) as advance:
        # Update the bar for completed chunks
        chunks_by_id = {c.chunk_id: c for c in chunks}
        for r in phase_recovery.pairs.values():
            if r.state == ArtifactState.COMPLETE:
                advance((chunks_by_id[r.chunk_id].end_timestamp - chunks_by_id[r.chunk_id].start_timestamp), AdvanceState.SKIPPED)

        # Run parallel encoding
        result = asyncio.run(
            _encode_chunks_parallel(
                encoder         = encoder,
                chunks          = chunks,
                reference_dir   = reference_dir,
                strategies      = resolved_strategies,
                quality_targets = quality_targets,
                max_parallel    = max_parallel,
                force           = force,
                phase_recovery  = phase_recovery,
                advance         = advance,
                collector       = collector,
            )
        )
        advance(0, AdvanceState.COMPLETE)

    # Log summary
    logger.info(
        "Encoding complete: %d newly encoded, %d reused, %d failed",
        result.encoded_count, result.reused_count, len(result.failed_chunks),
    )

    if result.failed_chunks:
        logger.error("Failed chunks: %s", ", ".join(result.failed_chunks))

    return result

# ---------------------------------------------------------------------------
# EncodingPhase — Phase object (task 9)
# ---------------------------------------------------------------------------

@_dataclass
class EncodedArtifact(Artifact):
    """Encoding artifact for a single ``(chunk_id, strategy)`` pair.

    Attributes:
        chunk_id: Chunk identifier.
        strategy: Strategy used to produce this artifact.
        crf:      Winning CRF value; ``None`` when state is not ``COMPLETE``.
    """

    chunk_id: str = ""
    strategy: str = ""
    crf:      Decimal | None = None


@_dataclass
class EncodingPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying encoding-specific payload.

    Attributes:
        encoded: All ``(chunk, strategy)`` artifacts in any state.
    """

    encoded: list[EncodedArtifact] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.encoded is None:
            self.encoded = []


class EncodingPhase:
    """Phase object for CRF-search chunk encoding.

    Owns artifact enumeration, recovery, invalidation, execution, and logging
    for the encoding phase.  Wraps the existing ``encode_all_chunks`` helper.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name: str = "encoding"

    def __init__(
        self,
        config:    "PipelineConfig",
        phases:    "dict[type[Phase], Phase] | None" = None,
        *,
        collector: "MetricsCollector",
    ) -> None:
        from pyqenc.phases.chunking import ChunkingPhase as _ChunkingPhase
        from pyqenc.phases.job import JobPhase as _JobPhase
        from pyqenc.phases.optimization import OptimizationPhase as _OptimizationPhase

        self._config:       "PipelineConfig"            = config
        self._collector:    "MetricsCollector"          = collector
        self._job:          "_JobPhase | None"          = cast("_JobPhase",          phases[_JobPhase])          if phases else None
        self._chunking:     "_ChunkingPhase | None"     = cast("_ChunkingPhase",     phases[_ChunkingPhase])     if phases else None
        self._optimization: "_OptimizationPhase | None" = cast("_OptimizationPhase", phases[_OptimizationPhase]) if phases else None
        self.params:        "EncodingParams | None"     = None
        self.result:        "EncodingPhaseResult | None" = None
        self.quality_labels: dict[str, str]             = {}
        """Maps strategy name → quality_label (e.g. ``'CRF'``, ``'CQ'``) for all
        strategies resolved during the last ``run()`` call.  Empty until ``run()``
        completes.  Used by downstream phases (e.g. ``MergePhase``) to label plots."""
        self.dependencies:  "list[Phase]"               = [d for d in [self._job, self._chunking, self._optimization] if d is not None]

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def scan(self) -> "EncodingPhaseResult":
        """Classify existing encoding artifacts without executing any work.

        Returns:
            ``EncodingPhaseResult`` with all artifacts classified.
        """
        if self.result is not None:
            return self.result

        dep_result = self._ensure_dependencies(execute=False)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        artifacts = self._recover(force_wipe=force_wipe, execute=False)
        outcome   = self._outcome_from_artifacts(artifacts, did_work=False)

        self.result = EncodingPhaseResult(
            outcome   = outcome,
            artifacts = artifacts,
            message   = _recovery_message(artifacts),
            encoded   = [a for a in artifacts if isinstance(a, EncodedArtifact)],
        )
        return self.result

    def run(self, dry_run: bool = False) -> "EncodingPhaseResult":
        """Recover, encode pending pairs, cache and return result.

        Sequence:
        1. Emit phase banner.
        2. Ensure dependencies have results.
        3. Run ``_recover()`` — handles ``force_wipe`` and crop mismatch.
        4. Log recovery result line.
        5. In dry-run mode: return ``DRY_RUN`` if any pairs are pending.
        6. Encode pending pairs via ``encode_all_chunks``.
        7. Log phase completion summary.

        Args:
            dry_run: When ``True``, report what would be done without encoding.

        Returns:
            ``EncodingPhaseResult`` with all artifacts ``COMPLETE`` on success.
        """
        emit_phase_banner("ENCODING", logger)

        logger.info("Scanning for existing artifacts...")

        dep_result = self._ensure_dependencies(execute=True)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        from pyqenc.metrics import TimeKey

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)
        crop       = getattr(job_result, "crop", None)

        # Key parameters — strategies come from OptimizationPhase after deps are resolved
        with self._collector.time(TimeKey.RECOVERY):
            artifacts = self._recover(force_wipe=force_wipe, execute=True)

        # Log key parameters now that dependencies are resolved
        opt_result = self._optimization.result if self._optimization else None  # type: ignore[union-attr]
        strategies = getattr(opt_result, "selected_strategies", []) if opt_result else []
        chunking_result = self._chunking.result if self._chunking else None  # type: ignore[union-attr]
        chunks = getattr(chunking_result, "chunks", []) if chunking_result else []
        logger.info("Chunks:      %d", len(chunks))
        logger.info("Strategies:  %s", ", ".join(s.name for s in strategies) if strategies else "none")
        if crop:
            logger.info("Crop:        %s", crop)
        logger.info("Targets:     %s", ", ".join(f"{t.metric}-{t.statistic}≥{t.value}" for t in self._config.quality_targets))

        complete_count = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
        pending_count  = sum(1 for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY))
        log_recovery_line(logger, complete_count, pending_count, unit="pair")
        # Dry-run path
        if dry_run:
            outcome = PhaseOutcome.REUSED if pending_count == 0 else PhaseOutcome.DRY_RUN
            self.result = EncodingPhaseResult(
                outcome   = outcome,
                artifacts = artifacts,
                message   = "dry-run",
                encoded   = [a for a in artifacts if isinstance(a, EncodedArtifact)],
            )
            return self.result

        # Nothing to do
        if pending_count == 0:
            self.result = EncodingPhaseResult(
                outcome   = PhaseOutcome.REUSED,
                artifacts = artifacts,
                message   = "all encoding pairs reused",
                encoded   = [a for a in artifacts if isinstance(a, EncodedArtifact)],
            )
            return self.result

        # Execute encoding
        result = self._execute_encoding(artifacts, crop)
        self.result = result
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dependencies(self, execute: bool) -> "EncodingPhaseResult | None":
        """Scan/run dependencies if they have no cached result; fail fast if incomplete."""
        if self._job is None:
            return _enc_failed("EncodingPhase requires JobPhase")

        if self._job.result is None:
            if execute:
                self._job.run()
            else:
                self._job.scan()

        if not self._job.result.is_complete:  # type: ignore[union-attr]
            err = "JobPhase did not complete successfully"
            logger.critical(err)
            return _enc_failed(err)

        if self._chunking is None:
            return _enc_failed("EncodingPhase requires ChunkingPhase")

        if self._chunking.result is None:
            if execute:
                self._chunking.run()
            else:
                self._chunking.scan()

        if not self._chunking.result.is_complete:  # type: ignore[union-attr]
            err = "ChunkingPhase did not complete successfully"
            logger.critical(err)
            return _enc_failed(err)

        if self._optimization is None:
            return _enc_failed("EncodingPhase requires OptimizationPhase")

        if self._optimization.result is None:
            if execute:
                self._optimization.run()
            else:
                self._optimization.scan()

        if not self._optimization.result.is_complete:  # type: ignore[union-attr]
            err = "OptimizationPhase did not complete successfully"
            logger.critical(err)
            return _enc_failed(err)

        return None

    def _recover(self, force_wipe: bool, execute: bool) -> list[EncodedArtifact]:
        """Classify ``(chunk, strategy)`` pairs and handle force-wipe / crop mismatch.

        Steps:
        1. If ``force_wipe`` and execute: delete ``encoding/``, ``encoded/``,
           and ``encoding.yaml``.
        2. Check crop mismatch against ``encoding.yaml``.
        3. Clean up leftover ``.tmp`` files (execute mode only).
        4. Call ``_recover_encoding_attempts`` to classify all pairs.
        5. Re-evaluate ``COMPLETE`` pairs against current quality targets.

        Args:
            force_wipe: When ``True``, wipe all encoding artifacts first.
            execute:    When ``True``, wipe and cleanup are performed.

        Returns:
            List of ``EncodedArtifact`` objects.
        """
        work_dir = self._config.work_dir
        enc_dir  = work_dir / ENCODING_WORKSPACE_DIR
        out_dir  = work_dir / ENCODED_OUTPUT_DIR
        yaml_path = work_dir / _ENCODING_YAML

        # Step 1: force-wipe
        if force_wipe and execute:
            for d in (enc_dir, out_dir):
                if d.exists():
                    _shutil.rmtree(d)
                    logger.debug("force_wipe: deleted %s", d)
            if yaml_path.exists():
                yaml_path.unlink()
                logger.debug("force_wipe: deleted %s", yaml_path)

        # Step 2: crop mismatch check
        if execute and not force_wipe:
            persisted_enc = EncodingParams.load(yaml_path)
            job_result    = self._job.result  # type: ignore[union-attr]
            crop          = getattr(job_result, "crop", None)
            self.params   = EncodingParams(crop=crop)

            if persisted_enc is not None:
                # Crop mismatch: requires --force to proceed
                crop_changed = persisted_enc.crop != crop
                if crop_changed:
                    if self._config.force:
                        logger.warning(
                            "Crop params changed since last encoding run "
                            "(persisted=%s, current=%s) — --force: deleting encoding artifacts",
                            persisted_enc.crop, crop,
                        )
                        for d in (enc_dir, out_dir):
                            if d.exists():
                                _shutil.rmtree(d)
                                logger.debug("Crop mismatch --force: deleted %s", d)
                        if yaml_path.exists():
                            yaml_path.unlink()
                    else:
                        err = (
                            "Crop params changed since last encoding run "
                            f"(persisted={persisted_enc.crop}, current={crop}). "
                            "Re-run with --force to delete stale encoding artifacts and continue."
                        )
                        logger.critical(err)
                        return [EncodedArtifact(
                            path     = work_dir / _ENCODING_YAML,
                            state    = ArtifactState.ABSENT,
                            chunk_id = "__crop_mismatch__",
                            strategy = "",
                        )]

        # Step 3: clean up .tmp files (execute mode only)
        if execute and enc_dir.exists():
            for tmp in enc_dir.rglob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Step 4: get chunks and strategies from dependencies
        chunking_result    = self._chunking.result  # type: ignore[union-attr]
        optimization_result = self._optimization.result  # type: ignore[union-attr]

        chunks: list[ChunkMetadata] = getattr(chunking_result, "chunks", [])
        strategies = getattr(optimization_result, "selected_strategies", [])

        if not chunks or not strategies:
            return []

        chunk_ids      = [c.chunk_id for c in chunks]
        strategy_names = [s.name for s in strategies]

        # Step 5: recover pairs
        phase_recovery = _recover_encoding_attempts(
            work_dir   = work_dir,
            chunk_ids  = chunk_ids,
            strategies = strategy_names,
        )

        # Convert to EncodedArtifact list
        artifacts: list[EncodedArtifact] = []
        for chunk_id in chunk_ids:
            for strategy_name in strategy_names:
                pair_rec = phase_recovery.pairs.get((chunk_id, strategy_name))
                if pair_rec is None or pair_rec.state == ArtifactState.ABSENT:
                    artifacts.append(EncodedArtifact(
                        path     = work_dir / ENCODED_OUTPUT_DIR / strategy_name / f"{chunk_id}.mkv",
                        state    = ArtifactState.ABSENT,
                        chunk_id = chunk_id,
                        strategy = strategy_name,
                    ))
                    continue

                artifacts.append(EncodedArtifact(
                    path     = pair_rec.winning_file or (
                        _enc_encoded_strategy_dir(work_dir, strategy_name) / f"{chunk_id}.mkv"
                    ),
                    state    = pair_rec.state,
                    chunk_id = chunk_id,
                    strategy = strategy_name,
                ))

        return artifacts

    def _execute_encoding(
        self,
        artifacts: list[EncodedArtifact],
        crop:      "CropParams | None",
    ) -> "EncodingPhaseResult":
        """Encode all pending ``(chunk, strategy)`` pairs.

        Args:
            artifacts: Artifact list from ``_recover()``.
            crop:      Crop parameters from ``JobPhase.result``.

        Returns:
            ``EncodingPhaseResult`` after encoding.
        """
        work_dir = self._config.work_dir

        # Resolve chunks and strategies from dependencies
        chunking_result     = self._chunking.result  # type: ignore[union-attr]
        optimization_result = self._optimization.result  # type: ignore[union-attr]

        chunks: list[ChunkMetadata] = getattr(chunking_result, "chunks", [])
        strategies = getattr(optimization_result, "selected_strategies", [])

        if not chunks:
            err = "No chunks available from ChunkingPhase"
            logger.critical(err)
            return _enc_failed(err)

        if not strategies:
            err = "No strategies available from OptimizationPhase"
            logger.critical(err)
            return _enc_failed(err)

        strategy_names = [s.name for s in strategies]

        # Cache quality labels for downstream phases (e.g. MergePhase CRF plot)
        self.quality_labels = {s.name: s.codec.quality_label for s in strategies}

        # Persist encoding.yaml with current crop params
        encoding_yaml = work_dir / _ENCODING_YAML
        if self.params is None:
            self.params = EncodingParams(crop=crop)
        self.params.save(encoding_yaml)
        logger.debug("Wrote encoding.yaml (crop=%s)", crop)

        # Reference dir is the chunks directory
        reference_dir = work_dir / CHUNKS_DIR

        # Run encoding via the existing encode_all_chunks function
        enc_result = encode_all_chunks(
            chunks           = chunks,
            reference_dir    = reference_dir,
            strategies       = strategy_names,
            quality_targets  = self._config.quality_targets,
            work_dir         = work_dir,
            collector        = self._collector,
            max_parallel     = self._config.max_parallel,
            force            = self._config.force,
            dry_run          = False,
            crop_params      = crop,
            encoding_yaml    = encoding_yaml,
            cleanup_level    = self._config.cleanup,
            visual_hash      = self._config.visual_hash,
            metrics_sampling = self._config.metrics_sampling,
        )

        if enc_result.outcome == PhaseOutcome.FAILED:
            err = enc_result.error or "Encoding failed"
            logger.critical(err)
            return _enc_failed(err)

        # Re-run recovery to get final artifact states
        chunk_ids = [c.chunk_id for c in chunks]
        final_recovery = _recover_encoding_attempts(
            work_dir   = work_dir,
            chunk_ids  = chunk_ids,
            strategies = strategy_names,
        )

        # Build final artifact list
        final_artifacts: list[EncodedArtifact] = []
        failed_pairs: list[str] = []

        for chunk_id in chunk_ids:
            for strategy_name in strategy_names:
                pair_rec = final_recovery.pairs.get((chunk_id, strategy_name))
                state    = pair_rec.state if pair_rec else ArtifactState.ABSENT

                encoded_strategy_dir = _enc_encoded_strategy_dir(work_dir, strategy_name)
                artifact_path = encoded_strategy_dir / f"{chunk_id}.mkv"

                crf: Decimal | None = None
                if state == ArtifactState.COMPLETE and pair_rec and pair_rec.winning_file:
                    artifact_path = pair_rec.winning_file
                    m = ENCODED_ATTEMPT_NAME_PATTERN.match(pair_rec.winning_file.name)
                    if m:
                        try:
                            crf = Decimal(str(m.group("quality")))
                        except (ValueError, TypeError):
                            pass

                if state != ArtifactState.COMPLETE:
                    failed_pairs.append(f"{chunk_id}/{strategy_name}")

                final_artifacts.append(EncodedArtifact(
                    path     = artifact_path,
                    state    = state,
                    chunk_id = chunk_id,
                    strategy = strategy_name,
                    crf      = crf,
                ))

        # Log phase summary
        complete_count = sum(1 for a in final_artifacts if a.state == ArtifactState.COMPLETE)
        total_count    = len(final_artifacts)
        logger.info(THICK_LINE)
        logger.info("ENCODING SUMMARY")
        logger.info(THICK_LINE)
        logger.info(
            "  Encoded: %d/%d pairs complete (%d newly encoded, %d reused)",
            complete_count, total_count,
            enc_result.encoded_count, enc_result.reused_count,
        )
        if failed_pairs:
            logger.error("  Failed pairs: %s", ", ".join(failed_pairs[:10]))
        logger.info(THICK_LINE)

        if failed_pairs:
            return EncodingPhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = final_artifacts,
                message   = f"{len(failed_pairs)} pair(s) failed",
                error     = f"Failed pairs: {', '.join(failed_pairs[:5])}",
                encoded   = final_artifacts,
            )

        outcome = PhaseOutcome.COMPLETED if enc_result.encoded_count > 0 else PhaseOutcome.REUSED
        return EncodingPhaseResult(
            outcome   = outcome,
            artifacts = final_artifacts,
            message   = f"{complete_count} pair(s) complete",
            encoded   = final_artifacts,
        )

    @staticmethod
    def _outcome_from_artifacts(
        artifacts: list[EncodedArtifact],
        did_work:  bool,
    ) -> PhaseOutcome:
        """Derive ``PhaseOutcome`` from artifact states."""
        if not artifacts:
            return PhaseOutcome.REUSED
        if any(a.state == ArtifactState.ABSENT and a.chunk_id == "__crop_mismatch__" for a in artifacts):
            return PhaseOutcome.FAILED
        if all(a.state == ArtifactState.COMPLETE for a in artifacts):
            return PhaseOutcome.COMPLETED if did_work else PhaseOutcome.REUSED
        return PhaseOutcome.DRY_RUN


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _enc_failed(error: str) -> "EncodingPhaseResult":
    """Return a ``FAILED`` ``EncodingPhaseResult`` with the given error."""
    return EncodingPhaseResult(
        outcome   = PhaseOutcome.FAILED,
        artifacts = [],
        message   = error,
        error     = error,
        encoded   = [],
    )


def _recovery_message(artifacts: list[EncodedArtifact]) -> str:
    """Build a human-readable recovery summary string."""
    complete = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
    pending  = sum(1 for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY))
    if pending == 0:
        return f"{complete} pair(s) complete — reusing"
    if complete == 0:
        return f"{pending} pair(s) pending — full run needed"
    return f"{complete} pair(s) complete, {pending} pending — resuming"
