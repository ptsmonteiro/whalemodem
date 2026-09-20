"""Unit tests for the settling-head bench instruments.

These cannot run against the radios, so they test the two things that can be
tested without them: that every piece of pure logic does what its docstring
claims, and that the whole measurement chain recovers quantities that were
put into a synthesised capture on purpose. The second is the point -- an
instrument whose numbers have never been checked against a known answer is
not worth the air time a campaign would spend on it.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import head_analyse as ha  # noqa: E402
import head_bench as hb  # noqa: E402

from whale import sweep  # noqa: E402
from whale.framing import AIR_HEADER_BYTES  # noqa: E402


# -- scenario expansion ---------------------------------------------------

def test_expansion_covers_every_axis_exactly_once():
    cells = hb.expand(scenarios=("single",), modes=("hc0", "hc1w"),
                      directions=("ab", "ba"), heads=(0.1, 0.6), reps=2)
    gaps = hb.PLAN["single"]["gaps"]
    assert len(cells) == 2 * 2 * 2 * len(gaps) * 2
    assert len({cell.key for cell in cells}) == len(cells)


def test_expansion_is_deterministic():
    first = hb.expand(scenarios=("single", "cold"), modes=("hc0",), reps=1)
    second = hb.expand(scenarios=("single", "cold"), modes=("hc0",), reps=1)
    assert [c.key for c in first] == [c.key for c in second]


def test_expansion_is_scenario_major_so_an_interrupt_leaves_whole_scenarios():
    cells = hb.expand(scenarios=("single", "cold"), modes=("hc0",),
                      directions=("ab",), heads=(0.6,), reps=1)
    scenarios = [cell.scenario for cell in cells]
    assert scenarios == sorted(scenarios, key=lambda s: s != "single")


def test_burst_scenario_asks_for_more_than_one_frame_per_keying():
    cells = hb.expand(scenarios=("burst",), modes=("hc0",),
                      directions=("ab",), heads=(0.6,), reps=1)
    assert cells and all(cell.frames > 1 for cell in cells)


def test_turnaround_carries_its_gap_and_alternate_carries_a_second_mode():
    turn = hb.expand(scenarios=("turnaround",), modes=("hc0",),
                     directions=("ab",), heads=(0.6,), reps=1)
    assert turn and all(c.turnaround_seconds is not None for c in turn)

    alt = hb.expand(scenarios=("alternate",), modes=("hc0", "hf6"),
                    directions=("ab",), heads=(0.6,), reps=1)
    assert alt and all(c.alt_mode not in (None, c.mode) for c in alt)


def test_alternate_is_skipped_when_there_is_nothing_to_alternate_with():
    assert hb.expand(scenarios=("alternate",), modes=("hc0",),
                     directions=("ab",), heads=(0.6,), reps=1) == []


def test_cell_key_ignores_nothing_that_changes_the_air():
    base = dict(scenario="single", direction="ab", mode="hc0",
                head_seconds=0.6, gap_seconds=1.0, rep=1)
    for field, value in [("head_seconds", 0.3), ("gap_seconds", 2.0),
                         ("direction", "ba"), ("mode", "hc1w"), ("rep", 2),
                         ("frames", 4), ("turnaround_seconds", 0.2),
                         ("alt_mode", "hf6")]:
        assert hb.Cell(**base).key != hb.Cell(**{**base, field: value}).key


# -- the run log: round trip and resume -----------------------------------

def _record(cell, **extra):
    return {"cell_key": cell.key, "mode": cell.mode,
            "direction": cell.direction, "scenario": cell.scenario,
            "head_seconds": cell.head_seconds, "gap_seconds": cell.gap_seconds,
            "frame_count": 1, "decoded_count": 1, "frames": [],
            "settle_seconds": 0.21, "rx_overflows": 0, "tx_underflows": 0,
            **extra}


def test_run_log_round_trips_and_reports_completed_cells(tmp_path):
    log = hb.RunLog(tmp_path / "run.jsonl")
    cells = hb.expand(scenarios=("single",), modes=("hc0",),
                      directions=("ab",), heads=(0.6,), reps=1)
    for cell in cells[:2]:
        log.append(_record(cell))
    log.close()

    reopened = hb.RunLog(tmp_path / "run.jsonl")
    assert reopened.completed_keys() == {c.key for c in cells[:2]}
    assert len(reopened.records()) == 2
    assert reopened.records()[0]["mode"] == "hc0"


def test_run_log_serialises_numpy_values(tmp_path):
    log = hb.RunLog(tmp_path / "run.jsonl")
    log.append({"cell_key": "x", "conf": np.float32(0.5),
                "env": np.arange(3, dtype=np.float64)})
    log.close()
    record = hb.RunLog(tmp_path / "run.jsonl").records()[0]
    assert record["conf"] == pytest.approx(0.5)
    assert record["env"] == [0.0, 1.0, 2.0]


def test_resume_skips_recorded_cells_and_merges_cleanly(tmp_path):
    path = tmp_path / "run.jsonl"
    cells = hb.expand(scenarios=("single",), modes=("hc0",),
                      directions=("ab",), heads=(0.6,), reps=1)
    log = hb.RunLog(path)
    for cell in cells[:3]:
        log.append(_record(cell))
    log.close()

    resumed = hb.RunLog(path)
    done = resumed.completed_keys()
    pending = [c for c in cells if c.key not in done]
    assert len(pending) == len(cells) - 3
    for cell in pending:
        resumed.append(_record(cell))
    resumed.close()

    final = hb.RunLog(path)
    assert final.completed_keys() == {c.key for c in cells}
    assert len(final.records()) == len(cells)


def test_a_truncated_final_line_is_dropped_rather_than_poisoning_a_resume(tmp_path):
    path = tmp_path / "run.jsonl"
    cells = hb.expand(scenarios=("single",), modes=("hc0",),
                      directions=("ab",), heads=(0.6,), reps=1)
    log = hb.RunLog(path)
    log.append(_record(cells[0]))
    log.close()
    # An interrupt landing mid-write leaves exactly this.
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"cell_key": "half-writ')

    resumed = hb.RunLog(path)
    assert resumed.completed_keys() == {cells[0].key}
    assert len(resumed.records()) == 1


def test_captures_round_trip_as_float32_at_the_receive_rate(tmp_path):
    log = hb.RunLog(tmp_path / "run.jsonl")
    audio = np.linspace(-1, 1, 1000).astype(np.float32)
    path = log.save_capture("cell", audio)
    loaded = np.load(path)
    assert loaded.dtype == np.float32
    np.testing.assert_allclose(loaded, audio)


# -- payload derivation ---------------------------------------------------

def test_payload_is_reproducible_from_run_mode_and_sequence():
    mode = hb.mode_by_name("hc0")
    first = hb.payload_for(1234, mode, 3)
    assert first == hb.payload_for(1234, mode, 3)
    assert first != hb.payload_for(1234, mode, 4)
    assert first != hb.payload_for(1235, mode, 3)


def test_payload_matches_the_sweeps_own_derivation():
    mode = hb.mode_by_name("hc0")
    assert hb.payload_for(7, mode, 2) == sweep.frame_payload(
        7, mode.mode_id, 2, AIR_HEADER_BYTES + mode.chunk_size)


def test_payload_fills_the_modes_frame():
    for name in ("hc0", "hc1w", "hf6"):
        mode = hb.mode_by_name(name)
        assert len(hb.payload_for(1, mode, 0)) == (
            AIR_HEADER_BYTES + mode.chunk_size)


def test_run_id_is_stable_across_runs_so_a_resume_derives_the_same_payloads():
    assert hb.run_id_from("run1") == hb.run_id_from("run1")
    assert hb.run_id_from("run1") != hb.run_id_from("run2")
    assert 0 <= hb.run_id_from("run1") <= 0xFFFF


# -- ground truth: where the preamble actually is -------------------------

@pytest.mark.parametrize("name", ["hr0", "hc0", "hc1w"])
@pytest.mark.parametrize("head", [0.0, 0.1, 0.3, 0.6])
def test_preamble_offset_matches_the_fsk_phys_own_rounding(name, head):
    """Guards against a rounding rule changing upstream: every landing
    measurement is relative to this number."""
    phy = pytest.importorskip(f"whale.phy.{name}")
    expected = phy.settling_head_samples(head) // hb.DECIMATION
    assert hb.preamble_offset_rx(name, head) == expected


@pytest.mark.parametrize("head", [0.1, 0.3, 0.6])
def test_preamble_offset_matches_where_ofdm_actually_decodes(head):
    """The strongest form of the check: modulate with this head, decode, and
    confirm the decoder's own start sample is where we said the preamble is."""
    name = "hf6"
    mode = hb.headed_mode(name, head)
    payload = hb.payload_for(1, hb.mode_by_name(name), 0)
    audio_rx = np.asarray(mode.encode(payload), np.float64)[::hb.DECIMATION]
    result = hb._ofdm_phy(name).demodulate(audio_rx)
    assert result["start_sample"] == hb.preamble_offset_rx(name, head)


