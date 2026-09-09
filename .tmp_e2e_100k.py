import hashlib
import logging
import os
import threading
import time

from whale import mode_history
from whale.link import Link
from whale.policy import HF_SSB
from whale.transport import RadioTransport


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
os.environ["WHALE_FORCE_MODE"] = "14"
SIZE = int(os.environ.get("WHALE_E2E_SIZE", "100000"))
TRANSFER_TIMEOUT = float(os.environ.get("WHALE_E2E_TIMEOUT", "1800"))
PAYLOAD = (
    hashlib.sha256(b"whale-hf7-e2e-100k-fixed").digest()
    * (SIZE // 32 + 1)
)[:SIZE]


def main():
    transport_a = RadioTransport("ic7300")
    transport_b = RadioTransport("ic705")
    history_a = {}
    history_b = {}
    mode_history.record_good_mode(history_a, "STA1", "STA2", 14)
    mode_history.record_good_mode(history_b, "STA2", "STA1", 14)
    station_a = Link(
        transport_a, "STA1", mode_history_store=history_a, policy=HF_SSB)
    station_b = Link(
        transport_b, "STA2", mode_history_store=history_b, policy=HF_SSB)
    listen_result = {}
    send_result = {}
    started = time.monotonic()

    try:
        station_a.start()
        station_b.start()
        print("warming up 3s...", flush=True)
        time.sleep(3)

        def listen():
            listen_result["peer"] = station_b.listen_once(timeout=180)

        listener = threading.Thread(target=listen, daemon=True)
        listener.start()
        time.sleep(1)
        connected = station_a.connect(
            "STA2", timeout_per_try=20, retries=10)
        listener.join(timeout=180)
        print(
            "CONNECTED", connected, listen_result.get("peer"),
            "tx", station_a.tx_profile.name,
            "rx", station_a.rx_profile.name,
            "head", station_a._tx_head_seconds,
            flush=True,
        )
        if not connected or listen_result.get("peer") != "STA1":
            raise RuntimeError("connection failed")

        transfer_started = time.monotonic()

        def send():
            try:
                station_a.send_message(PAYLOAD)
                send_result["ok"] = True
            except BaseException as exc:
                send_result["ok"] = False
                send_result["error"] = repr(exc)

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        received = station_b.recv_message(timeout=TRANSFER_TIMEOUT)
        sender.join(timeout=60)
        elapsed = time.monotonic() - transfer_started
        valid = received == PAYLOAD
        received_size = len(received or b"")
        print(
            "TRANSFER_RESULT",
            "send_ok", send_result.get("ok"),
            "error", send_result.get("error"),
            "valid", valid,
            "received", received_size,
            "seconds", round(elapsed, 2),
            "bps", round(8 * received_size / elapsed, 1),
            "sha256", hashlib.sha256(received or b"").hexdigest(),
            flush=True,
        )
        print(
            "A_METRICS", station_a.qualification_metrics,
            "final_tx", station_a.tx_profile.name,
            "head", station_a._tx_head_seconds,
            flush=True,
        )
        print(
            "B_METRICS", station_b.qualification_metrics,
            "final_rx", station_b.rx_profile.name,
            "head", station_b._rx_head_seconds,
            flush=True,
        )

        disconnect_done = threading.Event()
        peer_state = {}

        def service_disconnect():
            while not disconnect_done.is_set():
                if not station_b.service_while_idle():
                    break
                time.sleep(0.02)
            peer_state["idle"] = station_b.state == "IDLE"

        peer = threading.Thread(target=service_disconnect, daemon=True)
        peer.start()
        try:
            disconnected = station_a.disconnect(timeout=20, retries=5)
        finally:
            disconnect_done.set()
            peer.join(timeout=3)
        print(
            "DISCONNECT", disconnected, peer_state.get("idle"),
            "TOTAL_SECONDS", round(time.monotonic() - started, 2),
            flush=True,
        )
        if not (
            send_result.get("ok")
            and valid
            and disconnected
            and peer_state.get("idle")
        ):
            raise RuntimeError("end-to-end validation failed")
    finally:
        station_a.stop()
        station_b.stop()
        transport_a.close()
        transport_b.close()


if __name__ == "__main__":
    main()
