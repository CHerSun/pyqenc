# Design Document — Progress Bar Revamp

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-19
- Completed: 2026-03-19

## Overview

Replace the current `duration_bar` context manager and the ad-hoc `alive_bar` usage in the audio phase with a single `ProgressBar` context manager that:

- Always uses `alive_bar` in **manual mode** (0.0–1.0 fraction, renders as %).
- Tracks `success`, `skipped`, and `failed` counters via an `AdvanceState` enum.
- Reduces the effective total when items are skipped/reused, keeping ETA accurate.
- Does **not** force the bar to 1.0 on exit — the final fraction reflects actual progress.
- Displays `✔ {success}  ⏭ {skipped}` (plus `  ✘ {failed}` when failures exist) as bar text.

The `update_bar` helper and `PROGRESS_DURATION_UNIT` / `PROGRESS_CHUNK_UNIT` constants are removed as part of this change.

---

## Architecture

All changes are confined to existing files — no new modules are introduced.

| File | Change |
|---|---|
| `pyqenc/utils/alive.py` | Add `AdvanceState` enum; replace `duration_bar` + `update_bar` with `ProgressBar` |
| `pyqenc/constants.py` | Remove `PROGRESS_DURATION_UNIT` and `PROGRESS_CHUNK_UNIT`; add `SKIPPED_SYMBOL = "⏭"` |
| `pyqenc/phases/chunking.py` | Replace `duration_bar` import/usage with `ProgressBar` |
| `pyqenc/phases/encoding.py` | Replace `duration_bar` / `update_bar` with `ProgressBar`; pass `AdvanceState` |
| `pyqenc/phases/optimization.py` | Replace `duration_bar` / `update_bar` with `ProgressBar`; pass `AdvanceState` |
| `pyqenc/utils/visualization.py` | Replace `duration_bar` with `ProgressBar` |
| `pyqenc/phases/audio.py` | Replace `alive_bar` in runners with `ProgressBar`; add per-task output-exists check |

---

## Components and Interfaces

### `AdvanceState` enum (`pyqenc/utils/alive.py`)

```python
from enum import Enum

class AdvanceState(Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED  = "failed"
```

Three mutually exclusive outcomes for a completed pipeline item.

---

### `ProgressBar` context manager (`pyqenc/utils/alive.py`)

```python
@contextmanager
def ProgressBar(
    total: int | float,
    title: str,
    show_counters: bool = True,
) -> Generator[Callable[[int | float, AdvanceState], None], None, None]:
    ...
```

**Parameters:**

| Parameter | Default | Purpose |
|---|---|---|
| `total` | — | Total weight (seconds or item count). `0` → indeterminate mode. |
| `title` | — | Bar title shown to the user. |
| `show_counters` | `True` | When `True`, bar text shows `✔ {success}  ⏭ {skipped}` (+ failures). When `False`, bar text shows `{cumulative:.1f} / {total:.1f}` (continuous progress readout). |

**Internal state** (captured in closures):

| Variable | Type | Purpose |
|---|---|---|
| `_remaining` | `float` | Effective total remaining; starts at `total`; reduced on SKIPPED |
| `_cumulative` | `float` | Accumulated SUCCESS increments |
| `_success` | `int` | Success counter |
| `_skipped` | `int` | Skipped counter |
| `_failed` | `int` | Failed counter |

**`advance(increment, state)` logic:**

```
def advance(increment=1, state=AdvanceState.SUCCESS):
    update bar text
    if state == SKIPPED:
        _skipped += 1
        _remaining -= increment          # shrink the target
        if _remaining <= 0:
            set bar fraction to 1.0
        else:
            set bar fraction to _cumulative / _remaining
    elif state == FAILED:
        _failed += 1
        # no fraction change, no total change
    else:  # SUCCESS
        _success += 1
        _cumulative += increment
        if _remaining > 0:
            set bar fraction to min(1.0, _cumulative / _remaining)
```

**Indeterminate mode** (`total <= 0`): open `alive_bar` without `manual=True`; `advance` only updates bar text, never touches the fraction.

**On context exit**: no fraction override — `alive_bar` closes with whatever fraction was last set.

**Bar text format:**

```python
def _text() -> str:
    if show_counters:
        base = f"{SUCCESS_SYMBOL_MINOR} {_success}  {SKIPPED_SYMBOL} {_skipped}"
        return base if _failed == 0 else f"{base}  {FAILURE_SYMBOL_MINOR} {_failed}"
    else:
        return f"{_cumulative:.1f} / {_remaining + _cumulative:.1f}"
```

`SUCCESS_SYMBOL_MINOR` (`✔`) and `FAILURE_SYMBOL_MINOR` (`✘`) are imported from `pyqenc/constants.py`. A new `SKIPPED_SYMBOL = "⏭"` constant is added to `pyqenc/constants.py`.

When `show_counters=False`, the text always shows cumulative progress vs. the original total (not the shrinking `_remaining`), so the readout is stable and intuitive for streaming use cases like metrics generation.

---

### Removed: `duration_bar` and `update_bar`