def test_preamble_offset_grows_with_the_head():
    offsets = [hb.preamble_offset_rx("hc0", h) for h in (0.0, 0.2, 0.6)]
    assert offsets == sorted(offsets) and offsets[0] == 0


def test_a_mode_with_no_head_is_refused_rather_than_silently_measured_at_zero():
    with pytest.raises(KeyError):
        hb.preamble_offset_rx("vf12", 0.6)


# -- quantity (A): the envelope and settle-time estimator -----------------

def _ramped_tone(tau_seconds, seconds=4.0, amplitude=0.3, noise=1e-4, seed=0):
    """A steady tone behind an exponential gain ramp, preceded by silence.

    Exactly the shape an AGC produces, with the answer known: the level is
    within 1 dB of final after -ln(1 - 10**(-1/20)) * tau = 2.20 * tau.
    """
    rng = np.random.default_rng(seed)
    quiet = noise * rng.standard_normal(int(1.0 * hb.RX_RATE))
    n = int(seconds * hb.RX_RATE)
    tone = amplitude * np.sin(2 * np.pi * 1500.0 * np.arange(n) / hb.RX_RATE)
    ramp = 1.0 - np.exp(-np.arange(n) / (tau_seconds * hb.RX_RATE))
    keyed = tone * ramp + noise * rng.standard_normal(n)
    return np.concatenate([quiet, keyed, quiet]).astype(np.float32)


@pytest.mark.parametrize("tau", [0.05, 0.1, 0.25, 0.5])
def test_settle_time_recovers_a_synthesised_agc_ramp(tau):
    measured = hb.settle_measurement(hb.level_envelope(_ramped_tone(tau)))
    expected = -np.log(1 - 10 ** (-hb.SETTLE_TOLERANCE_DB / 20.0)) * tau
    assert measured["settle_seconds"] == pytest.approx(
        expected, abs=3 * hb.ENVELOPE_HOP_SECONDS)


def test_onset_is_found_where_rf_actually_starts():
    measured = hb.settle_measurement(hb.level_envelope(_ramped_tone(0.1)))
    assert measured["onset_seconds"] == pytest.approx(1.0, abs=0.05)
    assert measured["keyed_seconds"] == pytest.approx(4.0, abs=0.1)


def test_a_single_noisy_hop_before_the_rise_is_not_the_onset():
    """The defect: onset was the FIRST hop over floor + ONSET_RISE_DB.

    A pre-key floor quiet enough (and after a 30 s listen it is: -65 dB
    against a -18 dB keyed level) makes a 6 dB threshold trivial for one
    noise draw to clear, and the whole keying is then measured from a moment
    before RF exists. On `logs/head/smoke_ba.jsonl` index 5 that read onset
    at 0.26 s against a true 0.55-0.58 s rise and reported a 340 ms AGC ramp
    where the true one is 40 ms.
    """
    envelope = hb.level_envelope(_ramped_tone(0.1))
    clean = hb.settle_measurement(envelope)

    blipped = np.array(envelope, dtype=np.float64)
    blip = 30                                   # 0.3 s, well before key-up
    blipped[blip] = clean["floor_db"] + 4 * hb.ONSET_RISE_DB
    measured = hb.settle_measurement(blipped)

    # The blip really does clear the threshold a single crossing would fire
    # on, so this is the case that used to be scored 0.7 s early.
    assert blipped[blip] > clean["floor_db"] + hb.ONSET_RISE_DB
    assert measured["onset_seconds"] == pytest.approx(clean["onset_seconds"])
    assert measured["settle_seconds"] == pytest.approx(clean["settle_seconds"])


def test_a_single_hop_dipping_back_under_the_threshold_does_not_delay_onset():
    """The persistence requirement's own failure mode, and it is not
    hypothetical: on `logs/head/cold_ab.jsonl` index 6 one hop 0.08 dB under
    the threshold, in the middle of an otherwise clean step, put the onset
    60 ms late. Requiring a run of raw hops would trade a 300 ms error at
    high SNR for a 60 ms one at low SNR."""
    envelope = hb.level_envelope(_ramped_tone(0.1))
    clean = hb.settle_measurement(envelope)
    onset = int(round(clean["onset_seconds"] / hb.ENVELOPE_HOP_SECONDS))

    dipped = np.array(envelope, dtype=np.float64)
    # Inside the run the onset has to hold, where the real one was.
    dipped[onset + 8] = clean["floor_db"]
    measured = hb.settle_measurement(dipped)

    assert measured["onset_seconds"] == pytest.approx(clean["onset_seconds"])


def test_a_single_noisy_hop_after_the_unkey_does_not_extend_the_keyed_span():
    """The same weakness read from the other end: `rf_end` was the LAST
    single hop over the threshold, so one noise draw in the post-roll
    stretched the keyed span and with it the steady-state window and the
    span the settle time is checked against."""
    envelope = hb.level_envelope(_ramped_tone(0.1))
    clean = hb.settle_measurement(envelope)

    blipped = np.array(envelope, dtype=np.float64)
    blipped[-20] = clean["floor_db"] + 4 * hb.ONSET_RISE_DB
    measured = hb.settle_measurement(blipped)

    assert measured["keyed_seconds"] == pytest.approx(clean["keyed_seconds"])
    assert measured["settle_seconds"] == pytest.approx(clean["settle_seconds"])


