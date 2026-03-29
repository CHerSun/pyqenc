"""Emoji visual hash test for chunk encoding log lines.

Usage:
  uv run python emoji_test.py              — replay sample log with emoji hashes
  uv run python emoji_test.py --audit      — print EAW audit table for all emojis

The --audit mode is a developer tool used to classify emojis by terminal width
(East Asian Width property) so the two pools below can be maintained correctly.
Run it whenever you add new emojis to either list to verify classification.
"""
import hashlib
import re
import sys
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# Emoji pools
#
# Classification rule:
#   WIDE   — unicodedata.east_asian_width(e[0]) == "W"
#             Renders as 2 terminal columns in monospace fonts.
#             No variation-selector suffix (U+FE0F) — those are excluded because
#             they are narrow base chars that only *request* emoji presentation
#             and render inconsistently across terminals.
#   NARROW — east_asian_width == "N" (Narrow), single column.
# ---------------------------------------------------------------------------

VISUAL_HASH_EMOJIS_WIDE: list[str] = [
    # Animals
    "\U0001F436","\U0001F431","\U0001F42D","\U0001F430","\U0001F43B","\U0001F43C",
    "\U0001F42F","\U0001F981","\U0001F42E","\U0001F437","\U0001F438","\U0001F435",
    "\U0001F414","\U0001F427","\U0001F426","\U0001F43A","\U0001F434","\U0001F41D",
    "\U0001F41B","\U0001F40C","\U0001F41E","\U0001F422","\U0001F40D","\U0001F419",
    "\U0001F42C","\U0001F433","\U0001F40B","\U0001F40A","\U0001F405","\U0001F406",
    "\U0001F418","\U0001F98A","\U0001F99D","\U0001F9A8","\U0001F9A1","\U0001F9A5",
    "\U0001F994","\U0001F43E","\U0001F983","\U0001F99A","\U0001F99C","\U0001F9A2",
    "\U0001F9A9","\U0001F407","\U0001F98C","\U0001F999","\U0001F998","\U0001F9AC",
    "\U0001F402","\U0001F403","\U0001F404","\U0001F40E","\U0001F40F","\U0001F411",
    "\U0001F410","\U0001F415","\U0001F429","\U0001F9AE","\U0001F408","\U0001F413",
    "\U0001F9A4","\U0001F985","\U0001F986","\U0001F989","\U0001F987","\U0001F417",
    "\U0001F98B","\U0001F41C","\U0001F99F","\U0001F997","\U0001F982","\U0001F98E",
    "\U0001F996","\U0001F995","\U0001F993","\U0001F9A7","\U0001F9A3","\U0001F99B",
    "\U0001F98F","\U0001F42A","\U0001F42B","\U0001F992","\U0001F416",
    # Food & drink
    "\U0001F34E","\U0001F34A","\U0001F34B","\U0001F347","\U0001F353","\U0001F352",
    "\U0001F351","\U0001F95D","\U0001F346","\U0001F955","\U0001F33D","\U0001F344",
    "\U0001F95C","\U0001F35E","\U0001F9C0","\U0001F356","\U0001F355","\U0001F354",
    "\U0001F32E","\U0001F35C","\U0001F363","\U0001F369","\U0001F36A","\U0001F382",
    "\U0001F36B","\U0001F36C","\U0001F36D","\U0001F9C1",
    # Nature / weather / space
    "\U0001F338","\U0001F33A","\U0001F33B","\U0001F339","\U0001F340","\U0001F33F",
    "\U0001F335","\U0001F334","\U0001F30A","\U0001F525","\U0001F4A7","\U000026A1",
    "\U0001F308","\U0001F319","\U00002B50","\U0001F31F","\U0001F4AB","\U0001F30D",
    "\U0001F30B","\U0001F341",
    # Vehicles
    "\U0001F697","\U0001F695","\U0001F699","\U0001F68C","\U0001F68E","\U0001F693",
    "\U0001F691","\U0001F692","\U0001F690","\U0001F69A","\U0001F69B","\U0001F69C",
    "\U0001F6B2","\U0001F680","\U0001F6F8","\U0001F681","\U0001F6F6","\U000026F5",
    "\U0001F682","\U0001F6A2",
    # Objects / tools
    "\U0001F48E","\U0001F48D","\U0001F451","\U0001F511","\U0001F514","\U0001F3B5",
    "\U0001F3B8","\U0001F3B9","\U0001F3BA","\U0001F3BB","\U0001F941","\U0001F3AE",
    "\U0001F3B2","\U0001F3AF","\U0001F3B3","\U0001F3C6","\U0001F947","\U0001F381",
    "\U0001F380","\U0001F388","\U0001F389","\U0001F52D","\U0001F52C","\U0001F4A1",
    "\U0001F526",
    # Sports
    "\U000026BD","\U0001F3C0","\U0001F3C8","\U000026BE","\U0001F3BE","\U0001F3D0",
    "\U0001F3C9","\U0001F3B1","\U0001F3D3","\U0001F3F8","\U0001F94A","\U0001F94B",
    "\U0001F3BF","\U0001F3C4","\U0001F93F","\U0001F9D7","\U0001F3C7","\U0001F938",
    "\U0001F93C",
    # Clothing / accessories
    "\U0001F452","\U0001F3A9","\U0001F9E2","\U0001F453","\U0001F97D","\U0001F302",
    "\U0001F45C","\U0001F45D","\U0001F392","\U0001F9F3","\U0001F4BC","\U0001F45F",
    "\U0001F460","\U0001F461","\U0001F462","\U0001F97E","\U0001F9E4","\U0001F9E3",
    "\U0001F9E5",
    # Buildings / places
    "\U0001F3E0","\U0001F3E1","\U0001F3E2","\U0001F3E3","\U0001F3E4","\U0001F3E5",
    "\U0001F3E6","\U0001F3E8","\U0001F3E9","\U0001F3EA","\U0001F3EB","\U0001F3EC",
    "\U0001F3ED","\U0001F3EF","\U0001F3F0","\U000026EA","\U0001F54C","\U0001F54D",
    "\U0001F5FC","\U0001F5FD","\U0001F5FF",
    # Fantasy / mythology
    "\U0001F9D9","\U0001F9DD","\U0001F9DB","\U0001F9DF","\U0001F9DE","\U0001F9DC",
    "\U0001F9DA","\U0001F47B","\U0001F480","\U0001F47D","\U0001F47E","\U0001F916",
    "\U0001F383","\U0001F984","\U0001F432","\U0001F409","\U0001F9FF","\U0001F52E",
    "\U0001F9F2",
    # Hands / gestures
    "\U0001F44B","\U0001F91A","\U0000270B","\U0001F446","\U0001F447","\U0001F44D",
    "\U0001F44E","\U0000270A","\U0001F44A","\U0001F91B","\U0001F91C","\U0001F44F",
    "\U0001F64C","\U0001F932","\U0001F91D","\U0001F64F",
    # Symbols / tech
    "\U0001F6AB","\U000026D4","\U0001F51E","\U0001F515","\U0001F507","\U0001F508",
    "\U0001F509","\U0001F50A","\U0001F4E2","\U0001F4E3","\U0001F50B","\U0001F50C",
    "\U0001F4BB","\U0001F4BE","\U0001F4BF","\U0001F4C0","\U0001F4F7","\U0001F4F8",
    "\U0001F4F9","\U0001F3A5",
]

