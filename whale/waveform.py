"""Stable contracts between the link protocol and physical-layer modes."""

from dataclasses import dataclass, field, replace
from importlib import import_module
from typing import Mapping, Protocol, Sequence, TypedDict, runtime_checkable

import numpy as np


class DecodeResult(TypedDict, total=False):
    """The canonical shape of a mode's `decode()` return value.

    Every mode has grown its own dict by accretion, and three different
    spellings of "receive SNR" (`snr_db`, `carrier_snr_db`, `tone_snr_db`)
    and two of "frequency offset" (`freq_offset_hz`, `cfo_hz`) are in use by
    shipped modes and read by shipped callers (link_receiver.py,
    streaming.py, qualification.py). This TypedDict is documentation and a
    mypy/IDE aid, not an enforced schema -- no mode is required to populate
    every key, and no existing key has been renamed or removed to match it.

    `snr_db` and `freq_offset_hz` are the canonical spellings going forward.
    A mode that only had the older spelling keeps emitting it (callers still
    read it) and, where `canonicalize_result` below is used, also gets the
    canonical key as an additive alias.
    """

    payload: bytes | None
    crc_ok: bool
    confidence: float
    decoded_length: int
    start_index: int | None
    end_index: int | None
    sync_end_index: int | None
    start_sample: int | None
    snr_db: float  # canonical: receive SNR, however the mode estimates it
    freq_offset_hz: float  # canonical: residual carrier/clock frequency offset
    decode_cpu_seconds: float
    failure: str
    received_crc32: int
    computed_crc32: int


def canonicalize_result(result: dict) -> dict:
    """Add the canonical `snr_db`/`freq_offset_hz` keys, in place, if absent.

    Purely additive: never overwrites a key the mode already set, never
    removes or renames anything, so every existing caller keeps reading
    exactly what it read before. Modes call this at the outer decode()
    boundary; it is not run automatically because not every mode's raw
    result is a plain dict worth mutating (some diagnostics-only paths).
    """
    if "snr_db" not in result:
        tone = result.get("tone_snr_db")
        if tone is not None and np.isfinite(tone):
            result["snr_db"] = float(tone)
        else:
            carrier = result.get("carrier_snr_db")
            if carrier is not None:
                finite = np.asarray(carrier, dtype=float)
                finite = finite[np.isfinite(finite)]
                if finite.size:
                    result["snr_db"] = float(np.median(finite))
            if "snr_db" not in result:
                channel = result.get("channel_snr_db")
                if channel is not None and np.isfinite(channel):
                    result["snr_db"] = float(channel)
    if "freq_offset_hz" not in result:
        cfo = result.get("cfo_hz")
        if cfo is not None and np.isfinite(cfo):
            result["freq_offset_hz"] = float(cfo)
    return result


@runtime_checkable
class WaveformMode(Protocol):
    """One negotiable physical-layer capability.

    Implementations own modulation, synchronization, framing/coding, timing,
    and decode diagnostics.  The link layer only deals in packets and these
    operations; it does not need to know which waveform carries them.
    """

    name: str
    mode_id: int
    chunk_size: int
    confidence_threshold: float
    # Lowest to highest carrier/tone centre frequency, in Hz.  Not the
    # occupied bandwidth: that is a measurement and lives in docs/MODES.md.
    band_hz: tuple[float, float]
    # The two facts that cannot be derived from code, stated once here in the
    # wording docs/MODES.md uses: "49-carrier 32-QAM OFDM", "QC-LDPC 3/4".
    modulation: str
    fec: str
    # Encoded arrays and receive-buffer indices deliberately use different
    # clocks: radio I/O/TX stays at 48 kHz while the shared RX front end
    # supplies every decoder at 12 kHz.
    tx_sample_rate: int
    rx_sample_rate: int
    # Declared capabilities, not duck-typed probes: link_receiver.py and
    # streaming.py read these directly rather than getattr()-guessing
    # through a mode's private structure. `ModeDescription` below defaults
    # both to "no capability", so a new mode need only override the one it
    # actually has.
    supports_frequency_hint: bool
    # None, or the OFDM PHY streaming.py's OfdmReceiver can run against.
    streaming_phy: object | None

    def encode(self, payload: bytes) -> np.ndarray: ...

    # Callers may pass mode-specific acquisition hints -- link_receiver.py
    # passes freq_hint_hz, streaming.py passes acquisition -- so every mode
    # must accept and may ignore arbitrary keyword options.
    def decode(self, audio: np.ndarray, **kwargs) -> dict: ...

    def airtime(self, payload_len: int) -> float: ...

    def describe(self) -> str: ...


@dataclass(frozen=True)
class ModeRegistry:
    """Ordered negotiable modes plus the robust control-plane mode."""

    modes: Sequence[WaveformMode]
    control: WaveformMode

    def __post_init__(self):
        modes = tuple(self.modes)
        if not modes:
            raise ValueError("at least one waveform mode is required")
        by_id = {mode.mode_id: mode for mode in modes}
        if len(by_id) != len(modes):
            raise ValueError("waveform mode IDs must be unique")
        if self.control.mode_id not in by_id:
            raise ValueError("control mode must be present in modes")
        object.__setattr__(self, "modes", modes)
        object.__setattr__(self, "by_id", by_id)

    by_id: Mapping[int, WaveformMode] = field(init=False, repr=False)

    @property
    def supported_ids(self):
        return tuple(mode.mode_id for mode in self.modes)

    def resolve(self, mode_id: int) -> WaveformMode:
        return self.by_id.get(mode_id, self.control)

    def step(self, current: WaveformMode, direction: int):
        try:
            index = next(i for i, mode in enumerate(self.modes)
                         if mode.mode_id == current.mode_id)
        except StopIteration:
            return None
        new_index = index + direction
        if not 0 <= new_index < len(self.modes):
            return None
        return self.modes[new_index]


