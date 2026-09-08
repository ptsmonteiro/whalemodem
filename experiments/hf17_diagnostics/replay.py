"""Offline known-payload OFDM error decomposition. Oracle results are not decodes."""
import argparse
import json
from pathlib import Path
import numpy as np
from whale.phy import ofdm49 as phy


def metrics(z, truth, bits, mode):
    hard = phy.symbols_to_bits(z.reshape(-1), mode.bits_per_symbol)[:len(bits)]
    error = np.abs(z - truth) ** 2
    radii = np.round(np.abs(truth)**2, 6)
    by_radius = {str(r): float(np.sqrt(np.mean(error[radii == r])))
                 for r in np.unique(radii)}
    return dict(ber=float(np.mean(hard != bits)),
                evm=float(np.sqrt(error.mean() / np.mean(np.abs(truth)**2))),
                evm_by_symbol=np.sqrt(error.mean(axis=1)).tolist(),
                evm_by_carrier=np.sqrt(error.mean(axis=0)).tolist(),
                error_by_constellation_energy=by_radius)


def analyse(path):
    run = json.loads(path.read_text())
    cfg = run['config']
    mode = phy.OFDM49Mode(**{k: v for k, v in cfg.items()
                            if k in phy.OFDM49Mode.__dataclass_fields__})
    records = []
    for trial in run['trials']:
        rng = np.random.default_rng(np.random.SeedSequence([run['seed'], trial['trial']]))
        payload = rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()
        _, coded = mode.pack_and_encode_bits(payload)
        bits = coded ^ phy._bits.pn_bits(len(coded), phy.WHITENER_SEED)
        padded = np.pad(bits, (0, mode.n_data_ofdm_symbols * mode.bits_per_ofdm_symbol-len(bits)))
        truth = phy.bits_to_symbols(padded, mode.bits_per_symbol).reshape(-1, mode.n_data_bins)
        cap = np.load(path.parent / 'captures' / trial['capture_file'])
        result = mode.demodulate(cap, diagnostics=True)
        z = result['equalized_symbols']
        energy = np.sum(np.abs(truth)**2, axis=1)
        gain = np.sum(z * truth.conj(), axis=1) / energy
        phase = z * np.exp(-1j*np.angle(gain))[:, None]
        common = z / gain[:, None]
        # Fit a phase ramp per symbol, weighted by reference energy.
        x = np.arange(mode.n_data_bins)
        slopes = []
        ramp = np.empty_like(z)
        for i in range(len(z)):
            p = np.unwrap(np.angle(common[i] * truth[i].conj()))
            coef = np.polyfit(x, p, 1, w=np.abs(truth[i]))
            slopes.append(float(coef[0]))
            ramp[i] = common[i] * np.exp(-1j*np.polyval(coef, x))
        # A static per-carrier fit diagnoses channel-estimate bias.
        carrier_gain = np.sum(z*truth.conj(), axis=0)/np.sum(np.abs(truth)**2, axis=0)
        carrier = z/carrier_gain
        smoothing = {}
        for width in (3, 5, 9, 15):
            alt = mode.demodulate(cap, diagnostics=True, gain_smoothing=width)
            smoothing[str(width)] = metrics(alt['equalized_symbols'], truth, bits, mode)
        fec = {}
        if mode.fec_rate:
            for width in (1, 3):
                for estimator in ('legacy', 'repeat'):
                    alt = mode.demodulate(cap, gain_smoothing=width, noise_estimator=estimator)
                    fec[f'{width}/{estimator}'] = dict(decoded=alt.get('payload') == payload,
                        converged=alt.get('ldpc_ok'), iterations=alt.get('ldpc_iterations'))
        records.append(dict(trial=trial['trial'], decoded=result.get('payload') == payload,
                            tx_peak=trial.get('tx_peak'), baseline=metrics(z,truth,bits,mode),
                            oracle_phase=metrics(phase,truth,bits,mode),
                            oracle_common_gain=metrics(common,truth,bits,mode),
                            oracle_phase_ramp=metrics(ramp,truth,bits,mode),
                            oracle_static_carrier=metrics(carrier,truth,bits,mode),
                            pilot_smoothing=smoothing,
                            fec_replay=fec,
                            phase_degrees=np.rad2deg(np.angle(gain)).tolist(),
                            amplitude=np.abs(gain).tolist(), slope=slopes))
    return dict(source=str(path), note='Oracle fits use transmitted data; not usable decoder results.', trials=records)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('results', nargs='+', type=Path)
    args = ap.parse_args()
    for path in args.results:
        report = analyse(path)
        path.with_name('diagnostics.json').write_text(json.dumps(report, indent=2)+'\n')
        print(path.parent.name)
        for row in report['trials']:
            print(row['trial'], {k: round(v['ber'],4) for k,v in row.items() if isinstance(v,dict) and 'ber' in v},
                  'smooth', {k: round(v['ber'],4) for k,v in row['pilot_smoothing'].items()},
                  'phase', np.round(row['phase_degrees'],1), 'gain',np.round(row['amplitude'],2))
            if row['fec_replay']:
                print('FEC', row['fec_replay'])
