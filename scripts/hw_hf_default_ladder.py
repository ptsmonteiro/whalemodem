"""One-way real-radio smoke test for every default HF mode."""
import argparse, json, sys, time
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import bench
from whale.modes.hc0_mode import HC0
from whale.modes.hc1_mode import HC1
from whale.modes.hr0_mode import HR0

MODES = (HR0, HC0, HC1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=1)
    ap.add_argument('--a', default='ic705')
    ap.add_argument('--b', default='ic7300')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    rng = np.random.default_rng(20260907)
    records = []
    with bench.radio_pair(args.b, args.a, warmup=3.0,
                          a_receive_only=True, b_receive_only=False) as (rx, tx):
        for mode in MODES:
            ok = 0
            print(f'\n== {mode.name} ({mode.mode_id}) ==')
            for trial in range(1, args.trials + 1):
                payload = rng.integers(0, 256, mode.chunk_size, dtype=np.uint8).tobytes()
                stale = rx.snapshot_rx(); rx.consume_rx(len(stale))
                audio = mode.encode(payload)
                keyed = tx.send(audio)
                time.sleep(1.5)
                captured = rx.snapshot_rx()
                result = mode.decode(captured)
                decoded = result.get('payload') == payload
                ok += int(decoded)
                print(f'  {trial}/{args.trials}: keyed={keyed:.2f}s rx={len(captured)} '
                      f'conf={result.get("confidence")} decoded={decoded} '
                      f'failure={result.get("failure")!r}')
                records.append({'mode': mode.name, 'mode_id': mode.mode_id,
                                'trial': trial, 'decoded': decoded,
                                'keyed_seconds': keyed, 'rx_samples': len(captured),
                                'confidence': result.get('confidence'),
                                'failure': result.get('failure')})
                time.sleep(.5)
            print(f'  result: {ok}/{args.trials}')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({'direction': f'{args.a}->{args.b}',
                                    'records': records}, indent=2, default=str) + '\n')
    return 0 if all(r['decoded'] for r in records) else 1

if __name__ == '__main__':
    raise SystemExit(main())
