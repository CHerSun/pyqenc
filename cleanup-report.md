# Cleanup Report — `cleanup` branch

Audit table for the 2026-09-30 cleanup pass. Every candidate from the investigation
sweeps gets a row: what it was, why it was flagged, the action taken (delete / inline /
keep / dedup / rewrite — with reason), and the post-check that verified it.
A kept-with-reason row is as valuable as a deleted row.

Scope & rules (approved plan):
- Functionality-preserving; the only intended behavior change is the fixed
  `pyqenc measure` kwarg crash.
- Tests do not count as live references — test-only production code is dead code.
  Sanctioned exception: `QualitySearch` V1/V2 stay (reference for the planned V4, TODO §51).
- Comments: short spec links allowed; essays, task notes and historical narration go.
- DRY: cheap now, costly logged in TODO.md for later.
- Protected symbols (never touched): `display_name`/`safe_name`, pydantic
  validators/serializers, `Phase` template methods & hooks, dunders, argparse
  callbacks, registry `.resolve()` methods.

## Baselines (Stage 0, 2026-09-30, HEAD 09d61ba)

| Check | Command | Baseline |
|---|---|---|
| ruff | `uv run ruff check` | 367 findings (defaults, no config existed; ruff 0.16.8 not pinned) |
| pytest | `uv run python -m pytest -q` | 11 failed / 721 passed / 9 skipped (2m16s) — the 11 known stale audio tests (TODO §43), no others |
| vulture | `uvx vulture pyqenc` | 88 findings (≈25 real, rest pydantic/framework noise) |
| ty | `uv run ty check` | 357 diagnostics (190 invalid-argument-type, 79 unresolved-attribute, …; best-effort scope) |
| size | `scc pyqenc` / `scc tests` | source: 11,352 code + 8,163 comment + 2,492 blank (42 files); tests: 11,243 code + 3,997 comment (52 files) |

Codebase-memory-mcp graph re-indexed after baselines (was stale: still showed
deleted `Phase._dep`).

## Stage 1 — ruff

Baseline 367 findings (no config existed; ruff 0.16.8 defaults). Outcome: **`uv run ruff check` exits 0** project-wide (`.kiro` excluded — archived spec material).

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | `pyproject.toml` | config | No ruff config, ruff not pinned | Added `[tool.ruff]` (py313, line-length 120, exclude `.kiro`) + `ruff>=0.16.0` to dev group; only ignore: FURB157 | String-form `Decimal("1")` is the house convention, uniform with `Decimal("0.5")` where the string form is required; E501 stays unselected (long aligned signatures are house style, up to ~205 chars) | `ruff check` reproducible via pinned dep |
| 2 | whole tree | dead-code | 35 auto-fixable (F401 unused imports, RUF100 dead noqa, I001, UP037, PYI041, F541, C408) | `ruff check --fix` | Mechanical, safe | ruff clean |
| 3 | BLE001 ×35 prod, ×4 tests | dead-code | Blind `except Exception` in recovery/cleanup scaffolds | Narrowed per-site to the natural set: `(OSError, ValueError, yaml.YAMLError)` for sidecar load/save, `OSError` for FS ops, `(OSError, subprocess.SubprocessError, ValueError)` for ffprobe, `(OSError, FrameCountError)` for frame-count, `(OSError, RuntimeError)` for run_ffmpeg, `(OSError, ValueError, TypeError)` for parse_metrics | House "try (not check)" stays; programming bugs (KeyError/AttributeError/TypeError at load sites) now surface per TODO §41 direction. One deliberate broad catch kept: merge.py per-strategy boundary (`except Exception` + `logger.exception`) — "one bad strategy must not kill the rest"; BLE001 exempts bodies that log via logger.exception | ruff clean; module tests green |
| 4 | S110 ×6 | dead-code | try-except-pass silent | Added `logger.debug` lines (encoding.py ×2, measure.py ×2, ffmpeg_runner kill_all_ffmpeg) | Silent swallow hides diagnosable failures | ruff clean |
| 5 | RUF046 ×7 | dead-code | `int(round(x))` on int | Removed redundant cast | round() without ndigits returns int | tests green |
| 6 | F821 ×19 in tests | dead-code | Quoted annotations, names imported in function bodies | Hoisted imports to module top (test_metrics_integration ×16, test_merge_mkvmerge ×1) | Tests have no cycles; annotation now resolvable | ruff clean |
| 7 | RUF012 ×8 | config | Mutable class defaults | `ClassVar[...]` annotations (utils/logging.py LEVEL_COLORS, test_quality fixtures ×7) | Correctness of intent | ruff clean |
| 8 | misc singles | dead-code | SIM102×2, SIM117×7, SIM113×2, C408×3, PLC0206×2, PERF102, RUF034, UP031×3, UP043, RUF022, G201, PLW1510×3, TRY004×2, RUF059, F841×7, PYI049 | Fixed per rule (combine ifs/withs, .items()/.values(), f-strings, `logger.exception`, explicit `check=False`, TypeError for type guards) | Mechanical honesty fixes | ruff clean |
| 9 | `quality.py:1302` `#!` marker | comment | `#! NO NEW_POINT...` mid-file (EXE005) | Rewritten as `#` comment | Was a comment styled as shebang | ruff clean |
| 10 | `quality.py` `_MetricStatistics` | repair | PYI049 "private TypedDict never used" — false positive (used ×6 cross-module in visualization.py; ruff checks per-file) | Renamed to public `FullMetricStatistics` (docstrings updated) | Making the cross-module type honestly public resolves the rule without noqa | ruff clean; tests green |

