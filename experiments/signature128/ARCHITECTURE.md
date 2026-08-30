# Lead-format architecture

The first HF format is implemented in `whale.modes.hf_lead` and composed by
the HC0/HC1 mode adapters. Each HF mode declares its `lead_label`; the link
uses the common detector to order decoder attempts without making the hint
authoritative. Adaptive duration remains stored in seconds.

HF and FM should not be forced to share a lead waveform. They should share a
small link-facing contract while channel policy selects the implementation.

```python
class LeadFormat(Protocol):
    name: str
    format_id: int
    tx_sample_rate: int
    rx_sample_rate: int

    def encode(self, mode_id: int, seconds: float) -> np.ndarray: ...
    def candidates(self, audio: np.ndarray) -> Sequence[LeadCandidate]: ...
    def measure(self, audio: np.ndarray, candidate: LeadCandidate) -> float: ...
```

A `LeadCandidate` contains at least the indicated waveform mode, proposed
frame-start sample, confidence and optional carrier-offset estimate. It is a
hint, not authenticated protocol state. The receive loop tries candidates in
rank order and accepts one only after that mode's checked frame validates.

When the FM format is added, `ChannelPolicy` should own an explicit
`LeadFormat`, alongside its mode registry:

```text
HF policy -> HF repeated MFSK signature -> HC0 / HC1 candidates
FM policy -> future FM lead format      -> CPFSK / VF3 / future candidates
```

The link continues to store adaptive leading loss in seconds. It asks the
selected format to quantize that duration, so neither link timing feedback nor
individual payload modes know the signature's block size. This preserves the
existing per-direction adaptation and permits HF and FM to use different
minimum durations, modulation, label alphabets and timing resolution.

Compatibility belongs to connection negotiation, not waveform guessing. A
future connection format should advertise the lead-format ID and version when
more than the current fixed HF mapping exists. The present implementation
keeps fallback body acquisition, so an erased or wrong signature cannot bypass
the checked frame decoder.
