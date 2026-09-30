"""Track-selection resolver for ``audio.select``.

:func:`resolve_selection` turns the extracted audio tracks plus the ordered
``audio.select`` config into the *working track set* — the tracks that chains
will then process. It is a **pure** function: it matches user regexes against
each track's conventional string (:meth:`AudioStream.selector_string`) and
returns a subset of the given tracks. It performs no I/O and never re-probes.

Selection model:

- **Empty/absent ``select``** → every track is selected, original order
  preserved.
- **Per entry, independently** — candidates are the tracks whose conventional
  string matches ``for`` and (when set) do *not* match ``exclude``.
  Matching is case-insensitive (``re.IGNORECASE``); the conventional string
  itself is never lower-cased so it stays faithful (e.g. ``ch=5.1(side)``).
- **Within an entry**, ``prefer`` tiers are evaluated in order: the first tier
  matching ≥1 candidate wins and contributes *all* of its matching candidates;
  tiers are never merged. If no tier matches, the implicit fallback
  contributes all candidates. With no ``prefer``, all candidates are
  contributed.
- **Across entries**, picks are *additive*: the per-entry sets are unioned into
  the working set, and a track picked by more than one entry appears once.
  Order follows the original track order for determinism.

Dedup key: the track's ``path``. Each extracted audio track is written to its
combination, so ``(file path, track_id)`` is the robust,
stable identity for de-duplicating overlapping entry picks.
"""
# CHerSun 2026

import re
from pathlib import Path

from pyqenc.app_config import SelectEntry
from pyqenc.stream_model import AudioStream


def resolve_selection(
    tracks: list[AudioStream],
    select: list[SelectEntry],
) -> list[AudioStream]:
    """Resolve the working track set from extracted tracks and the select config.

    Pure function over the already-extracted audio metadata — no I/O, no
    re-probe. Regexes are matched case-insensitively against each track's
    :meth:`~pyqenc.stream_model.AudioStream.selector_string`.

    Args:
        tracks: Extracted audio tracks, in extraction order.
        select: Ordered ``audio.select`` entries. Empty selects all tracks.

    Returns:
        The working track set — a de-duplicated subset of ``tracks`` in original
        order. When ``select`` is empty, all ``tracks`` are returned unchanged.
    """
    if not select:
        return list(tracks)

    picked_ids: set[tuple[Path, int]] = set()
    for entry in select:
        for stream in _pick_entry(tracks, entry):
            picked_ids.add((stream.file.path, stream.info.track_id))

    # Preserve original track order for deterministic output.
    return [stream for stream in tracks if (stream.file.path, stream.info.track_id) in picked_ids]


def _pick_entry(tracks: list[AudioStream], entry: SelectEntry) -> list[AudioStream]:
    """Return the tracks a single select entry contributes.

    Args:
        tracks: All extracted tracks.
        entry:  The select entry to evaluate.

    Returns:
        The entry's picked tracks: the winning ``prefer`` tier's matches, or all
        candidates when no tier matches or ``prefer`` is absent.
    """
    for_re = re.compile(entry.for_, re.IGNORECASE)
    exclude_re = re.compile(entry.exclude, re.IGNORECASE) if entry.exclude else None

    candidates = [
        track for track in tracks
        if for_re.search(track.selector_string())
        and not (exclude_re and exclude_re.search(track.selector_string()))
    ]

    if not entry.prefer:
        return candidates

    for tier in entry.prefer:
        tier_re = re.compile(tier, re.IGNORECASE)
        tier_matches = [track for track in candidates if tier_re.search(track.selector_string())]
        if tier_matches:
            return tier_matches

    # No tier matched any candidate — implicit fallback is all candidates.
    return candidates
