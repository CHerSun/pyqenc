"""Unit tests for chain resolution and the generic combined-``-af`` executor.

Each test names the concrete bug it guards against and checks observable
behaviour — the ffmpeg invocations the executor issues (count, the ``-af``
strings, the ``output_file``) and the resolved chain's encode target — never
internal loop state. A **spy runner** replaces ``run_ffmpeg_async`` so no real
ffmpeg runs; it records every ``(cmd, output_file)`` and returns a canned,
successful :class:`FFmpegRunResult`.

Bugs guarded:

- Wrong invocation count for K measuring filters (must be K+1).
- A measurement pass measuring the wrong signal (must include all already-frozen
  fragments).
- ``last_output`` leaking from a finished filter to the next one.
- Wrong output extension (FLAC default / last-encode-wins).
- A downmix no-op producing a stray/doubled comma in ``-af``.
- An all-empty chain still emitting ``-af``.
- Filter-type logic leaking into the loop (a test-only two-pass filter must drive
  the same K+1 behaviour with no executor change).
"""
# CHerSun 2026

from collections.abc import Iterator

import pytest
from pydantic import BaseModel, ConfigDict

from pyqenc.app_config import ChainSpec, FilterInstance
from pyqenc.audio.chain import (
    FLAC_DEFAULT,
    ResolvedChain,
    _scale_bitrate,
    chain_output_path,
    execute_chain,
    resolve_chain,
)
from pyqenc.audio.filters import (
    _FILTER_REGISTRY,
    FilterStep,
    FilterType,
    register_filter,
)
from pyqenc.audio.layout import ChannelLayout
from pyqenc.constants import FFMPEG_ARG_AF
from pyqenc.utils.ffmpeg_runner import FFmpegRunResult
from pyqenc.utils.long_path import LongPath

_STEREO = ChannelLayout.parse("2.0")
_51     = ChannelLayout.parse("5.1")

# Canned stderr that the built-in two-pass filters can scrape, so a chain that
# includes real peaknorm/loudnorm resolves to its final fragment on pass 2.
_VOLUMEDETECT_STDERR = ["[Parsed_volumedetect] max_volume: -6.0 dB"]
_LOUDNORM_STDERR = [
    '[Parsed_loudnorm] {',
    '  "input_i" : "-18.0",',
    '  "input_tp" : "-2.0",',
    '  "input_lra" : "5.0",',
    '  "input_thresh" : "-28.0",',
    '  "target_offset" : "0.5"',
    "}",
]


class _SpyRunner:
    """Records every ffmpeg invocation and returns a canned result.

    Signature-compatible with ``run_ffmpeg_async`` for the two arguments the
    executor passes: ``cmd`` positionally and ``output_file`` by keyword.

    Attributes:
        calls: One ``(cmd, output_file)`` tuple per invocation, in order.
    """

    def __init__(self, stderr_per_call: list[list[str]] | None = None) -> None:
        self.calls: list[tuple[list, object]] = []
        self._stderr_per_call = stderr_per_call or []

    async def __call__(self, cmd, output_file=None, **_kwargs) -> FFmpegRunResult:
        idx = len(self.calls)
        self.calls.append((list(cmd), output_file))
        stderr = self._stderr_per_call[idx] if idx < len(self._stderr_per_call) else []
        return FFmpegRunResult(returncode=0, success=True, stderr_lines=stderr)

    def af_of(self, call_index: int) -> str | None:
        """Return the ``-af`` value of the recorded call, or ``None`` if absent."""
        cmd = self.calls[call_index][0]
        for i, arg in enumerate(cmd):
            if arg == FFMPEG_ARG_AF:
                return cmd[i + 1]
        return None


def _fi(type_id: str, **params) -> FilterInstance:
    """Build a validated config-side ``FilterInstance`` from raw params."""
    return FilterInstance(type=type_id, **params)  # type: ignore[arg-type]


def _resolved(name: str, palette: dict[str, FilterInstance], order: list[str]) -> ResolvedChain:
    """Resolve a chain from a palette and an ordered list of filter names."""
    return resolve_chain(ChainSpec(name=name, filters=order), palette)


@pytest.fixture
def source(tmp_path) -> LongPath:
    """A dummy source track path (never actually read — the runner is a spy)."""
    return LongPath(tmp_path) / "#02 lang=eng ch=5.1.mka"


@pytest.fixture
def out_dir(tmp_path) -> LongPath:
    """The dedicated audio output directory the executor writes into."""
    return LongPath(tmp_path) / "audio"