## Stage 2 — dead code

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | `pyqenc/phases/recovery.py` | dead-code | Whole module, 0 importers (vulture + grep) | Deleted | Leftover from phase-uniformity refactor | grep 0 refs; ruff/pytest green |
| 2 | `tests/fixtures/config_fixtures.py` (143 ln), `state_fixtures.py` (43 ln) | dead-code | 0 importers; config fixture even uses deleted `{input}` sentinel syntax | Deleted both | Dead weight, stale syntax | pytest collects fine |
| 3 | 15 constants in `constants.py` (TIMEOUT_SECONDS_×3, THIN_LINE, PADDING_FRAME_NUMBER, METRICS_SUBDIR_SUFFIX, SCREENSHOT_TIMESTAMP_FMT, KEEP_RAW_METRICS_FILES, ENCODED_ATTEMPT_GLOB_PATTERN, FFMPEG_MAP_FIRST_AUDIO, FFMPEG_ARG_NO_×4, VISUAL_HASH_EMOJIS_NARROW) + orphaned CHUNKS_DIR docstring | dead-code | 0 refs outside constants.py (loop-grep verified) | Deleted; THIN_LINE's "Think" typo dies with it; header comment updated (WIDE-only pool) | Unused; NARROW pool never got a consumer | grep 0 refs; ruff clean |
| 4 | emoji exclusion comments `constants.py:319,324` | comment | Flagged as commented-out code | **Kept** | They document deliberate curation (rendering exclusions, TODO §34), not restorable code | n/a |
| 5 | `_in_range` + `T_numeric` TypeVar (`quality.py`) | dead-code | 0 callers anywhere | Deleted | Superseded leftover | ruff clean |
| 6 | `_fmt_savings`, `_fmt_target_value` (`log_format.py`) | dead-code | 0 callers anywhere | Deleted (+ 2 now-unused imports auto-removed) | Dead formatters | ruff clean |
| 7 | `PhaseResult.complete/pending/did_work` (`phase.py`) | dead-code | Test-only (production uses `is_complete` + Recovery.pending field) | Deleted + pruning tests (TestCompleteProperty, TestPendingProperty, TestDidWork in test_phase_result.py) | Properties dead since artifact-model landed | 27 phase tests pass |
| 8 | `AudioPhaseResult.audio_files` (`audio.py`) | dead-code | 0 readers | Deleted; docstring mentions updated | Write-only derived list | ruff clean |
| 9 | `ExtractionPhaseResult.chapters_path` | dead-code | 0 readers (`_expected_chapters_path` itself is live at extraction.py:993) | Deleted property only | Thin dead wrapper | ruff clean |
| 10 | `EncodedChunk.file_name` (`stream_model.py`) | dead-code | 0 readers | Deleted (`format_file_name`/`parse_file_name` stay live) | Dead property | 50 stream_model tests pass |
| 11 | `StreamInfo.start_timestamp`, `VideoStreamInfo.pix_fmt` | dead-code | Write-only: populated from ffprobe, never read (chunk windows carry their own timestamps; tests read chunk-level fields) | Deleted fields + extraction.py write sites + test fixture/expected-set updates | Write-only persistence bloat; pre-alpha, no compat | 36 extraction/probe tests pass |
| 12 | `get_progress` + `_completed` set (`encoding.py`) | dead-code | 0 callers; `_completed` read only by get_progress | Deleted method, field, and pre-population line | Dead progress bookkeeping | encoding tests pass |
| 13 | `counter_failed` ×3 lines (`encoding.py`) | dead-code | Assigned/incremented, never read | Deleted | `result.failed_chunks` already tracks failures | encoding tests pass |
| 14 | `_screenshot_timestamps_count/_interval/_filename` (`measure.py`) | dead-code | Test-only; superseded by `compute_screenshot_positions*` + `ScreenshotPositions.filename_ts` | Deleted + 3 test classes (test_measure.py) + whole test_measure_properties.py (both its properties targeted only these helpers) | Tests are not live references | 46 measure/encoding tests pass |
| 15 | `analyze_chunk_quality` + `_auto_output_path` (`visualization.py`) | dead-code | Test-only; production path is `QualityEvaluator._finish_evaluation` (parse→normalize→stats inline) | Deleted both; Properties 10/11 retargeted to the composable pipeline (`parse_metrics`→`normalize_metrics`→`compute_metric_stats`) — they were normalize_metrics' only coverage; `QualityLogs` docstring now names the real owner | Keeps the only normalization coverage alive at behavior level | 17 VIF tests pass |
| 16 | `compose_command` (`ffmpeg_runner.py`) | repair | Docstring claimed "the runner launches this composition" — false: `run_ffmpeg_async` called `_launch_argv` directly; wrapper discarded the tmp→final pair | `_launch_argv` renamed to `compose_command` returning `(argv, tmp_to_final)`; runner now consumes it; 29 golden-test call sites retargeted to `[0]` | Wiring the truth in: argv-pinning golden tests now test the code the runner actually runs; no tests-only production symbol remains | 17 ffmpeg_runner tests pass; full runner path exercised by suite |
| 17 | 12 ANSI colors in `utils/logging.py` (BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE, BRIGHT_GREEN/BLUE/MAGENTA/WHITE) | dead-code | Only 6 of 18 palette members referenced (LEVEL_COLORS uses BRIGHT_BLACK/CYAN/YELLOW/RED + BOLD/RESET) | Deleted 12 dead members | Unused palette | ruff clean |
| 18 | `MetricInfo.id` (`quality.py`) | dead-code | Write-only; duplicates `MetricType.value` ("vmaf"…). 0 `.id` reads anywhere | Deleted field + 4 construction sites + 1 test construction | Redundant with the enum | 104 quality/VIF tests pass |
| 19 | `TargetMeasureResult.graph` (`measure.py`) | dead-code | Vulture: field never read | **Kept** | Result-DTO field of the standalone measure API (same class as `sidecar`/`screenshots_dir`, also CLI-unread): the PNG is genuinely produced and the field documents where; deleting one field makes the DTO asymmetric | n/a (vulture 60%-noise accepted here) |
| 20 | metrics `DottedGroup.prefix_seconds/prefix_duration`, `TimeDistribution.total_duration` | dead-code | Vulture 60% flags | **Kept** | Written into the persisted `metrics.yaml` report — consumed by the human reader and roundtrip-tested (test_metrics_properties) | n/a |
| 21 | `QualitySearch` V1 + V2 (`quality.py`) | dead-code | Test-only, ~390 lines | **Kept (sanctioned)** | User decision: V3 is to be reworked into V4 based on V2 (which hacks onto V1) — reference material for TODO §51 | tests still pass |

## Stage 3 — comments

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 4 — imports

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 5 — inlining

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 6 — DRY

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 7 — repairs

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 8 — tests

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Totals

(filled at the end)
