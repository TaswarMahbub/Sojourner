"""One control session: frames -> task controller -> publisher -> receiver oracle.

The live camera run (tests/run_live_e2e.py), replay (tests/run_replay.py) and
the failure drills (tests/drills.py) all drive this one Pipeline, so they
print the same per-tick transcript and the same summary, and they measure
stop latency the same way (control/latency.py).

What is real and what is not, stated once here and again in every header:

* control/ runs unmodified: TaskController.step(), Publisher.tick(),
  TaskController.note_published(), the status gate and its persisted state.
* The receiver is the MotorReceiver contract oracle, in this process. It is
  not firmware, there is no radio hop, and no motor driver. So "datagram ->
  receiver torque off" is the oracle's handling time, not the rover's.
* There is no gesture classifier in this build. Each actionable frame gets
  the reserved "neutral" gesture over that single frame, with a fixed
  placeholder score (PLACEHOLDER_CONFIDENCE), so cues come only from the
  calibrated channels. A non-actionable frame gets no gesture.
* Frame capture time is when the camera read returned
  (perception/mediapipe_source.py). Sensor exposure and driver buffering
  before that are not measured.

Two clocks: WallClock for the live run and real-time replay, SimClock for
drills and fast replay. Every latency stamp in one run comes from one clock.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from control.cue_map import CUE_MAP
from control.cues import FASTER, GO, SLOWER, STOP
from control.latency import BUDGET_MS, CUED, SIGNAL_LOSS, LatencyLog, StopSample, format_table
from control.publisher import is_actionable_frame
from control.task import HOLD, SPEED_DEFAULT_MPS
from control.task_controller import TaskController
from control.tiers import channel_value, cue_bindings
from tests.control_rig import FakeClock, Rig, gesture as rig_gesture

TICK_MS = 50
PLACEHOLDER_CONFIDENCE = 0.95
HEARTBEAT_MS = 1000
RECORDING_SCHEMA = 1
# Cues that can change the speed setting (Go only with a Tier 1 magnitude).
SPEED_CUES = frozenset({SLOWER, FASTER, GO})


# ----- clocks -------------------------------------------------------------------


class WallClock:
    """Real time. mono/unix are integer ms for the publisher; stamp() is float ms on the Unix clock."""

    def mono_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000

    def unix_ms(self) -> int:
        return time.time_ns() // 1_000_000

    @property
    def mono(self) -> int:
        return self.mono_ms()

    @property
    def unix(self) -> int:
        return self.unix_ms()

    def stamp(self) -> float:
        return time.time_ns() / 1_000_000

    def advance(self, ms: float) -> None:
        if ms > 0:
            time.sleep(ms / 1000.0)

    wait = advance


class SimClock(FakeClock):
    """Simulated time: nothing passes unless advanced. stamp() is the Unix clock as a float."""

    def stamp(self) -> float:
        return float(self.unix)

    def wait(self, ms: float) -> None:
        if ms > 0:
            self.advance(int(math.ceil(ms)))


# ----- rig with a chosen clock and timed delivery -------------------------------


@dataclass
class Delivery:
    message: dict
    result: str
    t_sent: float
    t_done: float
    torque_before: bool | None
    torque_after: bool | None


class HarnessRig(Rig):
    """tests/control_rig.Rig on a chosen clock, with every delivery timed and watchdog trips counted.

    `receiver` lets a second publisher (a restarted laptop process) talk to
    the same receiver boot.
    """

    def __init__(self, state_dir, clock, *, boot_nonce: int = 7, provisioned: bool = True, receiver=None):
        self.deliveries: list[Delivery] = []
        self.watchdog_trips: list[float] = []
        super().__init__(state_dir, boot_nonce=boot_nonce, provisioned=provisioned)
        self.clock = clock
        self.publisher.clock = clock
        if receiver is not None:
            self.receiver = receiver
        self.sync()

    def sync(self) -> None:
        """Bring the receiver's monotonic clock to now, noting a watchdog trip if one happens."""
        was = self.receiver.fault_latch
        self.receiver.set_monotonic(self.clock.mono)
        if not was and self.receiver.fault_latch:
            self.watchdog_trips.append(self.clock.stamp())

    def advance(self, ms: int) -> None:
        self.clock.advance(ms)
        self.sync()

    def status(self) -> str:
        """The receiver reports its state as of now, not as of the last time its clock was set."""
        self.sync()
        return super().status()

    def _deliver(self, payload: bytes) -> None:
        message = json.loads(payload)
        self.sent.append(message)
        t_sent = self.clock.stamp()
        if not self.link_up:
            self.results.append("lost-in-transit")
            self.deliveries.append(Delivery(message, "lost-in-transit", t_sent, t_sent, None, None))
            return
        self.sync()
        before = self.receiver.motors_have_torque()
        fault_was = self.receiver.fault_latch
        result = self.receiver.handle_raw(payload)
        after = self.receiver.motors_have_torque()
        t_done = self.clock.stamp()
        if not fault_was and self.receiver.fault_latch and message.get("task_state") != "fault":
            self.watchdog_trips.append(t_done)
        self.results.append(result)
        self.deliveries.append(Delivery(message, result, t_sent, t_done, before, after))


