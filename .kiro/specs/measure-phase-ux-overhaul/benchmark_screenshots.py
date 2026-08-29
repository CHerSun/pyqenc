"""Screenshot capture performance benchmark.

Tests single-pass ffmpeg strategies for capturing N screenshots at exact
locations from a video file. All strategies decode the full video in one pass.

Strategies tested:
  A1 — timestamp-based: select='gte(t,T)*not(gte(prev_selected_t,T))+...'
  A2 — frame-number-based: select='eq(n,F1)+eq(n,F2)+...'
  A3 — timestamp-based with isnan guard (alternative expression)
  A4 — mod-based: select='not(mod(n,step))*gt(n,0)' — selects every Nth frame.
       Positions are snapped to frame boundaries (not arbitrary timestamps).
       Step is computed as total_frames // (count + 1) so exactly `count`
       frames are selected at evenly-spaced frame-boundary positions.

Usage (from project root):
    uv run python .kiro/specs/measure-phase-ux-overhaul/benchmark_screenshots.py

Results are printed to stdout and written to benchmark_results.md in the
same directory. All captured PNGs are kept for manual/hash comparison.
"""
# CHerSun 2026

from __future__ import annotations

import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SOURCE_VIDEO:   Path       = Path(r"D:\_encoding\pyqenc\measure\(1).mkv")
TARGET_VIDEOS:  list[Path] = [
    Path(r"D:\_encoding\pyqenc\measure\(1.1) slow_h265.mkv"),
    Path(r"D:\_encoding\pyqenc\measure\(1.1) any_vulkan_hevc-10bit.mkv"),
]
FULL_MOVIE:     Path       = Path(r"D:\_encoding\movie.mkv")
SCREENSHOT_COUNT: int      = 20
OUTPUT_DIR:     Path       = Path(r"D:\_encoding\pyqenc\measure\benchmark_screenshots")
RESULTS_FILE:   Path       = Path(__file__).parent / "benchmark_results.md"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probe(video: Path) -> tuple[float, float]:
    """Return (duration_s, fps) via ffprobe."""
    # Query duration from container format
    dur_result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True, text=True, check=True,
    )
    duration = float(dur_result.stdout.strip())

    # Query avg_frame_rate from the first video stream only
    fps_result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True, text=True, check=True,
    )
    fps_str = fps_result.stdout.strip()
    num, den = fps_str.split("/")
    fps = float(num) / float(den)
    return duration, fps


def _compute_positions(total_frames: int, fps: float, count: int) -> tuple[list[int], list[float], int]:
    """Compute screenshot positions from source video frame count.

    Uses mod-step arithmetic so positions are frame-boundary-aligned and
    compatible with the mod selector. All strategies share these positions.

    Returns:
        frames:     list of 0-based frame numbers  [step, 2*step, ..., N*step]
        timestamps: corresponding timestamps in seconds
        step:       the frame step used
    """
    step       = total_frames // (count + 1)
    frames     = [step * i for i in range(1, count + 1)]
    timestamps = [f / fps for f in frames]
    return frames, timestamps, step


def _run(cmd: list[str | Path], out_dir: Path) -> int:
    """Run ffmpeg command, return png_count."""
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return len(list(out_dir.glob("*.png")))


# ---------------------------------------------------------------------------
# Strategy A1: single-pass, timestamp select (gte + prev_selected_t guard)
# ---------------------------------------------------------------------------

def _strategy_a1(video: Path, timestamps: list[float], out_dir: Path) -> tuple[float, int]:
    """Single pass, select by timestamp using gte(t,T)*not(gte(prev_selected_t,T))."""
    expr = "+".join(
        f"gte(t,{t:.6f})*not(gte(prev_selected_t,{t:.6f}))" for t in timestamps
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video),
        "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
        "-vsync", "0",
        str(out_dir / "%04d.png"),
    ]
    t0 = time.perf_counter()
    captured = _run(cmd, out_dir)
    return time.perf_counter() - t0, captured


# ---------------------------------------------------------------------------
# Strategy A2: single-pass, frame-number select (eq(n,F))
# ---------------------------------------------------------------------------

