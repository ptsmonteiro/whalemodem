import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_fixed_mode_transfer.py"
SPEC = importlib.util.spec_from_file_location("benchmark_fixed_mode_transfer", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_distribution_free_median_interval_is_order_statistics():
    interval, rank = benchmark.median_confidence_interval(list(range(20)))
    assert interval == [5, 14]
    assert rank == 6


def test_packet_preserves_air_header_and_body():
    body = bytes([1, 0]) + b"payload"
    packet = benchmark._packet(benchmark.link_protocol.PT_DATA,
                               benchmark.HF3, body)
    decoded = benchmark.link_protocol.decode_air_header(packet[:10])
    assert decoded == (benchmark.link_protocol.PT_DATA,
                       benchmark.HF3.mode_id, len(body) - 2, body[:2])
    assert packet[10:] == body[2:]