# ----- the pipeline -------------------------------------------------------------


class TimedReader:
    """Wraps the controller's tier reader to stamp when each read returned. Changes nothing it reads."""

    def __init__(self, inner, clock):
        self._inner = inner
        self._clock = clock
        self.last_stamp: float | None = None

    def read(self, frame, now_ms, moving):
        cues = self._inner.read(frame, now_ms, moving=moving)
        self.last_stamp = self._clock.stamp()
        return cues

    def reset(self) -> None:
        self._inner.reset()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def placeholder_gesture(frame: dict) -> dict:
    """The reserved "neutral" gesture over this one frame. No classifier exists in this build."""
    g = rig_gesture(frame["timestamp_ms"], "neutral")
    g["confidence"] = PLACEHOLDER_CONFIDENCE
    return g


def frame_text(frame: dict | None, newest: dict | None, now_unix: int) -> str:
    if frame is None:
        if newest is None:
            return "no frame yet"
        return f"no new frame (newest {now_unix - newest['timestamp_ms']} ms old)"
    if frame.get("tracking_state") != "tracking" or frame.get("head_pose") is None:
        return f"frame {frame.get('tracking_state')}"
    pose = frame["head_pose"]
    jaw = frame["features"].get("jawOpen", float("nan"))
    return f"jaw={jaw:.3f} pitch={pose['pitch_deg']:6.1f} yaw={pose['yaw_deg']:6.1f} roll={pose['roll_deg']:5.1f}"


@dataclass
class SpeedChange:
    t_ms: float
    before: float
    after: float
    task_before: str
    cause: str

    @property
    def in_hold(self) -> bool:
        return self.task_before == HOLD and not self.cause.startswith("reset")


