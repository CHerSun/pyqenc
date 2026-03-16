"""Mock metric results for testing."""

from pathlib import Path
import json

# Sample VMAF JSON output
SAMPLE_VMAF_JSON = {
    "version": "2.3.1",
    "frames": [
        {"frameNum": 0, "metrics": {"vmaf": 95.234, "psnr": 42.123, "ssim": 0.9876}},
        {"frameNum": 1, "metrics": {"vmaf": 96.123, "psnr": 43.456, "ssim": 0.9890}},
        {"frameNum": 2, "metrics": {"vmaf": 94.567, "psnr": 41.789, "ssim": 0.9865}},
        {"frameNum": 3, "metrics": {"vmaf": 97.234, "psnr": 44.123, "ssim": 0.9912}},
        {"frameNum": 4, "metrics": {"vmaf": 95.890, "psnr": 42.890, "ssim": 0.9885}},
    ],
    "pooled_metrics": {
        "vmaf": {
            "min": 94.567,
            "max": 97.234,
            "mean": 95.8096,
            "harmonic_mean": 95.8076
        }
    }
}

# Sample SSIM log output
SAMPLE_SSIM_LOG = """n:1 Y:0.987654 U:0.991234 V:0.989876 All:0.989588 (15.834)
n:2 Y:0.989012 U:0.992345 V:0.990987 All:0.990781 (16.345)
n:3 Y:0.986543 U:0.990123 V:0.988765 All:0.988477 (15.389)
n:4 Y:0.991234 U:0.993456 V:0.992109 All:0.992266 (17.123)
n:5 Y:0.988765 U:0.991876 V:0.990432 All:0.990358 (16.012)
"""

# Sample PSNR log output
SAMPLE_PSNR_LOG = """n:1 mse_avg:12.34 mse_y:11.23 mse_u:13.45 mse_v:12.89 psnr_avg:42.123 psnr_y:43.234 psnr_u:41.012 psnr_v:41.890
n:2 mse_avg:10.23 mse_y:9.12 mse_u:11.34 mse_v:10.78 psnr_avg:43.456 psnr_y:44.567 psnr_u:42.345 psnr_v:42.890
n:3 mse_avg:13.45 mse_y:12.34 mse_u:14.56 mse_v:13.90 psnr_avg:41.789 psnr_y:42.890 psnr_u:40.678 psnr_v:41.234
n:4 mse_avg:9.12 mse_y:8.01 mse_u:10.23 mse_v:9.67 psnr_avg:44.123 psnr_y:45.234 psnr_u:43.012 psnr_v:43.567
n:5 mse_avg:11.01 mse_y:9.90 mse_u:12.12 mse_v:11.56 psnr_avg:42.890 psnr_y:43.901 psnr_u:41.779 psnr_v:42.345
"""


def create_mock_vmaf_file(output_path: Path) -> Path:
    """Create a mock VMAF JSON file.
    
    Args:
        output_path: Path where to create the file
        
    Returns:
        Path to created file
    """
    output_path.write_text(json.dumps(SAMPLE_VMAF_JSON, indent=2))
    return output_path


def create_mock_ssim_file(output_path: Path) -> Path:
    """Create a mock SSIM log file.
    
    Args:
        output_path: Path where to create the file
        
    Returns:
        Path to created file
    """
    output_path.write_text(SAMPLE_SSIM_LOG)
    return output_path


def create_mock_psnr_file(output_path: Path) -> Path:
    """Create a mock PSNR log file.
    
    Args:
        output_path: Path where to create the file
        
    Returns:
        Path to created file
    """
    output_path.write_text(SAMPLE_PSNR_LOG)
    return output_path


def get_expected_vmaf_stats() -> dict[str, float]:
    """Get expected VMAF statistics from sample data.
    
    Returns:
        Dictionary with min, max, mean, median values
    """
    return {
        "min": 94.567,
        "max": 97.234,
        "mean": 95.8096,
        "median": 95.890
    }


def get_expected_ssim_stats() -> dict[str, float]:
    """Get expected SSIM statistics from sample data.
    
    Returns:
        Dictionary with min, max, mean, median values
    """
    values = [0.989588, 0.990781, 0.988477, 0.992266, 0.990358]
    return {
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
        "median": sorted(values)[len(values) // 2]
    }


def get_expected_psnr_stats() -> dict[str, float]:
    """Get expected PSNR statistics from sample data.
    
    Returns:
        Dictionary with min, max, mean, median values
    """
    values = [42.123, 43.456, 41.789, 44.123, 42.890]
    return {
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
        "median": sorted(values)[len(values) // 2]
    }
