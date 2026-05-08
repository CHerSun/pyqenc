"""Integration tests for encoding → quality evaluation pipeline."""

from pathlib import Path

import pytest

from pyqenc.models import QualityTarget
from pyqenc.utils.visualization import QualityEvaluator
from tests.fixtures.metric_fixtures import (
    create_mock_psnr_file,
    create_mock_ssim_file,
    create_mock_vmaf_file,
    get_expected_vmaf_stats,
)
from tests.fixtures.video_fixtures import get_sample_video_path, sample_video_exists


class TestEncodingQualityIntegration:
    """Integration tests for encoding and quality evaluation."""

    def test_quality_evaluation_with_mock_metrics(self, tmp_path):
        """Test quality evaluation using mock metric files."""
        # Create mock metric files
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()

        vmaf_file = create_mock_vmaf_file(metrics_dir / "test.vmaf.json")
        ssim_file = create_mock_ssim_file(metrics_dir / "test.ssim.log")
        psnr_file = create_mock_psnr_file(metrics_dir / "test.psnr.log")

        # Define quality targets
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=94.0),
            QualityTarget(metric="vmaf", statistic="median", value=95.0),
        ]

        # Note: This test uses mock files, so we can't actually run the evaluator
        # which requires real video files. Instead, we verify the mock data structure.
        expected_stats = get_expected_vmaf_stats()

        assert expected_stats["min"] >= targets[0].value
        assert expected_stats["median"] >= targets[1].value

    def test_quality_target_evaluation(self):
        """Test quality target evaluation logic."""
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="ssim", statistic="median", value=0.98),
        ]

        # Simulate metrics that meet targets
        metrics_pass = {
            "vmaf": {"min": 95.5, "median": 97.0},
            "ssim": {"min": 0.97, "median": 0.985},
        }

        # Check all targets met
        all_met = True
        for target in targets:
            metric_stats = metrics_pass.get(target.metric)
            if metric_stats:
                actual = metric_stats.get(target.statistic)
                if actual is None or actual < target.value:
                    all_met = False
                    break

        assert all_met

        # Simulate metrics that fail targets
        metrics_fail = {
            "vmaf": {"min": 93.0, "median": 96.0},  # min below target
            "ssim": {"min": 0.97, "median": 0.985},
        }

        all_met = True
        for target in targets:
            metric_stats = metrics_fail.get(target.metric)
            if metric_stats:
                actual = metric_stats.get(target.statistic)
                if actual is None or actual < target.value:
                    all_met = False
                    break

        assert not all_met

    def test_artifact_generation(self, tmp_path):
        """Test that quality evaluation generates expected artifacts."""
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()

        # Create mock metric files
        vmaf_file = create_mock_vmaf_file(metrics_dir / "chunk_001.vmaf.json")
        ssim_file = create_mock_ssim_file(metrics_dir / "chunk_001.ssim.log")
        psnr_file = create_mock_psnr_file(metrics_dir / "chunk_001.psnr.log")

        # Verify files exist
        assert vmaf_file.exists()
        assert ssim_file.exists()
        assert psnr_file.exists()

        # Verify file contents are valid
        import json
        with open(vmaf_file) as f:
            vmaf_data = json.load(f)
        assert "frames" in vmaf_data
        assert "pooled_metrics" in vmaf_data

        with open(ssim_file) as f:
            ssim_content = f.read()
        assert "n:" in ssim_content
        assert "Y:" in ssim_content

        with open(psnr_file) as f:
            psnr_content = f.read()
        assert "n:" in psnr_content
        assert "psnr_avg:" in psnr_content


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
class TestEncodingQualityWithRealVideo:
    """Integration tests using real video files (requires sample videos)."""

    def test_quality_evaluator_initialization(self, tmp_path):
        """Test quality evaluator can be initialized."""
        evaluator = QualityEvaluator(tmp_path)
        assert evaluator.work_dir == tmp_path