# Narrow emojis (EAW = N, single terminal column).
# Kept separate — use only where single-column alignment is needed.
VISUAL_HASH_EMOJIS_NARROW: list[str] = [
    "\U0001F3CE","\U0001F3CD","\U0001F5DD","\U0001F579","\U0001F56F",
    "\U0001F5A5","\U0001F5A8","\U0001F5B1","\U0001F4FD","\U0001F39E",
    "\U0001F576","\U0001F590","\U0001F43F","\U0001F577",
]


# ---------------------------------------------------------------------------
# Hash function
# ---------------------------------------------------------------------------

def visual_hash(strategy: str, chunk_id: str, pool: list[str]) -> str:
    """Return 1 emoji deterministically derived from strategy+chunk_id.

    Args:
        strategy: Encoding strategy name (e.g. ``"veryslow+h264"``).
        chunk_id: Chunk timestamp range identifier.
        pool:     Emoji list to pick from (use VISUAL_HASH_EMOJIS_WIDE normally).
    """
    h = int.from_bytes(hashlib.md5(f"{strategy}:{chunk_id}".encode()).digest()[:4], "big")
    return pool[h % len(pool)]


# ---------------------------------------------------------------------------
# Developer audit tool — run with: uv run python emoji_test.py --audit
#
# Prints a table of every emoji in both pools with its Unicode codepoint,
# East Asian Width category, and Python len(). Use this to verify that
# emojis are in the correct pool before adding them to production constants.
# ---------------------------------------------------------------------------

def _audit_emoji_pools() -> None:
    """Print EAW classification table for all emojis in both pools.

    Also reports duplicates within each pool and cross-pool collisions.
    """
    all_pools: list[tuple[str, list[str]]] = [
        ("WIDE",   VISUAL_HASH_EMOJIS_WIDE),
        ("NARROW", VISUAL_HASH_EMOJIS_NARROW),
    ]
    for pool_name, pool in all_pools:
        dupes = [e for e in pool if pool.count(e) > 1]
        print(f"\n--- {pool_name} pool ({len(pool)} entries, {len(set(pool))} unique) ---")
        if dupes:
            print(f"  DUPLICATES: {list(dict.fromkeys(dupes))}")
        print(f"  {'emoji':<6} {'codepoint':<10} {'eaw':<5} {'len'}")
        print("  " + "-" * 28)
        for e in pool:
            cp  = ord(e[0])
            eaw = unicodedata.east_asian_width(e[0])
            print(f"  {e:<6} U+{cp:04X}     {eaw:<5} {len(e)}")

    cross = set(VISUAL_HASH_EMOJIS_WIDE) & set(VISUAL_HASH_EMOJIS_NARROW)
    if cross:
        print(f"\nCROSS-POOL COLLISIONS: {cross}")
    else:
        print("\nNo cross-pool collisions.")


# ---------------------------------------------------------------------------
# Log replay
# ---------------------------------------------------------------------------

_LOG_PATTERN: re.Pattern[str] = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2} \[INFO\]) (｟(.+?) ｠) (\S+) (.+)$'
)

_SAMPLE_LOG = Path(__file__).parent / "emoji_test.sample.log"


def _replay_log(pool: list[str]) -> None:
    """Read sample log and print each line with an emoji hash inserted after [INFO]."""
    lines = _SAMPLE_LOG.read_text(encoding="utf-8").splitlines()
    print(f"Wide pool: {len(VISUAL_HASH_EMOJIS_WIDE)}  |  Narrow pool: {len(VISUAL_HASH_EMOJIS_NARROW)}")
    print(f"Active pool: {len(pool)} emojis")
    print()
    for line in lines:
        if not line.strip():
            continue
        m = _LOG_PATTERN.match(line)
        if m:
            prefix, bracket_full, strategy, chunk_id, rest = m.groups()
            vh = visual_hash(strategy, chunk_id, pool)
            print(f"{prefix} {vh} {bracket_full} {chunk_id} {rest}")
        else:
            print(line)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--audit" in sys.argv:
        _audit_emoji_pools()
    else:
        _replay_log(VISUAL_HASH_EMOJIS_WIDE)
