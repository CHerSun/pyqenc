"""
Encoding phase for the quality-based encoding pipeline.

This module handles chunk encoding with iterative CRF adjustment to meet
quality targets, including parallel execution and artifact-based resumption.
"""
# CHerSun 2026

import asyncio
import logging
import shutil as _shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import dataclass as _dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from alive_progress import config_handler

from pyqenc.config import ConfigManager
from pyqenc.constants import (
    CHUNKS_DIR,
    ENCODED_ATTEMPT_GLOB_PATTERN,
    ENCODED_ATTEMPT_NAME_PATTERN,
    ENCODED_OUTPUT_DIR,
    ENCODING_WORKSPACE_DIR,
    FAILURE_SYMBOL_MINOR,
    PADDING_CRF,
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
    StrategyConfig,
    VideoMetadata,
)
from pyqenc.phase import Artifact, Phase, PhaseResult
from pyqenc.quality import CRFHistory, adjust_crf
from pyqenc.state import ArtifactState, EncodingResultSidecar, MetricsSidecar
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.ffmpeg_runner import run_ffmpeg
from pyqenc.utils.log_format import (
    emit_phase_banner,
    fmt_chunk,
    fmt_chunk_attempt_result,
    fmt_chunk_attempt_start,
    fmt_chunk_final,
    fmt_chunk_start,
    log_recovery_line,
)
from pyqenc.utils.visualization import QualityEvaluator
from pyqenc.utils.yaml_utils import write_yaml_atomic

