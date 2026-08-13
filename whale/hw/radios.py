"""Which radios are on this bench, and how to reach each one.

This is the single place that knows bench-specific facts -- device names, USB
IDs, CI-V addresses. audio_io.py and ptt.py hold only the mechanism (find a
WASAPI device, key a radio); every test and sweep script selects a radio by
name from RADIOS here.

To add a radio, append one Radio to RADIOS: give it the substring Windows
shows for its sound card and a zero-argument callable that opens its PTT.

Copied from radiomodem's shark/hw/radios.py (unchanged).
"""

from dataclasses import dataclass
from functools import partial
from typing import Callable

from . import audio_io
from . import ptt

# Both Icoms use the same generic "USB Audio CODEC" chip and are told apart
# only by the model name Windows prefixes to it ("IC-705 (USB Audio CODEC )"
# vs "IC-7300 (2- USB Audio CODEC )"). Match on the model, never on the "2-"
# enumeration-order prefix, which moves when devices are replugged.
IC705_AUDIO = "IC-705"
IC7300_AUDIO = "IC-7300"
HT_AUDIO = "USB Audio Device"

# The Digirig has no distinguishing USB ID worth probing, so its port stays
# pinned here; the Icoms are discovered by USB ID inside ptt.py.
HT_PTT_PORT = "COM5"


@dataclass(frozen=True)
class Radio:
    """One radio: a sound card to match, and a way to key it."""

    name: str
    description: str
    audio_name: str
    open_ptt: Callable[[], object]

    def devices(self):
        """Returns (output_device_index, input_device_index) for this radio."""
        return (
            audio_io.find_device(self.audio_name, "output"),
            audio_io.find_device(self.audio_name, "input"),
        )

    def ptt(self):
        """Opens and returns this radio's PTT control."""
        return self.open_ptt()


RADIOS = {
    radio.name: radio
    for radio in [
        Radio(
            name="ic705",
            description="IC-705 (VHF), CI-V PTT over its USB CDC port",
            audio_name=IC705_AUDIO,
            open_ptt=partial(
                ptt.open_icom_ptt, ptt.IC705_USB_ID, "IC-705", ptt.IC705_DEFAULT_ADDR
            ),
        ),
        Radio(
            name="ic7300",
            description="IC-7300 (HF), CI-V PTT over its CP210x USB-UART bridge",
            audio_name=IC7300_AUDIO,
            open_ptt=partial(
                ptt.open_icom_ptt, ptt.IC7300_USB_ID, "IC-7300", ptt.IC7300_DEFAULT_ADDR
            ),
        ),
        Radio(
            name="ht",
            description="HT via a Digirig-style interface, PTT on RTS",
            audio_name=HT_AUDIO,
            open_ptt=partial(ptt.LinePtt, HT_PTT_PORT, line="rts"),
        ),
    ]
}