def _strategy_a2(video: Path, frames: list[int], out_dir: Path) -> tuple[float, int]:
    """Single pass, select by exact frame number using eq(n,F)."""
    expr = "+".join(f"eq(n,{f})" for f in frames)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video),
        "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
        "-vsync", "0",
        str(out_dir / "%04d.png"),
    ]
    t0 = time.perf_counter()
    captured = _run(cmd, out_dir)
    return time.perf_counter() - t0, captured


# ---------------------------------------------------------------------------
# Strategy A3: single-pass, timestamp select using gte only (no guard)
#   Simpler expression — may select extra frames near each target if
#   multiple frames share the same rounded timestamp. Included to test
#   whether the guard expression adds overhead.
# ---------------------------------------------------------------------------

def _strategy_a3(video: Path, timestamps: list[float], out_dir: Path) -> tuple[float, int]:
    """Single pass, select by timestamp using plain gte(t,T) with -frames:v N total."""
    # Use isnan(prev_selected_t) as the "first match" guard — cleaner alternative
    expr = "+".join(
        f"(gte(t,{t:.6f})*(isnan(prev_selected_t)+lt(prev_selected_t,{t:.6f})))"
        for t in timestamps
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video),
        "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
        "-vsync", "0",
        str(out_dir / "%04d.png"),
    ]
    t0 = time.perf_counter()
    captured = _run(cmd, out_dir)
    return time.perf_counter() - t0, captured


# ---------------------------------------------------------------------------
# Strategy A4: single-pass, mod-based frame selection
#   not(mod(n, step)) * gt(n, 0) selects every `step`-th frame, skipping
#   frame 0. Step is chosen so exactly `count` frames are captured.
#   Positions are snapped to frame boundaries — not arbitrary timestamps.
# ---------------------------------------------------------------------------

def _strategy_a4(video: Path, step: int, count: int, out_dir: Path) -> tuple[float, int]:
    """Single pass, select every step-th frame (mod-based). Skips frame 0."""
    expr = f"not(mod(n,{step}))*gt(n,0)"
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video),
        "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
        "-vsync", "0",
        "-frames:v", str(count),   # cap output in case step arithmetic is off by one
        str(out_dir / "%04d.png"),
    ]
    t0 = time.perf_counter()
    captured = _run(cmd, out_dir)
    return time.perf_counter() - t0, captured


# ---------------------------------------------------------------------------
# Strategy C: N individual calls, fast -ss seek before -i, -frames:v 1
#   Modern ffmpeg (2.1+) with -ss before -i: seeks to nearest keyframe,
#   then decodes forward to the exact target timestamp. Only a few seconds
#   of video are decoded per call — potentially much faster than a full pass.
# ---------------------------------------------------------------------------

