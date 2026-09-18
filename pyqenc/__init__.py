"""
pyqenc - Quality-based video encoding pipeline.

A comprehensive video processing system that orchestrates extraction, scene-based
chunking, quality-targeted encoding, audio processing, and final merging of video files.
"""

__version__ = "0.14.1"

# Public API exports
from pyqenc.api import (
    chunk_video,
    encode_chunks,
    extract_streams,
    measure_quality,
    merge_final,
    process_audio,
    run_pipeline,
)

__all__ = [
    "__version__",
    "chunk_video",
    "encode_chunks",
    "extract_streams",
    "measure_quality",
    "merge_final",
    "process_audio",
    "run_pipeline",
]