if TYPE_CHECKING:
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
    attempt_path: Path,
    targets_met:  bool,
    crf:          float,
    metrics:      dict[str, float],
) -> None:
    """Atomically write a per-attempt metrics sidecar alongside an encoded attempt.

    Uses ``write_yaml_atomic`` so a crash during writing never leaves a partial
    sidecar.  Stores ALL measured metric values (not filtered to current targets)
    so the CRF history is reusable when quality targets change (Req 6a.1).

    Args:
        attempt_path: Path to the encoded attempt ``.mkv`` file.
        targets_met:  Whether quality targets were met (for human inspection only).
        crf:          CRF value used for this attempt.
        metrics:      ALL measured quality metrics dict (not filtered to targets).
    """
    sidecar = attempt_path.with_suffix(".yaml")
    data    = MetricsSidecar(crf=crf, targets_met=targets_met, metrics=metrics)
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
    crf:             float,
    metrics:         dict[str, float],
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
    """
    sidecar_path = output_dir / f"{chunk_id}.{resolution}.yaml"
    data = EncodingResultSidecar(
        winning_attempt=winning_attempt.name,
        crf=crf,
        metrics=metrics,
    )
    try:
        write_yaml_atomic(sidecar_path, data.to_yaml_dict())
        logger.debug(
            "Wrote encoding result sidecar: %s (crf=%.1f)", sidecar_path.name, crf
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
class _AttemptRecovery:
    """Recovery state for one encoded attempt file (one CRF value)."""

    path:    Path
    crf:     float
    state:   ArtifactState
    metrics: dict[str, float] | None = None


@dataclass
class _EncodingRecovery:
    """Recovery state for a ``(chunk_id, strategy)`` pair — the CRF search as a whole."""

    chunk_id: str
    strategy: str
    state:    ArtifactState
    history:  CRFHistory                  = field(default_factory=CRFHistory)
    attempts: list[_AttemptRecovery]      = field(default_factory=list)


@dataclass
class _PhaseRecovery:
    """Recovery result for an entire optimization or encoding phase."""

    pairs:   dict[tuple[str, str], _EncodingRecovery] = field(default_factory=dict)
    pending: list[tuple[str, str]]                    = field(default_factory=list)


def _enc_strategy_dir(work_dir: Path, strategy: str) -> Path:
    """Return the CRF search workspace directory for *strategy* under ``encoding/``."""
    safe = strategy.replace("+", "_").replace(":", "_")
    return work_dir / ENCODING_WORKSPACE_DIR / safe


def _enc_encoded_strategy_dir(work_dir: Path, strategy: str) -> Path:
    """Return the finalized output directory for *strategy* under ``encoded/``."""
    safe = strategy.replace("+", "_").replace(":", "_")
    return work_dir / ENCODED_OUTPUT_DIR / safe


def _enc_load_metrics_sidecar(attempt_path: Path) -> MetricsSidecar | None:
    """Load a per-attempt YAML metrics sidecar for *attempt_path*."""
    import yaml as _yaml
    sidecar_path = attempt_path.with_suffix(".yaml")
    if not sidecar_path.exists():
        return None
    try:
        with sidecar_path.open("r", encoding="utf-8") as fh:
            data = _yaml.safe_load(fh)
        return MetricsSidecar.from_yaml_dict(data or {})
    except Exception as exc:
        logger.debug("Could not load metrics sidecar %s: %s", sidecar_path, exc)
        return None


def _enc_load_encoding_result_sidecar(
    strategy_dir: Path,
    chunk_id:     str,
    resolution:   str,
) -> EncodingResultSidecar | None:
    """Load the encoding result sidecar for a ``(chunk_id, resolution)`` pair."""
    import yaml as _yaml
    sidecar_path = strategy_dir / f"{chunk_id}.{resolution}.yaml"
    if not sidecar_path.exists():
        return None
    try:
        with sidecar_path.open("r", encoding="utf-8") as fh:
            data = _yaml.safe_load(fh)
        return EncodingResultSidecar.from_yaml_dict(data or {})
    except Exception as exc:
        logger.debug("Could not load encoding result sidecar %s: %s", sidecar_path, exc)
        return None


def _enc_recover_pair(
    chunk_id:        str,
    strategy:        str,
    encoding_dir:    Path,
    encoded_dir:     Path,
    quality_targets: list[QualityTarget],
) -> _EncodingRecovery:
    """Classify a single ``(chunk_id, strategy)`` pair and reconstruct its history.

    A pair is ``COMPLETE`` only when a result sidecar exists *and* its stored
    metrics still satisfy all current ``quality_targets``.  If the sidecar
    exists but metrics no longer meet targets, the pair is downgraded to
    ``ARTIFACT_ONLY`` so the CRF search resumes from the existing history.
    """
    result_sidecar:      EncodingResultSidecar | None = None
    result_sidecar_path: Path | None                 = None

    if encoded_dir.exists():
        for candidate in encoded_dir.glob(f"{chunk_id}.*.yaml"):
            stem_parts = candidate.stem.split(".")
            if len(stem_parts) < 2:
                continue
            resolution_candidate = stem_parts[-1]
            if not (resolution_candidate.count("x") == 1 and all(
                part.isdigit() for part in resolution_candidate.split("x")
            )):
                continue
            loaded = _enc_load_encoding_result_sidecar(encoded_dir, chunk_id, resolution_candidate)
            if loaded is not None:
                result_sidecar      = loaded
                result_sidecar_path = candidate
                break

    attempt_recoveries: list[_AttemptRecovery] = []
    history = CRFHistory()

    if encoding_dir.exists():
        for attempt_file in sorted(encoding_dir.glob(ENCODED_ATTEMPT_GLOB_PATTERN)):
            m = ENCODED_ATTEMPT_NAME_PATTERN.match(attempt_file.name)
            if m is None or m.group("chunk_id") != chunk_id:
                continue
            if not attempt_file.exists():
                continue
            try:
                crf = float(m.group("crf"))
            except ValueError:
                continue
            sidecar = _enc_load_metrics_sidecar(attempt_file)
            if sidecar is not None:
                history.add_attempt(crf, sidecar.metrics)
                attempt_recoveries.append(_AttemptRecovery(
                    path=attempt_file, crf=crf,
                    state=ArtifactState.COMPLETE, metrics=sidecar.metrics,
                ))
            else:
                attempt_recoveries.append(_AttemptRecovery(
                    path=attempt_file, crf=crf,
                    state=ArtifactState.ARTIFACT_ONLY, metrics=None,
                ))

    if result_sidecar is not None and result_sidecar_path is not None:
        winning_path = encoded_dir / result_sidecar.winning_attempt
        if not winning_path.exists():
            logger.warning(
                "Encoding result sidecar %s references missing file %s — "
                "deleting sidecar and treating pair as ARTIFACT_ONLY",
                result_sidecar_path.name, result_sidecar.winning_attempt,
            )
            try:
                result_sidecar_path.unlink()
            except OSError as exc:
                logger.warning("Could not delete stale result sidecar: %s", exc)
            result_sidecar = None

    if result_sidecar is not None:
        # Re-evaluate stored metrics against current quality targets (targets may have changed)
        required_keys = {f"{t.metric}_{t.statistic}" for t in quality_targets}
        metrics       = result_sidecar.metrics or {}
        targets_met   = (
            required_keys.issubset(metrics.keys())
            and all(metrics.get(f"{t.metric}_{t.statistic}", 0.0) >= t.value for t in quality_targets)
        )
        if not targets_met:
            logger.debug(
                "Pair %s/%s: result sidecar metrics no longer meet quality targets — "
                "downgrading COMPLETE → ARTIFACT_ONLY",
                chunk_id, strategy,
            )
            result_sidecar = None
        return _EncodingRecovery(
            chunk_id=chunk_id, strategy=strategy,
            state=ArtifactState.COMPLETE, history=history, attempts=attempt_recoveries,
        )
    if attempt_recoveries:
        return _EncodingRecovery(
            chunk_id=chunk_id, strategy=strategy,
            state=ArtifactState.ARTIFACT_ONLY, history=history, attempts=attempt_recoveries,
        )
    return _EncodingRecovery(
        chunk_id=chunk_id, strategy=strategy, state=ArtifactState.ABSENT,
    )


def _recover_encoding_attempts(
    work_dir:        Path,
    chunk_ids:       list[str],
    strategies:      list[str],
    quality_targets: list[QualityTarget],
) -> _PhaseRecovery:
    """Classify all ``(chunk_id, strategy)`` pairs and reconstruct CRF histories.

    Replaces ``recover_attempts`` from ``recovery.py`` (Req 4.3).

    A pair is ``COMPLETE`` only when a result sidecar exists *and* its stored
    metrics still satisfy all current ``quality_targets``.  Pairs whose sidecar
    metrics no longer meet targets are downgraded to ``ARTIFACT_ONLY`` so the
    CRF search can resume from the existing history.

    Args:
        work_dir:        Pipeline working directory.
        chunk_ids:       Chunk identifiers to recover.
        strategies:      Strategy names to recover.
        quality_targets: Current quality targets for pass/fail re-evaluation.

    Returns:
        ``_PhaseRecovery`` with per-pair recovery state and pending list.
    """
    pairs:   dict[tuple[str, str], _EncodingRecovery] = {}
    pending: list[tuple[str, str]]                    = []

    complete_count = artifact_only_count = absent_count = 0

    for chunk_id in chunk_ids:
        for strategy in strategies:
            enc_dir     = _enc_strategy_dir(work_dir, strategy)
            encoded_dir = _enc_encoded_strategy_dir(work_dir, strategy)
            recovery    = _enc_recover_pair(chunk_id, strategy, enc_dir, encoded_dir, quality_targets)
            pairs[(chunk_id, strategy)] = recovery

            if recovery.state == ArtifactState.COMPLETE:
                complete_count += 1
            elif recovery.state == ArtifactState.ARTIFACT_ONLY:
                artifact_only_count += 1
                pending.append((chunk_id, strategy))
            else:
                absent_count += 1
                pending.append((chunk_id, strategy))

    logger.debug(
        "Attempts recovery: %d pair(s) total — %d COMPLETE, %d ARTIFACT_ONLY, %d ABSENT",
        len(pairs), complete_count, artifact_only_count, absent_count,
    )
    return _PhaseRecovery(pairs=pairs, pending=pending)


@dataclass
class ChunkEncodingResult:
    """Result of encoding a single chunk.

    Attributes:
        chunk_id:     Chunk identifier.
        strategy:     Strategy used.
        success:      Whether encoding succeeded.
        final_crf:    Final CRF value used.
        attempts:     Number of encoding attempts.
        encoded_file: Metadata for the final encoded attempt artifact.
        reused:       Whether existing encoding was reused.
        error:        Error message if failed.
    """

    chunk_id:     str
    strategy:     str
    success:      bool
    final_crf:    float          | None = None
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
        config_manager:    ConfigManager,
        quality_evaluator: QualityEvaluator,
        work_dir:          Path,
        crop_params:       CropParams | None = None,
        cleanup_level:     CleanupLevel      = CleanupLevel.NONE,
    ):
        """Initialize chunk encoder.

        Args:
            config_manager:    Configuration manager for strategy parsing
            quality_evaluator: Quality evaluator for metric calculation
            work_dir:          Working directory for artifacts
            crop_params:       Optional crop parameters to apply to every chunk attempt.
            cleanup_level:     Controls deletion of intermediate attempt files after
                               a pair converges (Req 12.3).
        """
        self.config_manager    = config_manager
        self.quality_evaluator = quality_evaluator
        self.work_dir          = work_dir
        self._crop_params      = crop_params
        self._cleanup_level    = cleanup_level

    def _get_output_dir(self, strategy: str) -> Path:
        """Get the CRF search workspace directory for *strategy*.

        Attempt files (intermediate) are written here during the CRF search.
        On convergence the winning attempt is hard-linked into ``_get_encoded_dir``.

        Args:
            strategy: Strategy name (display form, e.g. ``'slow+h265-aq'``).

        Returns:
            Path to ``<work_dir>/encoding/<safe_strategy>/``.
        """
        safe_strategy = strategy.replace("+", "_").replace(":", "_")
        return self.work_dir / ENCODING_WORKSPACE_DIR / safe_strategy

    def _get_encoded_dir(self, strategy: str) -> Path:
        """Get the finalized output directory for *strategy*.

        Hard-linked winning attempts, result sidecars, and quality graphs are
        written here.  The presence of a result sidecar marks a pair as
        ``COMPLETE``.

        Args:
            strategy: Strategy name (display form, e.g. ``'slow+h265-aq'``).

        Returns:
            Path to ``<work_dir>/encoded/<safe_strategy>/``.
        """
        safe_strategy = strategy.replace("+", "_").replace(":", "_")
        return self.work_dir / ENCODED_OUTPUT_DIR / safe_strategy

    def _get_attempt_path(
        self,
        chunk_id:   str,
        strategy:   str,
        resolution: str | None = None,
        crf:        float | None = None,
    ) -> Path:
        """Get the final path for a CRF-only encoded attempt.

        Naming pattern: ``<chunk_id>.<width>x<height>.crf<CRF>.mkv``

        Falls back to a simpler name when resolution or CRF are not yet known.

        Args:
            chunk_id:   Chunk identifier (e.g. ``'00꞉00꞉00․000-00꞉05꞉20․000'``).
            strategy:   Strategy name.
            resolution: Output resolution string (e.g. ``'1920x800'``).
            crf:        CRF value used for this attempt.

        Returns:
            Path to encoded file for this attempt.
        """
        output_dir = self._get_output_dir(strategy)
        if resolution and crf is not None:
            filename = f"{chunk_id}.{resolution}.crf{crf:{PADDING_CRF}}.mkv"
        else:
            filename = f"{chunk_id}.mkv"
        return output_dir / filename

    def _check_existing_encoding(
        self,
        chunk_id:   str,
        strategy:   str,
        resolution: str | None,
        crf:        float,
    ) -> AttemptMetadata | None:
        """Check if a complete encoded attempt already exists on disk.

        Scans the strategy output directory for a file matching
        ``ENCODED_ATTEMPT_NAME_PATTERN`` with the correct ``chunk_id``,
        ``resolution``, and ``crf``.  No progress-tracker lookup is performed.

        Args:
            chunk_id:   Chunk identifier.
            strategy:   Strategy name.
            resolution: Expected resolution string (e.g. ``'1920x800'``).
                        When ``None`` any resolution is accepted.
            crf:        CRF value to look for.

        Returns:
            ``AttemptMetadata`` if a matching file exists, ``None`` otherwise.
        """
        output_dir = self._get_output_dir(strategy)
        if not output_dir.exists():
            return None

        for candidate in output_dir.glob(ENCODED_ATTEMPT_GLOB_PATTERN):
            m = ENCODED_ATTEMPT_NAME_PATTERN.match(candidate.name)
            if m is None:
                continue
            if m.group("chunk_id") != chunk_id:
                continue
            try:
                file_crf = float(m.group("crf"))
            except ValueError:
                continue
            if abs(file_crf - crf) > 0.05:
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
                strategy=strategy,
                crf=file_crf,
                resolution=file_resolution,
                file_size_bytes=size,
            )
        return None

    def _load_history_from_sidecars(
        self,
        chunk_id:        str,
        strategy:        str,
        quality_targets: list[QualityTarget],
    ) -> tuple[CRFHistory, float | None]:
        """Pre-populate a ``CRFHistory`` from all existing per-attempt sidecar files.

        Scans every ``*.yaml`` in the strategy output directory that belongs to
        ``chunk_id`` and adds each attempt to the history.  Also returns the
        highest-passing CRF as the recommended starting point for the next
        search iteration.

        Per-attempt sidecars store ALL measured metrics; pass/fail is always
        re-evaluated from ``metrics`` against the current quality targets
        (Req 6a.2, 6a.3).

        Falls back to legacy ``.metrics.json`` sidecars when no YAML sidecar
        exists for an attempt (Req 8.2).

        Args:
            chunk_id:        Chunk identifier to filter sidecars.
            strategy:        Strategy name.
            quality_targets: Current quality targets for pass/fail re-evaluation.

        Returns:
            Tuple of ``(history, seed_crf)`` where ``seed_crf`` is the highest
            CRF that met targets (best efficiency found so far), or ``None`` if
            no passing attempt exists yet.
        """
        import yaml as _yaml

        output_dir = self._get_output_dir(strategy)
        history    = CRFHistory()
        seed_crf:  float | None = None

        if not output_dir.exists():
            return history, seed_crf

        required_keys = {f"{t.metric}_{t.statistic}" for t in quality_targets}

        for candidate in output_dir.glob(ENCODED_ATTEMPT_GLOB_PATTERN):
            m = ENCODED_ATTEMPT_NAME_PATTERN.match(candidate.name)
            if m is None or m.group("chunk_id") != chunk_id:
                continue

            # Try YAML sidecar first, then legacy JSON
            sidecar_data: dict | None = None
            yaml_sidecar = candidate.with_suffix(".yaml")
            if yaml_sidecar.exists():
                try:
                    with yaml_sidecar.open("r", encoding="utf-8") as fh:
                        sidecar_data = _yaml.safe_load(fh)
                except Exception:
                    pass

            if sidecar_data is None:
                import json as _json
                json_sidecar = candidate.with_suffix(".metrics.json")
                if json_sidecar.exists():
                    try:
                        with json_sidecar.open("r", encoding="utf-8") as fh:
                            sidecar_data = _json.load(fh)
                    except Exception:
                        pass

            if sidecar_data is None:
                # ARTIFACT_ONLY — attempt file exists but no sidecar; skip for history
                continue

            try:
                crf     = float(sidecar_data["crf"])
                metrics = {k: float(v) for k, v in sidecar_data.get("metrics", {}).items()}
                history.add_attempt(crf, metrics)

                # Re-evaluate pass/fail from metrics against current targets (Req 6a.2)
                if required_keys and required_keys.issubset(metrics.keys()):
                    targets_met = all(
                        metrics.get(f"{t.metric}_{t.statistic}", 0.0) >= t.value
                        for t in quality_targets
                    )
                    if targets_met:
                        if seed_crf is None or crf > seed_crf:
                            seed_crf = crf
            except Exception:
                continue

        return history, seed_crf

    def _finalize_winning_attempt(
        self,
        strategy:        str,
        chunk_id:        str,
        resolution:      str,
        winning_attempt: Path,
        crf:             float,
        metrics:         dict[str, float],
    ) -> None:
        """Hard-link the winning attempt into ``encoded/`` and write the result sidecar.

        On CRF search convergence:
        1. Hard-link the winning ``.mkv`` from ``encoding/<strategy>/`` into
           ``encoded/<strategy>/`` (same filename).
        2. Hard-link the winning ``.png`` quality graph (if present) alongside it.
        3. Write the encoding result sidecar ``<chunk_id>.<res>.yaml`` into
           ``encoded/<strategy>/`` — its presence marks the pair as ``COMPLETE``.

        Args:
            strategy:        Strategy name (display form).
            chunk_id:        Chunk identifier.
            resolution:      Output resolution string (e.g. ``'1920x800'``).
            winning_attempt: Path to the winning attempt ``.mkv`` in ``encoding/``.
            crf:             Winning CRF value.
            metrics:         All measured metric values for the winning attempt.
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
            output_dir=encoded_dir,
            chunk_id=chunk_id,
            resolution=resolution,
            winning_attempt=dst_mkv,
            crf=crf,
            metrics=metrics,
        )

        # 4. Intermediate cleanup: delete all attempt files for this pair from encoding/
        #    (Req 6.6, 12.3) — only after the hard-link and sidecar are safely written.
        if self._cleanup_level >= CleanupLevel.INTERMEDIATE:
            encoding_dir = self._get_output_dir(strategy)
            if encoding_dir.exists():
                for attempt_file in list(encoding_dir.glob(ENCODED_ATTEMPT_GLOB_PATTERN)):
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
        chunk:           ChunkMetadata,
        strategy_config: StrategyConfig,
        crf:             float,
        output_file:     Path,
    ) -> bool:
        """Encode chunk with FFmpeg using the runner's ``.tmp``-then-rename protocol.

        The runner substitutes ``output_file`` with a ``<stem>.tmp`` sibling,
        runs ffmpeg, and renames to the final name on success.

        Args:
            chunk:           Chunk information.
            strategy_config: Strategy configuration.
            crf:             CRF value to use.
            output_file:     Intended final output path.

        Returns:
            ``True`` if encoding succeeded, ``False`` otherwise.
        """
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Build FFmpeg command
        ffmpeg_args = strategy_config.to_ffmpeg_args(crf)

        # Inject crop filter when crop params are set and non-empty
        if self._crop_params and not self._crop_params.is_empty():
            ffmpeg_args = ["-vf", self._crop_params.to_ffmpeg_filter(), *ffmpeg_args]

        cmd: list[str | Path] = [
            "ffmpeg",
            "-y",
            "-i", chunk.path,
            *ffmpeg_args,
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
        strategy:         str,
        quality_targets:  list[QualityTarget],
        initial_crf:      float = 20.0,
        force:            bool  = False,
        max_attempts:     int   = 10,
        initial_history:  "CRFHistory | None" = None,
    ) -> ChunkEncodingResult:
        """Encode single chunk, adjusting CRF until quality targets met.

        Args:
            chunk:            Chunk information.
            reference:        Reference chunk for quality comparison.
            strategy:         Strategy string (e.g., ``'slow+h265-aq'``).
            quality_targets:  Quality targets to meet.
            initial_crf:      Initial CRF value (if no history available).
            force:            If ``False``, reuse existing encoding that meets targets.
            max_attempts:     Unused; kept for API compatibility.
            initial_history:  Pre-populated ``CRFHistory`` from recovery; when provided
                              the sidecar scan is skipped and this history is used directly.

        Returns:
            ChunkEncodingResult with encoding outcome.
        """
        logger.debug(fmt_chunk_start(strategy, chunk.chunk_id))

        # Parse strategy up-front so we know the codec CRF range
        try:
            strategy_configs = self.config_manager.parse_strategy(strategy)
            if not strategy_configs:
                raise ValueError(f"Strategy '{strategy}' resolved to no configurations")
            strategy_config = strategy_configs[0]
        except ValueError as e:
            logger.error("Invalid strategy '%s': %s", strategy, e)
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=False,
                error=str(e),
            )

        crf_min, crf_max = strategy_config.codec.crf_range

        # Pre-populate CRF history — use injected history from recovery when available,
        # otherwise scan sidecar files on disk.  This gives adjust_crf complete bounds
        # from the very first call so the search resumes correctly.
        if initial_history is not None:
            history  = initial_history
            # Derive seed_crf from the injected history (highest passing CRF)
            required_keys = {f"{t.metric}_{t.statistic}" for t in quality_targets}
            seed_crf: float | None = None
            for attempt_crf, attempt_metrics in history.attempts:
                if required_keys and required_keys.issubset(attempt_metrics.keys()):
                    if all(attempt_metrics.get(f"{t.metric}_{t.statistic}", 0.0) >= t.value
                           for t in quality_targets):
                        if seed_crf is None or attempt_crf > seed_crf:
                            seed_crf = attempt_crf
        else:
            history, seed_crf = self._load_history_from_sidecars(chunk.chunk_id, strategy, quality_targets)

        if seed_crf is not None:
            current_crf = seed_crf
            logger.info(
                fmt_chunk(strategy, chunk.chunk_id,
                f"Restored {len(history.attempts)} attempt(s) from sidecars; resuming from best-passing CRF {current_crf:{PADDING_CRF}}")
            )
        else:
            current_crf = initial_crf
            logger.debug(f"No prior sidecars found; starting from CRF {current_crf:{PADDING_CRF}}")

        attempt_number    = 0
        final_attempt:    AttemptMetadata | None = None
        best_crf:         float | None           = None
        best_metrics:     dict[str, float]       = {}

        while True:
            attempt_number += 1

            if attempt_number == THRESHOLD_ATTEMPTS_WARNING:
                logger.warning(
                    fmt_chunk(strategy, chunk.chunk_id,
                              f"reached {THRESHOLD_ATTEMPTS_WARNING} attempts without meeting targets — "
                              "continuing search")
                )

            logger.debug(fmt_chunk_attempt_start(strategy, chunk.chunk_id, attempt_number, current_crf))

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

            # Check for existing encoding at this CRF (filesystem scan, no tracker)
            if not force:
                existing = self._check_existing_encoding(
                    chunk.chunk_id, strategy, check_resolution, current_crf
                )
                if existing is not None:
                    sidecar = _read_metrics_sidecar(existing.path)
                    # Validate sidecar contains all required metric keys (Req 10.4)
                    required_keys = {f"{t.metric}_{t.statistic}" for t in quality_targets}
                    sidecar_valid = (
                        sidecar is not None
                        and required_keys.issubset(sidecar.get("metrics", {}).keys())
                    )
                    if sidecar_valid and sidecar is not None:
                        # Re-evaluate pass/fail from metrics against current targets (Req 6a.2)
                        all_sidecar_metrics: dict[str, float] = {
                            k: float(v) for k, v in sidecar.get("metrics", {}).items()
                        }
                        targets_met: bool = all(
                            all_sidecar_metrics.get(f"{t.metric}_{t.statistic}", 0.0) >= t.value
                            for t in quality_targets
                        )
                        targets_set_reused = {f"{t.metric}_{t.statistic}" for t in quality_targets}
                        metrics_dict: dict[str, float] = {
                            k: v for k, v in all_sidecar_metrics.items() if k in targets_set_reused
                        }
                        metric_summary = ", ".join(f"{k}={v:.1f}" for k, v in metrics_dict.items())
                        pass_fail = (
                            f"{SUCCESS_SYMBOL_MINOR} pass"
                            if targets_met
                            else f"{FAILURE_SYMBOL_MINOR} miss"
                        )
                        best_string = ""
                        if targets_met:
                            if final_attempt is None or existing.crf > (best_crf or 0):
                                best_crf      = existing.crf
                                final_attempt = existing
                                best_metrics  = metrics_dict
                                best_string   = " NEW BEST"
                        logger.info(
                            fmt_chunk_attempt_result(
                                strategy, chunk.chunk_id, attempt_number,
                                f"{pass_fail} with CRF {existing.crf:{PADDING_CRF}} ({metric_summary}){best_string} [reused]",
                            )
                        )
                        history.add_attempt(existing.crf, metrics_dict)
                        next_crf = adjust_crf(
                            existing.crf, metrics_dict, quality_targets, history,
                            crf_min=crf_min, crf_max=crf_max,
                        )
                        if next_crf is None:
                            if final_attempt is not None:
                                logger.info(fmt_chunk_final(strategy, chunk.chunk_id, best_crf, attempt_number))
                                # Hard-link winning attempt into encoded/ and write result sidecar
                                self._finalize_winning_attempt(
                                    strategy=strategy,
                                    chunk_id=chunk.chunk_id,
                                    resolution=existing.resolution,
                                    winning_attempt=final_attempt.path,
                                    crf=best_crf or existing.crf,
                                    metrics=best_metrics,
                                )
                            else:
                                logger.warning(
                                    "CRF search space exhausted for chunk %s strategy %s after %d attempts",
                                    chunk.chunk_id, strategy, attempt_number,
                                )
                            break
                        current_crf = next_crf
                        continue
                    else:
                        # File exists but sidecar is missing or incomplete — re-evaluate metrics only
                        logger.info(
                            fmt_chunk(strategy, chunk.chunk_id,
                            f"existing attempt (crf={existing.crf:.2f}) — re-evaluating metrics"),
                        )
                        output_file = existing.path
                        # Skip encoding, jump straight to quality evaluation below
                        encode_success = True
                        # Build a placeholder so the code below can use output_file
                        goto_eval = True
                else:
                    goto_eval = False
            else:
                goto_eval = False

            if not goto_eval:
                # Build final output path (resolution known from chunk metadata)
                output_file = self._get_attempt_path(
                    chunk.chunk_id, strategy, resolution=resolution, crf=current_crf
                )
                encode_success = self._encode_with_ffmpeg(
                    chunk, strategy_config, current_crf, output_file
                )

                if not encode_success:
                    error_msg = f"Encoding failed for chunk {chunk.chunk_id}"
                    logger.error(error_msg)
                    return ChunkEncodingResult(
                        chunk_id=chunk.chunk_id,
                        strategy=strategy,
                        success=False,
                        attempts=attempt_number,
                        error=error_msg,
                    )

                # Update resolution from actual output (crop may change dimensions)
                actual_resolution = _probe_resolution(output_file)
                if actual_resolution and actual_resolution != resolution:
                    # Rename to correct resolution-based path
                    correct_path = self._get_attempt_path(
                        chunk.chunk_id, strategy, resolution=actual_resolution, crf=current_crf
                    )
                    try:
                        output_file.replace(correct_path)
                    except OSError:
                        output_file.rename(correct_path)
                    output_file = correct_path
                    resolution  = actual_resolution

            # Evaluate quality — raw metric logs/stats go into a per-attempt subfolder;
            # the plot and YAML sidecar stay next to the .mkv (Req 8.1)
            attempt_metrics_dir = output_file.parent / output_file.stem
            attempt_plot_path   = output_file.parent / f"{output_file.stem}.png"
            evaluation  = self.quality_evaluator.evaluate_chunk(
                encoded=output_file,
                reference=reference.path,
                ref_crop=self._crop_params or CropParams(),
                targets=quality_targets,
                output_dir=attempt_metrics_dir,
                plot_path=attempt_plot_path,
                chunk_start_seconds=chunk.start_timestamp,
            )

            # Collect ALL measured metrics (not filtered to current targets) for the sidecar
            # so the CRF history is reusable when quality targets change (Req 6a.1)
            all_metrics: dict[str, float] = {
                f"{metric.value}_{stat}": float(value)
                for metric, stats in evaluation.metrics.items()
                for stat, value in stats.items()
            }

            # Targeted metrics subset (for history and convergence decisions)
            targets_set  = {f"{t.metric}_{t.statistic}" for t in quality_targets}
            metrics_dict = {k: v for k, v in all_metrics.items() if k in targets_set}

            # Write per-attempt metrics sidecar atomically (Req 6a.1)
            _write_metrics_sidecar(output_file, evaluation.targets_met, current_crf, all_metrics)

            # Build AttemptMetadata for this attempt
            attempt_meta = AttemptMetadata(
                path=output_file,
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                crf=current_crf,
                resolution=resolution or "",
                file_size_bytes=output_file.stat().st_size,
            )

            history.add_attempt(current_crf, metrics_dict)

            best_string = ""
            if evaluation.targets_met:
                if final_attempt is None or current_crf > (best_crf or 0):
                    best_crf      = current_crf
                    final_attempt = attempt_meta
                    best_metrics  = all_metrics
                    best_string   = " NEW BEST"

            metric_summary = ", ".join(f"{k}={v:.1f}" for k, v in metrics_dict.items())
            pass_fail = (
                f"{SUCCESS_SYMBOL_MINOR} pass"
                if evaluation.targets_met
                else f"{FAILURE_SYMBOL_MINOR} miss"
            )
            logger.info(
                fmt_chunk_attempt_result(
                    strategy, chunk.chunk_id, attempt_number,
                    f"{pass_fail} with CRF {current_crf:{PADDING_CRF}} ({metric_summary}){best_string}",
                )
            )

            next_crf = adjust_crf(
                current_crf, metrics_dict, quality_targets, history,
                crf_min=crf_min, crf_max=crf_max,
            )

            if next_crf is None:
                if final_attempt is not None:
                    logger.info(fmt_chunk_final(strategy, chunk.chunk_id, best_crf, attempt_number))
                    # Hard-link winning attempt into encoded/ and write result sidecar
                    self._finalize_winning_attempt(
                        strategy=strategy,
                        chunk_id=chunk.chunk_id,
                        resolution=resolution or "",
                        winning_attempt=final_attempt.path,
                        crf=best_crf or current_crf,
                        metrics=best_metrics,
                    )
                else:
                    logger.warning(
                        "CRF search space exhausted for chunk %s strategy %s after %d attempts",
                        chunk.chunk_id, strategy, attempt_number,
                    )
                break

            logger.debug("Adjusting CRF from %.2f to %.2f", current_crf, next_crf)
            current_crf = next_crf

        if final_attempt is not None:
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=True,
                final_crf=best_crf,
                attempts=attempt_number,
                encoded_file=final_attempt,
                reused=False,
            )
        else:
            error_msg = f"Failed to meet quality targets after {attempt_number} attempts"
            logger.error("Chunk %s: %s", chunk.chunk_id, error_msg)
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=False,
                attempts=attempt_number,
                error=error_msg,
            )



