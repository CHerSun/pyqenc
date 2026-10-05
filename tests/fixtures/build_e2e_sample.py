"""Build the e2e sample MKV from a developer-owned real video.

The e2e suites (``tests/e2e/``) skip without the sample at
``samples/sample-e2e.mkv``: a small MKV exercising every stream kind — a
real-content video window, two generated audio tracks (aac + flac), a
subtitle, a cover attachment, and chapters. The real source supplies the
video content (and its cover attachment, when present); everything else is
generated. The source is never modified; intermediates land in
``samples/_build/`` (gitignored).

Usage::

    uv run python -m tests.fixtures.build_e2e_sample <real_video.mkv>
        [--start SECONDS] [--duration SECONDS]

The result is verified (ffprobe) before the script reports success: at
least one video, two audio, one subtitle, one attachment, and chapters.
"""

# CHerSun 2026

import argparse
import json
import shutil
import subprocess
import sys
from os import PathLike
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = _REPO_ROOT / "samples"
BUILD_DIR = SAMPLES_DIR / "_build"
OUTPUT = SAMPLES_DIR / "sample-e2e.mkv"

_SUBTITLE_SRT = """\
1
00:00:00,500 --> 00:00:03,000
Sample subtitle line one.

2
00:00:05,000 --> 00:00:08,500
A second line for the extract e2e.

3
00:00:20,000 --> 00:00:24,000
And a third, near the end.
"""

_CHAPTERS_XML = """\
<?xml version="1.0"?>
<!-- <!DOCTYPE Chapters SYSTEM "matroskachapters.dtd"> -->
<Chapters>
  <EditionEntry>
    <ChapterAtom>
      <ChapterTimeStart>00:00:00.000000000</ChapterTimeStart>
      <ChapterDisplay>
        <ChapterString>Opening</ChapterString>
        <ChapterLanguage>eng</ChapterLanguage>
      </ChapterDisplay>
    </ChapterAtom>
    <ChapterAtom>
      <ChapterTimeStart>00:00:12.000000000</ChapterTimeStart>
      <ChapterDisplay>
        <ChapterString>Middle</ChapterString>
        <ChapterLanguage>eng</ChapterLanguage>
      </ChapterDisplay>
    </ChapterAtom>
    <ChapterAtom>
      <ChapterTimeStart>00:00:26.000000000</ChapterTimeStart>
      <ChapterDisplay>
        <ChapterString>Finale</ChapterString>
        <ChapterLanguage>eng</ChapterLanguage>
      </ChapterDisplay>
    </ChapterAtom>
  </EditionEntry>
</Chapters>
"""


def _run(cmd: list[str | PathLike[str]], *, what: str) -> subprocess.CompletedProcess[bytes]:
    """Run a build step; a non-zero exit aborts with its stderr."""
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode(errors="replace"))
        raise SystemExit(f"Failed to {what} (exit {result.returncode})")
    return result


def _build_video_window(source: Path, start: int, duration: int) -> Path:
    target = BUILD_DIR / "sample_video.mkv"
    _run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(duration),
         "-i", source, "-map", "0:v:0", "-map_chapters", "-1", "-c", "copy", target],
        what="cut the video window",
    )
    return target


def _build_audio_tracks(duration: int) -> tuple[Path, Path]:
    aac = BUILD_DIR / "sample_aac.m4a"
    flac = BUILD_DIR / "sample_flac.flac"
    _run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
         "-c:a", "aac", "-b:a", "128k", aac],
        what="generate the aac track",
    )
    _run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=880:sample_rate=48000:duration={duration}",
         "-c:a", "flac", flac],
        what="generate the flac track",
    )
    return aac, flac


def _build_cover(source: Path) -> Path:
    """The source's own cover attachment when it has one; a synthetic jpg otherwise."""
    cover = BUILD_DIR / "cover.jpg"
    extracted = subprocess.run(
        # "1:<file>" is an attachment spec mkvextract parses itself — plain form.
        ["mkvextract", source, "attachments", f"1:{cover}"],
        capture_output=True,
        check=False,
    )
    if extracted.returncode == 0 and cover.exists():
        return cover
    _run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=0x704214:s=64x64", "-frames:v", "1", cover],
        what="generate a fallback cover",
    )
    return cover


def _mux(video: Path, aac: Path, flac: Path, cover: Path) -> None:
    _run(
        ["mkvmerge", "-q", "-o", OUTPUT,
         "--title", "Sample e2e",
         "--chapters", BUILD_DIR / "sample_chapters.xml",
         video,
         "--language", "0:eng", "--track-name", "0:Surround AAC", aac,
         "--language", "0:rus", "--track-name", "0:Stereo FLAC", flac,
         "--language", "0:eng", "--track-name", "0:Full subs",
         BUILD_DIR / "sample_subs.srt",
         "--attachment-name", "cover.jpg",
         "--attachment-mime-type", "image/jpeg",
         "--attach-file", cover],
        what="mux the sample",
    )


def _verify() -> None:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_chapters", OUTPUT],
        check=True, capture_output=True,
    )
    data = json.loads(probe.stdout)
    kinds: dict[str, int] = {}
    for stream in data["streams"]:
        kinds[stream["codec_type"]] = kinds.get(stream["codec_type"], 0) + 1
    problems: list[str] = []
    if kinds.get("video", 0) < 1:
        problems.append("no video track")
    if kinds.get("audio", 0) < 2:
        problems.append(f"expected 2 audio tracks, found {kinds.get('audio', 0)}")
    if kinds.get("subtitle", 0) < 1:
        problems.append("no subtitle track")
    if not data.get("chapters"):
        problems.append("no chapters")
    has_attachment = any(
        s.get("disposition", {}).get("attached_pic") == 1
        for s in data["streams"] if s["codec_type"] == "video"
    )
    if not has_attachment:
        problems.append("no cover attachment")
    if problems:
        raise SystemExit(f"Sample verification failed: {'; '.join(problems)}")
    print(f"OK: {OUTPUT} — video, {kinds['audio']} audio, subtitle, attachment, "
          f"{len(data['chapters'])} chapters")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="A real video to take content from")
    parser.add_argument("--start", type=int, default=1200,
                        help="Window start in the source, seconds (default: 1200)")
    parser.add_argument("--duration", type=int, default=35,
                        help="Window length, seconds (default: 35)")
    args = parser.parse_args()

    for tool in ("ffmpeg", "mkvextract", "mkvmerge", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH")
    if not args.source.exists():
        raise SystemExit(f"Source video not found: {args.source}")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    video = _build_video_window(args.source, args.start, args.duration)
    aac, flac = _build_audio_tracks(args.duration)
    (BUILD_DIR / "sample_subs.srt").write_text(_SUBTITLE_SRT, encoding="utf-8")
    (BUILD_DIR / "sample_chapters.xml").write_text(_CHAPTERS_XML, encoding="utf-8")
    cover = _build_cover(args.source)
    _mux(video, aac, flac, cover)
    _verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
