# Glossary

Project vocabulary. Terms are reused verbatim across code identifiers, docs, and
user-facing text. Introducing a new term requires explicit approval — the same
gate as new packages/dependencies. Established 2026-10-17 (TODO §129).

## Run modes

- **fixed mode / search mode** — the two run modes (persisted `mode` tags). A
  collapsed quality range or `-q` fixes a run; otherwise the tool searches.
  Prose never uses "pinned run", "ranged strategy", "single-point" — a *range*
  may be called collapsed.
- **quality** — the encoder knob value of one encode. `quality` on interface
  surfaces; `q` in compact contexts (CLI `-q`, filename tokens `.q<q>` /
  `<LABEL>=<q>`, `QualityPoint.q`). `crf`/`cq`/`qp` appear only as literal
  ffmpeg arguments; display goes through **quality label**; persisted forms are
  neutral (`quality`).
- **quality label** — the codec's display name for the knob (CRF, CQ, QP,
  Mbit/s).
- **quality range** — the knob's `[better, worse]` domain, config order;
  `quality_better`/`quality_worse` are the endpoints; granularity and max-step
  quantize proposals.
- **quality target** (plural: **targets**) — an acceptance constraint: the
  metric-statistic pair + value. `QualityTarget` (field `identity`), config
  entries of `encoding.target_profiles`, `--targets`. Bare "target" in
  encoding context means this.
- **target profile** — a named bundle of quality targets
  (`encoding.target_profiles`; `ultra`/`high`/`medium`/`low` shipped,
  calibrated at h265+slow CRF 14/18/22/26 anchor levels). Selected by name
  via `--targets <name>` or by default via `default_target_profile`. The
  qualified compound of **profile**: "profile" alone means the encoding
  profile.

## Judging and comparison

- **anchor** — the runtime-elected reference strategy of a compared fixed run
  (test / merge / presentation anchor by site); never persisted.
- **anchor targets** — the anchor's aggregated metrics expressed as a target
  set; the fixed-mode judging reference.
- **verdict** — the live pass/fail judgment of measurements against the current
  reference; never persisted (`targets_met` is the persisted fact of a
  concluded search).
- **comparison basis** — ordinary English for what table deltas read against:
  the targets (search) or the anchor (fixed). Not an identifier;
  `ComparisonTable.targets` / `.anchor` are. A table with neither is
  *uncompared*.

## Measurement

- **measurement / measure** — scoring an encode against its source
  (VMAF/PSNR/SSIM/VIF); facts only, never verdicts. `QualityMeasurement`,
  `QualityMeasurer`, `measure_chunk`.
- **quality metrics** — the VMAF/PSNR/SSIM/VIF world. Where a more specific
  word applies, it wins: targets, sampling, measurement. The identity
  vocabulary lives in `quality_metrics.py`.
- **metric-statistic pair** — the identity of one measured value: a metric
  paired with a statistic (`MetricStatKey`). The single naming authority for
  its key forms: canonical persistence (`vmaf_median`), CLI/config
  (`vmaf-min`), display (`VMAF 50%`).
- **measured set / targeted set** — the full set of one measurement's pairs
  and values (`MeasuredMetrics`; the re-judging substrate on sidecars) vs.
  the subset narrowed to a pair set (`TargetedMetrics`; the judging and
  presentation basis). Narrowing goes through `narrowed_to` / the plan's
  `targeted()` front — never hand-rolled.
- **fixed comparison pairs** — `FIXED_COMPARISON_PAIRS`: the fixed-mode
  narrower (every metric × {p10, median}); the sole authority for what
  fixed-mode comparison consumes.
- **app metrics** — telemetry about how the app runs (`app_metrics.py`,
  `metrics.yaml`, `AppMetricsCollector`, `--no-metrics`).
- **sampling** — the frame subsampling factor (config `measurement.sampling`);
  the one spelling everywhere.
- **source / distorted** — the standalone measure command's pair:
  `source_video` is the reference, `distorted_video` is the file being
  measured.

## Search and artifacts

- **quality search** — the per-chunk knob search (`QualitySearch` V1/V2/V3
  lineage).
- **attempt** — one encoded chunk at one quality under one strategy: an
  *intermediate* plus its sidecar. The reproduction cache: re-judging
  substrate, lazily re-measured, cleanup-swept, never traversed by later
  phases.
- **winner** — the attempt with the smallest positive delta from the targets
  (on exhaustion, the best failing attempt — `targets_met: false`), promoted
  per (chunk, strategy): an *artifact* plus its sidecar. *Early acceptance* is
  recognizing the winner without exhausting the search — a mechanism, not a
  separate concept.
- **artifact** — a product (winner, merged output) plus its **artifact
  sidecar** (`WinnerSidecar`, `MergedOutputSidecar`): the acceptance record;
  presence + identity match ⇒ `COMPLETE`; traversible by later phases.
- **intermediate** — the reproduction-cache layer (attempts); the cleanup
  tiers' removal domain.
- **phase sidecar** — `<Phase>Sidecar`, the phase's own record
  (`<phase>.yaml`); key-bearing (optimization, probe, chunking) or replay-only
  (encoding, merge) per phase.

## Optimization

- **test basis** — the chunks the strategies are tested on before full-length
  encoding: the selection *rules* (config `optimization.basis`,
  `TestBasisConfig`) and the concept; the selected chunk ids themselves are
  the optimization sidecar's `test_chunks` list. Distinct from *comparison
  basis* (table deltas).
- **selection / selected** — the strategy-size pruning of the optimization
  phase (`tolerance` band); `--no-optimize` runs skip it (`plan.optimize`).