def _policy_for(mode_id: int) -> str:
    """The channel policy a mode is declared on, or "?" if undeclared.

    Imported lazily: whale.mode_qualification imports ModeRegistry from here.
    """
    from .mode_qualification import MANIFEST
    for entry in MANIFEST:
        if entry.mode_id == mode_id:
            return entry.policy
    return "?"


def format_mode(mode: WaveformMode) -> str:
    """The one-line description every mode renders through.

    Built here rather than per mode so the 13 modes cannot drift into 13
    formats, which is what happened to the ad-hoc describe() functions this
    replaces.
    """
    from .framing import AIR_HEADER_BYTES
    # Derived here, from the same airtime() and chunk_size the encoder uses,
    # so the figures cannot disagree with the waveform that produced them.
    frame_seconds = mode.airtime(AIR_HEADER_BYTES + mode.chunk_size)
    net_bps = 8 * mode.chunk_size / frame_seconds
    lo, hi = mode.band_hz
    band = f"{lo:.0f} Hz" if lo == hi else f"{lo:.0f}-{hi:.0f} Hz"
    return (f"{mode.name} ({mode.mode_id}) {_policy_for(mode.mode_id)}  "
            f"{band}  {mode.modulation}  {mode.fec}  "
            f"{mode.chunk_size} B/{frame_seconds:.3f} s = {net_bps:.0f} bit/s")


class ModeDescription:
    """Mixin giving every mode the shared `describe()` and capability defaults.

    Every shipped mode inherits this (directly, or through a family base
    like `ScFdeMode` or a sibling mode like `Vf14Mode`), so these two class
    attributes are the "no capability" default `WaveformMode` promises:
    a mode that ignores them gets no frequency hint and no streaming
    receiver rather than an AttributeError or a silent `getattr` default
    duplicated at every call site. A mode that has the capability overrides
    the attribute -- see hf6-hf9's `supports_frequency_hint` field and
    hf7/hf8/hf9's `streaming_phy` property.
    """

    supports_frequency_hint = False
    streaming_phy = None

    def describe(self) -> str:
        return format_mode(self)


class _SettlingHeadVariant:
    """One mode wearing a settling head of a length it was not shipped with.

    A wrapper rather than a field on every mode: the head buys the PTT ramp
    and the receiver's AGC their settling time and nothing on the air path
    is allowed to depend on it, so the modes stay ignorant of it varying
    and only a bench sweep ever asks for one of these.
    """

    def __init__(self, mode, encode, airtime, head_seconds):
        self._mode = mode
        self._encode = encode
        self._airtime = airtime
        self.head_seconds = head_seconds

    def __getattr__(self, name):
        return getattr(self._mode, name)

    def __repr__(self) -> str:
        return f"<{self._mode.name} head={self.head_seconds:g}s>"

    def encode(self, payload: bytes) -> np.ndarray:
        return self._encode(bytes(payload))

    def decode(self, audio, **kwargs) -> dict:
        return self._mode.decode(audio, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self._airtime(payload_len)

    def describe(self) -> str:
        return format_mode(self)


#: `dataclasses.replace` on an OFDM49Mode re-runs a `__post_init__` that
#: builds every reference constellation, so a sweep that re-encodes per
#: frame must not pay for it per frame.
_OFDM_VARIANTS: dict = {}


def with_settling_head(mode: WaveformMode, head_seconds: float):
    """`mode`, but keying a settling head of `head_seconds` instead of its own.

    The single entry point a head-length sweep goes through: `encode()`
    emits the new head and `airtime()` accounts for it, while acquisition,
    decoding and every negotiated property stay exactly as shipped.  Only
    the HF modes that have a head can be asked; the rest raise, because
    silently returning a mode with no head would make a sweep's zero point
    indistinguishable from a mode that was never in it.
    """
    from whale.phy import hc0, hc1w, hr0
    phys = {"hr0": hr0, "hc0": hc0, "hc1w": hc1w}
    if mode.name in phys:
        phy = phys[mode.name]
        delta = ((phy.settling_head_samples(head_seconds)
                  - phy.settling_head_samples()) / phy.SAMPLE_RATE)
        return _SettlingHeadVariant(
            mode,
            lambda payload: phy.modulate(payload, head_seconds),
            lambda payload_len: mode.airtime(payload_len) + delta,
            head_seconds)

    if mode.name not in ("hf6", "hf7", "hf8", "hf9"):
        raise ValueError(f"{mode.name} has no settling head to resize")
    module = import_module(f"whale.modes.{mode.name}_mode")
    base = getattr(module, f"{mode.name.upper()}_PHY")
    key = (mode.name, float(head_seconds))
    if key not in _OFDM_VARIANTS:
        _OFDM_VARIANTS[key] = replace(base, head_seconds=head_seconds)
    variant = _OFDM_VARIANTS[key]
    return _SettlingHeadVariant(
        mode, variant.modulate,
        lambda payload_len: variant.keying_seconds(), head_seconds)