class TestInvocationCount:
    """K measuring filters ⇒ exactly K+1 ffmpeg invocations (Req 6.2, 6.4)."""

    @pytest.mark.asyncio
    async def test_k0_single_application_pass(self, source: LongPath, out_dir: LongPath) -> None:
        """No measuring filter ⇒ one application invocation (bug: needless passes)."""
        palette = {"dyn": _fi("dynaudnorm", framelen=150, gausssize=15, peak=0.9, maxgain=9.0, targetrms=0.0)}
        resolved = _resolved("normal", palette, ["dyn"])
        spy = _SpyRunner()

        await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        assert len(spy.calls) == 1

    @pytest.mark.asyncio
    async def test_k1_two_invocations(self, source: LongPath, out_dir: LongPath) -> None:
        """One two-pass filter ⇒ 1 measurement + 1 application (bug: wrong count)."""
        palette = {"peak": _fi("peaknorm", target_dbfs=-1.0)}
        resolved = _resolved("peak", palette, ["peak"])
        spy = _SpyRunner(stderr_per_call=[_VOLUMEDETECT_STDERR])

        await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        assert len(spy.calls) == 2
        # First call is a measurement (no output file), second is the application.
        assert spy.calls[0][1] is None
        assert spy.calls[1][1] is not None

    @pytest.mark.asyncio
    async def test_k2_three_invocations(self, source: LongPath, out_dir: LongPath) -> None:
        """Two two-pass filters ⇒ K+1 = 3 invocations (bug: wrong count on stacking)."""
        palette = {
            "peak": _fi("peaknorm", target_dbfs=-1.0),
            "loud": _fi("loudnorm", i=-16.0, tp=-1.5, lra=11.0),
        }
        resolved = _resolved("both", palette, ["peak", "loud"])
        spy = _SpyRunner(stderr_per_call=[_VOLUMEDETECT_STDERR, _LOUDNORM_STDERR])

        await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        assert len(spy.calls) == 3
        assert [c[1] for c in spy.calls] == [None, None, spy.calls[2][1]]


class TestMeasurementAfIncludesFrozenFragments:
    """Each measurement pass carries all already-finalized fragments (Req 6.2)."""

    @pytest.mark.asyncio
    async def test_second_filters_measurement_includes_first(self, source: LongPath, out_dir: LongPath) -> None:
        """peaknorm frozen fragment must precede loudnorm's analysis in the 2nd measure.

        Bug: measuring loudnorm on the raw signal instead of on the already-peak-
        normalised signal, giving a wrong measurement.
        """
        palette = {
            "peak": _fi("peaknorm", target_dbfs=-1.0),
            "loud": _fi("loudnorm", i=-16.0, tp=-1.5, lra=11.0),
        }
        resolved = _resolved("both", palette, ["peak", "loud"])
        spy = _SpyRunner(stderr_per_call=[_VOLUMEDETECT_STDERR, _LOUDNORM_STDERR])

        await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        # Call 0: peaknorm measurement — just "volumedetect".
        assert spy.af_of(0) == "volumedetect"
        # Call 1: loudnorm measurement — the FROZEN peaknorm fragment, then loudnorm.
        loud_measure_af = spy.af_of(1)
        assert loud_measure_af is not None
        assert loud_measure_af.startswith("volume=")           # frozen peaknorm result
        assert ",loudnorm=" in loud_measure_af                  # then loudnorm analysis
        assert "print_format=json" in loud_measure_af

    @pytest.mark.asyncio
    async def test_last_output_cleared_between_filters(self, source: LongPath, out_dir: LongPath) -> None:
        """A finished filter's measurement must not drive the next filter.

        loudnorm's pass-1 ``resolve`` gets ``last_output=None`` (fresh), not
        peaknorm's volumedetect result. If the executor leaked ``last_output``,
        loudnorm would wrongly try to scrape volumedetect stderr as its JSON and
        the loudnorm analysis pass would never be issued — we would see only 2
        calls, and the loudnorm measurement pass (call 1) would be missing its
        ``print_format=json`` analysis fragment.

        Bug: ``last_output`` accumulated/leaked across filters (Req 6.3).
        """
        palette = {
            "peak": _fi("peaknorm", target_dbfs=-1.0),
            "loud": _fi("loudnorm", i=-16.0, tp=-1.5, lra=11.0),
        }
        resolved = _resolved("both", palette, ["peak", "loud"])
        spy = _SpyRunner(stderr_per_call=[_VOLUMEDETECT_STDERR, _LOUDNORM_STDERR])

        await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        # Exactly K+1 = 3 calls proves loudnorm ran its own analysis pass with a
        # cleared last_output (it did not consume peaknorm's result).
        assert len(spy.calls) == 3
        assert "print_format=json" in (spy.af_of(1) or "")
        # The final application af has BOTH final fragments, neither an analysis one.
        final_af = spy.af_of(2) or ""
        assert final_af.startswith("volume=")
        assert ":linear=true" in final_af
        assert "print_format=json" not in final_af


