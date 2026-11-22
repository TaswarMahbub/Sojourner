"""The software-only motor mirror bridge (demo/motor_bridge.py) and the scripted demo run.

Constructed sessions only: synthetic frames, the labelled test key, the
in-process receiver oracle. Nothing here is hardware.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import re
import tempfile
from pathlib import Path

import pytest

from demo.motor_bridge import DEFAULT_PORT, PORT_ENV, Fanout, OpLog, SharedCamera, TapReceiver, bridge_port
from demo.run_live_sim import run_scripted
from tests.control_auth import TEST_KEY
from tests.motor_receiver import MotorReceiver
from tests.session_harness import SimClock

ROOT = Path(__file__).resolve().parent.parent


def scripted_log() -> OpLog:
    clock = SimClock()
    log = OpLog(clock_ms=lambda: clock.unix)
    with tempfile.TemporaryDirectory() as tmp:
        run_scripted(clock, Path(tmp), log, out=lambda _line: None)
    return log


def replay(log: OpLog) -> list:
    """Apply the streamed operations to a fresh oracle, as the browser mirror does with its own port."""
    hello = log.hello_record
    rx = MotorReceiver(hello["boot_nonce"], key=bytes.fromhex(hello["key_hex"]), controller_id=hello["controller_id"])
    results = []
    for record in log.records:
        op = record["op"]
        kind = op["op"]
        if kind == "mono":
            results.append(rx.set_monotonic(op["ms"]))
        elif kind == "sensor":
            results.append(rx.set_sensor(op["state"]))
        elif kind == "startup":
            results.append(rx.complete_startup())
        elif kind == "ack_cutoff":
            results.append(rx.acknowledge_cutoff())
        elif kind == "status":
            results.append(rx.status())
        elif kind == "datagram":
            results.append(rx.handle_raw(base64.b64decode(op["raw_b64"])))
        else:
            raise AssertionError(f"unexpected op {kind}")
    return results


def test_port_default_and_override():
    assert bridge_port({}) == DEFAULT_PORT == 8767
    assert bridge_port({PORT_ENV: "9001"}) == 9001
    for bad in ("", "x", "0", "70000", "-1"):
        with pytest.raises(ValueError):
            bridge_port({PORT_ENV: bad})


def test_port_literal_appears_once_per_side():
    """sim-decisions section 3: the port is configurable and its default is written in one place."""
    python = [p for p in (ROOT / "demo").rglob("*.py") if re.search(r"\b8767\b", p.read_text(encoding="utf-8"))]
    assert python == [ROOT / "demo" / "motor_bridge.py"]
    assert len(re.findall(r"\b8767\b", (ROOT / "demo" / "motor_bridge.py").read_text(encoding="utf-8"))) == 1
    js = [p for p in (ROOT / "frontend" / "src").rglob("*") if p.suffix in {".js", ".jsx"}
          and not p.name.endswith(".test.js") and re.search(r"\b8767\b", p.read_text(encoding="utf-8"))]
    assert js == [ROOT / "frontend" / "src" / "live" / "config.js"]


def test_tap_refuses_any_key_but_the_labelled_test_key():
    with pytest.raises(ValueError):
        TapReceiver(MotorReceiver(7, key=b"\x01" * 32), OpLog())
    log = OpLog()
    TapReceiver(MotorReceiver(7), log)
    assert log.hello_record["key_hex"] == TEST_KEY.hex() and log.hello_record["software_only"] is True


def test_every_datagram_is_forwarded_byte_identical_including_bad_ones():
    log = OpLog(clock_ms=lambda: 0)
    tap = TapReceiver(MotorReceiver(7), log)
    tap.set_monotonic(10)
    payloads = [b'{"truncated": ', b"\xff\xfe not utf-8", json.dumps({"schema_version": "4"}).encode()]
    results = [tap.handle_raw(p) for p in payloads]
    datagrams = [r for r in log.records if r["op"]["op"] == "datagram"]
    assert [base64.b64decode(r["op"]["raw_b64"]) for r in datagrams] == payloads
    assert [r["result"] for r in datagrams] == results
    assert [r["n"] for r in log.records] == list(range(len(log.records)))


def test_reads_pass_through_without_an_operation():
    log = OpLog()
    tap = TapReceiver(MotorReceiver(7), log)
    before = len(log.records)
    assert tap.motors_have_torque() is False
    assert tap.fault_latch is False
    tap.snapshot()
    assert len(log.records) == before
    with pytest.raises(NotImplementedError):
        tap.handle({"schema_version": "4"})


def test_scripted_run_streams_a_replayable_session():
    log = scripted_log()
    outcomes = {r["result"] for r in log.records if r["op"]["op"] == "datagram"}
    assert {"accepted_start", "accepted_rearm", "accepted_motion", "accepted_stop", "invalid_stop"} <= outcomes
    # The stream alone reproduces every oracle result: nothing that changes receiver state was missed.
    assert replay(log) == [r["result"] for r in log.records]


def test_committed_frontend_fixture_matches_the_generator():
    fixture = ROOT / "frontend" / "src" / "live" / "__fixtures__" / "live-session.jsonl.gz"
    lines = gzip.decompress(fixture.read_bytes()).decode("utf-8").strip().split("\n")
    assert lines == scripted_log().history()


def test_fanout_replays_history_to_a_late_client_then_streams_in_order():
    from websockets.asyncio.client import connect

    server = Fanout(0, replay=True)
    try:
        for i in range(3):
            server.publish(json.dumps({"n": i}))

        async def client():
            async with connect(f"ws://127.0.0.1:{server.port}") as ws:
                got = [json.loads(await ws.recv()) for _ in range(3)]
                for i in range(3, 6):
                    server.publish(json.dumps({"n": i}))
                got += [json.loads(await ws.recv()) for _ in range(3)]
                return got

        assert asyncio.run(asyncio.wait_for(client(), 10)) == [{"n": i} for i in range(6)]
    finally:
        server.close()


def test_shared_camera_relays_each_frame_once_and_passes_items_through():
    class Source:
        def __init__(self):
            self.items = [("frame", {"timestamp_ms": 1}, 0.0, None), None, ("error", "gone")]
            self.closed = False

        def get(self, _timeout):
            return self.items.pop(0)

        def close(self):
            self.closed = True

    class Relay:
        def __init__(self):
            self.sent = []

        def publish(self, text):
            self.sent.append(text)

    source, relay = Source(), Relay()
    shared = SharedCamera(source, relay)
    items = [shared.get(50) for _ in range(3)]
    assert items[0][0] == "frame" and items[1] is None and items[2] == ("error", "gone")
    assert [json.loads(t) for t in relay.sent] == [{"timestamp_ms": 1}]
    shared.close()
    assert source.closed