Both functions are deleted from `pyqenc/utils/alive.py`. All call sites are updated to use `ProgressBar` directly.

---

## Call-site Changes

### `chunking.py` — `split_chunks_from_state`

```python
# Before
with duration_bar(total_seconds, title="Chunking") as advance:
    ...
    advance(end_ts - start_ts)

# After
with ProgressBar(total_seconds, title="Chunking") as advance:
    ...
    advance(end_ts - start_ts)  # AdvanceState.SUCCESS by default
```

No reuse/failure states needed here — chunking always produces new output.

---

### `encoding.py` — `_encode_chunks_parallel` / `encode_all_chunks`

The `bar` parameter type changes from `Callable[[float], None] | None` to `Callable[[int | float, AdvanceState], None] | None`.

```python
# Reference chunk missing (failure)
update_bar(bar, increment=0.0)
# → advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)

# Chunk reused
# chunk_result.reused == True → advance(..., AdvanceState.SKIPPED)

# Chunk encoded successfully
update_bar(bar, increment=chunk.end_timestamp - chunk.start_timestamp)
# → advance(chunk.end_timestamp - chunk.start_timestamp)  # SUCCESS default
```

The outer `encode_all_chunks` call site:

```python
# Before
with duration_bar(total_seconds, title="Encoding") as advance:
    result = asyncio.run(_encode_chunks_parallel(..., bar=advance))

# After
with ProgressBar(total_seconds, title="Encoding") as advance:
    result = asyncio.run(_encode_chunks_parallel(..., bar=advance))
```

---

### `optimization.py` — `_encode_strategy_chunks_parallel` / `find_optimal_strategy`

Same pattern as encoding. The `bar` parameter type is updated. Reused COMPLETE pairs call `advance(..., AdvanceState.SKIPPED)`; errors call `advance(..., AdvanceState.FAILED)`.

```python
# Before
with duration_bar(total_seconds, title="Optimization") as advance:
    ...

# After
with ProgressBar(total_seconds, title="Optimization") as advance:
    ...
```

---

### `visualization.py` — `QualityEvaluator.evaluate_chunk`

```python
# Before
with duration_bar(_NUM_METRIC_PASSES * (duration_seconds or 0.0), title=f"Metrics: {bar_title}") as advance:
    ...

# After
with ProgressBar(_NUM_METRIC_PASSES * (duration_seconds or 0.0), title=f"Metrics: {bar_title}", show_counters=False) as advance:
    ...
```

`show_counters=False` gives a `{cumulative:.1f} / {total:.1f}` readout — meaningful for a streaming sub-second progress feed from ffmpeg, where item counters would be noise.

---

### `audio.py` — `SynchronousRunner.process`

```python
# Before
with alive_bar(len(self.tasks), title="Audio Pipeline") as progress:
    ...
    progress.text = _summary()
    progress()

# After
with ProgressBar(len(self.tasks), title="Audio Pipeline") as advance:
    if task.output.exists():          # per-task reuse check (new)
        advance(state=AdvanceState.SKIPPED)
    elif task.parent and task.parent.failed:
        task.failed = True
        advance(state=AdvanceState.FAILED)
    else:
        try:
            task.strategy.execute(...)
            advance()                 # SUCCESS
        except Exception:
            task.failed = True
            advance(state=AdvanceState.FAILED)
```

The `_summary()` helper and manual `progress.text` assignments are removed — `ProgressBar` manages bar text internally.

---

### `audio.py` — `AsyncRunner.process`

```python
# Before
with alive_bar(len(self.tasks), title="Audio Pipeline") as progress:
    self._progress = progress
    await asyncio.gather(...)

# After
with ProgressBar(len(self.tasks), title="Audio Pipeline") as advance:
    self._advance = advance
    await asyncio.gather(...)
```

`_run_task` calls `self._advance(state=AdvanceState.SKIPPED/FAILED)` or `self._advance()` (SUCCESS). The `add_done_callback` that called `p()` is replaced by explicit `advance` calls inside `_run_task`.

The per-task output-exists check is added at the start of `_run_task`:

```python
if task.output.exists():
    self._advance(state=AdvanceState.SKIPPED)
    return True
```

---

## Data Models

No new data models. `AdvanceState` is a simple `Enum` — no persistence, no serialisation.

---

## Error Handling

| Scenario | Handling |
|---|---|
| `total <= 0` | Indeterminate spinner; `advance` updates text only |
| `_remaining` reduced to ≤ 0 by SKIPPED items | Bar fraction set to 1.0 |
| Exception inside `ProgressBar` body | `alive_bar.__exit__` handles cleanup; no special wrapping needed |
| `advance` called after context exit | Not a supported use case; callers always call inside the `with` block |

---

## Testing Strategy

Unit tests for `ProgressBar` in `tests/unit/` covering:

- SUCCESS increments advance the fraction correctly.
- SKIPPED increments reduce `_remaining` and recompute the fraction.
- FAILED increments do not change the fraction or `_remaining`.
- `total=0` opens indeterminate mode; `advance` does not crash.
- Bar text format with and without failures.
