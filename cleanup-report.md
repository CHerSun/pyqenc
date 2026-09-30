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
| 11 | `utils/alive.py` progress footprints | repair | PYI041 auto-fix collapsed `int\|float` → `float` on ProgressBarState/advance during stage 1 — accepted then as cosmetic; user review flagged it as contract loss (audio advances per-result int units against an int total; video advances float seconds; ProgressBarState *renders* the two differently via `isinstance(total, float)`) | Reverted all 4 annotations to `int\|float`; PYI041 disabled in pyproject with justification (gradual-typing subsumption contradicts the honest-footprint doctrine where the union is load-bearing) | Footprint states the contract; the rule's premise fails when behavior branches on the difference | ruff clean; 22 progress/audio tests pass |

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
| 11 | `StreamInfo.start_timestamp`, `VideoStreamInfo.pix_fmt` | dead-code | Write-only: populated from ffprobe, never read | Deleted in stage 2 — **REVERSED by user review (2026-09-30) and restored**: these are retained *external facts* (ffprobe `start_time` is critical for future stream alignment if video/audio/subtitles are ever merged; `pix_fmt` is a source property), not dead code — data preservation has different rules than code elimination. Fields, extraction write sites, and test expectations restored with docstring notes on why they're kept | External info worth preserving even when currently unread | 79 stream/extraction tests pass |
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
| 4 | Aliased duplicate bindings: `dataclass` imported plain AND as `_dataclass` in encoding.py (alias dead there) + audio.py (used); `shutil as _shutil`; `_field` | import | Same name bound twice / needless aliases — caught in user review of the hoisted encoding.py block | encoding.py: dead alias deleted, `shutil` de-aliased, one `@_dataclass` decorator normalized (a paren-only grep had missed it); audio.py: `_dataclass`/`_field` de-aliased to plain names. `replace as _dc_replace` kept deliberately (disambiguates from three nearby `str.replace` calls). AST scan then confirmed 0 duplicate bindings / dead aliases package-wide | One binding per name, one convention; ruff has no rule for aliased self-duplicates | ruff clean; 27 phase tests pass |

## Stage 5 — inlining

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | 16 microscopic single-caller helpers: `_parse_quality_targets` (cli), `Strategy.pre_input_args` + `CodecConfig.quality_higher_is_better` (models — dumb accessor wrappers), `AudioSidecar.signature_of` (state), `_is_attachment`/`_sidecar_source` (extraction), `_persist_scenes` (chunking), `_enc_encoded_strategy_dir` (encoding), `_targeted_metrics`/`_get_expected_strategies`/`_default_duration_ns` (merge), `_build_key` (metrics), `_fmt_chunk_prefix` (log_format), `_pick_entry` (select), `_resolve_selected` (optimization), `_normalize_token` (layout) | inline | TODO §58 first bullet; each verified single-caller | All 16 inlined into their consumers; behavior byte-identical (same expressions, same logs). Tests retargeted where they pinned the symbol: TestDefaultDurationNs → asserts argv from `_build_mkvpropedit_args`; metrics property test computes the joined key inline | Footprint shrink; no accessor wrappers | 698/9/0 maintained; ruff clean; grep: no inlined symbol survives |
| 2 | KEEP list: `_set_process_priority`, `_audio_info`/`_subtitle_info`/`_attachment_info`/`_duration_from_tags` (readability of `_enumerate_streams`; re-homing is TODO §57), `AudioSidecar.from_resolved` (named constructor), `_write_mkvmerge_options_file`, `_fmt_inline_metrics`, `_collect_output_files`/`_write_atomic`/`_clean_tmp`/`_safe_size` (Stage-6 dedup targets) | inline | Single-caller but non-microscopic or concept-naming | **Kept** with reasons | A helper earning its keep stays | n/a |
| 3 | `_format_seconds` (utils/ffmpeg_runner.py) | inline | Pure `str()` wrapper; survived stage 5 because its spec-essay docstring made it look load-bearing — the comment trim exposed it (user review) | Inlined as `str(...)` at the 2 call sites; the one real fact (window bounds go as plain str(float), no fixed-precision formatting) moved to a comment at the seek site | Dumb accessor wrapper rule | ruff clean; 17 golden argv tests pass |