class TestExtensionCorrectness:
    """Output extension: FLAC default, else last encode's extension (Req 8.3)."""

    @pytest.mark.asyncio
    async def test_flac_default_when_no_encode(self, source: LongPath, out_dir: LongPath) -> None:
        """No encode filter ⇒ .flac and no -b:a (bug: wrong ext / bitrate on FLAC)."""
        palette = {"dyn": _fi("dynaudnorm", framelen=150, gausssize=15, peak=0.9, maxgain=9.0, targetrms=0.0)}
        resolved = _resolved("normal", palette, ["dyn"])
        assert resolved.encode == FLAC_DEFAULT
        spy = _SpyRunner()

        out = await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        assert out.suffix == ".flac"
        assert out.name.endswith(" chain=normal.flac")
        # Output lands in the dedicated audio dir, not next to the source.
        assert out.parent == out_dir
        assert "-b:a" not in spy.calls[0][0]     # FLAC ignores bitrate
        assert "-c:a" in spy.calls[0][0]

    @pytest.mark.asyncio
    async def test_application_cmd_is_audio_only(self, source: LongPath, out_dir: LongPath) -> None:
        """The application cmd drops video/subs/data streams (bug: stray data stream).

        A regression that re-introduces a ``bin_data``/data stream in the output
        is caught here: the audio-only flags ``-vn -sn -dn`` must all be present.
        """
        palette = {"aac": _fi("encode", codec="aac", bitrate_per_channel="64k", extension="m4a")}
        resolved = _resolved("enc_only", palette, ["aac"])
        spy = _SpyRunner()

        await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        cmd = spy.calls[0][0]
        assert "-vn" in cmd and "-sn" in cmd and "-dn" in cmd

    @pytest.mark.asyncio
    async def test_last_encode_wins(self, source: LongPath, out_dir: LongPath) -> None:
        """Two encode filters ⇒ the last one's extension/codec wins (bug: first wins)."""
        palette = {
            "flac_enc": _fi("encode", codec="flac", bitrate_per_channel="0k", extension="flac"),
            "aac": _fi("encode", codec="aac", bitrate_per_channel="64k", extension="m4a"),
        }
        resolved = _resolved("dual", palette, ["flac_enc", "aac"])
        assert resolved.encode.extension == "m4a"
        spy = _SpyRunner()

        out = await execute_chain(resolved, source, _51, out_dir, runner=spy)

        assert out.name.endswith(" chain=dual.m4a")
        cmd = spy.calls[0][0]
        assert "aac" in cmd
        # Bitrate scaled by the 5.1 channel count: 64k * 6 = 384k.
        assert "-b:a" in cmd
        assert cmd[cmd.index("-b:a") + 1] == "384k"


class TestAfJoining:
    """The -af argument is a clean comma-join; all-empty chains omit it (Req 6.1, 6.6)."""

    @pytest.mark.asyncio
    async def test_downmix_noop_contributes_no_stray_comma(self, source: LongPath, out_dir: LongPath) -> None:
        """A no-op downmix (source ≤ target) must not add a comma or empty fragment.

        Bug: a downmix no-op contributing ``""`` produces a leading/doubled comma
        like ``,dynaudnorm=…`` or ``dynaudnorm=…,``.
        """
        palette = {
            "down": _fi("downmix", to="2.0"),   # source is stereo → no-op
            "dyn": _fi("dynaudnorm", framelen=150, gausssize=15, peak=0.9, maxgain=9.0, targetrms=0.0),
        }
        resolved = _resolved("norm2", palette, ["down", "dyn"])
        spy = _SpyRunner()

        await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        af = spy.af_of(0)
        assert af is not None
        assert af.startswith("dynaudnorm=")   # no leading comma
        assert ",," not in af
        assert not af.endswith(",")

    @pytest.mark.asyncio
    async def test_all_empty_chain_omits_af(self, source: LongPath, out_dir: LongPath) -> None:
        """A chain whose only filter is an encode (af="") must omit -af entirely.

        Bug: emitting ``-af ""`` (empty filter chain) which ffmpeg rejects.
        """
        palette = {"aac": _fi("encode", codec="aac", bitrate_per_channel="64k", extension="m4a")}
        resolved = _resolved("enc_only", palette, ["aac"])
        spy = _SpyRunner()

        await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        assert len(spy.calls) == 1
        assert "-af" not in spy.calls[0][0]

    @pytest.mark.asyncio
    async def test_downmix_active_then_dyn_joined_with_single_comma(self, source: LongPath, out_dir: LongPath) -> None:
        """An active downmix (5.1→2.0) then dynaudnorm join with exactly one comma."""
        palette = {
            "down": _fi("downmix", to="2.0", matrix="std"),
            "dyn": _fi("dynaudnorm", framelen=150, gausssize=15, peak=0.9, maxgain=9.0, targetrms=0.0),
        }
        resolved = _resolved("down_norm", palette, ["down", "dyn"])
        spy = _SpyRunner()

        await execute_chain(resolved, source, _51, out_dir, runner=spy)

        af = spy.af_of(0) or ""
        assert af.startswith("pan=stereo|")
        assert af.count(",dynaudnorm=") == 1
        assert ",," not in af