def test_floor_and_steady_levels_are_separated_by_the_real_level():
    measured = hb.settle_measurement(hb.level_envelope(_ramped_tone(0.1)))
    assert measured["floor_db"] < -60
    assert measured["steady_db"] > measured["floor_db"] + 30


def test_settle_is_reported_at_a_ladder_of_tolerances():
    measured = hb.settle_measurement(hb.level_envelope(_ramped_tone(0.25)))
    ladder = measured["settle_by_tolerance_db"]
    assert set(ladder) >= {"0.5", "1", "2", "3"}
    # A looser tolerance is reached sooner. That is the only monotonicity
    # this estimator promises, and it is worth holding it to.
    values = [ladder[k] for k in ("0.5", "1", "2", "3")]
    assert values == sorted(values, reverse=True)


def test_a_capture_with_no_rf_yields_no_settle_time_rather_than_a_wrong_one():
    quiet = (1e-4 * np.random.default_rng(0).standard_normal(
        2 * hb.RX_RATE)).astype(np.float32)
    measured = hb.settle_measurement(hb.level_envelope(quiet))
    assert measured["onset_seconds"] is None
    assert measured["settle_seconds"] is None


def test_referencing_to_the_transmitted_envelope_removes_the_waveforms_ripple():
    """An OFDM frame's own envelope ripples by more than the 1 dB tolerance,
    so the unreferenced measurement reads the ripple as an unsettled level.
    Dividing the transmitted envelope out is what makes this a measurement.
    """
    name, head, tau = "hf6", 0.6, 0.1
    payload = hb.payload_for(1, hb.mode_by_name(name), 0)
    tx, _, _ = hb.keying_audio(name, head, [payload])
    link = hb.SimulatedPair(seed=0, agc_seconds=tau)
    link.send(tx)
    capture = link.read_rx()[2]

    reference = hb.level_envelope(np.asarray(tx, np.float64)[::hb.DECIMATION])
    envelope = hb.level_envelope(capture)
    referenced = hb.settle_measurement(envelope, reference)
    unreferenced = hb.settle_measurement(envelope)

    expected = -np.log(1 - 10 ** (-hb.SETTLE_TOLERANCE_DB / 20.0)) * tau
    assert referenced["referenced"] is True
    assert referenced["settle_seconds"] == pytest.approx(expected, abs=0.05)
    assert unreferenced["settle_seconds"] > referenced["settle_seconds"] * 2


# -- outcome and landing classification -----------------------------------

def test_the_three_outcomes_are_three_distinct_states():
    assert hb.outcome_of({"payload_ok": True, "synced": True,
                          "confidence": 0.9}, 0.12) == "decoded"
    assert hb.outcome_of({"payload_ok": False, "synced": True,
                          "confidence": 0.9}, 0.12) == "synced"
    assert hb.outcome_of({"payload_ok": False, "synced": False,
                          "confidence": 0.01}, 0.12) == "never-seen"