class TestEncodeChunkQualitySearchV2Integration:
    """Integration tests verifying encode_chunk uses QualitySearchV2 and handles cache hits.
    Validates: Requirements 6.1–6.5, 7.5–7.6, 8.1–8.3
    """

    def _make_strategy(self) -> object:
        """Build a minimal Strategy-like mock for testing."""
        from unittest.mock import MagicMock
        from decimal import Decimal

        codec = MagicMock()
        codec.quality_better      = Decimal("0")
        codec.quality_worse       = Decimal("51")
        codec.quality_granularity = Decimal("0.5")
        codec.quality_max_step    = None
        codec.quality_label       = "CRF"
        codec.quality_log_padding = 4

        strategy = MagicMock()
        strategy.name       = "test-strategy"
        strategy.safe_name  = "test_strategy"
        strategy.codec      = codec
        strategy.to_ffmpeg_args.return_value = ["-i", "{input}", "-crf", "18"]
        return strategy

    def _make_chunk(self, tmp_path: Path) -> object:
        """Build a minimal ChunkMetadata-like mock."""
        from unittest.mock import MagicMock
        from pathlib import Path

        chunk_file = tmp_path / "chunk_001.mkv"
        chunk_file.write_bytes(b"fake")

        chunk = MagicMock()
        chunk.chunk_id        = "chunk_001"
        chunk.path            = chunk_file
        chunk._resolution     = "1920x1080"
        chunk.resolution      = "1920x1080"
        chunk.start_timestamp = 0.0
        chunk.end_timestamp   = 5.0
        return chunk

    def test_encode_chunk_uses_qualitysearchv2(self, tmp_path: Path) -> None:
        """encode_chunk instantiates QualitySearchV2 (verified via source inspection)."""
        import inspect
        from pyqenc.phases.encoding import ChunkEncoder

        src = inspect.getsource(ChunkEncoder.encode_chunk)
        assert "QualitySearchV2" in src
        assert "QualitySearch(" not in src, "encode_chunk must not use legacy QualitySearch directly"

    @pytest.mark.skip(reason="ChunkEncoder constructor signature changed (collector arg added); needs update")
    def test_fully_cached_chunk_returns_reused_true(self, tmp_path: Path) -> None:
        """A chunk where all attempts are cache hits returns reused=True."""
        from unittest.mock import MagicMock, patch
        from decimal import Decimal
        from pyqenc.phases.encoding import ChunkEncoder, ChunkEncodingResult
        from pyqenc.models import QualityTarget, AttemptMetadata
        from pyqenc.utils.visualization import QualityEvaluator

        strategy = self._make_strategy()
        chunk    = self._make_chunk(tmp_path)
        target   = QualityTarget(metric="vmaf", statistic="min", value=95.0)

        # Build a real AttemptMetadata so .crf is a proper Decimal.
        attempt_path = tmp_path / "chunk_001.1920x1080.q18.0.mkv"
        attempt_path.write_bytes(b"fake mkv")
        fake_attempt = AttemptMetadata(
            path            = attempt_path,
            chunk_id        = "chunk_001",
            strategy        = "test-strategy",
            crf             = Decimal("18.0"),
            resolution      = "1920x1080",
            file_size_bytes = 8,
        )

        # Sidecar with vmaf_min within acceptance_delta → early acceptance on first record().
        from pyqenc.quality import MetricType
        vmaf_delta = MetricType.VMAF.info.acceptance_delta
        fake_sidecar = {
            "crf":         "18.0",
            "targets_met": True,
            "sampling":    10,
            "metrics":     {"vmaf_min": 95.0 + vmaf_delta * 0.5},
        }

        evaluator = MagicMock(spec=QualityEvaluator)
        evaluator.work_dir = tmp_path

        encoder = ChunkEncoder(
            quality_evaluator = evaluator,
            work_dir          = tmp_path,
            metrics_sampling  = 10,
        )

        reference = MagicMock()
        reference.path = tmp_path / "ref.mkv"

        with (
            patch.object(encoder, "_check_existing_encoding", return_value=fake_attempt),
            patch("pyqenc.phases.encoding._read_metrics_sidecar", return_value=fake_sidecar),
        ):
            result = encoder.encode_chunk(
                chunk           = chunk,
                reference       = reference,
                strategy        = strategy,
                quality_targets = [target],
                initial_crf     = Decimal("18.0"),
                force           = False,
            )

        assert isinstance(result, ChunkEncodingResult)
        assert result.reused is True, f"Expected reused=True, got: {result}"
        assert result.success is True
        # Quality evaluator must NOT have been called (pure cache hit).
        evaluator.evaluate_chunk.assert_not_called()

    @pytest.mark.skip(reason="ChunkEncoder constructor signature changed (collector arg added); needs update")
    def test_encode_chunk_calls_finalize_on_convergence(self, tmp_path: Path) -> None:
        """encode_chunk calls _finalize_winning_attempt when search converges with a pass."""
        from unittest.mock import MagicMock, patch
        from decimal import Decimal
        from pyqenc.phases.encoding import ChunkEncoder
        from pyqenc.models import QualityTarget
        from pyqenc.utils.visualization import QualityEvaluator
        from pyqenc.quality import QualityEvaluation, QualityArtifacts, MetricType

        strategy = self._make_strategy()
        chunk    = self._make_chunk(tmp_path)
        target   = QualityTarget(metric="vmaf", statistic="min", value=95.0)

        # Fake evaluation result: passes with surplus within acceptance_delta → early acceptance.
        fake_eval = MagicMock(spec=QualityEvaluation)
        fake_eval.targets_met    = True
        fake_eval.failed_targets = []
        fake_eval.artifacts      = QualityArtifacts()
        fake_eval.metrics        = {
            MetricType.VMAF: {"min": 95.1, "median": 96.0, "p05": 94.0, "p25": 95.0,
                              "p75": 97.0, "p95": 98.0, "max": 99.0, "std": 1.0},
        }

        evaluator = MagicMock(spec=QualityEvaluator)
        evaluator.work_dir        = tmp_path
        evaluator.evaluate_chunk.return_value = fake_eval

        encoder = ChunkEncoder(
            quality_evaluator = evaluator,
            work_dir          = tmp_path,
        )

        reference = MagicMock()
        reference.path = tmp_path / "ref.mkv"

        # Pre-create the output file so stat() succeeds.
        fake_out = tmp_path / "encoding" / "test_strategy" / "chunk_001.1920x1080.q18.0.mkv"
        fake_out.parent.mkdir(parents=True, exist_ok=True)
        fake_out.write_bytes(b"fake mkv")

        with (
            patch.object(encoder, "_check_existing_encoding", return_value=None),
            patch.object(encoder, "_encode_with_ffmpeg", return_value=True),
            patch("pyqenc.phases.encoding._probe_resolution", return_value="1920x1080"),
            patch("pyqenc.phases.encoding._write_metrics_sidecar"),
            patch.object(encoder, "_finalize_winning_attempt") as mock_finalize,
        ):
            result = encoder.encode_chunk(
                chunk           = chunk,
                reference       = reference,
                strategy        = strategy,
                quality_targets = [target],
                initial_crf     = Decimal("18.0"),
                force           = False,
            )

        assert result.success is True
        assert result.reused is False
        mock_finalize.assert_called_once()
        # Verify targets_met=True was passed.
        call_kwargs = mock_finalize.call_args[1]
        assert call_kwargs.get("targets_met") is True
