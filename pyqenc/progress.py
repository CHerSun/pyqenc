"""
Progress tracking for the encoding pipeline.

This module provides persistent state management for pipeline execution,
enabling resumption after interruptions and tracking of encoding attempts.
"""

import atexit
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

from pyqenc.models import (
    AttemptInfo,
    ChunkState,
    ChunkUpdate,
    ChunkVideoMetadata,
    PhaseMetadata,
    PhaseState,
    PhaseStatus,
    PhaseUpdate,
    PipelineState,
    StrategyState,
    VideoMetadata,
)

logger = logging.getLogger(__name__)


class ProgressTracker:
    """Persistent progress tracker for pipeline execution.

    Manages state serialization and provides methods for updating
    and querying pipeline progress.  Registers signal handlers and
    ``atexit`` callbacks so in-memory state is always flushed before
    the process exits.
    """

    STATE_VERSION  = "1.0"
    STATE_FILENAME = "progress.json"

    def __init__(self, work_dir: Path, batch_updates: bool = True, batch_size: int = 10):
        """Initialize progress tracker.

        Args:
            work_dir:       Working directory where state file will be stored.
            batch_updates:  If True, batch updates to reduce I/O (default: True).
            batch_size:     Number of updates to batch before writing (default: 10).
        """
        self.work_dir        = work_dir
        self.state_file      = work_dir / self.STATE_FILENAME
        self._state:          PipelineState | None = None
        self._batch_updates  = batch_updates
        self._batch_size     = batch_size
        self._pending_updates = 0

        self._register_flush_handlers()

    # ------------------------------------------------------------------
    # Crash-safe flush registration
    # ------------------------------------------------------------------

    def _register_flush_handlers(self) -> None:
        """Register atexit and signal handlers to flush state on exit."""
        atexit.register(self.flush)

        def _signal_handler(signum: int, frame: object) -> None:
            logger.warning(
                "ProgressTracker: received signal %s — flushing state before exit.", signum
            )
            self.flush()
            # Re-raise default behaviour
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _signal_handler)
            except (OSError, ValueError):
                # May fail in non-main threads; ignore gracefully
                pass

        if sys.platform == "win32":
            for sig in (signal.CTRL_C_EVENT, signal.CTRL_BREAK_EVENT):  # type: ignore[attr-defined]
                try:
                    signal.signal(sig, _signal_handler)
                except (OSError, ValueError):
                    pass

    # ------------------------------------------------------------------
    # State load / save
    # ------------------------------------------------------------------

    def load_state(self) -> PipelineState | None:
        """Load existing state from disk.

        Returns:
            PipelineState if state file exists, None for fresh start.
        """
        if not self.state_file.exists():
            return None

        try:
            import json
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._state = self._deserialize_state(data)
            return self._state
        except Exception as e:
            logger.warning("Could not load state file: %s", e)
            return None

    def save_state(self, state: PipelineState, force: bool = False) -> None:
        """Persist current state to disk.

        Args:
            state: Pipeline state to save.
            force: If True, save immediately regardless of batching.
        """
        self._state = state
        self._pending_updates += 1

        should_save = force or not self._batch_updates or self._pending_updates >= self._batch_size

        if not should_save:
            logger.debug("Batching update (%d/%d)", self._pending_updates, self._batch_size)
            return

        self.work_dir.mkdir(parents=True, exist_ok=True)

        data = self._serialize_state(state)

        import json
        temp_file = self.state_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        try:
            temp_file.replace(self.state_file)
        except OSError:
            if self.state_file.exists():
                self.state_file.unlink()
            temp_file.rename(self.state_file)

        self._pending_updates = 0
        logger.debug("State saved to disk")

    def flush(self) -> None:
        """Force write any pending updates to disk.

        Idempotent: returns immediately if there are no pending updates.
        """
        if self._state is not None and self._pending_updates > 0:
            self.save_state(self._state, force=True)

    # ------------------------------------------------------------------
    # Typed update methods
    # ------------------------------------------------------------------

    def update_phase(
        self,
        phase_or_update: "str | PhaseUpdate",
        status: PhaseStatus | None = None,
        metadata: PhaseMetadata | None = None,
    ) -> None:
        """Update phase status.

        Accepts either a :class:`PhaseUpdate` instance as the sole positional
        argument, or the legacy ``(phase: str, status, metadata)`` signature
        for backward compatibility.

        Args:
            phase_or_update: Either a ``PhaseUpdate`` object or a phase name string.
            status:          Phase status (required when ``phase_or_update`` is a string).
            metadata:        Optional typed phase metadata.
        """
        if self._state is None:
            raise RuntimeError("State not initialized. Call load_state() or initialize new state first.")

        if isinstance(phase_or_update, PhaseUpdate):
            update = phase_or_update
        else:
            if status is None:
                raise ValueError("status is required when phase_or_update is a string")
            update = PhaseUpdate(phase=phase_or_update, status=status, metadata=metadata)

        timestamp = datetime.now().isoformat()

        self._state.phases[update.phase] = PhaseState(
            status=update.status,
            timestamp=timestamp,
            metadata=update.metadata,
        )
        self._state.current_phase = update.phase
        self.save_state(self._state, force=True)

    def update_chunk(
        self,
        chunk_id_or_update: "str | ChunkUpdate",
        strategy: str | None = None,
        attempt: AttemptInfo | None = None,
    ) -> None:
        """Update chunk attempt information.

        Accepts either a :class:`ChunkUpdate` instance as the sole positional
        argument, or the legacy ``(chunk_id, strategy, attempt)`` signature.

        Args:
            chunk_id_or_update: Either a ``ChunkUpdate`` object or a chunk ID string.
            strategy:           Strategy name (required when first arg is a string).
            attempt:            Attempt information (required when first arg is a string).
        """
        if self._state is None:
            raise RuntimeError("State not initialized. Call load_state() or initialize new state first.")

        if isinstance(chunk_id_or_update, ChunkUpdate):
            update = chunk_id_or_update
        else:
            if strategy is None or attempt is None:
                raise ValueError("strategy and attempt are required when chunk_id_or_update is a string")
            update = ChunkUpdate(chunk_id=chunk_id_or_update, strategy=strategy, attempt=attempt)

        if update.chunk_id not in self._state.chunks:
            self._state.chunks[update.chunk_id] = ChunkState(
                chunk_id=update.chunk_id,
                strategies={},
            )

        chunk_state = self._state.chunks[update.chunk_id]

        if update.strategy not in chunk_state.strategies:
            chunk_state.strategies[update.strategy] = StrategyState(
                status=PhaseStatus.IN_PROGRESS,
                attempts=[],
            )

        strategy_state = chunk_state.strategies[update.strategy]
        strategy_state.attempts.append(update.attempt)

        if update.attempt.success:
            strategy_state.status    = PhaseStatus.COMPLETED
            strategy_state.final_crf = update.attempt.crf

        self.save_state(self._state, force=False)

    # ------------------------------------------------------------------
    # Source file change detection
    # ------------------------------------------------------------------

    def _check_source_file_changes(self, state: PipelineState) -> None:
        """Compare persisted VideoMetadata fields against live values.

        Logs a warning for any field that differs and flags affected chunks
        as NOT_STARTED so they will be re-processed.

        Args:
            state: Loaded pipeline state to validate.
        """
        persisted = state.source_video
        live      = VideoMetadata(path=persisted.path)

        # Only compare fields that were actually cached (non-None in persisted)
        fields_to_check: list[tuple[str, object, object]] = []

        if persisted._file_size_bytes is not None:
            live_val = live.file_size_bytes
            if live_val is not None and live_val != persisted._file_size_bytes:
                fields_to_check.append(("file_size_bytes", persisted._file_size_bytes, live_val))

        if persisted._duration_seconds is not None:
            live_val = live.duration_seconds
            if live_val is not None and abs(live_val - persisted._duration_seconds) > 0.1:
                fields_to_check.append(("duration_seconds", persisted._duration_seconds, live_val))

        if persisted._fps is not None:
            live_val = live.fps
            if live_val is not None and abs(live_val - persisted._fps) > 0.01:
                fields_to_check.append(("fps", persisted._fps, live_val))

        if persisted._resolution is not None:
            live_val = live.resolution
            if live_val is not None and live_val != persisted._resolution:
                fields_to_check.append(("resolution", persisted._resolution, live_val))

        if persisted._frame_count is not None:
            live_val = live.frame_count
            if live_val is not None and live_val != persisted._frame_count:
                fields_to_check.append(("frame_count", persisted._frame_count, live_val))

        if not fields_to_check:
            return

        for field_name, old_val, new_val in fields_to_check:
            logger.warning(
                "Source file metadata changed — field '%s': persisted=%s, live=%s. "
                "Affected chunks will be reset to NOT_STARTED.",
                field_name, old_val, new_val,
            )

        # Flag all chunks as NOT_STARTED so they are re-processed
        for chunk_id, chunk_state in state.chunks.items():
            for strategy_name, strategy_state in chunk_state.strategies.items():
                if strategy_state.status != PhaseStatus.NOT_STARTED:
                    logger.warning(
                        "Resetting chunk '%s' strategy '%s' to NOT_STARTED due to source file change.",
                        chunk_id, strategy_name,
                    )
                    strategy_state.status = PhaseStatus.NOT_STARTED

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def _serialize_state(self, state: PipelineState) -> dict:
        """Serialise pipeline state to a JSON-compatible dictionary.

        Uses ``model_dump_full()`` for VideoMetadata so cached probe fields
        survive round-trips, and ``model_dump()`` for all other Pydantic models.

        Args:
            state: Pipeline state to serialise.

        Returns:
            JSON-compatible dictionary.
        """
        # Serialise source_video with cached private fields
        source_video_dict = {
            k: str(v) if isinstance(v, Path) else v
            for k, v in state.source_video.model_dump_full().items()
        }
        # Serialise chunks_metadata with cached private fields
        chunks_metadata_dict = {
            chunk_id: {
                k: str(v) if isinstance(v, Path) else v
                for k, v in meta.model_dump_full().items()
            }
            for chunk_id, meta in state.chunks_metadata.items()
        }

        # Serialise phases using Pydantic model_dump
        phases_dict = {}
        for name, phase in state.phases.items():
            phase_data = phase.model_dump()
            phase_data["status"] = phase.status.value
            if phase.metadata is not None:
                phase_data["metadata"] = phase.metadata.model_dump()
            phases_dict[name] = phase_data

        # Serialise chunks
        chunks_dict = {}
        for chunk_id, chunk_state in state.chunks.items():
            strategies_dict = {}
            for strategy_name, strategy_state in chunk_state.strategies.items():
                attempts_list = []
                for attempt in strategy_state.attempts:
                    attempt_data = attempt.model_dump()
                    if attempt.file_path is not None:
                        attempt_data["file_path"] = str(attempt.file_path)
                    attempts_list.append(attempt_data)

                strategies_dict[strategy_name] = {
                    "status":    strategy_state.status.value,
                    "attempts":  attempts_list,
                    "final_crf": strategy_state.final_crf,
                }

            chunks_dict[chunk_id] = {"strategies": strategies_dict}

        return {
            "version":         state.version,
            "source_video":    source_video_dict,
            "current_phase":   state.current_phase,
            "phases":          phases_dict,
            "chunks_metadata": chunks_metadata_dict,
            "chunks":          chunks_dict,
        }

    def _deserialize_state(self, data: dict) -> PipelineState:
        """Deserialise pipeline state from a dictionary.

        Pre-fills ``VideoMetadata`` backing fields from persisted data so no
        probe is triggered on first property access.  Also runs source file
        change detection when an existing state is loaded.

        Args:
            data: Dictionary produced by :meth:`_serialize_state`.

        Returns:
            Fully populated ``PipelineState`` instance.
        """
        # --- source_video ---
        source_video_data = data.get("source_video", {})
        if isinstance(source_video_data, str):
            source_video = VideoMetadata(path=Path(source_video_data))
        else:
            source_video = VideoMetadata.model_validate_full(
                {**source_video_data, "path": Path(source_video_data.get("path", ""))}
            )
        # --- phases ---
        phases: dict[str, PhaseState] = {}
        for name, phase_data in data.get("phases", {}).items():
            phases[name] = PhaseState(
                status=PhaseStatus(phase_data["status"]),
                timestamp=phase_data.get("timestamp"),
                metadata=(
                    PhaseMetadata.model_validate(phase_data["metadata"])
                    if phase_data.get("metadata")
                    else None
                ),
            )

        # --- chunks_metadata ---
        chunks_metadata: dict[str, ChunkVideoMetadata] = {}
        for chunk_id, meta_data in data.get("chunks_metadata", {}).items():
            chunks_metadata[chunk_id] = ChunkVideoMetadata.model_validate_full(
                {**meta_data, "path": Path(meta_data.get("path", ""))}
            )

        # --- chunks ---
        chunks: dict[str, ChunkState] = {}
        for chunk_id, chunk_data in data.get("chunks", {}).items():
            strategies: dict[str, StrategyState] = {}
            for strategy_name, strategy_data in chunk_data.get("strategies", {}).items():
                attempts = [
                    AttemptInfo(
                        attempt_number=a["attempt_number"],
                        crf=a["crf"],
                        metrics=a["metrics"],
                        success=a["success"],
                        file_path=Path(a["file_path"]) if a.get("file_path") else None,
                        file_size=a.get("file_size"),
                    )
                    for a in strategy_data.get("attempts", [])
                ]
                strategies[strategy_name] = StrategyState(
                    status=PhaseStatus(strategy_data["status"]),
                    attempts=attempts,
                    final_crf=strategy_data.get("final_crf"),
                )
            chunks[chunk_id] = ChunkState(chunk_id=chunk_id, strategies=strategies)

        state = PipelineState(
            version=data.get("version", self.STATE_VERSION),
            source_video=source_video,
            current_phase=data.get("current_phase", ""),
            phases=phases,
            chunks_metadata=chunks_metadata,
            chunks=chunks,
        )

        # Run source file change detection
        self._check_source_file_changes(state)

        return state

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_chunk_state(self, chunk_id: str, strategy: str) -> ChunkState | None:
        """Retrieve state for a specific chunk.

        Args:
            chunk_id: Chunk identifier.
            strategy: Strategy name (unused; kept for API compatibility).

        Returns:
            ChunkState if it exists, None otherwise.
        """
        if self._state is None:
            return None
        return self._state.chunks.get(chunk_id)

    def get_successful_crf_average(self, strategy: str) -> float | None:
        """Calculate average CRF from successful chunks for a given strategy.

        Args:
            strategy: Strategy name.

        Returns:
            Average CRF value, or None if no successful chunks.
        """
        if self._state is None:
            return None

        successful_crfs: list[float] = []
        for chunk_state in self._state.chunks.values():
            if strategy in chunk_state.strategies:
                strategy_state = chunk_state.strategies[strategy]
                if strategy_state.final_crf is not None:
                    successful_crfs.append(strategy_state.final_crf)

        if not successful_crfs:
            return None

        return sum(successful_crfs) / len(successful_crfs)

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def update_source_metadata(
        self,
        duration_seconds: float | None = None,
        frame_count:      int   | None = None,
        fps:              float | None = None,
        resolution:       str   | None = None,
    ) -> None:
        """Pre-fill source video backing fields without triggering a probe.

        Args:
            duration_seconds: Video duration in seconds.
            frame_count:      Total number of frames.
            fps:              Frames per second.
            resolution:       Video resolution string.
        """
        if self._state is None:
            raise RuntimeError("State not initialized")

        sv = self._state.source_video
        if duration_seconds is not None:
            sv._duration_seconds = duration_seconds
        if frame_count is not None:
            sv._frame_count = frame_count
        if fps is not None:
            sv._fps = fps
        if resolution is not None:
            sv._resolution = resolution

        self.save_state(self._state, force=True)

    def get_source_metadata(self) -> VideoMetadata | None:
        """Get source video metadata.

        Returns:
            VideoMetadata if available, None otherwise.
        """
        if self._state is None:
            return None
        return self._state.source_video

    def update_chunk_metadata(self, chunk_meta: ChunkVideoMetadata) -> None:
        """Store metadata for a chunk.

        Args:
            chunk_meta: Chunk metadata to store.
        """
        if self._state is None:
            raise RuntimeError("State not initialized")

        self._state.chunks_metadata[chunk_meta.chunk_id] = chunk_meta
        self.save_state(self._state, force=True)

    def get_chunk_metadata(self, chunk_id: str) -> ChunkVideoMetadata | None:
        """Get metadata for a chunk.

        Args:
            chunk_id: Chunk identifier.

        Returns:
            ChunkVideoMetadata if available, None otherwise.
        """
        if self._state is None:
            return None
        return self._state.chunks_metadata.get(chunk_id)
