"""Clean paired-audio sessions; synthetic ID 240 is test-only, not advertised.

Run from the repository with the test dependencies installed. This opens
localhost sockets and replaces sound cards only. Airtime is summed transmitted
audio, not wall-clock or a noisy-channel retry-latency measurement.
"""
import sys, json
from dataclasses import replace
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from support.audio_link import run_audio_session
from whale.waveform import ModeRegistry
from whale.policy import HF
from whale.modes.hc0_mode import HC0
from experiments.hr0_fast_control.legacy_hr0_mode import HR0
from experiments.hr0_fast_control.candidate import FAST16, FAST32, MARGIN32
out = []
for raw in [HR0, FAST16, FAST32, MARGIN32]:
    control = raw if raw is HR0 else replace(raw, mode_id=240)
    registry = ModeRegistry((control, HC0), control)
    result = run_audio_session(bytes(range(128)), bytes(range(127,-1,-1)), mode_registry=registry, policy=HF)
    row = dict(mode=raw.name, setup_airtime=result.setup_airtime, transfer_airtime=result.transfer_airtime, disconnect_airtime=result.disconnect_airtime)
    out.append(row)
    print(row, flush=True)
Path(__file__).parent.joinpath('results/session_smoke.json').write_text(json.dumps(out, indent=2)+'\n')
