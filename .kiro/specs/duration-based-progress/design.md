# Design Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-17
- Completed: 2026-03-17

## Overview

Replace all chunk-count-based `alive_bar` progress displays in the pyqenc pipeline with duration-based progress (seconds of video content). The change affects three pipeline phases (chunking, optimization, encoding) and unifies the existing duration-based bar in `visualization.py` under a shared helper.

The core idea is simple: every chunk already carries `start_timestamp` and `end_timestamp` (float seconds), so `chunk_duration = end_timestamp - start_timestamp` is always available without any extra probing. The progress bar total becomes the sum of those durations (multiplied by the number of strategies where applicable), and each increment is the duration of the just-finished chunk.

## Architecture

No new modules are introduced. Changes are confined to:

| File | Change |
|---|---|
| `pyqenc/constants.py` | Add `PROGRESS_DURATION_UNIT = " s"` |
| `pyqenc/utils/alive.py` | Update `update_bar` signature; add `duration_bar` context manager |
| `pyqenc/phases/chunking.py` | Switch `alive_bar` to `duration_bar`; advance by chunk duration |
| `pyqenc/phases/optimization.py` | Switch `alive_bar` to `duration_bar`; advance by chunk duration |
| `pyqenc/phases/encoding.py` | Switch `alive_bar` to `duration_bar`; pass duration through to `_encode_chunks_parallel` |
| `pyqenc/utils/visualization.py` | Refactor inline `manual=True` pattern to use `duration_bar` |

## Components and Interfaces

### `duration_bar` context manager (`pyqenc/utils/alive.py`)

This is the single new abstraction. It wraps `alive_bar` in `manual=True` mode and hides the fraction arithmetic from callers.

```python
from contextlib import contextmanager
from collections.abc import Generator, Callable

@contextmanager
def duration_bar(
    total_seconds: float,
    title: str,
    unit: str = PROGRESS_DURATION_UNIT,
) -> Generator[Callable[[float], None], None, None]:
    """Context manager that opens a duration-based alive_bar.

    Yields an ``advance(seconds: float) -> None`` callable.
    The bar fraction is updated as ``min(1.0, cumulative / total)``.
    """
```

Usage pattern (same in all three phases):

```python
with duration_bar(total_seconds, title="Chunking") as advance:
    for chunk in chunks:
        do_work(chunk)
        advance(chunk.end_timestamp - chunk.start_timestamp)
```

### Updated `update_bar` (`pyqenc/utils/alive.py`)

The existing helper is used inside `_encode_chunks_parallel` and `_encode_strategy_chunks_parallel` (optimization). Its signature changes from `increment: int` to `increment: float` to carry seconds. The bar itself will now be a `duration_bar`-yielded `advance` callable, so `update_bar` becomes a thin wrapper that also manages the failed-chunk text.

```python
def update_bar(bar: Callable[[float], None] | None, increment: float = 1.0, failed: int = 0) -> None:
```

### Chunking phase (`phases/chunking.py`)

`split_chunks_from_state` currently does:

```python
with alive_bar(len(boundaries), title="Chunking", unit=PROGRESS_CHUNK_UNIT) as bar:
    ...
    bar()  # +1 chunk
```

After the change, `video_meta.duration_seconds` (already probed and cached before the loop) is used directly as the bar total — no need to sum boundary pairs:

```python
total_seconds = video_meta.duration_seconds or 0.0
with duration_bar(total_seconds, title="Chunking") as advance:
    ...
    advance(end_ts - start_ts)
```

### Optimization phase (`phases/optimization.py`)

`find_optimal_strategy` currently does:

```python
total_chunks = len(strategies) * len(test_chunks)
with alive_bar(total_chunks, title="Optimization", unit=PROGRESS_CHUNK_UNIT) as bar:
    ...
    bar()  # inside _encode_strategy_chunks_parallel
```

After the change, the total is the sum of test-chunk durations multiplied by the number of strategies:

```python
total_seconds = sum(c.end_timestamp - c.start_timestamp for c in test_chunks) * len(strategies)
with duration_bar(total_seconds, title="Optimization") as advance:
    ...
    # advance(chunk_duration) called inside _encode_strategy_chunks_parallel
```

The `bar` handle passed into `_encode_strategy_chunks_parallel` changes type from the raw `alive_bar` handle to the `advance` callable yielded by `duration_bar`. The inner function calls `update_bar(bar, increment=chunk_duration)`.

### Encoding phase (`phases/encoding.py`)

`encode_all_chunks` currently does:

```python
total_items = len(chunks) * len(strategies)
with alive_bar(total_items, title="Encoding", unit=PROGRESS_CHUNK_UNIT) as bar:
    result = asyncio.run(_encode_chunks_parallel(..., bar=bar))
```

After the change, the total is the sum of all chunk durations multiplied by the number of strategies:

```python
total_seconds = sum(c.end_timestamp - c.start_timestamp for c in chunks) * len(strategies)
with duration_bar(total_seconds, title="Encoding") as advance:
    result = asyncio.run(_encode_chunks_parallel(..., bar=advance))
```

Inside `_encode_chunks_parallel`, `update_bar(bar, increment=chunk_duration)` replaces `update_bar(bar, increment=int(chunk_result.success))`. The chunk duration is derived from `chunk.end_timestamp - chunk.start_timestamp` at the call site.

### Visualization (`utils/visualization.py`)

`evaluate_chunk` currently has an inline `manual=True` block with a local `_advance` closure. This is replaced by:

```python
with duration_bar(_NUM_METRIC_PASSES * (duration_seconds or 0.0), title=f"Metrics: {bar_title}") as advance:
    psnr_log, ssim_log, vmaf_json = asyncio.run(
        self._generate_metrics(..., bar_advance=advance, ...)
    )
```

The `bar_advance` parameter type in `_generate_metrics` changes from `Callable[[float], None] | None` to `Callable[[float], None] | None` — no change in signature, only the source of the callable changes.

## Data Models

No model changes. `ChunkMetadata.start_timestamp` and `ChunkMetadata.end_timestamp` (both `float`, already present) are the sole data source for duration computation.

Helper function for safe duration extraction (inlined at call sites):

```python
chunk_duration = chunk.end_timestamp - chunk.start_timestamp
```

## Error Handling

| Scenario | Handling |
|---|---|
| `total_seconds <= 0` (empty chunk list) | `duration_bar` opens indeterminate spinner; `advance` is a no-op (Req 5.5) |
| Exception inside `duration_bar` body | Context manager exits normally; `alive_bar` cleans up via its own `__exit__` |

## Testing Strategy

- No automated tests are required for this purely cosmetic/UX change.
- Manual verification: run `uv run pyqenc --work-dir D:\_current\pyqenc1 <source>` and confirm the Chunking, Optimization, and Encoding bars show `s` as the unit and that the ETA/throughput values are plausible (seconds-per-second ≈ real-time speed).
