# The splitter — algorithm reference

<!-- markdownlint-disable MD024 -->

- Created: 2026-11-21
- Completed:

Implementable specification of the boundary-selection algorithm that is
implemented in `pyqenc/chunking_split.py`: the causal-Hampel descent. One
splitter for both modes; fast mode feeds it the content score alone,
slow mode adds the probe encode's traces.

It replaces the two-window splitter this spec started from, which is not
implemented and is kept in `algorithm-window-mean.md`. Nothing in this
file reuses that design's values.

## Inputs

- **Content trace**: the per-frame content score, from one decode with
  the detector used purely as a scorer (its cut logic never runs). Frame
  0 has no predecessor and scores 0.
- **Extra traces** (slow mode only): per-frame PSNR, SSIM and
  `log1p(packet size in bytes)` from the probe encode, aligned by frame
  index with the content trace.
- **PTS axis**: the per-frame presentation timestamps in seconds, read
  from the extraction timestamps index. This is the pipeline's frame
  axis: its length is the frame count, the scorer must deliver one score
  per indexed frame, and a disagreement with the decoder fails the phase.
- **`min_scene_length`** (config, frames): the only length ruler.

Constants (`chunking_split.py`, not config):

| Constant | Value | Role |
|---|---|---|
| `SENSITIVITY` | 4.0 | The significance bar, in Hampel z |
| `HAMPEL_LOOKBACK` | 12 | Frames before each frame that form its neighborhood |
| `HAMPEL_SCALE` | 1.4826 | MAD to standard deviation for Gaussian noise |
| `SCALE_FLOOR` | 1e-9 | Lower clamp on the neighborhood scale |
| `MIN_CONTENT_SCORE` | 15.0 | Raw content-score floor |
| `JUMP_HALF_WIDTH` | 3 | Half-width of the centered average |
| `MIN_JUMP` | 10.0 | Minimum departure from that average |

## Step 1 — significance, computed once for the whole trace

**Causal Hampel z**, for each frame `i` of a trace:

1. `window` = the 12 frames strictly before `i` (fewer at the start).
   Frames with fewer than two predecessors score 0.
2. `median` = `sorted(window)[len(window) // 2]`.
3. `mad` = the same statistic of `|v − median|` over the window.
4. `scale` = `max(1.4826 · mad, 1e-9)`; `z_i = |x_i − median| / scale`.

**Content guards** (raw score units, a guard and not a ruler). The
content feature's z counts only where both hold:

- `x_i ≥ 15`;
- `jump_i ≥ 10`, with `jump_i = |x_i − mean(x[i−3 .. i+3])|`, the window
  clipped at the ends.

**Combined significance** = the content feature's guarded z, then the
element-wise maximum with each extra trace's ungated z.

Why it is built this way:

- The content score is an event signal: one spike per cut, noise
  elsewhere. The score measures change against the previous frame, so
  the neighborhood is causal too. A cut is a huge outlier of its own
  neighborhood; a pan or ramp with history is not, because the median
  tracks the climb.
- z is scale-free, so a gradual drift that starts from a flat stretch
  collapses the MAD and reads as a huge outlier. The jump guard supplies
  the absolute scale a real cut has and a drift lacks. The centered
  average may look ahead because it rejects continuity: on a ramp the
  next frames are already climbing and absorb the peak.
- A constant neighborhood gives a zero MAD; the floor keeps z finite, and
  the raw guards are what bound it.

## Step 2 — the descent

1. **Candidates**: every frame with significance ≥ 4, ordered by content
   score descending; equal scores keep the lower frame first.
2. Start with one chunk, the whole trace. Take a chunk `[start, end)`:
   the first candidate `c` in that order with
   `start + L ≤ c < end − L` (`L = min_scene_length`) splits it into
   `[start, c)` and `[c, end)`; both are processed the same way. A chunk
   with no such candidate is terminal and stays whole at any length.

There is no target-length pass and no forced split: a segment is split
only where a frame clears the significance bar.

Properties:

- Every segment holds at least `L` frames, and each split leaves at least
  `L` in both children, so the descent terminates.
- The significance is computed once for the whole trace. That is exact
  for every legal candidate only while the lookback (12) and the jump
  half-width (3) do not exceed `min_scene_length`: a candidate at least
  `L` frames from its chunk's start never looks past that start. The
  default 15 satisfies it; a smaller configured minimum would make
  whole-trace and per-chunk values differ, and whole-trace is the
  definition.
- Not nested by construction: candidates are offered by content score, so
  a looser bar can let a stronger-raw candidate take the place of the
  frame that split a chunk before. Measured on the reference source it
  does not happen — tightening either setting only removes boundaries
  and loosening adds none (bar 2 → 8: 115 → 113; minimum 12 → 24 frames:
  115 → 110).

## Step 3 — boundaries and anchoring

- A boundary is the exact frame that fired. Its timestamp is that
  frame's entry in the PTS axis, taken exactly — never `frame × period`.
  On a millisecond-quantized source a grid timestamp can sit above the
  real PTS and let the boundary frame slip into the predecessor's window,
  or one millisecond below it and pull the previous frame into the
  successor. Both break frame preservation. This rule is inherited from
  the research and unchanged.
- The splitter returns interior boundaries only; the phase prepends the
  stream start. No interior boundary means the whole video is one chunk.
- A trace shorter than `2 · min_scene_length`, or one whose length
  differs from the PTS axis, yields no boundaries.

## Known behavior on the reference source

Validated for fast mode against a full visual inspection (111
must-break, 5 optional, 8 must-not-break groups): 115 boundaries — all
111 must-break groups plus 4 optional, none in a must-not-break group,
none unaccounted for.

- **Blends**: the boundary is the peak frame. For two three-frame blends
  that is one frame later than the stock detector, which cuts at the
  first frame over its threshold (1506 against 1505, 8288 against 8287).
  Whether to cut at the first frame of the event instead is an open
  decision.
- **Blind to encoding-only transitions**: a movement-to-grain transition
  near 11.7 s, and the 39.5 s mixed segment 05:06–05:45 with its
  difficulty cliff near 05:15, show no event in the content score and are
  not cut. The content score is the ceiling for fast mode; these are slow
  mode's task.
- **The bar matters at movie scale**: on the 92-min source the bar
  changes the boundary count (2 → 8: 849 → 804, against 804 for stock
  chunking), unlike on the short source. At the shipped setting 767 of
  the stock run's 804 boundaries are reproduced on the exact frame.

## Alternative recorded, not adopted

A neighbor-gap guard — `x_i − max(x[i−1], x[i+1]) > 10` — with the raw
floor subsumed gives an identical boundary set on the reference source.
Not adopted: a two-neighbor gate can swallow a fast blend of about three
frames that the centered seven-frame average still sees. It is noted at
`centered_jump` in the module.

## Slow mode — coded, not validated

The same descent with PSNR, SSIM and log-bits as extra traces, entered
as ungated z. Open points:

- Nothing has been validated: the window-mean splitter's codec-trace
  results do not transfer to this shape.
- The extra features carry no raw guard. A nearly flat stretch of a
  codec trace gives a tiny deviation a large z.
- The probe's frame rate is derived from the PTS axis where the rest of
  the pipeline uses the stream's declared fraction.