def _strategy_c(video: Path, timestamps: list[float], out_dir: Path) -> tuple[float, int]:
    """N calls with fast -ss seek before -i. No select filter needed."""
    t0 = time.perf_counter()
    for i, ts in enumerate(timestamps):
        cmd = [
            "ffmpeg", "-hide_banner", "-nostats",
            "-ss", f"{ts:.6f}",
            "-i", str(video),
            "-frames:v", "1",
            str(out_dir / f"{i:04d}.png"),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    captured = len(list(out_dir.glob("*.png")))
    return time.perf_counter() - t0, captured


# ---------------------------------------------------------------------------
# Frame exactness check: compare strategy C vs A2 by SHA-256 hash
# ---------------------------------------------------------------------------

def _check_exactness(stem: str) -> None:
    """Compare C vs A1 PNG files by SHA-256 hash and print results."""
    import hashlib

    dir_a1 = OUTPUT_DIR / f"{stem}_strategy_a1"
    dir_c  = OUTPUT_DIR / f"{stem}_strategy_c"
    if not dir_a1.exists() or not dir_c.exists():
        print("  (skipping exactness check — directories not found)")
        return

    files_a1 = sorted(dir_a1.glob("*.png"))
    files_c  = sorted(dir_c.glob("*.png"))
    if len(files_a1) != len(files_c):
        print(f"  ⚠ Count mismatch: A1={len(files_a1)}  C={len(files_c)}")
        return

    matches = mismatches = 0
    for f_a1, f_c in zip(files_a1, files_c):
        h_a1 = hashlib.sha256(f_a1.read_bytes()).hexdigest()
        h_c  = hashlib.sha256(f_c.read_bytes()).hexdigest()
        if h_a1 == h_c:
            matches += 1
        else:
            mismatches += 1
    total = matches + mismatches
    if mismatches == 0:
        print(f"  Exactness A1 vs C: {matches}/{total} MATCH ✅ (bit-identical frames)")
    else:
        print(f"  Exactness A1 vs C: {matches}/{total} match, {mismatches}/{total} DIFFER ⚠")


# ---------------------------------------------------------------------------
# Per-video benchmark
# ---------------------------------------------------------------------------

_STRATEGIES: list[tuple[str, str]] = [
    ("c",  "C  — N calls, fast -ss seek, -frames:v 1     "),
    ("a1", "A1 — single-pass timestamp (gte+guard)       "),
    ("a2", "A2 — single-pass frame-number (eq(n,F))      "),
    ("a3", "A3 — single-pass timestamp (gte+isnan guard) "),
    ("a4", "A4 — single-pass mod (not(mod(n,step))*gt)   "),
]


def _benchmark_video(
    video:      Path,
    count:      int,
    frames:     list[int],
    timestamps: list[float],
    step:       int,
) -> dict:
    print(f"\n{'='*60}")
    print(f"Video: {video.name}")
    duration, fps = _probe(video)
    print(f"Duration: {duration:.1f}s ({duration/3600:.2f}h)  FPS: {fps:.3f}")
    print(f"Positions (first 3): ts={[f'{t:.1f}' for t in timestamps[:3]]}  "
          f"frames={frames[:3]}  step={step}")

    stem    = video.stem
    target  = duration / 3
    results = {"video": video.name, "duration_s": duration, "fps": fps,
               "screenshot_count": count, "target_s": target,
               "mod_step": step, "total_frames_source": frames[-1] + step}

    for key, label in _STRATEGIES:
        out_dir = OUTPUT_DIR / f"{stem}_strategy_{key}"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nRunning {label.strip()}...")
        if key == "a1":
            elapsed, captured = _strategy_a1(video, timestamps, out_dir)
        elif key == "a2":
            elapsed, captured = _strategy_a2(video, frames, out_dir)
        elif key == "a3":
            elapsed, captured = _strategy_a3(video, timestamps, out_dir)
        elif key == "a4":
            elapsed, captured = _strategy_a4(video, step, count, out_dir)
        else:
            elapsed, captured = _strategy_c(video, timestamps, out_dir)
        pct = elapsed / duration * 100
        ok  = "✅" if elapsed <= target else "❌"
        print(f"  {elapsed:6.1f}s  ({pct:.0f}% of duration)  {captured}/{count} captured  {ok}  → {out_dir}")
        results[f"{key}_seconds"]  = elapsed
        results[f"{key}_captured"] = captured

    print(f"\nSummary for {video.name}  (target ≤ {target:.1f}s):")
    for key, label in _STRATEGIES:
        t = results[f"{key}_seconds"]
        ok = "✅" if t <= target else "❌"
        print(f"  {label} {t:6.1f}s  {ok}")

    print("\nExactness check (C vs A1 — same timestamps, different method):")
    _check_exactness(stem)

    return results


# ---------------------------------------------------------------------------
# Results writer
# ---------------------------------------------------------------------------

def _write_results(all_results: list[dict]) -> None:
    lines = [
        "# Screenshot Benchmark Results",
        "",
        f"- Run date: {time.strftime('%Y-%m-%d')}",
        f"- Screenshot count per video: {SCREENSHOT_COUNT}",
        "",
        "## Results",
        "",
    ]
    for r in all_results:
        dur    = r["duration_s"]
        target = r["target_s"]
        lines += [
            f"### {r['video']}",
            "",
            f"- Duration: {dur:.1f}s ({dur/3600:.2f}h)  FPS: {r['fps']:.3f}",
            f"- Target wall-clock (≤1/3 duration): {target:.1f}s",
            f"- Mod step: {r['mod_step']} frames  (total frames ≈ {r['total_frames']})",
            f"- A4 actual positions: frames {r['mod_step']}, {r['mod_step']*2}, {r['mod_step']*3}, ... "
            f"(≈ {r['mod_step']/r['fps']:.1f}s, {r['mod_step']*2/r['fps']:.1f}s, {r['mod_step']*3/r['fps']:.1f}s, ...)",
            "",
            "| Strategy | Time (s) | % of duration | Meets target | Captured |",
            "|---|---|---|---|---|",
        ]
        for key, label in _STRATEGIES:
            t   = r[f"{key}_seconds"]
            cap = r[f"{key}_captured"]
            ok  = "✅" if t <= target else "❌"
            lines.append(
                f"| {label} | {t:.1f} | {t/dur*100:.0f}% | {ok} | {cap} |"
            )
        lines.append("")

    lines += [
        "## Exactness check",
        "",
        "Compare PNG files across strategies for the same video:",
        "```",
        "# PowerShell — compare A1 vs A2 for source video",
        r"$a1 = Get-ChildItem 'D:\_encoding\pyqenc\measure\benchmark_screenshots\(1)_strategy_a1\*.png'",
        r"$a2 = Get-ChildItem 'D:\_encoding\pyqenc\measure\benchmark_screenshots\(1)_strategy_a2\*.png'",
        "for ($i=0; $i -lt $a1.Count; $i++) {",
        "    $h1 = (Get-FileHash $a1[$i] -Algorithm SHA256).Hash",
        "    $h2 = (Get-FileHash $a2[$i] -Algorithm SHA256).Hash",
        "    if ($h1 -eq $h2) { Write-Host \"$i: MATCH\" } else { Write-Host \"$i: DIFFER\" }",
        "}",
        "```",
        "",
        "## Recommendation",
        "",
        "<!-- Fill in after running: which strategy to use and why. -->",
        "",
    ]

    RESULTS_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nResults written to: {RESULTS_FILE}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    short_videos = [SOURCE_VIDEO] + TARGET_VIDEOS

    missing = [v for v in short_videos if not v.exists()]
    if missing:
        print("ERROR: Missing required video files:")
        for v in missing:
            print(f"  {v}")
        return

    if not FULL_MOVIE.exists():
        print(f"WARNING: Full movie not found, skipping: {FULL_MOVIE}")

    # Compute shared positions from source video only
    print(f"\nProbing source for shared positions: {SOURCE_VIDEO.name}")
    src_duration, src_fps = _probe(SOURCE_VIDEO)
    src_total_frames      = round(src_duration * src_fps)
    frames, timestamps, step = _compute_positions(src_total_frames, src_fps, SCREENSHOT_COUNT)
    print(f"  Source: {src_duration:.1f}s  {src_fps:.3f}fps  {src_total_frames} frames")
    print(f"  Step: {step}  Positions: {frames[:3]}... → {[f'{t:.1f}s' for t in timestamps[:3]]}...")

    all_results = []

    # Short videos — all share the same positions
    for video in short_videos:
        result = _benchmark_video(video, SCREENSHOT_COUNT, frames, timestamps, step)
        all_results.append(result)

    # Full movie — compute its own positions
    if FULL_MOVIE.exists():
        print(f"\nProbing full movie for its own positions: {FULL_MOVIE.name}")
        fm_duration, fm_fps = _probe(FULL_MOVIE)
        fm_total_frames     = round(fm_duration * fm_fps)
        fm_frames, fm_timestamps, fm_step = _compute_positions(
            fm_total_frames, fm_fps, SCREENSHOT_COUNT
        )
        print(f"  Full movie: {fm_duration:.1f}s ({fm_duration/3600:.2f}h)  "
              f"{fm_fps:.3f}fps  {fm_total_frames} frames  step={fm_step}")
        result = _benchmark_video(FULL_MOVIE, SCREENSHOT_COUNT, fm_frames, fm_timestamps, fm_step)
        all_results.append(result)

    _write_results(all_results)
    print(f"\nAll screenshots retained at: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
