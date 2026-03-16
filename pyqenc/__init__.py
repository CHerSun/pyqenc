"""
pyqenc - Quality-based video encoding pipeline.

A comprehensive video processing system that orchestrates extraction, scene-based
chunking, quality-targeted encoding, audio processing, and final merging of video files.
"""

__version__ = "0.1.0"

# Public API exports
from pyqenc.api import (
    chunk_video,
    encode_chunks,
    extract_streams,
    merge_final,
    process_audio,
    run_pipeline,
)

__all__ = [
    "__version__",
    "run_pipeline",
    "extract_streams",
    "chunk_video",
    "encode_chunks",
    "process_audio",
    "merge_final",
]
