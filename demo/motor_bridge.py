"""Software-only motor mirror: the in-process receiver oracle, streamed to the 3D simulator.

The live control run (tests/run_live_e2e.py) sends every motor datagram to the
MotorReceiver contract oracle in its own process. This module puts a tap on
that oracle and streams, over a local WebSocket, every call that can change
the receiver's state: clock advances, status reads, local inputs, and every
datagram, valid or not, byte-identical (docs/sim-decisions.md section 3.1:
the bridge does not filter).

The browser applies the same calls, in the same order, to its own unmodified
frontend/src/sim/motorReceiver.js. That port is parity-tested against the
Python oracle (frontend/src/sim/motorReceiver.parity.test.js), so it reaches
the same verdict on its own. It judges every datagram, and the rover moves
only on the output it applied. No second motor-command format exists: a
datagram is forwarded as the exact bytes the publisher sent.

Software only. The tap refuses a receiver that does not hold the labelled
test key from tests/control_auth.py, and the publisher in this run sends only
to the in-process oracle. No UDP socket is opened and no rover can receive
anything.

Framing (sim-decisions section 3): one JSON object per WebSocket text message,
one operation per message, never coalesced or reordered. Each carries a
running index ``n`` and the bridge receive time ``t_unix_ms``. A client that
connects late first receives the hello and every earlier operation, so its
receiver starts from the same boot.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time

from tests.control_auth import TEST_KEY

PORT_ENV = "SOJOURNER_MOTOR_BRIDGE_PORT"
# The one default for the motor bridge port (sim-decisions section 3: proposed, not confirmed).
DEFAULT_PORT = 8767
HELLO_SCHEMA = 1


def bridge_port(environ=os.environ) -> int:
    """The bridge port from SOJOURNER_MOTOR_BRIDGE_PORT, or the default."""
    raw = environ.get(PORT_ENV)
    if raw is None:
        return DEFAULT_PORT
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise ValueError(f"{PORT_ENV} must be a port number")
    return int(raw)


class TapReceiver:
    """Wraps the oracle and reports every state-changing call, after the oracle ran it.

    Reads (``fault_latch``, ``motors_have_torque()``, ``snapshot()``) pass
    straight through and are not reported, because they change nothing.
    """

    def __init__(self, receiver, sink):
        if getattr(receiver, "key", None) != TEST_KEY:
            raise ValueError("the mirror bridge carries only the labelled test key; refusing this receiver")
        object.__setattr__(self, "_rx", receiver)
        object.__setattr__(self, "_sink", sink)
        sink.hello({
            "type": "hello",
            "schema": HELLO_SCHEMA,
            "boot_nonce": receiver.boot_nonce,
            "controller_id": receiver.controller_id,
            "key_hex": TEST_KEY.hex(),
            "receiver": "in-process contract oracle (tests/motor_receiver.py)",
            "software_only": True,
        })

    def __getattr__(self, name):
        return getattr(self._rx, name)

    def __setattr__(self, name, value):
        setattr(self._rx, name, value)

    def _emit(self, op: dict, result=None):
        # result is the oracle's own answer (status dict, cutoff bool, datagram outcome, or None).
        # The browser shows whether its receiver agrees; it never acts on this field.
        self._sink.op({"type": "op", "op": op, "result": result})
        return result

    # ----- calls that change receiver state, in the parity fixture's vocabulary -----

    def set_monotonic(self, ms: int) -> None:
        self._rx.set_monotonic(ms)
        self._emit({"op": "mono", "ms": ms})

    def set_sensor(self, state: str) -> None:
        self._rx.set_sensor(state)
        self._emit({"op": "sensor", "state": state})

    def set_physical_estop(self, asserted: bool) -> None:
        # The oracle defines this as set_sensor; the browser receiver has only setSensor.
        self.set_sensor("asserted" if asserted else "released")

    def complete_startup(self) -> None:
        self._rx.complete_startup()
        self._emit({"op": "startup"})

    def acknowledge_cutoff(self) -> bool:
        return self._emit({"op": "ack_cutoff"}, self._rx.acknowledge_cutoff())

    def status(self):
        return self._emit({"op": "status"}, self._rx.status())

    def lose_motor_power(self) -> None:
        self._rx.lose_motor_power()
        self._emit({"op": "lose_power"})

    def restore_motor_power(self) -> None:
        self._rx.restore_motor_power()
        self._emit({"op": "restore_power"})

    def freeze_firmware(self) -> None:
        self._rx.freeze_firmware()
        self._emit({"op": "freeze"})

    def unfreeze_firmware(self) -> None:
        self._rx.unfreeze_firmware()
        self._emit({"op": "unfreeze"})

    def poll(self) -> None:
        self._rx.poll()
        self._emit({"op": "poll"})

    def note_verified_clock(self, unix_ms: int) -> None:
        self._rx.note_verified_clock(unix_ms)
        self._emit({"op": "sync", "ms": unix_ms})

    def brownout_reset(self, new_nonce: int) -> None:
        self._rx.brownout_reset(new_nonce)
        self._emit({"op": "brownout", "nonce": new_nonce})

    def handle_raw(self, raw) -> str:
        data = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")
        result = self._rx.handle_raw(raw)
        # The exact bytes, valid or not. The browser decodes them as a receiver decodes a UDP payload.
        return self._emit({"op": "datagram", "raw_b64": base64.b64encode(bytes(data)).decode("ascii")}, result)

    def set_unix(self, unix_ms: int) -> None:
        raise NotImplementedError("set_unix is not part of the mirrored receiver interface")

    def handle(self, message) -> str:
        raise NotImplementedError("send datagrams as bytes through handle_raw so they can be mirrored")


class OpLog:
    """In-memory sink: the hello and every operation, in order. Used by tests and by the server."""

    def __init__(self, clock_ms=lambda: time.time_ns() // 1_000_000):
        self._clock_ms = clock_ms
        self.hello_record: dict | None = None
        self.records: list[dict] = []
        self.listeners: list = []

    def hello(self, record: dict) -> None:
        self.hello_record = record
        for listener in self.listeners:
            listener(json.dumps(record, separators=(",", ":")))

    def op(self, record: dict) -> None:
        record = {**record, "n": len(self.records), "t_unix_ms": self._clock_ms()}
        self.records.append(record)
        text = json.dumps(record, separators=(",", ":"), allow_nan=False)
        for listener in self.listeners:
            listener(text)

    def history(self) -> list[str]:
        head = [] if self.hello_record is None else [json.dumps(self.hello_record, separators=(",", ":"))]
        return head + [json.dumps(r, separators=(",", ":"), allow_nan=False) for r in self.records]


class Fanout:
    """A local WebSocket server on its own thread. Every client gets every message, in order.

    With ``replay`` a new client first receives everything published so far
    (the motor mirror needs the whole boot). Without it, only what follows
    (camera frames). One message per publish; nothing is coalesced.
    """

    def __init__(self, port: int, *, host: str = "127.0.0.1", replay: bool = False):
        self.host = host
        self.port = port
        self.replay = replay
        self._history: list[str] = []
        self._queues: set = set()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error

    def publish(self, text: str) -> None:
        self._loop.call_soon_threadsafe(self._push, text)

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=5)

    def _push(self, text: str) -> None:
        if self.replay:
            self._history.append(text)
        for queue in self._queues:
            queue.put_nowait(text)

    async def _handler(self, websocket) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        for text in self._history:  # same loop thread as _push: nothing can interleave here
            queue.put_nowait(text)
        self._queues.add(queue)
        try:
            while True:
                await websocket.send(await queue.get())
        except Exception:
            pass
        finally:
            self._queues.discard(queue)

    def _run(self) -> None:
        from websockets.asyncio.server import serve

        asyncio.set_event_loop(self._loop)

        async def main():
            self._stop = asyncio.Event()
            try:
                server = await serve(self._handler, self.host, self.port)
            except BaseException as exc:  # port in use, reported to the caller
                self._error = exc
                self._ready.set()
                return
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            async with server:
                await self._stop.wait()

        self._loop.run_until_complete(main())


class SharedCamera:
    """One camera, two consumers: the control loop reads frames, and each frame is also relayed.

    Wraps tests/session_harness.CameraThread (or any source with get/close).
    A relayed frame is the same compact JSON perception.publish serves on
    ws://127.0.0.1:8765, so the dashboard connects unchanged and no second
    process opens the camera.
    """

    def __init__(self, source, relay: Fanout | None):
        from perception.fake_stream import encode

        self._source = source
        self._relay = relay
        self._encode = encode

    def get(self, timeout_ms):
        item = self._source.get(timeout_ms)
        if self._relay is not None and isinstance(item, tuple) and item and item[0] == "frame":
            self._relay.publish(self._encode(item[1]))
        return item

    def close(self) -> None:
        self._source.close()