class Recorder:
    """JSON lines a replay can read back: header, frames with arrival times, events, end."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = open(self.path, "w", encoding="utf-8", newline="\n")

    def write(self, record: dict) -> None:
        self._handle.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


class Pipeline:
    """Feeds frames through the unmodified controller and publisher and prints one line per tick."""

    def __init__(self, rig: HarnessRig, profile: dict, cue_map=CUE_MAP, *, label: str, out=print,
                 every_tick: bool = True, recorder: Recorder | None = None):
        self.rig = rig
        self.clock = rig.clock
        self.label = label
        self.out = out
        self.every_tick = every_tick
        self.recorder = recorder
        self.profile_dict = profile
        self.controller = TaskController(profile, cue_map)
        self.reader = TimedReader(self.controller.reader, self.clock)
        self.controller.reader = self.reader
        profile_obj = self.controller.profile
        stop_channel = profile_obj.stop_channel
        stop_cb = next(b for b in cue_bindings(profile_obj, cue_map) if b.channel == stop_channel)
        self.stop_channel = stop_channel
        self.stop_side = stop_cb.side
        self.stop_binding = profile_obj.binding(stop_channel)
        self.t_start = self.clock.stamp()
        self.frame: dict | None = None
        self.gesture: dict | None = None
        self.last_actionable_capture: int | None = None
        self.crossing_start: int | None = None
        self.log = LatencyLog()
        self.speed_changes: list[SpeedChange] = []
        self.ticks = 0
        self.frames = 0
        self.actionable_frames = 0
        self.stops = Counter()
        self.camera_errors: list[str] = []
        self.lines: list[str] = []
        self._last_key = None
        self._last_note = None
        self._tracking_state = None
        # Per tick: (cues, task kind, speed setting, receiver result). Lets a replay be compared with its source.
        self.history: list[tuple] = []
        self._last_print_t = -HEARTBEAT_MS
        self._first_delivery = len(rig.deliveries)
        if recorder is not None:
            recorder.write({"record_type": "session_header", "recording_schema_version": RECORDING_SCHEMA,
                            "protocol_kind": "control_session", "label": label, "started_ms": self.t_start,
                            "tick_ms": TICK_MS, "placeholder_confidence": PLACEHOLDER_CONFIDENCE,
                            "profile": profile, "cue_map": [list(b.__dict__.values()) for b in cue_map]
                            if not isinstance(cue_map, dict) else cue_map})

    # ----- output ----------------------------------------------------------------

    def emit(self, text: str) -> None:
        line = f"{self.label} | {text}"
        self.lines.append(line)
        self.out(line)

    def t(self) -> float:
        return self.clock.stamp() - self.t_start

    def note(self, text: str) -> None:
        self.emit(f"{self.t():8.0f} ms  -- {text}")

    def camera_error(self, detail: str) -> None:
        self.camera_errors.append(detail)
        self.note(f"camera error: {detail}. Ticks continue with no new frame.")
        if self.recorder is not None:
            self.recorder.write({"record_type": "event", "event": "camera_error", "at_ms": self.clock.stamp(),
                                 "detail": detail})

    # ----- one tick --------------------------------------------------------------

    def tick(self, frame: dict | None = None, *, note: str = "", estop: bool = False, rearm: bool = False,
             arrival: float | None = None, brightness: float | None = None, force_print: bool = False) -> Delivery | None:
        rig, publisher, controller = self.rig, self.rig.publisher, self.controller
        rig.sync()
        rig.status()
        if frame is not None:
            self._take_frame(frame, arrival, brightness)
        was = controller.task
        self.reader.last_stamp = None
        unix = publisher.clock.unix_ms()
        t_step0 = self.clock.stamp()
        request = controller.step(self.gesture, self.frame, unix)
        t_step1 = self.clock.stamp()
        n_before = len(rig.deliveries)
        message = publisher.tick(motion=request, operator_estop=estop, operator_rearm=rearm)
        controller.note_published(request, message, publisher.last_decision)
        delivery = rig.deliveries[-1] if len(rig.deliveries) > n_before else None
        self.ticks += 1
        self._account_stop(was, t_step0, t_step1, delivery)
        self._account_speed(was)
        self.history.append((tuple(c.kind for c in controller.last_cues), controller.task.kind,
                             controller.task.speed_mps, delivery.result if delivery else None))
        self._line(frame, note, request, delivery, unix, force_print or estop or rearm or note != self._last_note)
        self._last_note = note
        return delivery

    def _take_frame(self, frame: dict, arrival, brightness) -> None:
        self.frames += 1
        state = frame.get("tracking_state")
        if state != self._tracking_state:
            light = "" if brightness is None else f", image brightness {brightness:.1f} of 255"
            self.note(f"tracking {self._tracking_state or 'start'} -> {state}{light}")
            self._tracking_state = state
        self.frame = frame
        self.rig.publisher.observe_frame(frame)
        if is_actionable_frame(frame):
            self.actionable_frames += 1
            self.gesture = placeholder_gesture(frame)
            self.rig.publisher.observe_gesture(self.gesture)
            self.last_actionable_capture = frame["timestamp_ms"]
            value = channel_value(frame, self.stop_channel)
            if value is not None and self.stop_binding.past(value, self.stop_side):
                if self.crossing_start is None:
                    self.crossing_start = frame["timestamp_ms"]
            else:
                self.crossing_start = None
        else:
            self.crossing_start = None
        if self.recorder is not None:
            self.recorder.write({"record_type": "frame", "message": frame,
                                 "arrival_ms": self.clock.stamp() if arrival is None else arrival,
                                 "brightness": brightness})

    def _account_stop(self, was, t_step0: float, t_step1: float, delivery: Delivery | None) -> None:
        stop_cues = [c for c in self.controller.last_cues if c.kind == STOP]
        if was.kind == HOLD or not stop_cues:
            return
        if any(c.source == self.reader.source for c in stop_cues):
            kind, t0, t1 = CUED, self.crossing_start, self.reader.last_stamp
        elif any(c.source == "signal" for c in stop_cues):
            kind, t0, t1 = SIGNAL_LOSS, self.last_actionable_capture, t_step0
        else:
            self.stops["other"] += 1
            return
        self.stops[kind] += 1
        if delivery is None or delivery.message["stop"] is not True:
            self.log.skip(kind, f"t={self.t():.0f} ms: no stop datagram was sent on the stop tick")
        elif delivery.torque_before is not True:
            self.log.skip(kind, f"t={self.t():.0f} ms: the receiver had no torque before the stop "
                                f"(result {delivery.result}), so there was nothing to turn off")
        elif delivery.torque_after is not False:
            self.log.skip(kind, f"t={self.t():.0f} ms: torque still on after the stop datagram ({delivery.result})")
        elif t0 is None or t1 is None:
            self.log.skip(kind, f"t={self.t():.0f} ms: no frame timestamp to measure from")
        else:
            self.log.add(StopSample(kind, (float(t0), t1, t_step1, delivery.t_sent, delivery.t_done)))

    def _account_speed(self, was) -> None:
        now = self.controller.task
        if now.speed_mps == was.speed_mps:
            return
        speed_cues = [c for c in self.controller.last_cues if c.kind in SPEED_CUES]
        # A Stop in the tick wins arbitration (control/cues.py), so a speed cue beside it was not applied.
        if speed_cues and not any(c.kind == STOP for c in self.controller.last_cues):
            cause = "cue " + ",".join(f"{c.kind}({c.source})" for c in speed_cues)
        else:
            cause = "reset to default by the Go lockout"
        self.speed_changes.append(SpeedChange(self.t(), was.speed_mps, now.speed_mps, was.kind, cause))

    def _line(self, frame, note, request, delivery, unix, force) -> None:
        c = self.controller
        task = c.task
        cues = ",".join(f"{x.kind}({x.source})" for x in c.last_cues) or "-"
        if c.last_suppressed:
            cues += " [dropped: " + ",".join(x.kind for x in c.last_suppressed) + "]"
        lock_ms = c.lockout_remaining_ms(unix)
        lock = f"lock {lock_ms:>4}" if lock_ms else "         "
        req = "None" if request is None else f"{request.linear_mps:.3f} {request.angular_rad_s:+.3f}"
        if delivery is None:
            sent, result = "nothing", "-"
        else:
            m = delivery.message
            sent = f"{m['task_state']} {'stop' if m['stop'] else 'move'} {m['linear_mps']:.3f} {m['angular_rad_s']:+.3f}"
            result = delivery.result
        torque = "on " if self.rig.receiver.motors_have_torque() else "off"
        t = self.t()
        key = (cues, task.kind, task.speed_mps, math.copysign(1, task.turn_rad) if task.turn_rad else 0,
               bool(lock_ms), sent.split(" ")[:2], result, torque, self.rig.publisher.last_decision)
        heartbeat = t - self._last_print_t >= HEARTBEAT_MS
        if not (self.every_tick or force or key != self._last_key or heartbeat):
            return
        self._last_key = key
        self._last_print_t = t
        self.emit(f"{t:8.0f} ms  {note:<24.24} {frame_text(frame, self.frame, unix):<46} | cues={cues:<22} "
                  f"task={task.kind:<7} v={task.speed_mps:.2f} turn={task.turn_rad:+.3f} {lock} | "
                  f"req={req:<14} sent={sent:<22} rx={result:<15} torque={torque} | {self.rig.publisher.last_decision}")

    # ----- summary ---------------------------------------------------------------

    def deliveries(self) -> list[Delivery]:
        return self.rig.deliveries[self._first_delivery:]

    def summary(self) -> list[str]:
        L = []
        ds = self.deliveries()
        results = Counter(d.result for d in ds)
        L.append("SUMMARY")
        L.append(f"  ran {self.t() / 1000:.1f} s: {self.ticks} ticks, {self.frames} frames "
                 f"({self.actionable_frames} actionable), {len(self.camera_errors)} camera errors")
        L.append(f"  datagrams {len(ds)}: " + ", ".join(f"{k} {v}" for k, v in sorted(results.items())))
        L.append(f"  accepted motion datagrams: {results.get('accepted_motion', 0)}")
        trips = ", ".join(f"t={t - self.t_start:.0f} ms" for t in self.rig.watchdog_trips)
        L.append(f"  watchdog trips: {len(self.rig.watchdog_trips)}" + (f" ({trips})" if trips else ""))
        L.append(f"  stops while moving: cued {self.stops[CUED]}, signal-loss {self.stops[SIGNAL_LOSS]}, "
                 f"other {self.stops['other']}")
        task = self.controller.task
        L.append(f"  speed setting now v={task.speed_mps:.2f} m/s (default {SPEED_DEFAULT_MPS:.2f}); "
                 f"{len(self.speed_changes)} changes, {sum(c.in_hold for c in self.speed_changes)} of them made "
                 f"while in Hold, where nothing moves to show it")
        for ch in self.speed_changes:
            flag = "  <- in Hold, no feedback" if ch.in_hold else ""
            L.append(f"    t={ch.t_ms:8.0f} ms  {ch.task_before:<7} v {ch.before:.2f} -> {ch.after:.2f}  {ch.cause}{flag}")
        for kind in (CUED, SIGNAL_LOSS):
            L.extend(format_table(self.log, kind, BUDGET_MS, prefix="  "))
        if isinstance(self.clock, SimClock):
            L.append("  clock: SIMULATED. Time passes only between ticks, so stages inside one tick read 0 ms; "
                     "these numbers show tick structure, not compute time.")
        L.append("  the receiver is the in-process contract oracle: no radio, no ESP32, no motor driver. "
                 "Frame capture time is when the camera read returned.")
        return L

    def print_summary(self) -> None:
        for line in self.summary():
            self.emit(line)
        if self.recorder is not None:
            self.recorder.write({"record_type": "session_end", "at_ms": self.clock.stamp()})


def start(rig: HarnessRig) -> None:
    """Receiver ready, session handshake, operator arm: the same steps as every scripted run."""
    rig.ready_receiver()
    rig.handshake()
    rig.arm()
    rig.sync()


# ----- frame sources for the real-time loop -------------------------------------

END = object()


class CameraThread:
    """Live camera on its own thread (every camera and detector call on one thread, as perception/publish.py does).

    get() returns ("frame", message, arrival, brightness), ("error", detail), None on timeout, or END.
    A camera that fails mid-run is reported once and then simply delivers nothing, so the
    control loop keeps ticking and the staleness stop fires.
    """

    def __init__(self, camera_index: int, mirrored: bool, clock, model_path=None):
        self._clock = clock
        self._model_path = model_path
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._open_error: Exception | None = None
        self._thread = threading.Thread(target=self._run, args=(camera_index, mirrored), daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._open_error is not None:
            raise self._open_error

    def _run(self, camera_index, mirrored) -> None:
        from perception.mediapipe_source import MODEL_PATH, CameraError, CameraSource

        try:
            source = CameraSource(camera_index, mirrored, self._model_path or MODEL_PATH)
        except Exception as exc:  # reported to the caller, never swallowed
            self._open_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    cf = source.read_frame()
                except CameraError as exc:
                    self._q.put(("error", str(exc)))
                    return
                self._q.put(("frame", cf.message, self._clock.stamp(), cf.brightness))
        finally:
            source.close()

    def get(self, timeout_ms: float):
        try:
            return self._q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


def load_recording(path) -> tuple[dict, list[dict]]:
    """(header, records) from a control-session recording or a perception probe recording."""
    header, records = None, []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            rec = json.loads(raw)
            if rec.get("record_type") == "session_header":
                header = rec
            else:
                records.append(rec)
    if header is None:
        raise ValueError(f"{path}: no session_header record")
    return header, records


class ReplaySource:
    """Plays recorded frames back at their recorded arrival offsets on the given clock.

    Each frame is re-stamped: its timestamp_ms becomes replay start + (capture - recording start),
    so it is as old, relative to now, as it was live. A probe recording carries no arrival times;
    there arrival is taken as the capture time, so detector inference is not included.
    """

    def __init__(self, records: list[dict], clock):
        self._clock = clock
        self.events = []
        for rec in records:
            kind = rec.get("record_type")
            if kind == "frame":
                msg = rec["message"]
                arrival = rec.get("arrival_ms")
                self.events.append((float(msg["timestamp_ms"]) if arrival is None else float(arrival), "frame", rec))
            elif kind == "event" and rec.get("event") == "camera_error":
                self.events.append((float(rec["at_ms"]), "error", rec))
            elif kind == "session_end":
                # A control recording writes at_ms; a perception probe recording writes ended_ms.
                self.events.append((float(rec.get("at_ms", rec.get("ended_ms", 0))), "end", rec))
        # The end is played last, after every recorded frame and event.
        self.events.sort(key=lambda e: (e[1] == "end", e[0]))
        frames = [e for e in self.events if e[1] == "frame"]
        if not frames:
            raise ValueError("the recording has no frames")
        self.arrivals_recorded = all(e[2].get("arrival_ms") is not None for e in frames)
        self.rec_start = frames[0][0]
        self.replay_start = clock.stamp()
        self._i = 0

    def get(self, timeout_ms: float):
        if self._i >= len(self.events):
            return END
        at, kind, rec = self.events[self._i]
        due = self.replay_start + (at - self.rec_start)
        wait = due - self._clock.stamp()
        if wait > timeout_ms:
            self._clock.wait(timeout_ms)
            return None
        self._clock.wait(wait)
        self._i += 1
        if kind == "end":
            return END
        if kind == "error":
            return ("error", f"REPLAYED: {rec.get('detail')}")
        msg = json.loads(json.dumps(rec["message"]))
        msg["timestamp_ms"] = int(round(self.replay_start + (msg["timestamp_ms"] - self.rec_start)))
        return ("frame", msg, self._clock.stamp(), rec.get("brightness"))

    def close(self) -> None:
        pass


def run_loop(pipeline: Pipeline, source, *, duration_s: float | None = None) -> None:
    """Tick on every frame as it arrives, or every TICK_MS when none arrives. Stops at END or the duration."""
    clock = pipeline.clock
    end_at = None if duration_s is None else clock.stamp() + duration_s * 1000.0
    while end_at is None or clock.stamp() < end_at:
        item = source.get(TICK_MS)
        if item is END:
            break
        if item is None:
            pipeline.tick()
        elif item[0] == "error":
            pipeline.camera_error(item[1])
            pipeline.tick()
        else:
            _, message, arrival, brightness = item
            pipeline.tick(message, arrival=arrival, brightness=brightness)