def test_landing_tells_in_head_from_late_from_on_preamble():
    tol = hb.LANDING_TOLERANCE_SAMPLES
    def landing(start):
        return hb.landing_of({"start_index": start, "confidence": 0.9},
                             1000, 0.12)["landing"]
    assert landing(1000) == "on-preamble"
    assert landing(1000 + tol // 2) == "on-preamble"
    assert landing(1000 - 5 * tol) == "in-head"
    assert landing(1000 + 5 * tol) == "late"


def test_an_acquisition_below_threshold_is_never_acquired_not_a_landing():
    result = hb.landing_of({"start_index": 500, "confidence": 0.01}, 1000, 0.12)
    assert result["landing"] == "never-acquired"
    assert result["landing_error_samples"] is None


# -- the whole chain, against a synthesised capture -----------------------

@pytest.mark.parametrize("name,head", [("hc0", 0.6), ("hf6", 0.6)])
def test_measure_keying_recovers_the_truth_it_was_given(name, head):
    """Modulate -> AGC-ramped, noisy, delayed loopback -> measure. Every
    number the record claims is checked against what went in."""
    tau = 0.15
    payload = hb.payload_for(42, hb.mode_by_name(name), 0)
    tx, realised_head, mode = hb.keying_audio(name, head, [payload])
    link = hb.SimulatedPair(seed=3, agc_seconds=tau)
    link.send(tx)
    capture = link.read_rx()[2]

    cell = hb.Cell(scenario="single", direction="ab", mode=name,
                   head_seconds=head, gap_seconds=1.0, rep=1)
    record = hb.measure_keying(cell, name, capture, [payload],
                               mode.confidence_threshold, realised_head,
                               tx_audio=tx)

    assert record["outcome"] == "decoded"
    assert record["decoded_count"] == 1
    # The preamble is where the head ends, and acquisition landed on it.
    assert record["preamble_offset_rx"] == hb.preamble_offset_rx(name, realised_head)
    assert record["frames"][0]["landing"] == "on-preamble"
    assert abs(record["frames"][0]["landing_error_samples"]) <= \
        hb.LANDING_TOLERANCE_SAMPLES
    # Quantity (A), referenced to the TX envelope, recovers the ramp.
    expected = -np.log(1 - 10 ** (-hb.SETTLE_TOLERANCE_DB / 20.0)) * tau
    assert record["settle_referenced_to_tx"] is True
    assert record["settle_seconds"] == pytest.approx(expected, abs=0.08)
    # A clean channel has no bit errors; a non-None BER proves the reference
    # payload really was compared against, not merely CRC-checked.
    assert record["frames"][0]["raw_ber"] == pytest.approx(0.0, abs=1e-9)
    assert record["cell_key"] == cell.key


def test_a_frame_that_never_arrives_is_never_seen_not_a_failed_decode():
    name = "hc0"
    payload = hb.payload_for(42, hb.mode_by_name(name), 0)
    quiet = (1e-3 * np.random.default_rng(1).standard_normal(
        6 * hb.RX_RATE)).astype(np.float32)
    cell = hb.Cell(scenario="single", direction="ab", mode=name,
                   head_seconds=0.6, gap_seconds=1.0, rep=1)
    record = hb.measure_keying(cell, name, quiet, [payload], 0.12, 0.6)
    assert record["outcome"] == "never-seen"
    assert record["settle_seconds"] is None


def test_in_head_confidence_is_measured_on_the_head_alone():
    """The live risk: `whale/streaming.py` gates candidates at 0.12 and the
    OFDM head's in-head score has been measured at 0.10-0.13, so this is
    logged every keying. It must come from the head and nothing else."""
    name, head = "hf6", 0.6
    payload = hb.payload_for(42, hb.mode_by_name(name), 0)
    tx, realised, _ = hb.keying_audio(name, head, [payload])
    audio_rx = np.asarray(tx, np.float64)[::hb.DECIMATION]
    offset = hb.preamble_offset_rx(name, realised)

    score = hb.in_head_confidence(name, audio_rx, 0, offset)
    assert score is not None
    # Well below the score the preamble itself gets, or acquisition would
    # never find the frame at all.
    assert score < hb._ofdm_phy(name).demodulate(audio_rx)["confidence"]


def test_a_keying_with_no_head_has_no_in_head_score_to_report():
    name = "hc0"
    payload = hb.payload_for(1, hb.mode_by_name(name), 0)
    tx, realised, _ = hb.keying_audio(name, 0.0, [payload])
    audio_rx = np.asarray(tx, np.float64)[::hb.DECIMATION]
    assert hb.in_head_confidence(name, audio_rx, 0, 0) is None


# -- the continuous capture -----------------------------------------------

def test_continuous_capture_loses_nothing_across_several_keyings():
    """`RadioTransport.send()` clears the buffer in its finally; the
    receiving end is receive-only so that path never runs, and the capture
    must be seamless across keyings for the pre-roll to exist at all."""
    a, b = hb.simulated_pair(seed=0)
    capture = hb.ContinuousCapture(b)
    total = 0
    for _ in range(3):
        audio = np.ones(hb.RX_RATE * hb.DECIMATION, np.float32) * 0.1
        a.send(audio)
        total += hb.RX_RATE + 2 * int(a.quiet_seconds * hb.RX_RATE)
        capture.drain()
    assert capture.position == total
    assert capture.generation_changes == 0
    assert len(capture.slice(0, capture.position)) == total


def test_continuous_capture_counts_a_generation_change_as_a_dropout():
    a, b = hb.simulated_pair(seed=0)
    capture = hb.ContinuousCapture(b)
    a.send(np.ones(1000 * hb.DECIMATION, np.float32) * 0.1)
    capture.drain()
    b.rx_generation += 1          # what an input overflow does
    a.send(np.ones(1000 * hb.DECIMATION, np.float32) * 0.1)
    capture.drain()
    assert capture.generation_changes == 1


def test_a_receive_only_transport_cannot_be_keyed():
    """The safety property, asserted against the real transport class rather
    than the simulated one: `receive_only` removes the ability, it does not
    merely discourage it."""
    from whale.transport import RadioTransport
    transport = RadioTransport.__new__(RadioTransport)
    transport.receive_only = True
    transport.radio = type("R", (), {"id": "x"})()
    with pytest.raises(RuntimeError, match="receive-only"):
        transport.send(np.zeros(10, np.float32))


# -- quantity (B): the offline trim sweep ---------------------------------

def test_trim_sweep_walks_the_whole_head_and_ends_on_the_preamble():
    name, head = "hc0", 0.6
    payload = hb.payload_for(9, hb.mode_by_name(name), 0)
    tx, realised, _ = hb.keying_audio(name, head, [payload])
    audio_rx = np.asarray(tx, np.float64)[::hb.DECIMATION]

    rows = ha.trim_sweep(name, audio_rx, realised, payload, onset_index=0,
                         step=0.1)
    assert rows[0]["trimmed_seconds"] == 0.0
    assert rows[0]["remaining_head_seconds"] == pytest.approx(realised, abs=0.01)
    assert rows[-1]["remaining_head_seconds"] < 0.1
    heads = [row["remaining_head_seconds"] for row in rows]
    assert heads == sorted(heads, reverse=True)


def test_trim_sweep_decodes_a_clean_capture_at_every_trim_point():
    """On a clean channel acquisition needs no head at all, so the whole
    sweep decodes. That is the quantity (B) answer -- and it is exactly why
    it must not be mistaken for a head budget."""
    name, head = "hc0", 0.6
    payload = hb.payload_for(9, hb.mode_by_name(name), 0)
    tx, realised, _ = hb.keying_audio(name, head, [payload])
    audio_rx = np.asarray(tx, np.float64)[::hb.DECIMATION]
    rows = ha.trim_sweep(name, audio_rx, realised, payload, onset_index=0,
                         step=0.2)
    assert all(row["outcome"] == "decoded" for row in rows)
    assert ha.shortest_decoding_head(rows) == pytest.approx(
        min(r["remaining_head_seconds"] for r in rows))


def test_trim_sweep_reports_no_shortest_head_when_nothing_decodes():
    rows = [{"remaining_head_seconds": 0.5, "outcome": "never-seen"},
            {"remaining_head_seconds": 0.2, "outcome": "synced"}]
    assert ha.shortest_decoding_head(rows) is None


def test_trim_sweep_landings_are_relative_to_the_shrinking_head():
    """The preamble moves towards the front of the audio as the head is
    trimmed; a landing still reported against the original position would
    read as "late" everywhere."""
    name, head = "hc0", 0.6
    payload = hb.payload_for(9, hb.mode_by_name(name), 0)
    tx, realised, _ = hb.keying_audio(name, head, [payload])
    audio_rx = np.asarray(tx, np.float64)[::hb.DECIMATION]
    rows = ha.trim_sweep(name, audio_rx, realised, payload, onset_index=0,
                         step=0.2)
    assert all(row["landing"] == "on-preamble" for row in rows)


def test_the_trim_caveat_names_both_quantities_wherever_it_is_printed(capsys):
    """Nobody reading a trim result must be able to mistake it for a head
    budget, so the caveat is part of the output, not only the docstring."""
    ha.print_trim("hc0", [{"trimmed_seconds": 0.0,
                           "remaining_head_seconds": 0.6,
                           "outcome": "decoded", "confidence": 0.9,
                           "landing": "on-preamble", "raw_ber": 0.0}])
    printed = capsys.readouterr().out
    assert "ACQUISITION, NOT AGC" in printed
    assert "LOWER BOUND" in printed
    assert "settle_seconds" in printed
    assert "QUANTITY (B) ONLY" in ha.TRIM_CAVEAT
    assert "QUANTITY (B)" in ha.__doc__ and "(A)" in ha.__doc__


# -- the aggregate report -------------------------------------------------

def test_aggregate_groups_and_counts_a_whole_run():
    cells = hb.expand(scenarios=("single",), modes=("hc0",),
                      directions=("ab",), heads=(0.2, 0.6), reps=1)
    records = []
    for index, cell in enumerate(cells):
        decoded = 1 if cell.head_seconds == 0.6 else index % 2
        records.append(_record(cell, decoded_count=decoded,
                               settle_seconds=0.2 + 0.01 * index,
                               frames=[{"landing": "on-preamble"}],
                               in_head_confidence=0.11))
    groups = ha.aggregate(records, ("mode", "direction", "scenario",
                                    "head_seconds"))
    assert len(groups) == 2
    long_head = groups[("hc0", "ab", "single", 0.6)]
    assert long_head["decoded"] == long_head["keyings"]
    short_head = groups[("hc0", "ab", "single", 0.2)]
    assert short_head["decoded"] < short_head["keyings"]
    assert short_head["landings"]["on-preamble"] == short_head["keyings"]


def test_aggregate_counts_xrun_keyings_separately_from_failures():
    cell = hb.expand(scenarios=("single",), modes=("hc0",),
                     directions=("ab",), heads=(0.6,), reps=1)[0]
    groups = ha.aggregate([_record(cell, rx_overflows=2, decoded_count=0)],
                          ("mode",))
    assert groups[("hc0",)]["xruns"] == 1


def test_aggregate_output_says_which_quantity_it_reports(capsys):
    cell = hb.expand(scenarios=("single",), modes=("hc0",),
                     directions=("ab",), heads=(0.6,), reps=1)[0]
    ha.print_aggregate(ha.aggregate([_record(cell)], ("mode",)), ("mode",))
    printed = capsys.readouterr().out
    assert "QUANTITY (A)" in printed
    assert "NOT the acquisition requirement" in printed


# -- the drivers, end to end, with no radios ------------------------------

def test_the_bench_runs_end_to_end_against_the_simulated_pair(tmp_path):
    out = tmp_path / "sim.jsonl"
    assert hb.main(["--simulate", "--out", str(out), "--scenario", "single",
                    "--mode", "hc0", "--head", "0.6", "--reps", "1",
                    "--gap-scale", "0"]) == 0
    records = hb.RunLog(out).records()
    assert len(records) == len(hb.PLAN["single"]["gaps"])
    assert all(r["outcome"] == "decoded" for r in records)
    assert all(r["settle_referenced_to_tx"] for r in records)


def test_a_second_invocation_resumes_instead_of_repeating(tmp_path, capsys):
    out = tmp_path / "sim.jsonl"
    argv = ["--simulate", "--out", str(out), "--scenario", "single",
            "--mode", "hc0", "--head", "0.6", "--reps", "1", "--gap-scale", "0"]
    hb.main(argv)
    before = len(hb.RunLog(out).records())
    capsys.readouterr()
    assert hb.main(argv) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert len(hb.RunLog(out).records()) == before


def test_turnaround_refuses_to_run_without_explicit_two_way_consent(tmp_path):
    """It is the one scenario that keys both radios, and the listening
    radio is otherwise opened receive-only precisely so it cannot be."""
    with pytest.raises(SystemExit):
        hb.main(["--out", str(tmp_path / "x.jsonl"),
                 "--scenario", "turnaround", "--dry-run"])


def test_dry_run_expands_the_matrix_without_touching_a_radio(tmp_path, capsys):
    assert hb.main(["--out", str(tmp_path / "x.jsonl"), "--dry-run",
                    "--scenario", "single", "--mode", "hc0",
                    "--head", "0.6", "--reps", "1"]) == 0
    printed = capsys.readouterr().out
    assert printed.count("single|ab|hc0") == len(hb.PLAN["single"]["gaps"])
    assert not (tmp_path / "x.jsonl").exists()


def test_the_analyser_reads_a_run_the_bench_wrote(tmp_path, capsys):
    out = tmp_path / "sim.jsonl"
    hb.main(["--simulate", "--out", str(out), "--scenario", "single",
             "--mode", "hc0", "--head", "0.6", "--reps", "1",
             "--gap-scale", "0", "--keep-pass-fraction", "1.0"])
    capsys.readouterr()
    assert ha.main(["--aggregate", str(out), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary
    assert all(v["keyings_ok"] == v["keyings"] for v in summary.values())

    capture = next(iter(hb.RunLog(out).records()))["capture_path"]
    assert ha.main(["--capture", capture, "--log", str(out),
                    "--no-profile"]) == 0
    printed = capsys.readouterr().out
    assert "true preamble" in printed and "raw BER" in printed


def test_the_analyser_refuses_a_capture_it_cannot_identify(tmp_path):
    path = tmp_path / "unknown.npy"
    np.save(path, np.zeros(100, np.float32))
    with pytest.raises(SystemExit):
        ha.main(["--capture", str(path)])


# -- defect 1: a burst's search window and its per-frame landing reference --

def _burst_capture(name, head, payloads, quiet_seconds=3.0, noise=1e-4,
                   seed=0):
    """A clean capture of one multi-frame keying, plus the TX buffer.

    No AGC ramp and no channel: this fixture is about where the search
    window goes and what each landing is measured against, and an attenuated
    first frame would only make acquisition's own choice of peak the
    variable under test.

    The quiet lead has to be more than a twentieth of the whole capture or
    the noise floor, taken as the median of its first tenth, is measured on
    signal and RF onset is never detected at all.
    """
    tx, realised, _ = hb.keying_audio(name, head, payloads)
    rng = np.random.default_rng(seed)
    quiet = noise * rng.standard_normal(int(quiet_seconds * hb.RX_RATE))
    rx = np.asarray(tx, np.float64)[::hb.DECIMATION]
    capture = np.concatenate([quiet, rx, quiet]).astype(np.float32)
    return capture, tx, realised


def _burst_cell(name, head, frames):
    return hb.Cell(scenario="burst", direction="ab", mode=name,
                   head_seconds=head, gap_seconds=1.0, rep=1, frames=frames)


def test_frame_stride_is_derived_from_the_tx_buffer_not_estimated():
    """One keying is one mode, one head and one payload size repeated, so
    the stride is the buffer divided by the frame count, exactly."""
    name, head = "hc0", 0.3
    mode = hb.mode_by_name(name)
    payloads = [hb.payload_for(7, mode, seq) for seq in range(4)]
    tx, _, _ = hb.keying_audio(name, head, payloads)
    one, _, _ = hb.keying_audio(name, head, payloads[:1])
    assert hb.frame_stride_rx(tx, 4) == len(np.asarray(one)[::hb.DECIMATION])
    # A single-frame keying has no next frame, and no TX buffer means no
    # ground truth to derive one from.
    assert hb.frame_stride_rx(tx, 1) == 0
    assert hb.frame_stride_rx(None, 4) == 0


def test_a_burst_frame_that_fails_to_verify_does_not_stall_the_search():
    """The defect this scenario existed to expose: advancing only on a
    byte-exact decode left a synced-but-failed frame re-decoding the same
    samples, so every frame behind it was never reached."""
    name, head, frames = "hc0", 0.3, 4
    mode = hb.mode_by_name(name)
    sent = [hb.payload_for(7, mode, seq) for seq in range(frames)]
    capture, tx, realised = _burst_capture(name, head, sent)
    # Frame 2 arrives, acquires, and does not match what the bench expected
    # of it -- exactly the "synced" outcome that used to stall the window.
    expected = list(sent)
    expected[2] = hb.payload_for(999, mode, 2)

    record = hb.measure_keying(_burst_cell(name, head, frames), name, capture,
                               expected, mode.confidence_threshold, realised,
                               tx_audio=tx)

    outcomes = [f["outcome"] for f in record["frames"]]
    assert outcomes == ["decoded", "decoded", "synced", "decoded"]
    # The frames behind the failure were reached, and located where they
    # actually are rather than where frame 0 was.
    assert all(f["landing"] == "on-preamble" for f in record["frames"])
    starts = [f["start_index"] for f in record["frames"]]
    assert starts == sorted(starts)
    stride = record["frame_stride_rx"]
    assert all(b - a == pytest.approx(stride, abs=hb.LANDING_TOLERANCE_SAMPLES)
               for a, b in zip(starts, starts[1:]))


def test_every_burst_frames_landing_is_measured_against_its_own_preamble():
    """`true_preamble_index` is frame 0's. Measuring frame n against it puts
    every later landing a whole n strides out and calls it `late`."""
    name, head, frames = "hc0", 0.3, 4
    mode = hb.mode_by_name(name)
    payloads = [hb.payload_for(11, mode, seq) for seq in range(frames)]
    capture, tx, realised = _burst_capture(name, head, payloads)

    record = hb.measure_keying(_burst_cell(name, head, frames), name, capture,
                               payloads, mode.confidence_threshold, realised,
                               tx_audio=tx)

    stride = record["frame_stride_rx"]
    assert stride > 0
    for seq, frame in enumerate(record["frames"]):
        assert frame["expected_preamble_index"] == (
            record["true_preamble_index"] + seq * stride)
        assert frame["landing"] == "on-preamble"
        assert abs(frame["landing_error_samples"]) <= \
            hb.LANDING_TOLERANCE_SAMPLES
    assert record["decoded_count"] == frames


def test_a_single_frame_keying_is_unaffected_by_the_burst_bookkeeping():
    """One frame has no stride and nothing to advance past; its landing is
    still measured against `true_preamble_index` itself."""
    name, head = "hc0", 0.3
    mode = hb.mode_by_name(name)
    payload = hb.payload_for(5, mode, 0)
    capture, tx, realised = _burst_capture(name, head, [payload])
    record = hb.measure_keying(_burst_cell(name, head, 1), name, capture,
                               [payload], mode.confidence_threshold, realised,
                               tx_audio=tx)
    assert record["frame_stride_rx"] == 0
    assert record["frames"][0]["expected_preamble_index"] == \
        record["true_preamble_index"]
    assert record["frames"][0]["landing"] == "on-preamble"


# -- defect 2: refusing a settle time the evidence cannot support ---------

def _spiked(envelope, settle, position, width=7, height_db=4.0):
    """The same envelope with an isolated excursion late in the keyed span."""
    envelope = np.array(envelope, dtype=np.float64)
    onset = int(round(settle["onset_seconds"] / hb.ENVELOPE_HOP_SECONDS))
    span = int(round(settle["keyed_seconds"] / hb.ENVELOPE_HOP_SECONDS))
    index = onset + int(position * span)
    envelope[index:index + width] += height_db
    return envelope, index


@pytest.mark.parametrize("position", [0.5, 0.7, 0.9])
def test_an_isolated_late_excursion_does_not_move_the_settle_time(position):
    """One spurious hop late in a keying used to rescore the whole keying as
    still settling -- 4260 ms against a true 570 ms, same cell, same
    physics, different noise draw."""
    envelope = hb.level_envelope(_ramped_tone(0.1))
    clean = hb.settle_measurement(envelope)
    spiked_env, index = _spiked(envelope, clean, position)
    spiked = hb.settle_measurement(spiked_env)

    # The excursion really is outside tolerance, so a "last hop that was
    # still unsettled" rule would have scored the keying there.
    assert spiked_env[index] - clean["steady_db"] > hb.SETTLE_TOLERANCE_DB
    assert index * hb.ENVELOPE_HOP_SECONDS > 2 * clean["settle_seconds"]
    assert spiked["settle_seconds"] == pytest.approx(clean["settle_seconds"])
    assert spiked["settle_unmeasured_reason"] is None


def test_a_settle_time_filling_the_keyed_span_is_refused_not_reported():
    """The docstring's caveat, made into a check the tool performs itself: a
    keying that is not several time constants long has no settled part to
    estimate a steady state from."""
    measured = hb.settle_measurement(hb.level_envelope(
        _ramped_tone(1.5, seconds=2.0)))
    assert measured["settle_seconds"] is None
    assert measured["settle_unmeasured_reason"] == "settle-exceeds-keyed-span"
    # The keyed span is still reported: the reason is only checkable with it.
    assert measured["keyed_seconds"] == pytest.approx(2.0, abs=0.1)


def _cold_envelope(floor_db=-22.4, steady_db=-13.7, onset_hop=60,
                   keyed_hops=550, seed=0):
    """`logs/head/cold_ab.jsonl`'s shape: a high floor and a clean step.

    Traced from the real records rather than invented -- a flat floor the
    AGC has wound up to -22 dB, a two-hop rise at 0.58-0.60 s, then 5.5 s
    within a few tenths of a dB of -14 dB. The whole point of these captures
    is that the ramp in them is plainly readable while the floor-referenced
    SNR is only 8 dB, because the floor moved and the signal did not.
    """
    rng = np.random.default_rng(seed)
    env = np.concatenate([
        np.full(onset_hop, floor_db),
        [0.5 * (floor_db + steady_db)],          # the one hop mid-rise
        np.full(keyed_hops, steady_db),
        np.full(60, floor_db),
    ])
    return env + 0.3 * rng.standard_normal(len(env))


def test_a_cold_capture_with_a_high_floor_is_measured_not_refused():
    """The defect: the guard refused exactly the measurements wanted most.

    After a long listen the receiver's AGC has wound its gain up and the
    noise floor with it, so a floor-referenced SNR reads ~8 dB on a capture
    whose ramp is unmistakable. All 10 records of `logs/head/cold_ab.jsonl`
    were thrown away that way.
    """
    measured = hb.settle_measurement(_cold_envelope())
    # It really is the capture the old 10 dB floor-referenced gate refused.
    assert measured["keyed_snr_db"] < 10.0
    assert measured["settle_unmeasured_reason"] is None
    assert measured["onset_seconds"] == pytest.approx(0.60, abs=0.02)
    # A step is settled as soon as it has stepped: within a hop or two.
    assert measured["settle_seconds"] == pytest.approx(0.0, abs=0.02)


def test_a_steady_state_that_never_stops_moving_is_refused():
    """The refusal path the SNR proxy stood in for. If the window the steady
    level is estimated from cannot itself hold a settled run at that level,
    "within tolerance of steady" is a condition about the wander and not
    about the AGC, and no settle time read against it means anything."""
    # A level that keeps swinging 3 dB about its own median, slowly enough
    # that the 5-hop smoother passes it straight through.
    swing = -20.0 + 3.0 * np.sin(2 * np.pi * np.arange(500) / 40.0)
    env = np.concatenate([np.full(60, -60.0), swing, np.full(60, -60.0)])
    measured = hb.settle_measurement(env)
    assert measured["onset_seconds"] is not None   # RF was still detected
    assert measured["settle_seconds"] is None
    assert measured["settle_unmeasured_reason"] == "steady-state-unstable"


def test_a_keying_whose_steady_state_is_silence_is_refused():
    """A dropout mid-keying: RF at both ends of the span and nothing in
    between, so the window the steady level is estimated from is the hole.
    A settle time measured to a "steady state" that is the noise floor is
    not a measurement of the AGC."""
    env = np.concatenate([np.full(60, -30.0),
                          np.full(100, -14.0),     # RF
                          np.full(300, -29.0),     # the dropout
                          np.full(30, -14.0),      # RF again
                          np.full(60, -30.0)])
    measured = hb.settle_measurement(env)
    assert measured["onset_seconds"] is not None
    assert measured["steady_rise_db"] < hb.MIN_SETTLE_RISE_DB
    assert measured["settle_unmeasured_reason"] == "rise-too-small"


def test_a_measurable_keying_still_reports_no_reason():
    measured = hb.settle_measurement(hb.level_envelope(_ramped_tone(0.1)))
    assert measured["settle_seconds"] is not None
    assert measured["settle_unmeasured_reason"] is None
    # Kept as a diagnostic now that it is no longer a gate.
    assert measured["keyed_snr_db"] > 10.0


def test_a_capture_with_no_rf_says_why_it_has_no_settle_time():
    quiet = (1e-4 * np.random.default_rng(0).standard_normal(
        2 * hb.RX_RATE)).astype(np.float32)
    measured = hb.settle_measurement(hb.level_envelope(quiet))
    assert measured["settle_unmeasured_reason"] == "no-rf-onset"


def test_the_record_carries_the_reason_a_settle_time_is_missing():
    name = "hc0"
    payload = hb.payload_for(42, hb.mode_by_name(name), 0)
    quiet = (1e-3 * np.random.default_rng(1).standard_normal(
        6 * hb.RX_RATE)).astype(np.float32)
    cell = hb.Cell(scenario="single", direction="ab", mode=name,
                   head_seconds=0.6, gap_seconds=1.0, rep=1)
    record = hb.measure_keying(cell, name, quiet, [payload], 0.12, 0.6)
    assert record["settle_seconds"] is None
    assert record["settle_unmeasured_reason"] == "no-rf-onset"


def test_unmeasured_keyings_are_counted_rather_than_averaged_in():
    """An unmeasurable cell must read as unmeasured, which is a different
    statement from settled-quickly or settled-slowly."""
    cell = hb.expand(scenarios=("single",), modes=("hc0",),
                     directions=("ab",), heads=(0.6,), reps=1)[0]
    records = [_record(cell, settle_seconds=0.55),
               _record(cell, settle_seconds=None,
                       settle_unmeasured_reason="steady-state-unstable")]
    bucket = ha.aggregate(records, ("mode",))[("hc0",)]
    assert bucket["settle"] == [0.55]
    assert dict(bucket["unmeasured"]) == {"steady-state-unstable": 1}


def test_the_aggregate_prints_why_a_cell_was_unmeasurable(capsys):
    cell = hb.expand(scenarios=("single",), modes=("hc0",),
                     directions=("ab",), heads=(0.6,), reps=1)[0]
    groups = ha.aggregate([_record(cell, settle_seconds=None,
                                   settle_unmeasured_reason="steady-state-unstable")],
                          ("mode",))
    ha.print_aggregate(groups, ("mode",))
    printed = capsys.readouterr().out
    assert "unmeasured: steady-state-unstable:1" in printed


# -- defect 3: the bench must decode through the receiver that ships -------

def _agc_ramped_burst(name, head, frames, tau=0.25, seed=3):
    """One keying of `frames` frames, through the simulated AGC ramp.

    The ramp is the point: `SimulatedPair` multiplies the keying by
    `1 - exp(-t/tau)`, so the LEADING frame is the attenuated one, exactly
    as a receiver's AGC attenuates the frame it is still settling on. That
    is what makes frame 0 lose a whole-capture argmax to the frames behind
    it, and what any honest burst measurement has to survive.
    """
    mode = hb.mode_by_name(name)
    payloads = [hb.payload_for(11, mode, seq) for seq in range(frames)]
    tx, realised, _ = hb.keying_audio(name, head, payloads)
    # The quiet lead is what the noise floor -- and so the RF onset the
    # bench measures its own ground truth from -- is taken from, as the
    # median of the capture's first tenth. A burst is long, so a fixed lead
    # would put that tenth inside the keying and no onset would be found at
    # all. See `_burst_capture`'s note on the same hazard.
    link = hb.SimulatedPair(seed=seed, agc_seconds=tau,
                            quiet_seconds=max(3.0, len(tx) / hb.TX_RATE / 4.0))
    link.send(tx)
    return link.read_rx()[2], tx, realised, payloads, mode


@pytest.mark.parametrize("name,head", [("hc0", 0.6), ("hf6", 0.6)])
def test_a_bursts_leading_frame_is_found_first_despite_the_agc_ramp(name, head):
    """The reason this bench moved onto `whale/streaming.py`.

    The shipped receiver walks a keying forward and takes the first frame
    over the confidence gate. The whole-capture decode this bench used
    before took the best-scoring frame anywhere in the capture, which on a
    burst is never frame 0 -- the AGC ramp attenuates it -- so every burst
    read as nothing decoded with every frame a whole stride late. Frame 0
    must be found, and found as frame 0.
    """
    frames = 4
    capture, tx, realised, payloads, mode = _agc_ramped_burst(name, head, frames)

    record = hb.measure_keying(_burst_cell(name, head, frames), name, capture,
                               payloads, mode.confidence_threshold, realised,
                               tx_audio=tx)

    stride = record["frame_stride_rx"]
    assert stride > 0
    assert record["decoded_count"] == frames
    assert record["frames"][0]["outcome"] == "decoded"
    for seq, frame in enumerate(record["frames"]):
        # Each frame is scored against its OWN preamble, which the bench
        # knows because it built the TX buffer.
        assert frame["expected_preamble_index"] == (
            record["true_preamble_index"] + seq * stride)
        assert frame["landing"] == "on-preamble"
        assert abs(frame["landing_error_samples"]) <= \
            hb.LANDING_TOLERANCE_SAMPLES
    starts = [f["start_index"] for f in record["frames"]]
    assert starts == sorted(starts)
    # Nothing was acquired that is not one of the frames we keyed.
    assert record["unmatched_acquisitions"] == []


@pytest.mark.parametrize("name,head", [("hc0", 0.6), ("hf6", 0.6)])
def test_the_whole_capture_search_this_replaced_misses_the_leading_frame(name, head):
    """Pin the defect, so the test above cannot pass for a weaker reason.

    `debug_decode` is still the right tool for `head_analyse.py --trim`,
    where a slice is chosen by the analyser and asked what is in it. Asked
    instead to find frame 0 of a ramped burst, it lands a whole stride or
    more past it -- which is precisely what the run log used to record.
    """
    frames = 4
    capture, tx, realised, payloads, _ = _agc_ramped_burst(name, head, frames)
    stride = hb.frame_stride_rx(tx, frames)
    settle = hb.settle_measurement(
        hb.level_envelope(capture),
        hb.level_envelope(np.asarray(tx, np.float64)[::hb.DECIMATION]))
    true_preamble = (int(round(settle["onset_seconds"] * hb.RX_RATE))
                     + hb.preamble_offset_rx(name, realised))

    whole = hb.debug_decode(name, capture, payloads[0])

    assert whole["start_index"] is not None
    # A whole stride or more past frame 0: the best-scoring frame in the
    # capture is not the first one.
    assert whole["start_index"] - true_preamble >= stride - \
        hb.LANDING_TOLERANCE_SAMPLES
    assert not whole["payload_ok"]


def test_the_streaming_ofdm_search_walks_a_burst_in_order():
    """hf6 has no `streaming_phy`, so its shipped decode is a whole-buffer
    one over the retained snapshot. hf8 has one, so this is the path that
    really runs `whale/streaming.py`'s `OfdmSearch` -- fixed 0.25 s windows,
    first candidate over 0.12 wins -- and it must find every frame of a
    burst, in order, one stride apart."""
    name, head, frames = "hf8", 0.6, 3
    mode = hb.mode_by_name(name)
    assert getattr(mode.codec, "streaming_phy", None) is not None
    payloads = [hb.payload_for(11, mode, seq) for seq in range(frames)]
    capture, tx, realised = _burst_capture(name, head, payloads,
                                           quiet_seconds=2.0)

    acquisitions = hb.receive_keying(name, capture)

    assert len(acquisitions) == frames
    starts = [a["start_index"] for a in acquisitions]
    assert starts == sorted(starts)
    stride = hb.frame_stride_rx(tx, frames)
    assert all(b - a == pytest.approx(stride, abs=hb.LANDING_TOLERANCE_SAMPLES)
               for a, b in zip(starts, starts[1:]))
    assert all(a["result"].get("payload") == p
               for a, p in zip(acquisitions, payloads))


def test_the_replay_transport_offers_only_a_receive_surface():
    """It replays a recording. There is nothing on it that can key a radio,
    and it hands the capture over in chunks rather than in one lump -- which
    is what makes "the first adequate frame wins" mean anything at all."""
    audio = np.arange(1000, dtype=np.float32)
    transport = hb._ReplayTransport(audio, chunk_samples=256)
    assert not hasattr(transport, "send")
    assert transport.is_transmitting() is False
    generation, start, samples = transport.read_rx()
    assert (generation, start, len(samples)) == (0, 0, 256)
    assert not transport.exhausted
    seen = len(samples)
    while not transport.exhausted:
        _, _, samples = transport.read_rx((0, seen))
        seen += len(samples)
    assert seen == len(audio)


def test_an_acquisition_is_filed_under_the_frame_it_is_nearest():
    """Ground truth decides which frame an acquisition belongs to, because
    the bench built the TX buffer and knows where each preamble is. A frame
    the receiver skipped stays skipped instead of renumbering every frame
    behind it into a failure."""
    stride, true_preamble = 1000, 500
    acquisitions = [{"start_index": 500}, {"start_index": 2500}]
    assigned, unmatched = hb.assign_frames(acquisitions, 3, true_preamble, stride)
    assert assigned[0] is acquisitions[0]
    assert assigned[1] is None            # frame 1 was never seen
    assert assigned[2] is acquisitions[1]  # and frame 2 is still frame 2
    assert unmatched == []


def test_acquisitions_arrive_in_order_and_never_claim_an_earlier_frame():
    stride, true_preamble = 1000, 0
    # A second acquisition landing back at frame 0's position cannot take a
    # slot already filled, nor reach backwards past one already passed.
    acquisitions = [{"start_index": 1000}, {"start_index": 0}]
    assigned, unmatched = hb.assign_frames(acquisitions, 3, true_preamble, stride)
    assert assigned[1] is acquisitions[0]
    assert assigned[2] is acquisitions[1]
    assert unmatched == []


def test_a_frame_with_no_acquisition_is_never_seen_not_a_wrong_landing():
    name, head, frames = "hc0", 0.3, 2
    mode = hb.mode_by_name(name)
    payloads = [hb.payload_for(11, mode, seq) for seq in range(frames)]
    # Only frame 0 is on the air; frame 1 was asked for and never sent.
    capture, tx, realised = _burst_capture(name, head, payloads[:1])
    record = hb.measure_keying(_burst_cell(name, head, frames), name, capture,
                               payloads, mode.confidence_threshold, realised,
                               tx_audio=tx)
    assert record["frames"][0]["outcome"] == "decoded"
    assert record["frames"][1]["outcome"] == "never-seen"
    assert record["frames"][1]["landing"] == "never-acquired"


def test_the_record_names_the_receive_path_that_produced_it():
    """So a log from before this change cannot be silently compared with
    one from after: they disagree on every burst."""
    name, head = "hc0", 0.3
    mode = hb.mode_by_name(name)
    payload = hb.payload_for(5, mode, 0)
    capture, tx, realised = _burst_capture(name, head, [payload])
    record = hb.measure_keying(_burst_cell(name, head, 1), name, capture,
                               [payload], mode.confidence_threshold, realised,
                               tx_audio=tx)
    assert record["receive_path"] == hb.RECEIVE_PATH == "streaming"
    assert record["receive_chunk_seconds"] == hb.RECEIVE_CHUNK_SECONDS
    assert record["acquisition_count"] == 1


def test_the_aggregate_refuses_to_compare_two_receive_paths(capsys):
    cell = hb.expand(scenarios=("burst",), modes=("hc0",),
                     directions=("ab",), heads=(0.6,), reps=1)[0]
    groups = ha.aggregate([_record(cell), _record(cell, receive_path="streaming")],
                          ("mode",))
    ha.print_aggregate(groups, ("mode",))
    printed = capsys.readouterr().out
    assert "WARNING" in printed and "whole-capture" in printed
    # A run written entirely by one receiver says nothing extra.
    groups = ha.aggregate([_record(cell, receive_path="streaming")], ("mode",))
    assert ha.receive_path_note(groups) is None


@pytest.mark.parametrize("name,head,source", [
    ("hf6", 0.6, hb.BER_FROM_SHIPPED_DECODE),
    ("hc0", 0.3, hb.BER_FROM_DEBUG_RERUN)])
def test_raw_ber_survives_the_move_and_says_where_it_came_from(name, head, source):
    """Every OFDM decode publishes `pre_fec_bits`, so its BER is the shipped
    receiver's own arithmetic. The FSK phys turn their bits into a BER only
    in `demodulate_debug`, which the shipped receiver never calls because it
    has no reference payload -- so that BER comes from re-running the phy's
    own debug entry point over the span the shipped receiver acquired, and
    the frame says so rather than pretending otherwise."""
    mode = hb.mode_by_name(name)
    payload = hb.payload_for(5, mode, 0)
    capture, tx, realised = _burst_capture(name, head, [payload])
    record = hb.measure_keying(_burst_cell(name, head, 1), name, capture,
                               [payload], mode.confidence_threshold, realised,
                               tx_audio=tx)
    frame = record["frames"][0]
    assert frame["outcome"] == "decoded"
    assert frame["raw_ber"] == pytest.approx(0.0, abs=1e-9)
    assert frame["raw_ber_source"] == source
    # The other per-frame diagnostics came through the move intact.
    assert frame["cfo_hz"] is not None
    assert frame["carrier_snr_db"] or frame["tone_snr_db"] is not None
