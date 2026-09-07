"""Matched seeded, blind-acquisition screen; bounded evidence, not qualification."""
import argparse
import json
import sys
import time
import subprocess
import hashlib
from pathlib import Path
import numpy as np
from scipy.signal import periodogram
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from whale import link, rx_audio
from experiments.hr0_fast_control.legacy_hr0_mode import HR0
from whale.modes.hc0_mode import HC0
from whale.qualification import channel_factory
from experiments.hr0_fast_control.candidate import FAST16, FAST32


FULL_CANDIDATES = False


def payload_for(mode, seed):
    rng = np.random.default_rng(seed)
    if mode is HC0 or FULL_CANDIDATES:
        body = bytes([seed % 128, 0]) + rng.bytes(mode.chunk_size)
        header, remainder = link._encode_air_header(link.PT_DATA, HC0.mode_id if mode is HC0 else HR0.mode_id, body)
    else:
        header, remainder = link._encode_air_header(link.PT_DATA_ACK, HR0.mode_id,
            bytes([seed % 128, 1, HC0.mode_id, 0]))
    return header + remainder


def bandwidth(audio):
    f, p = periodogram(audio, fs=48000, window='boxcar', detrend=False)
    c = np.cumsum(p) / p.sum()
    lo, hi = f[np.searchsorted(c, [.005, .995])]
    return dict(lower_hz=float(lo), upper_hz=float(hi), occupied_99_hz=float(hi-lo))


def run(mode, preset, snr, seed):
    payload = payload_for(mode, seed)
    tx = mode.encode(payload)
    factory = channel_factory('awgn' if preset == 'awgn' else 'watterson', snr,
                              watterson_preset=preset)
    channel = factory(seed)
    received = channel.process(tx)
    capture = np.concatenate((received.audio, channel.drain().audio,
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES)))
    t0 = time.monotonic()
    result = mode.decode(rx_audio.downsample(capture.astype(np.float32)))
    return dict(seed=seed, success=result.get('payload') == payload,
        synced=bool(result.get('synced')), confidence=float(result.get('confidence', 0)),
        failure=result.get('failure'), decode_seconds=time.monotonic()-t0,
        payload_bytes=len(payload), tx_seconds=len(tx)/48000,
        channel_description=channel.describe(), channel_measurements=received.measurements)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trials', type=int, default=12)
    parser.add_argument('--seed', type=int, default=260906)
    parser.add_argument('--snrs', type=float, nargs='+', default=[-12,-9,-6,-3,0,3,6,9,11,14])
    parser.add_argument('--presets', nargs='+', default=['awgn', 'mid_latitude_quiet', 'mid_latitude_moderate', 'mid_latitude_disturbed'])
    parser.add_argument('--full-candidates', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    global FULL_CANDIDATES
    FULL_CANDIDATES = args.full_candidates
    modes = [HR0, HC0, FAST16, FAST32]
    output = dict(full_candidates=args.full_candidates, errors=0, git_revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        source_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')},
        candidate_geometry={m.name:dict(tones=m.tones,symbol_samples=m.symbol_samples,sync_symbols=m.sync_symbols,first_bin=m.first_bin,body_symbols=[v[0] for v in m.codecs],interleaver_permutations=[v[1].interleaver.permutation.tolist() for v in m.codecs]) for m in [FAST16,FAST32]},
        method='Matched seeds; actual 12B ACK vs full HC0 DATA; canonical 48kHz channel_factory, passband_3khz, blind acquisition; whole-waveform received power reference. Offline screen only.', trials=args.trials, master_seed=args.seed,
        bandwidth={m.name: [bandwidth(m.encode(payload_for(m, args.seed+i))) for i in range(5)] for m in modes}, rows=[])
    for preset in args.presets:
        for snr in args.snrs:
            for mode in modes:
                trials=[run(mode,preset,snr,args.seed+i) for i in range(args.trials)]
                row=dict(preset=preset, snr_3khz_db=snr, mode=mode.name,
                         successes=sum(t['success'] for t in trials), trials=trials)
                output['rows'].append(row)
                print(preset,snr,mode.name,row['successes'],flush=True)
                args.output.write_text(json.dumps(output,indent=2)+'\n')

if __name__ == '__main__':
    main()