## Stage 6 — DRY

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | `.tmp` pre-clean loop ×9 (+ encoding double-scan) | DRY | 9 copies of glob→unlink→warn | New `utils/fs.py: remove_stale_tmp_files/remove_stale_tmp_file`; 9 sites replaced; the duplicate second scan of `encoding/` in `encode_all_chunks` deleted (verified sole caller always follows `_recover`'s clean of the same tree; optimization uses `_encode_chunks_parallel`, unaffected) | Steering DRY rule | suite 698/9/0; ruff clean |
| 2 | YAML sidecar load ×11 / save ×5 | DRY | Repeated exists→safe_load→validate→warn scaffolds | `yaml_utils.load_model/save_model` (generic, PEP 695 type param); 5 state classes + 4 phase loaders now one-liners; raw-dict loaders (merge/encoding) left (different shape, noted) | DRY | suite green |
| 3 | safe-stat-size ×4 | DRY | 4 copies of stat→except→None/0 | `fs.safe_stat_size`; audio/merge local helpers deleted | DRY | suite green |
| 4 | MB formatting hand-inlines ×3 | DRY | Existing `_fmt_size_mb` bypassed | Made public `fmt_size_mb`, used at optimization/merge sites. Note: those sites lacked the <1000MB special case — ≥1000MB values now render without decimals (cosmetic, consistent with measure summary) | DRY | suite green |
| 5 | `_targets_as_strings` twins (merge unsorted / optimization sorted) | DRY | Same serialization, two owners | Single `targets_as_strings` (sorted) in models.py. Note: merge ordering becomes sorted; a legacy `merge.yaml` mismatches once → single re-merge, no re-encode | DRY + one source of truth | suite green |
| 6 | ProbeState-from-probe-result ×4 | DRY | Hand-rolled construction in encoding×2/optimization/merge | `ProbeState.from_probe(probe_result)` classmethod (guards verified semantically identical; optimization's `crop if crop else None` was a no-op) | DRY | suite green |
| 7 | Decimal quality-range coercion ×2 | DRY | models vs app_config validators duplicate `Decimal(str(v))` pair logic | Shared private `_coerce_decimal_pair` in models.py; both validators delegate | DRY | suite green |
| 8 | `evaluate_chunk` sync/async full twins (~150 lines duplicated) | DRY | Only await-vs-asyncio.run differed | Async core + thin sync wrapper. **Bonus bug fixed**: sync no-progress branch silently omitted `fps_value` from `_generate_metrics` | DRY | suite green |
| 9 | Deferred (logged to TODO.md): CLI `_cmd_*` bodies ×6; test fixture factories ×5; EncodedChunk composition dup; ASCII summary-table scaffold ×2; repeated guard blocks; `MergePhaseResult`-owned output collection; measure→extraction private imports | DRY | Costly/structural | Deferred | Blast radius vs this branch's scope | TODO.md updated |

## Stage 6 — semantic notes (deliberate, cosmetic)

- `.tmp` cleanup is now recursive (`rglob`) at all sites — strictly wider hygiene; `.tmp_a2_*` scratch dirs unaffected.
- Sidecar-load warnings now emit from the `pyqenc.utils.yaml_utils` logger (was per-phase loggers).
- job.py source-stat warning loses the exception detail (helper is silent — majority semantics of the 4 unified sites).
- `evaluate_chunk` sync wrapper passes `fps_value` in the no-progress branch (was dropped — bug fixed, flagged by the dedup read).

## Stage 7 — repairs

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | tests/unit/test_audio_chain.py ×5, test_audio_filters.py ×4, test_audio_matrices.py ×2 | test | TODO §43: 11 failures — tests pin pre-astats (`volumedetect`/`max_volume:`) and pre-aformat/pre-recoefficiented downmix behavior that production left behind | Updated canned stderr to astats `Peak level dB:` format; `af` expectations gained the `aformat=sample_fmts=flt,` prefix; matrix expectations → current coefficients; error-match → "parseable peak volume"; renamed `test_51_to_20_lfe_preserved_verbatim` → `test_51_to_20_lfe_is_dolby_power_balanced_fold` (the "verbatim night fold" framing was the stale part) | Pins CURRENT behavior; whether the astats/downmix change itself needs revisiting stays a TODO note (kept) | 51 passed across the 3 files; full-suite failures drop 11 → 0 |
| 2 | `pyqenc measure` kwarg crash | repair | cli.py:868 passed `sampling=` ↔ api.py declared `metrics_sampling`; api.py passed `metrics_sampling=` ↔ run_measure declared `sampling` — TypeError before any work | Names aligned (api keeps `metrics_sampling`, forwards `sampling=`); config-derived `= 3` default dropped from api signature (moved to 3rd, required, position — all callers keyword-only); new tests/unit/test_measure_api.py pins the chain + interval parsing | Single source of truth for the config default is the CLI/config layer (§38 spirit) | 2/2 new tests pass; `pyqenc measure --help` path intact |
| 3 | Passthrough stub error message | test | Stage-3 comment trim shortened filters.py NotImplementedError; test matched the removed "in-memory-stream" phrase | Test now matches "not implemented yet" (the fail-loud behavior is the contract, not the spec name) | Message-content pinning reduced to the stable part | 18/18 audio filter tests pass |

## Stage 8 — tests

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| 1 | test_metrics_integration.py + test_phase.py + test_probe_phase.py: ~25 MagicMock-spy tests asserting on `collector.time` call args / `step()` call counts | test | TODO §5: internal-instrumentation pinning | Converted to behavior tests: real `YamlMetricsCollector` into tmp dir → flush → close → assert on the written `metrics.yaml` (top-level keys, dotted groups, convergence section, reuse-without-reaccrual). `_SteppingClock` (1s/step monotonic patch) makes spans survive the report's integer-second rounding | External behavior > internal calls; each retained test keeps/gets a bug-condition docstring | 96 scoped tests pass; full suite 696/9/0 |
| 2 | 2 tests deleted outright (audio reuse twin merged; optimization step-call-count pin — identical scenario covered by the convergence-report test) | test | Pure internal pinning | Deleted with coverage mapped | step() prefix is unobservable in the report (convergence keyed by strategy only — documented in the converted test) | n/a |
| 3 | Broken fixtures unmasked by the conversion (old spy assertions hid them) | test | — | (a) optimization "reuse" test never actually hit reuse (persisted IDs never matched — phase re-executed; test renamed to say what it really checks); (b) merge tests' stub returned `Path`s where `EncodedChunk`s were required — every strategy `AttributeError`'d before `merge.concat`; fixture now returns real EncodedChunks; (c) merge quality test could never reach `merge.quality_measure` (probe stream None) — fixed via `probe_stream` fixture param | Spy assertions passed while the scenario was broken — exactly the §5 failure mode | converted tests assert the spans now actually execute |
| 4 | tests/integration/test_encoding_quality.py stale `ChunkEncoder(...)` construction (missing `collector`, unknown `sampling` — would TypeError when un-skipped; found via ty) | test | kwarg drift | Fixed construction (`collector=MagicMock(spec=MetricsCollector)`, `metrics_sampling=10`) | Keep skipped integration tests runnable | 4 passed / 3 skipped |

## Totals

| Metric | Before | After | Δ |
|---|---|---|---|
| ruff findings | 367 (no config, unpinned) | **0** (pinned, configured) | −367 |
| pytest | 11 failed / 721 passed / 9 skipped | **0 failed / 696 passed / 9 skipped** | green |
| vulture findings | 88 | 64 (remainder = pydantic/framework 60%-noise + 2 documented false positives) | −24 |
| ty diagnostics | 357 (incl. tests) | 83 production-only (type-modeling noise; triaged) | −274 |
| source code lines (scc) | 11,352 | 10,970 | **−382** |
| source comment/docstring lines | 8,163 | 7,898 | **−265** |
| source complexity | 2,164 | 2,037 | −127 |
| test code lines (scc) | 11,243 | 11,004 | −239 |
| files (source) | 42 | 42 (recovery.py out, utils/fs.py in) | 0 |

Commits: `1b9acae` (stages 1-2) → `cfe2582` (7a audio tests) → `431ee5c` (3 comments) → `6adb5e5` (7b measure crash) → `7323111` (4 imports) → `be9d1fd` (5 inlining) → `618276f` (6 DRY) → this commit (8 tests + 9 bookkeeping).

Behavior changes (all deliberate, flagged in stage rows): fixed `pyqenc measure` crash; fixed sync-evaluation `fps_value` drop; ≥1000MB sizes lose decimals in two log tables; merge target list ordering now sorted (one-time merge.yaml invalidation on legacy workdirs); `.tmp` cleanup recursive everywhere; sidecar-load warnings logged from `yaml_utils`.

## E2E verification (2026-09-30, post-cleanup)

Work dir seeded with only `encoding/` (attempts + metric sidecars for
ultrafast+h265 / -anime / -aq) — everything else had to rebuild:

- `pyqenc auto test.mkv --strategies "h265*+ultrafast" -y` → ✅ success.
  Extraction/probe/chunking/audio ran fresh; **Encoding recovered from
  intermediates: 0 newly encoded, 214 reused, 0 failed** (winning CRFs
  re-selected from persisted attempt metrics — all attempt lines `[reused]`).
  Audio: 4 outputs (2 tracks × 2 chains). Merge: 2 strategies concatenated +
  quality-measured (vmaf-min 88.3/88.2 vs 93 target — expected at ultrafast,
  warned, non-fatal). Frame-preservation check skipped for attempts recovered
  without frame counts (recovery nuance, logged).
- `pyqenc measure test.mkv <merged h265> <merged h265-anime>` → ✅ success —
  the Stage-7b crash fix verified live (this invocation previously died with
  TypeError before any work). Crop auto-loaded from job.yaml (22,22,0,0);
  60/60 screenshots; per-target metrics at sampling=1 (~10 min each):
  both targets PSNR-med 48.7 / SSIM-med 99.0 / VMAF-med 96.5 / VIF-med 94.9 —
  matching the merge-phase measurements. Graphs + sidecars written; 0 stale
  .tmp files remain.

## Contract-enforcement audit (2026-09-30, user-requested doctrine pass)

Doctrine: `isinstance`/`is None` checks are for EXTERNAL optionality (CLI, config,
sidecar, media/ffprobe data, real API Optionals); PROGRAMMATIC contracts (values our
own construction guarantees) get `assert` or a tightened footprint.

**isinstance (39 sites): all compliant.** External parsing/validation (VMAF JSON,
ffprobe dicts, config YAML, deep-merge), assert-form param narrowing (audio filters),
and heterogeneous artifact-row dispatch (`isinstance(r.payload, X)` filters over
genuinely mixed lists). No changes needed.

**is None / is not None (274 code hits):** 157 external-OK, 70 dispatch-OK, 22 already
assert-form, 1 doubtful, **24 contract violations fixed**:

| Family | Sites | Fix |
|---|---|---|
| A — `_dep_result` values treated as Optional (can never be None; walk guarantees COMPLETED/REUSED deps) | encoding ×4, merge ×9 | Dead conjuncts/wrappers dropped; probe-stream reads now `assert probe_result.stream is not None, "probe guaranteed complete by the dependency walk"` |
| B — `ProbeState.from_probe` Optional footprint dead at all 4 call sites | state.py, merge.py | Footprint tightened: `from_probe(probe_result: ProbePhaseResult) -> Self` |
| C — chunking `_execute`/`_reused_result` silently FAILED on impossible None stream (its own `_recover` already asserts) | chunking ×2 | Mirrored the assert |
| D — `.get()` over always-populated pair-recovery dict (keys built from the same lists that populate it) | encoding ×3 | Direct indexing |
| E — success⇒winner | encoding ×1 | Assert added; **latent bug fixed**: all-cache-hit path (line ~1162) returned `success=True, encoded_file=None` when every cached attempt missed targets — now falls back to `best_fail_attempt` like its fresh-path sibling, instead of becoming a phantom failed pair |
| Singles | phase.py dep-walk, job.py ×2 | Asserts per house style (`"...set on every phase-built result"`) |
| Truthiness sibling | optimization.py | `self._current_probe.crop if self._current_probe else None` → direct read |
| Dead constant | audio.py | `_FALLBACK_LAYOUT_TOKEN` deleted (layout contract is enforced by asserts) |

**User decisions applied:** merge.py:859 compound `is None or not .exists()` KEPT
(defense-in-depth, fails loudly); three exception-form programmatic contracts CONVERTED
to asserts (EncodingConfig.resolved_targets/strategies resolve-order RuntimeError;
`_dep_result` + `_ensure_dependencies` registry TypeError; encoding COMPLETE-row
winning_file ValueError) — docstrings updated to AssertionError.

**Test fallout (both fixture defects, not behavior):** test_phase registry test now
pins AssertionError; merge fixture's `probe_stream=None` default constructed the
impossible state the new assert rejects — default is now a real stream.

Post-check: ruff clean; full suite 696 passed / 9 skipped / 0 failed.
