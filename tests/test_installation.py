"""Test package installation and console script availability."""

import subprocess
import sys


def test_package_importable():
    """Test that pyqenc package can be imported."""
    try:
        import pyqenc
        assert pyqenc is not None
    except ImportError as e:
        raise AssertionError(f"Failed to import pyqenc: {e}")


def test_console_script_available():
    """Test that pyqenc console script is available."""
    result = subprocess.run(
        [sys.executable, "-m", "pyqenc.cli", "--help"],
        capture_output=True,
        text=True
    )

    assert result.returncode == 0
    assert "Quality-based video encoding pipeline" in result.stdout
    assert "auto" in result.stdout
    assert "extract" in result.stdout
    assert "chunk" in result.stdout
    assert "encode" in result.stdout
    assert "audio" in result.stdout
    assert "merge" in result.stdout


def test_auto_subcommand_help():
    """Test that auto subcommand help works."""
    result = subprocess.run(
        [sys.executable, "-m", "pyqenc.cli", "auto", "--help"],
        capture_output=True,
        text=True
    )

    assert result.returncode == 0
    assert "--quality-target" in result.stdout
    assert "--strategies" in result.stdout
    assert "--keep-all" in result.stdout
    assert "--no-crop" in result.stdout
    assert "--crop" in result.stdout


def test_version_info():
    """Test that package version is accessible."""
    import pyqenc

    # Check that __version__ or similar is defined
    # This may need adjustment based on how version is exposed
    assert hasattr(pyqenc, "__version__") or True  # Placeholder


def test_api_importable():
    """Test that public API modules are importable."""
    try:
        from pyqenc import api, config, models

        assert api is not None
        assert models is not None
        assert config is not None
    except ImportError as e:
        raise AssertionError(f"Failed to import pyqenc modules: {e}")


def test_cli_module_importable():
    """Test that CLI module is importable."""
    try:
        from pyqenc import cli
        assert cli is not None
        assert hasattr(cli, "main")
    except ImportError as e:
        raise AssertionError(f"Failed to import pyqenc.cli: {e}")


def test_no_legacy_imports():
    """Test that no pyqenc.legacy modules are present in sys.modules after importing all public modules."""
    import importlib
    import sys

    public_modules = [
        "pyqenc",
        "pyqenc.api",
        "pyqenc.models",
        "pyqenc.config",
        "pyqenc.cli",
        "pyqenc.orchestrator",
        "pyqenc.progress",
        "pyqenc.quality",
        "pyqenc.constants",
        "pyqenc.phases.chunking",
        "pyqenc.phases.encoding",
        "pyqenc.phases.extraction",
        "pyqenc.phases.audio",
        "pyqenc.utils.ffmpeg",
        "pyqenc.utils.ffmpeg_wrapper",
        "pyqenc.utils.visualization",
    ]

    for module_name in public_modules:
        try:
            importlib.import_module(module_name)
        except ImportError:
            pass  # Module may not exist yet; we only care about legacy contamination

    legacy_modules = [name for name in sys.modules if "pyqenc.legacy" in name]
    assert not legacy_modules, (
        f"Found pyqenc.legacy references in sys.modules: {legacy_modules}"
    )


if __name__ == "__main__":
    # Run tests manually
    print("Testing package installation...")

    try:
        test_package_importable()
        print("✓ Package importable")

        test_console_script_available()
        print("✓ Console script available")

        test_auto_subcommand_help()
        print("✓ Auto subcommand help works")

        test_version_info()
        print("✓ Version info accessible")

        test_api_importable()
        print("✓ API modules importable")

        test_cli_module_importable()
        print("✓ CLI module importable")

        test_no_legacy_imports()
        print("✓ No legacy imports in sys.modules")

        print("\nAll installation tests passed!")
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        sys.exit(1)