class ChunkQueue:
    """Manages queue of chunks for parallel encoding.

    Prioritizes completing started chunks before starting new ones.
    """

    def __init__(self, chunks: list[ChunkMetadata], strategies: list[str]):
        """Initialize chunk queue.

        Args:
            chunks: List of chunks to encode
            strategies: List of strategies to apply
        """
        self.chunks = chunks
        self.strategies = strategies
        self._pending: list[tuple[ChunkMetadata, str]] = []
        self._in_progress: set[tuple[str, str]] = set()
        self._completed: set[tuple[str, str]] = set()

        # Build initial queue (all chunk+strategy combinations)
        for chunk in chunks:
            for strategy in strategies:
                self._pending.append((chunk, strategy))

    def get_next(self) -> tuple[ChunkMetadata, str] | None:
        """Get next chunk+strategy to encode.

        Prioritizes completing started chunks before starting new ones.

        Returns:
            Tuple of (chunk, strategy) or None if queue empty
        """
        if not self._pending:
            return None

        # Check if any in-progress chunks have other strategies pending
        for chunk, strategy in self._pending:
            if any((chunk.chunk_id, s) in self._in_progress for s in self.strategies):
                # This chunk has work in progress, prioritize it
                self._pending.remove((chunk, strategy))
                self._in_progress.add((chunk.chunk_id, strategy))
                return (chunk, strategy)

        # No in-progress chunks, take first pending
        chunk, strategy = self._pending.pop(0)
        self._in_progress.add((chunk.chunk_id, strategy))
        return (chunk, strategy)

    def mark_complete(self, chunk_id: str, strategy: str) -> None:
        """Mark chunk+strategy as complete.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
        """
        self._in_progress.discard((chunk_id, strategy))
        self._completed.add((chunk_id, strategy))

    def mark_failed(self, chunk_id: str, strategy: str) -> None:
        """Mark chunk+strategy as failed.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
        """
        self._in_progress.discard((chunk_id, strategy))

    def is_empty(self) -> bool:
        """Check if queue is empty.

        Returns:
            True if no more work to do
        """
        return len(self._pending) == 0 and len(self._in_progress) == 0

    def get_progress(self) -> tuple[int, int]:
        """Get current progress.

        Returns:
            Tuple of (completed, total)
        """
        total = len(self.chunks) * len(self.strategies)
        completed = len(self._completed)
        return (completed, total)


