from acceptance_test import _transfer_summary


def test_transfer_summary_includes_net_payload_throughput():
    assert _transfer_summary("B", 1024, 31.6) == (
        "   B received 1024 bytes in 31.6s (259.2 bit/s net)"
    )
