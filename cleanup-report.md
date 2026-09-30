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

Executed as three parallel passes (core+utils / phases / utils+config+models). Policy: current-state, concise; short spec links allowed; essays/task notes/history removed. All passes verified ruff-clean + module tests green.

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | quality.py | comment | Worst offender: PTS debugging narrative, "former/backward-compat" banners, V1/V2/V3 history essays, 23-line ✓-diagram extrapolation essay, MetricInfo preamble, code-restating one-liners | Framesync narrative 13→6 lines (kept the load-bearing why); banners de-narrated; V3 docstring states what it IS; extrapolation essay 23→6; ONE V1/V2 keep-note for planned V4 (TODO §51); preamble + one-liners deleted | History lives in git/specs | 146 tests pass |
| 2 | runner.py | comment | "replaces PipelineOrchestrator" + 9 Req citations + ~9 inline Req tags | De-narrated; one short spec link kept (phase-terminal-runner) | Current-state | tests pass |
| 3 | metrics.py | comment | "Symmetric with ffmpeg's…" 8-line banner; 2 Usage examples; stale `flush(partial=False)` doc | 1-line banner; one shortened example; doc fixed to `flush()` | Accuracy | tests pass |
| 4 | phase.py | comment | 10-step run() narration (29 lines); stale `self._job.result` refs; "(Task 9)" | 4-line summary; refs fixed to `_dep_result(JobPhase)`; task note dropped | Contract docs kept (load-bearing) | tests pass |
| 5 | encoding.py | comment | "moved from recovery.py" banner; duplicated `crop_params`/`collector` docs; missing Step 1; false claims about encoding.yaml pre-validation; numbered narration of straight-line code; ~12 Req tags | All fixed per policy; ownership claims corrected to one sentence each | Accuracy | 145 phase tests pass |
| 6 | audio.py | comment | Recovery steps numbered out of execution order; `tracks` unused-note ×2; 4-line Phase-Contract aside; ~20 Req tags | Steps renumbered to actual order (verified against code); notes merged to one Args line; aside → 1 line; Req tags removed | Accuracy | tests pass |
| 7 | extraction/merge/measure/optimization.py | comment | "no longer/deleted in Task 9/TimestampArtifact fold/deleted `mode` field docs/phantom merge_final_video refs" | Current-state phrasing; stale refs fixed or deleted; step comments matching code order kept | Accuracy | tests pass |
| 8 | visualization.py | comment | 26-line "why fig.text()" essay; "mirror create_unified_plot" ×4; VIF/VMAF note ×4 | Essay → 3 lines; mirror-note → 1 line at top site; same-file note stated once | Current-state | tests pass |
| 9 | constants.py, app_config.py, stream_model.py, models.py, state.py, audio/*, utils/*, chunking/job/probe | comment | Muxer "historical behaviour" rationale; name-restating docstrings; EAW essay; "Task 4/6" notes; "Replaces the former flat model"; deleted-field docs; "Preserved verbatim" ×2; `#@` markers; unwrapped docstring | Trimmed per policy; kept: David's-LFE provenance comments (curation), DON'T-use-volumedetect warning, real-constraint docstrings | Current-state | 155 tests pass |
| 10 | cli.py epilog / api.py | comment | Flagged for review | **Kept unchanged** — user-facing `--help` text; api docstrings already current | User-facing surface | n/a |

## Stage 4 — imports

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | ~28 function-body imports across encoding/audio/merge/measure/probe/api/cli | import | Not cycle breaks; several re-imported modules already at module top; steering rule: top-level imports only | Hoisted all to module top; duplicates deleted (`import os/shutil` inside `_hardlink_or_copy`, `MetricKey` in merge) | CLI laziness was pointless — `pyqenc/__init__` already imports api eagerly, so `import pyqenc.cli` pays the full 1.5s regardless (measured) | ruff clean; imports verified |
| 2 | Genuine cycle breaks: models.py:255 (marked), phase.py `_build_registry`, optimization⇄encoding ×3 sites, long_path.py:104 (documented) | import | Legit deferred imports, unmarked | Added explicit `# deferred: circular import (...)` markers per steering rule | Record for later review (encoding⇄optimization cycle noted in TODO.md) | grep shows only these remain |
| 3 | Cross-module private imports (measure→extraction `_probe_streams_json/_video_info`; api→measure `_parse_duration`; measure→log_format `_fmt_size_mb`) | import | Privates consumed cross-module (§57 smell) | Hoisted as-is (names unchanged) | Re-homing/renaming is a design call (TODO §57), out of cleanup scope | imports work |

## Stage 5 — inlining

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | 16 microscopic single-caller helpers: `_parse_quality_targets` (cli), `Strategy.pre_input_args` + `CodecConfig.quality_higher_is_better` (models — dumb accessor wrappers), `AudioSidecar.signature_of` (state), `_is_attachment`/`_sidecar_source` (extraction), `_persist_scenes` (chunking), `_enc_encoded_strategy_dir` (encoding), `_targeted_metrics`/`_get_expected_strategies`/`_default_duration_ns` (merge), `_build_key` (metrics), `_fmt_chunk_prefix` (log_format), `_pick_entry` (select), `_resolve_selected` (optimization), `_normalize_token` (layout) | inline | TODO §58 first bullet; each verified single-caller | All 16 inlined into their consumers; behavior byte-identical (same expressions, same logs). Tests retargeted where they pinned the symbol: TestDefaultDurationNs → asserts argv from `_build_mkvpropedit_args`; metrics property test computes the joined key inline | Footprint shrink; no accessor wrappers | 698/9/0 maintained; ruff clean; grep: no inlined symbol survives |
| 2 | KEEP list: `_set_process_priority`, `_audio_info`/`_subtitle_info`/`_attachment_info`/`_duration_from_tags` (readability of `_enumerate_streams`; re-homing is TODO §57), `AudioSidecar.from_resolved` (named constructor), `_write_mkvmerge_options_file`, `_fmt_inline_metrics`, `_collect_output_files`/`_write_atomic`/`_clean_tmp`/`_safe_size` (Stage-6 dedup targets) | inline | Single-caller but non-microscopic or concept-naming | **Kept** with reasons | A helper earning its keep stays | n/a |

## Stage 6 — DRY

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 7 — repairs

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | tests/unit/test_audio_chain.py ×5, test_audio_filters.py ×4, test_audio_matrices.py ×2 | test | TODO §43: 11 failures — tests pin pre-astats (`volumedetect`/`max_volume:`) and pre-aformat/pre-recoefficiented downmix behavior that production left behind | Updated canned stderr to astats `Peak level dB:` format; `af` expectations gained the `aformat=sample_fmts=flt,` prefix; matrix expectations → current coefficients; error-match → "parseable peak volume"; renamed `test_51_to_20_lfe_preserved_verbatim` → `test_51_to_20_lfe_is_dolby_power_balanced_fold` (the "verbatim night fold" framing was the stale part) | Pins CURRENT behavior; whether the astats/downmix change itself needs revisiting stays a TODO note (kept) | 51 passed across the 3 files; full-suite failures drop 11 → 0 |
| 2 | `pyqenc measure` kwarg crash | repair | cli.py:868 passed `sampling=` ↔ api.py declared `metrics_sampling`; api.py passed `metrics_sampling=` ↔ run_measure declared `sampling` — TypeError before any work | Names aligned (api keeps `metrics_sampling`, forwards `sampling=`); config-derived `= 3` default dropped from api signature (moved to 3rd, required, position — all callers keyword-only); new tests/unit/test_measure_api.py pins the chain + interval parsing | Single source of truth for the config default is the CLI/config layer (§38 spirit) | 2/2 new tests pass; `pyqenc measure --help` path intact |
| 3 | Passthrough stub error message | test | Stage-3 comment trim shortened filters.py NotImplementedError; test matched the removed "in-memory-stream" phrase | Test now matches "not implemented yet" (the fail-loud behavior is the contract, not the spec name) | Message-content pinning reduced to the stable part | 18/18 audio filter tests pass |

## Stage 8 — tests

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Totals

(filled at the end)