async def _encode_chunk_async(
    encoder:          ChunkEncoder,
    chunk:            ChunkMetadata,
    reference:        VideoMetadata,
    strategy:         str,
    quality_targets:  list[QualityTarget],
    initial_crf:      float,
    force:            bool,
    initial_history:  "CRFHistory | None" = None,
) -> ChunkEncodingResult:
    """Async wrapper for chunk encoding.

    Args:
        encoder:          ChunkEncoder instance.
        chunk:            Chunk to encode.
        reference:        Reference chunk.
        strategy:         Strategy to use.
        quality_targets:  Quality targets.
        initial_crf:      Initial CRF value.
        force:            Whether to force re-encoding.
        initial_history:  Pre-populated ``CRFHistory`` from recovery; when provided
                          the sidecar scan is skipped and this history is used directly.

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
        10,             # max_attempts (unused, kept for API compat)
        initial_history,
    )


async def _encode_chunks_parallel(
    encoder:        ChunkEncoder,
    chunks:         list[ChunkMetadata],
    reference_dir:  Path,
    strategies:     list[str],
    quality_targets: list[QualityTarget],
    max_parallel:   int,
    force:          bool,
    initial_crf:    float = 20.0,
    phase_recovery: "_PhaseRecovery | None" = None,
    bar:            Callable[[int | float, AdvanceState], None] | None = None,
) -> EncodingResult:
    """Encode chunks in parallel with semaphore control.

    Args:
        encoder:        ChunkEncoder instance
        chunks:         List of chunks to encode
        reference_dir:  Directory containing reference chunks
        strategies:     List of strategies to use
        quality_targets: Quality targets to meet
        max_parallel:   Maximum concurrent encodings
        force:          Whether to force re-encoding
        phase_recovery: Optional recovery state from ``recover_attempts``; when
                        provided, ``COMPLETE`` pairs are skipped and ``ARTIFACT_ONLY``
                        pairs resume from their recovered ``CRFHistory``.
        bar:            Optional advance callable from ``ProgressBar``; called with
                        chunk duration in seconds and an ``AdvanceState`` on each
                        chunk completion.

    Returns:
        EncodingResult with all encoding outcomes
    """
    result    = EncodingResult()
    semaphore = asyncio.Semaphore(max_parallel)
    counter_failed = 0

    # Pre-populate result with COMPLETE pairs from recovery (skip them in the queue)
    complete_pairs: set[tuple[str, str]] = set()
    if phase_recovery is not None:
        for chunk in chunks:
            for strategy in strategies:
                pair_recovery = phase_recovery.pairs.get((chunk.chunk_id, strategy))
                if pair_recovery is not None and pair_recovery.state == ArtifactState.COMPLETE:
                    logger.debug(
                        "Skipping COMPLETE pair %s/%s (encoding result sidecar valid)",
                        chunk.chunk_id, strategy,
                    )
                    if chunk.chunk_id not in result.encoded_chunks:
                        result.encoded_chunks[chunk.chunk_id] = {}
                    # Winning file lives in encoded/<strategy>/ — find it there
                    encoded_dir  = _enc_encoded_strategy_dir(encoder.work_dir, strategy)
                    winning_file: Path | None = None
                    if encoded_dir.exists():
                        for candidate in encoded_dir.glob(ENCODED_ATTEMPT_GLOB_PATTERN):
                            m = ENCODED_ATTEMPT_NAME_PATTERN.match(candidate.name)
                            if m and m.group("chunk_id") == chunk.chunk_id and candidate.exists():
                                winning_file = candidate
                                break
                    result.encoded_chunks[chunk.chunk_id][strategy] = winning_file
                    result.reused_count += 1
                    complete_pairs.add((chunk.chunk_id, strategy))

    queue = ChunkQueue(chunks, strategies)
    # Remove already-complete pairs from the queue
    queue._pending = [
        (c, s) for (c, s) in queue._pending
        if (c.chunk_id, s) not in complete_pairs
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
                    if bar is not None:
                        bar(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)
                    continue

                # Inject recovered CRFHistory for ARTIFACT_ONLY pairs (Req 6.3)
                recovered_history: CRFHistory | None = None
                if phase_recovery is not None:
                    pair_rec = phase_recovery.pairs.get((chunk.chunk_id, strategy))
                    if pair_rec is not None and pair_rec.state == ArtifactState.ARTIFACT_ONLY:
                        recovered_history = pair_rec.history
                        logger.info(
                            "Resuming ARTIFACT_ONLY pair %s/%s from %d recovered attempt(s)",
                            chunk.chunk_id, strategy, len(pair_rec.history.attempts),
                        )

                # Encode chunk
                chunk_result = await _encode_chunk_async(
                    encoder,
                    chunk,
                    VideoMetadata(path=reference),
                    strategy,
                    quality_targets,
                    initial_crf,
                    force,
                    initial_history=recovered_history,
                )

                # Update result
                if chunk_result.success:
                    if chunk.chunk_id not in result.encoded_chunks:
                        result.encoded_chunks[chunk.chunk_id] = {}
                    encoded_path = chunk_result.encoded_file.path if chunk_result.encoded_file else None
                    result.encoded_chunks[chunk.chunk_id][strategy] = encoded_path

                    if chunk_result.reused:
                        result.reused_count += 1
                        if bar is not None:
                            bar(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.SKIPPED)
                    else:
                        result.encoded_count += 1
                        if bar is not None:
                            bar(chunk.end_timestamp - chunk.start_timestamp)

                    queue.mark_complete(chunk.chunk_id, strategy)
                else:
                    queue.mark_failed(chunk.chunk_id, strategy)
                    result.failed_chunks.append(chunk.chunk_id)
                    counter_failed += 1
                    if bar is not None:
                        bar(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)


    # Start worker tasks
    workers = [asyncio.create_task(encode_worker()) for _ in range(max_parallel)]

    # Wait for all workers to complete
    await asyncio.gather(*workers)

    return result


def encode_all_chunks(
    chunks:           list[ChunkMetadata],
    reference_dir:    Path,
    strategies:       list[str],
    quality_targets:  list[QualityTarget],
    work_dir:         Path,
    config_manager:   ConfigManager,
    max_parallel:     int          = 2,
    force:            bool         = False,
    dry_run:          bool         = False,
    crop_params:      CropParams | None = None,
    encoding_yaml:    Path | None  = None,
    cleanup_level:    CleanupLevel = CleanupLevel.NONE,
) -> EncodingResult:
    """Encode all chunks with quality-targeted CRF adjustment.

    This is the main entry point for the encoding phase. It handles:
    - Pre-validating crop params against ``encoding.yaml`` (Req 3.5)
    - Writing ``encoding.yaml`` with current crop params (Req 2.4)
    - Calling ``recover_attempts`` to classify all ``(chunk, strategy)`` pairs
    - Skipping ``COMPLETE`` pairs and resuming ``ARTIFACT_ONLY`` pairs
    - Parallel encoding of chunks that need work

    Args:
        chunks:           List of chunks to encode
        reference_dir:    Directory containing reference chunks (already cropped)
        strategies:       List of encoding strategies to use
        quality_targets:  Quality targets to meet
        work_dir:         Working directory for artifacts
        config_manager:   Configuration manager
        max_parallel:     Maximum concurrent encoding processes
        force:            If False, reuse existing encodings that meet current targets
        dry_run:          If True, only report what would be done without encoding
        crop_params:      Crop parameters to apply uniformly to every chunk attempt.
                          When ``None``, no cropping is applied.
        encoding_yaml:    Optional path to ``encoding.yaml`` for crop pre-validation
                          and persistence.  When provided, crop pre-validation
                          and ``encoding.yaml`` persistence are enabled.
        cleanup_level:    Controls deletion of intermediate attempt files after each
                          pair converges (Req 6.6, 12.3).

    Returns:
        EncodingResult with paths to encoded chunks and statistics
    """
    from pyqenc.state import EncodingParams

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
    chunk_ids = [c.chunk_id for c in chunks]
    phase_recovery = _recover_encoding_attempts(work_dir, chunk_ids, strategies, quality_targets)

    if dry_run:
        pending_count = len(phase_recovery.pending)
        complete_count = len(chunk_ids) * len(strategies) - pending_count
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
        config_manager=config_manager,
        quality_evaluator=QualityEvaluator(work_dir),
        work_dir=work_dir,
        crop_params=crop_params,
        cleanup_level=cleanup_level,
    )

    # Run parallel encoding — COMPLETE pairs are skipped inside _encode_chunks_parallel
    logger.debug("Starting parallel encoding with %d workers", max_parallel)
    total_seconds = sum(c.end_timestamp - c.start_timestamp for c in chunks) * len(strategies)
    with ProgressBar(total_seconds, title="Encoding") as advance:
        # Update the bar for completed chunks
        chunks_by_id = {c.chunk_id: c for c in chunks}
        for r in phase_recovery.pairs.values():
            if r.state == ArtifactState.COMPLETE:
                advance((chunks_by_id[r.chunk_id].end_timestamp - chunks_by_id[r.chunk_id].start_timestamp), AdvanceState.SKIPPED)

        # Run parallel encoding
        result = asyncio.run(
            _encode_chunks_parallel(
                encoder=encoder,
                chunks=chunks,
                reference_dir=reference_dir,
                strategies=strategies,
                quality_targets=quality_targets,
                max_parallel=max_parallel,
                force=force,
                initial_crf=20.0,
                phase_recovery=phase_recovery,
                bar=advance,
            )
        )

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
    crf:      float | None = None


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
        config: "PipelineConfig",
        phases: "dict[type[Phase], Phase] | None" = None,
    ) -> None:
        from pyqenc.phases.chunking import ChunkingPhase as _ChunkingPhase
        from pyqenc.phases.job import JobPhase as _JobPhase
        from pyqenc.phases.optimization import OptimizationPhase as _OptimizationPhase

        self._config:       "PipelineConfig"            = config
        self._job:          "_JobPhase | None"          = cast("_JobPhase",          phases[_JobPhase])          if phases else None
        self._chunking:     "_ChunkingPhase | None"     = cast("_ChunkingPhase",     phases[_ChunkingPhase])     if phases else None
        self._optimization: "_OptimizationPhase | None" = cast("_OptimizationPhase", phases[_OptimizationPhase]) if phases else None
        self.result:        "EncodingPhaseResult | None" = None
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

        dep_result = self._ensure_dependencies(execute=True)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)
        crop       = getattr(job_result, "crop", None)

        # Key parameters — strategies come from OptimizationPhase after deps are resolved
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
        from pyqenc.state import EncodingParams

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

            if persisted_enc is not None and persisted_enc.crop != crop:
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
                    # Return a single ABSENT artifact to signal failure upstream
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
            work_dir        = work_dir,
            chunk_ids       = chunk_ids,
            strategies      = strategy_names,
            quality_targets = self._config.quality_targets,
        )

        # Convert to EncodedArtifact list
        artifacts: list[EncodedArtifact] = []
        for chunk_id in chunk_ids:
            for strategy_name in strategy_names:
                pair_rec = phase_recovery.pairs.get((chunk_id, strategy_name))
                if pair_rec is None:
                    artifacts.append(EncodedArtifact(
                        path     = work_dir / ENCODED_OUTPUT_DIR / strategy_name / f"{chunk_id}.mkv",
                        state    = ArtifactState.ABSENT,
                        chunk_id = chunk_id,
                        strategy = strategy_name,
                    ))
                    continue

                # Find the artifact path (winning file in encoded/ if COMPLETE)
                encoded_strategy_dir = _enc_encoded_strategy_dir(work_dir, strategy_name)
                artifact_path = encoded_strategy_dir / f"{chunk_id}.mkv"

                # Try to find the actual winning file
                if pair_rec.state == ArtifactState.COMPLETE and encoded_strategy_dir.exists():
                    for candidate in encoded_strategy_dir.glob(ENCODED_ATTEMPT_GLOB_PATTERN):
                        m = ENCODED_ATTEMPT_NAME_PATTERN.match(candidate.name)
                        if m and m.group("chunk_id") == chunk_id:
                            artifact_path = candidate
                            break

                crf: float | None = None
                if pair_rec.state == ArtifactState.COMPLETE and pair_rec.attempts:
                    # Find the winning CRF from the result sidecar
                    for ar in pair_rec.attempts:
                        if ar.state == ArtifactState.COMPLETE:
                            crf = ar.crf
                            break

                artifacts.append(EncodedArtifact(
                    path     = artifact_path,
                    state    = pair_rec.state,
                    chunk_id = chunk_id,
                    strategy = strategy_name,
                    crf      = crf,
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
        from pyqenc.config import ConfigManager
        from pyqenc.state import EncodingParams

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

        # Persist encoding.yaml with current crop params
        encoding_yaml = work_dir / _ENCODING_YAML
        EncodingParams(crop=crop).save(encoding_yaml)
        logger.debug("Wrote encoding.yaml (crop=%s)", crop)

        # Reference dir is the chunks directory
        reference_dir = work_dir / CHUNKS_DIR

        # Run encoding via the existing encode_all_chunks function
        enc_result = encode_all_chunks(
            chunks          = chunks,
            reference_dir   = reference_dir,
            strategies      = strategy_names,
            quality_targets = self._config.quality_targets,
            work_dir        = work_dir,
            config_manager  = ConfigManager(),
            max_parallel    = self._config.max_parallel,
            force           = self._config.force,
            dry_run         = False,
            crop_params     = crop,
            encoding_yaml   = encoding_yaml,
            cleanup_level   = self._config.cleanup,
        )

        if enc_result.outcome == PhaseOutcome.FAILED:
            err = enc_result.error or "Encoding failed"
            logger.critical(err)
            return _enc_failed(err)

        # Re-run recovery to get final artifact states
        chunk_ids = [c.chunk_id for c in chunks]
        final_recovery = _recover_encoding_attempts(
            work_dir        = work_dir,
            chunk_ids       = chunk_ids,
            strategies      = strategy_names,
            quality_targets = self._config.quality_targets,
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

                crf: float | None = None
                if state == ArtifactState.COMPLETE and pair_rec and pair_rec.attempts:
                    for ar in pair_rec.attempts:
                        if ar.state == ArtifactState.COMPLETE:
                            crf = ar.crf
                            break

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
