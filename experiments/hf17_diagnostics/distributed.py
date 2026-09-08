"""Compare distributed-pilot receivers on identical retained radio captures."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import numpy as np
from whale.phy import ofdm49 as phy


def compare(path, trackers=('off','legacy','common','residual'), widths=(1,3)):
    run = json.loads(path.read_text())
    mode = phy.OFDM49Mode(**{k:v for k,v in run['config'].items()
                            if k in phy.OFDM49Mode.__dataclass_fields__})
    rows=[]
    for trial in run['trials']:
        rng=np.random.default_rng(np.random.SeedSequence([run['seed'],trial['trial']]))
        payload=rng.integers(0,256,mode.max_payload_bytes,dtype=np.uint8).tobytes()
        _,truth=mode.pack_and_encode_bits(payload)
        cap=np.load(path.parent/'captures'/trial['capture_file'])
        for tracking in trackers:
            for width in widths:
                result=replace(mode,comb_tracking=tracking).demodulate(
                    cap,gain_smoothing=width,noise_estimator='repeat')
                bits=result.get('pre_fec_bits')
                rows.append(dict(trial=trial['trial'],tracking=tracking,smoothing=width,
                    ber=float(np.mean(bits != truth)) if bits is not None else None,
                    decoded=result.get('payload') == payload,
                    codewords_converged=sum(result.get('ldpc_codeword_ok',[]))))
    output=dict(source=str(path),application_bps=8*(mode.max_payload_bytes-10)/mode.frame_seconds(),rows=rows)
    filename = 'confidence-replay.json' if 'confidence' in trackers else 'distributed-replay.json'
    path.with_name(filename).write_text(json.dumps(output,indent=2)+'\n')
    for tracking in trackers:
        for width in widths:
            group=[r for r in rows if r['tracking']==tracking and r['smoothing']==width]
            print(tracking,width,'BER',np.mean([r['ber'] for r in group]),
                  'decoded',sum(r['decoded'] for r in group),'/',len(group))


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('result',type=Path)
    ap.add_argument('--confidence', action='store_true')
    args=ap.parse_args()
    if args.confidence:
        compare(args.result, ('off','common','confidence'), (3,))
    else:
        compare(args.result)
