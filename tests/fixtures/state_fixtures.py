"""Progress state fixtures for testing."""

import json
from pathlib import Path

# Sample progress state JSON
SAMPLE_PROGRESS_STATE = {
    "version": "1.0",
    "source_video": "test_video.mkv",
    "current_phase": "encoding",
    "crop_params": "140 140 0 0",
    "phases": {
        "extraction": {
            "status": "completed",
            "timestamp": "2026-02-23T10:00:00",
            "metadata": {
                "crop_detected": True,
                "original_resolution": "1920x1080",
                "cropped_resolution": "1920x800"
            }
        },
        "chunking": {
            "status": "completed",
            "timestamp": "2026-02-23T10:15:00",
            "chunks_count": 10
        },
        "optimization": {
            "status": "completed",
            "timestamp": "2026-02-23T10:30:00",
            "optimal_strategy": "slow+h265-aq"
        },
        "encoding": {
            "status": "in_progress",
            "timestamp": "2026-02-23T10:45:00",
            "completed": 5,
            "total": 10
        },
        "audio": {
            "status": "not_started"
        },
        "merge": {
            "status": "not_started"
        }
    },
    "chunks": {
        "chunk_001": {
            "strategies": {
                "slow+h265-aq": {
                    "status": "completed",
                    "attempts": [
                        {
                            "attempt_number": 1,
                            "crf": 20.0,
                            "vmaf_min": 93.2,
                            "vmaf_med": 96.1,
                            "ssim_min": 0.985,
                            "ssim_med": 0.990,
                            "psnr_min": 40.5,
                            "psnr_med": 42.3,
                            "metrics": {
                                "vmaf_min": 93.2,
                                "vmaf_med": 96.1,
                                "ssim_min": 0.985,
                                "ssim_med": 0.990,
                                "psnr_min": 40.5,
                                "psnr_med": 42.3
                            },
                            "success": False,
                            "file_path": None,
                            "file_size": None
                        },
                        {
                            "attempt_number": 2,
                            "crf": 18.0,
                            "vmaf_min": 95.3,
                            "vmaf_med": 97.8,
                            "ssim_min": 0.990,
                            "ssim_med": 0.995,
                            "psnr_min": 42.1,
                            "psnr_med": 44.2,
                            "metrics": {
                                "vmaf_min": 95.3,
                                "vmaf_med": 97.8,
                                "ssim_min": 0.990,
                                "ssim_med": 0.995,
                                "psnr_min": 42.1,
                                "psnr_med": 44.2
                            },
                            "success": True,
                            "file_path": None,
                            "file_size": None
                        }
                    ],
                    "final_crf": 18.0
                }
            }
        },
        "chunk_002": {
            "strategies": {
                "slow+h265-aq": {
                    "status": "completed",
                    "attempts": [
                        {
                            "attempt_number": 1,
                            "crf": 18.0,
                            "vmaf_min": 96.1,
                            "vmaf_med": 98.2,
                            "ssim_min": 0.992,
                            "ssim_med": 0.996,
                            "psnr_min": 43.2,
                            "psnr_med": 45.1,
                            "metrics": {
                                "vmaf_min": 96.1,
                                "vmaf_med": 98.2,
                                "ssim_min": 0.992,
                                "ssim_med": 0.996,
                                "psnr_min": 43.2,
                                "psnr_med": 45.1
                            },
                            "success": True,
                            "file_path": None,
                            "file_size": None
                        }
                    ],
                    "final_crf": 18.0
                }
            }
        }
    }
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

def get_sample_chunk_state() -> dict:
    """Get sample chunk state data.

    Returns:
        Dictionary representing chunk state
    """
    return SAMPLE_PROGRESS_STATE["chunks"]["chunk_001"]
