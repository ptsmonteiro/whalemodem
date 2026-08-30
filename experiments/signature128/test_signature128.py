import numpy as np

from experiments.signature128 import screen_signature128 as signature


def test_tones_are_orthogonal_over_one_hop():
    cycles = np.asarray(signature.lead2.TONE_HZ) * (
        signature.NOTE_SAMPLES / signature.lead2.RX_SAMPLE_RATE)
    assert np.allclose(cycles, np.round(cycles))


def test_hf_alphabet_has_two_balanced_labels():
    _, labels = signature.balanced_alphabet(10)
    assert labels.shape == (2, 10)
    assert np.array_equal(np.sort(labels[0]), np.sort(labels[1]))
    assert np.array_equal(labels[:, :5], labels[:, 5:])


def test_wrong_cyclic_alignment_has_bounded_coincidence():
    _, labels = signature.balanced_alphabet(10)
    for left_index, left in enumerate(labels):
        for right_index, right in enumerate(labels):
            for shift in range(5):
                if left_index == right_index and shift == 0:
                    continue
                assert np.sum(left == np.roll(right, shift)) <= 4


def test_signature_is_constant_envelope_and_106ms():
    audio = signature.signature(0, 10)
    assert len(audio) == 10 * signature.hc0.SYMBOL_SAMPLES
    crest = np.max(np.abs(audio)) / np.sqrt(np.mean(audio ** 2))
    assert np.isclose(crest, np.sqrt(2), atol=0.02)


def test_candidates_include_both_labels_for_each_boundary():
    audio = np.concatenate((np.zeros(600, np.float32),
                            signature.signature(0, 8),
                            np.zeros(600, np.float32)))
    candidates = signature.signature_candidates(audio, 8, limit=3)
    assert len(candidates) == 6
    for at in range(0, len(candidates), 2):
        pair = candidates[at:at + 2]
        assert pair[0][2] == pair[1][2]
        assert {pair[0][1], pair[1][1]} == {0, 1}


def test_pair_max_candidates_include_true_clean_boundary():
    prefix = np.zeros(600, np.float32)
    lead = signature.signature(1, 12)[::signature.lead2.DECIMATION]
    audio = np.concatenate((prefix, lead, prefix))
    candidates = signature.signature_candidates(
        audio, 12, limit=3, scorer="pair-max")
    expected = len(prefix) + len(lead)
    assert any(label == 1 and abs(start - expected) <= 16
               for _, label, start in candidates)


def test_selection_and_validation_seeds_are_disjoint():
    selection = {signature.SELECTION_SEED + i for i in range(1000)}
    validation = {signature.VALIDATION_SEED + i for i in range(1000)}
    assert selection.isdisjoint(validation)
