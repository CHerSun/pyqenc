"""Progress state fixtures for testing."""

import json
from pathlib import Path

# Sample progress state JSON (new schema: chunks = dict[str, ChunkMetadata])
SAMPLE_PROGRESS_STATE = {
    "version": "1.0",
    "source_video": "test_video.mkv",
    "current_phase": "encoding",
    "phases": {
        "extraction": {
            "status": "completed",
            "timestamp": "2026-02-23T10:00:00",
            "metadata": None,
        },
        "chunking": {
            "status": "completed",
            "timestamp": "2026-02-23T10:15:00",
            "metadata": None,
        },
        "encoding": {
            "status": "in_progress",
            "timestamp": "2026-02-23T10:45:00",
            "metadata": None,
        },
    },
    "chunks": {},
}


def create_progress_state_file(output_path: Path) -> Path:
    """Create a sample progress state file.

    Args:
        output_path: Path where to create the file

    Returns:
        Path to created file
    """
    output_path.write_text(json.dumps(SAMPLE_PROGRESS_STATE, indent=2))
    return output_path