class TestExecutorHasNoFilterTypeBranch:
    """A test-only two-pass filter drives K+1 with no executor change (Req 6.5)."""

    @pytest.fixture
    def two_pass_type(self) -> Iterator[str]:
        """Register a throwaway two-pass filter type; unregister after the test."""
        test_id = "test_two_pass"

        class _Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

        @register_filter
        class _TwoPass(FilterType):
            type_id      = test_id
            params_model = _Params

            def resolve(
                self,
                layout: ChannelLayout,
                last_output: FFmpegRunResult | None,
            ) -> FilterStep:
                if last_output is None:
                    return FilterStep(af="astats", needs_pass=True, out_layout=layout)
                return FilterStep(af="volume=1.0", needs_pass=False, out_layout=layout)

        try:
            yield test_id
        finally:
            _FILTER_REGISTRY.pop(test_id, None)

    @pytest.mark.asyncio
    async def test_custom_two_pass_drives_kplus1(self, source: LongPath, out_dir: LongPath, two_pass_type: str) -> None:
        """A brand-new two-pass type gets its measurement + application with no loop edit.

        Bug: the executor special-casing known filter types instead of driving
        the generic ``resolve``/``FilterStep`` contract.
        """
        palette = {"tp": _fi(two_pass_type)}
        resolved = _resolved("custom", palette, ["tp"])
        spy = _SpyRunner(stderr_per_call=[["astats output"]])

        await execute_chain(resolved, source, _STEREO, out_dir, runner=spy)

        assert len(spy.calls) == 2               # K=1 ⇒ K+1
        assert spy.af_of(0) == "astats"          # measurement fragment
        assert spy.calls[0][1] is None           # measurement writes no file
        assert spy.af_of(1) == "volume=1.0"      # final measured fragment
        assert spy.calls[1][1] is not None       # application writes a file


class TestResolveChain:
    """Chain resolution computes the effective encode without a synthetic filter."""

    def test_no_encode_resolves_to_flac_default(self) -> None:
        """A chain with no encode filter resolves ``encode`` to FLAC_DEFAULT."""
        palette = {"dyn": _fi("dynaudnorm", framelen=150, gausssize=15, peak=0.9, maxgain=9.0, targetrms=0.0)}
        resolved = _resolved("n", palette, ["dyn"])
        assert resolved.encode == FLAC_DEFAULT
        # No synthetic FLAC filter is appended.
        assert len(resolved.filters) == 1
        assert resolved.filters[0].type == "dynaudnorm"

    def test_encode_filter_sets_encode_target(self) -> None:
        """An encode filter's params become the resolved encode target."""
        palette = {"aac": _fi("encode", codec="aac", bitrate_per_channel="64k", extension="m4a")}
        resolved = _resolved("a", palette, ["aac"])
        assert resolved.encode.codec == "aac"
        assert resolved.encode.extension == "m4a"


class TestScaleBitrate:
    """Bitrate scaling multiplies the per-channel rate by the channel count."""

    def test_k_suffix_scaled(self) -> None:
        assert _scale_bitrate("64k", 2) == "128k"

    def test_k_suffix_scaled_51(self) -> None:
        assert _scale_bitrate("64k", 6) == "384k"

    def test_m_suffix_scaled(self) -> None:
        assert _scale_bitrate("0.5m", 2) == "1000k"


class TestChainOutputPath:
    """Output naming follows ``<stem> chain=<name>.<ext>`` (Req 8.1)."""

    def test_name_format(self, tmp_path) -> None:
        source = LongPath(tmp_path) / "movie track1.mka"
        output_dir = LongPath(tmp_path) / "audio"
        out = chain_output_path(source, "night", "flac", output_dir)
        assert out.name == "movie track1 chain=night.flac"
        # The output lives in the supplied dedicated dir, NOT next to the source.
        assert out.parent == output_dir
