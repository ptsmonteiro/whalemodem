"""One bounded longer-symbol candidate at matched three-dB-offset points."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
import subprocess
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from experiments.hr0_fast_control.candidate import MARGIN32
from experiments.hr0_fast_control.screen import run, bandwidth, payload_for
from whale.modes.hc0_mode import HC0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--trials', type=int, default=32)
    p.add_argument('--seed', type=int, default=930600)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--presets', nargs='+', default=['awgn', 'mid_latitude_quiet', 'mid_latitude_moderate', 'mid_latitude_disturbed'])
    p.add_argument('--watterson-control-snr', type=float, default=-6)
    p.add_argument('--awgn-control-snr', type=float, default=-9)
    a=p.parse_args()
    output=dict(git_revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        sources={p.name:p.read_text() for p in Path(__file__).parent.glob('*.py')},
        method='Actual short ACK at three dB below full HC0 DATA; paired seeds, canonical channel factory. No known timing supplied.',
        errors=0, master_seed=a.seed, trials=a.trials,
        presets=a.presets, watterson_control_snr=a.watterson_control_snr, awgn_control_snr=a.awgn_control_snr,
        bandwidth=[bandwidth(MARGIN32.encode(payload_for(MARGIN32,a.seed+i))) for i in range(5)],rows=[])
    for preset in a.presets:
        control_snr=a.awgn_control_snr if preset=='awgn' else a.watterson_control_snr
        for mode,snr in [(MARGIN32,control_snr),(HC0,control_snr+3)]:
            trials=[run(mode,preset,snr,a.seed+i) for i in range(a.trials)]
            row=dict(mode=mode.name,preset=preset,snr_3khz_db=snr,successes=sum(t['success'] for t in trials),trials=trials)
            output['rows'].append(row)
            print(preset,mode.name,snr,row['successes'],flush=True)
            a.output.write_text(json.dumps(output,indent=2)+'\n')

if __name__=='__main__':
    main()
