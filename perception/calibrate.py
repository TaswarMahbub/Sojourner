"""Run the 60-second calibration protocol live and write a per-user profile.

This is the measurement half of calibration as system identification: it
drives the source, issues prompts, and records everything to one JSONL file.
calibration.py does all the scoring from that file alone, so scoring can be
changed and re-run without the person calibrating again (--rescore).

Protocol, 60 s after a short warm-up (see calibration.plan_protocol):

    free exploration  15 s   "Move however you can."  no instruction
                             about what to move, to catch movement nobody
                             thought to ask for
    guided probes     30 s   six probes, three repetitions each, a release
                             between repetitions and a rest after each probe
    rest baseline     10 s   "Hold still."  the rest reference
    confirm and bind   5 s   the top-ranked channel is presented; making
                             that movement within the window accepts it

The warm-up (default 2 s) runs before the protocol so camera exposure
settles. Its brightness and frame rate go in the session header because
both the frame rate and the noise floor move with lighting, and a baseline
is only valid for the light it was measured under. The session_end record
repeats them for the whole session.

Events
------
Every marker, the scoring summary, the confirm result and the session
boundaries are dictionaries passed to each listener in order, and the same
dictionaries are written to the recording. They are the interface for the
calibration UI: a frontend drives its prompts from these events and never
parses console text. The console and the prompt window here are only two
listeners. --events json prints each event as one JSON line on stdout.

    python -m perception.calibrate --source camera --user ID
    python -m perception.calibrate --source fake --user ID       dry run
    python -m perception.calibrate --rescore PATH.jsonl          no camera

Camera recordings and profiles are human data: they are written under
captures/ by default, which git ignores.

Standard library only at module level. cv2 and mediapipe are imported
inside functions, and only for the camera source and the prompt window.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perception import calibration, fake_stream  # noqa: E402
from perception.calibration import (  # noqa: E402
    CONFIRM,
    CONFIRM_RESULT,
    FRAME,
    HEADER,
    MARKER,
    RECORDING_SCHEMA_VERSION,
    SCORING,
    SESSION_END,
    WARMUP,
    CrossingDetector,
)
from perception.filters import channel_values, is_tracking  # noqa: E402

DEFAULT_OUT_DIR = REPO_ROOT / "captures" / "calibration"
DEFAULT_WARMUP_SECONDS = 2.0
BRIGHTNESS_DEFINITION = "mean grayscale value of the captured image, BT.601 luma, 0 to 255"


class CalibrationAborted(RuntimeError):
    """The operator closed the session before it finished."""


@dataclass(frozen=True)
class Frame:
    """One published message plus the brightness of its image, or None."""

    message: dict
    brightness: float | None


# --- Recording and events ----------------------------------------------------


class Recorder:
    """Writes records to the JSONL recording and passes events to listeners.

    Each line is flushed as it is written, so an interrupted session still
    leaves every frame captured so far on disk.
    """

    def __init__(self, path: Path, listeners) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.listeners = list(listeners)
        self._handle = path.open("w", encoding="utf-8", newline="\n")

    def write(self, record: dict) -> None:
        self._handle.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
        self._handle.flush()

    def frame(self, frame: Frame) -> None:
        """Record a frame. Frames are not events; listeners only get ticks."""
        self.write({"record_type": FRAME, "message": frame.message, "brightness": frame.brightness})
        for listener in self.listeners:
            listener.tick(frame)

    def emit(self, event: dict) -> None:
        """Record an event and hand it to every listener."""
        self.write(event)
        for listener in self.listeners:
            listener.event(event)

    def close(self) -> None:
        self._handle.close()


class TextListener:
    """Renders events as one human-readable line each, on stderr."""

    def __init__(self) -> None:
        self._origin_ms: int | None = None

    def tick(self, frame: Frame) -> None:
        return None

    def event(self, event: dict) -> None:
        kind = event["record_type"]
        if kind == HEADER:
            self._origin_ms = event["started_ms"]
            print(f"session {event['session_id']}  source={event['source']}  "
                  f"warm-up brightness={fmt_opt(event['brightness']['mean'], '.1f')}  "
                  f"frame rate={fmt_opt(event['frame_rate_hz'], '.2f')} Hz", file=sys.stderr, flush=True)
        elif kind == MARKER:
            t = (event["start_ms"] - (self._origin_ms or event["start_ms"])) / 1000.0
            where = event["segment"] if event["probe"] is None else f"{event['segment']} {event['probe']}"
            print(f"[{t:5.1f} s] {where:<22} {event['prompt']}", file=sys.stderr, flush=True)
        elif kind == SCORING:
            print(f"scored: rest_quality={event['rest_quality']}  lighting_quality={event['lighting_quality']}  "
                  f"tier {event['tier']} ({event['mode']}); "
                  f"top channels {event['top_channels']}", file=sys.stderr, flush=True)
        elif kind == CONFIRM_RESULT:
            print(f"confirm: accepted={event['accepted']} channel={event['channel']}", file=sys.stderr, flush=True)
        elif kind == SESSION_END:
            print(f"session end: {event['frames']} frames, brightness={fmt_opt(event['brightness']['mean'], '.1f')}, "
                  f"frame rate={fmt_opt(event['frame_rate_hz'], '.2f')} Hz", file=sys.stderr, flush=True)


class JsonListener:
    """Prints every event as one compact JSON line on stdout."""

    def tick(self, frame: Frame) -> None:
        return None

    def event(self, event: dict) -> None:
        print(json.dumps(event, separators=(",", ":"), allow_nan=False), flush=True)


class PromptWindow:
    """A plain window showing the current prompt, for the person calibrating.

    It shows text on a dark background, never the camera image, plus a bar
    for the time left in the segment and whether the face is tracked. It is
    an operator aid only; the calibration UI proper consumes the events.
    Space starts the session, Esc aborts it.
    """

    NAME = "calibration"

    def __init__(self) -> None:
        from perception.mediapipe_source import import_cv2

        self.cv2 = import_cv2()
        # Resizable: the canvas, text included, scales to whatever size the
        # window is dragged to, keeping its aspect ratio.
        self.cv2.namedWindow(self.NAME, self.cv2.WINDOW_NORMAL | self.cv2.WINDOW_KEEPRATIO)
        self.cv2.resizeWindow(self.NAME, 1440, 600)
        self.prompt = "Press SPACE to start"
        self.detail = "Sit facing the camera. Esc aborts."
        self.start_ms: int | None = None
        self.duration_ms: int | None = None
        self.tracking = False

    def event(self, event: dict) -> None:
        if event["record_type"] == MARKER:
            self.prompt = event["prompt"]
            rep = "" if event["repetition"] is None else f" rep {event['repetition']}"
            self.detail = event["segment"].replace("_", " ") + ("" if event["probe"] is None else f": {event['probe']}{rep}")
            self.start_ms, self.duration_ms = event["start_ms"], event["planned_duration_ms"]
        elif event["record_type"] == SESSION_END:
            self.prompt, self.detail, self.duration_ms = "Done. Thank you.", "", None
        self.draw(None)

    def tick(self, frame: Frame) -> None:
        self.tracking = is_tracking(frame.message)
        self.draw(frame.message["timestamp_ms"])

    def draw(self, now_ms: int | None) -> int:
        """Redraw and service the window; return the key code read, -1 for none.

        Esc raises CalibrationAborted.
        """
        import numpy  # noqa: F401  cv2 needs it; imported here so tests never load it

        cv2 = self.cv2
        canvas = numpy.zeros((400, 960, 3), dtype=numpy.uint8)
        # Fitted to the canvas: an unfitted confirm prompt once ran 1224 px
        # into a 900 px area and hid the action the person had to perform.
        scale, lines, line_px = self.fit(self.prompt, 900, 190)
        for row, line in enumerate(lines):
            cv2.putText(canvas, line, (30, 90 + line_px * (row + 1) - line_px // 4), cv2.FONT_HERSHEY_SIMPLEX, scale,
                        (255, 255, 255), 3 if scale >= 1.0 else 2, cv2.LINE_AA)
        cv2.putText(canvas, self.detail, (30, 300), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (180, 180, 180), 2, cv2.LINE_AA)
        status, colour = ("face tracked", (80, 200, 80)) if self.tracking else ("face NOT tracked", (60, 60, 230))
        cv2.putText(canvas, status, (30, 360), cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2, cv2.LINE_AA)
        if now_ms is not None and self.start_ms is not None and self.duration_ms:
            left = max(0.0, 1.0 - (now_ms - self.start_ms) / self.duration_ms)
            cv2.rectangle(canvas, (30, 40), (30 + int(900 * left), 60), (200, 160, 60), -1)
        cv2.imshow(self.NAME, canvas)
        key = cv2.waitKey(1)
        if key != -1 and key & 0xFF == 27:
            raise CalibrationAborted("aborted from the prompt window")
        return key

    def fit(self, text: str, width_px: int, height_px: int) -> tuple[float, list[str], int]:
        """Largest font scale, from 1.4 down to 0.5, at which the wrapped text fits.

        Returns (scale, lines, line height in px). Every line fits width_px
        and all lines together fit height_px, so no part of a prompt is
        ever cut off.
        """
        scale = 1.4
        while True:
            thickness = 3 if scale >= 1.0 else 2
            lines = self.wrap(text, scale, thickness, width_px)
            line_px = int(self.cv2.getTextSize("Ag", self.cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)[0][1] * 1.9)
            widest = max(self.cv2.getTextSize(line, self.cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)[0][0] for line in lines)
            if (len(lines) * line_px <= height_px and widest <= width_px) or scale <= 0.5:
                return scale, lines, line_px
            scale = round(scale - 0.1, 1)

    def wrap(self, text: str, scale: float, thickness: int, width_px: int) -> list[str]:
        """Split text into lines that each render within width_px."""
        import re

        lines: list[str] = []
        # A bracketed group such as "(1 of 3)" is kept on one line.
        for word in re.findall(r"\([^)]*\)|\S+", text):
            candidate = f"{lines[-1]} {word}" if lines else word
            fits = self.cv2.getTextSize(candidate, self.cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)[0][0] <= width_px
            if lines and fits:
                lines[-1] = candidate
            else:
                lines.append(word)
        return lines

    def wait_for_start(self, frames) -> None:
        """Show the start screen, reading frames so exposure keeps settling, until Space."""
        for frame in frames:
            self.tracking = is_tracking(frame.message)
            key = self.draw(None)
            if key != -1 and key & 0xFF == ord(" "):
                return

    def close(self) -> None:
        self.cv2.destroyWindow(self.NAME)


def fmt_opt(value, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


# --- Session -----------------------------------------------------------------


def brightness_summary(frames: list[Frame]) -> dict:
    """Mean, min and max brightness over the frames that have an image."""
    values = [f.brightness for f in frames if f.brightness is not None]
    return {
        "definition": BRIGHTNESS_DEFINITION,
        "mean": statistics.fmean(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def collect_warmup(frames, seconds: float) -> list[Frame]:
    """Frames from the first `seconds`, by timestamp_ms; at least one."""
    collected: list[Frame] = []
    for frame in frames:
        collected.append(frame)
        if frame.message["timestamp_ms"] - collected[0].message["timestamp_ms"] >= seconds * 1000.0:
            break
    if not collected:
        raise RuntimeError("the source delivered no frames")
    return collected


def session_id_now() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def marker(planned: calibration.PlannedSegment, start_ms: int, issued_ms: int, prompt: str | None = None) -> dict:
    """The segment_start event for a planned segment."""
    return {
        "record_type": MARKER,
        "event": "segment_start",
        "segment": planned.segment,
        "probe": planned.probe,
        "repetition": planned.repetition,
        "prompt": planned.prompt if prompt is None else prompt,
        "start_ms": start_ms,
        "prompt_issued_ms": issued_ms,
        "planned_duration_ms": planned.duration_ms,
    }


def confirm_prompt(result: dict) -> tuple[str, dict | None]:
    """Prompt for the confirm step and the binding it asks to accept.

    The person accepts by making the top-ranked usable channel's movement
    once more; nothing to accept gives a prompt saying so and None.
    """
    usable = [r for r in result["ranking"] if r["status"] == "usable"]
    if result["rest_quality"]["status"] == calibration.REST_SUSPECT:
        return "Rest periods were not still. Calibration must be repeated.", None
    if result["lighting_quality"]["status"] == calibration.REST_SUSPECT:
        return "The lighting changed during calibration. It must be repeated.", None
    if not usable or result["tier"]["tier"] is None:
        return "No reliable channel found. Hold still.", None
    channels = result["channels"]
    top = channels[usable[0]["channel"]]
    names = ", ".join(r["channel"] for r in usable[:3])
    probe = next(p for p in result["probes"] if p["name"] == top["probe"])
    # The action comes first: it is what the person must do within 5 s.
    return f"{probe['prompt']} to accept. Top channels: {names}.", top


def run_session(frames, recorder: Recorder, *, user_id: str, source: str, warmup_seconds: float,
                clock=None, probes=calibration.PROBES, timing=calibration.DEFAULT_TIMING,
                session_id: str | None = None) -> None:
    """Warm up, run the protocol, score, confirm, and close the recording.

    frames yields Frame objects. clock() returns the wall-clock epoch ms at
    which a prompt is issued; None uses the triggering frame's timestamp_ms
    (the synthetic source, whose timestamps are not paced in real time).
    Segment boundaries follow frame timestamps: a segment starts on the
    first frame at or past its planned offset from the protocol start.
    session_id defaults to the current UTC time; pass the id used in the
    recording's file name so the two agree.
    """
    frames = iter(frames)
    warmup = collect_warmup(frames, warmup_seconds)
    messages = [f.message for f in warmup]
    started_ms = messages[0]["timestamp_ms"]
    recorder.emit({
        "record_type": HEADER,
        "recording_schema_version": RECORDING_SCHEMA_VERSION,
        "session_id": session_id or session_id_now(),
        "user_id": user_id,
        "source": source,
        "measured": source == "camera",
        "started_ms": started_ms,
        "protocol": calibration.protocol_description(probes, timing),
        "brightness": brightness_summary(warmup),
        "frame_rate_hz": calibration.frame_rate_hz(messages),
        "brightness_and_frame_rate_window": "warm-up before the protocol",
        "lighting_note": ("frame rate and rest noise depend on scene brightness; thresholds derived from this "
                          "session are valid only for lighting near this brightness"),
    })
    recorder.emit({"record_type": MARKER, "event": "segment_start", "segment": WARMUP, "probe": None,
                   "repetition": None, "prompt": "Warming up.", "start_ms": started_ms,
                   "prompt_issued_ms": started_ms, "planned_duration_ms": int(warmup_seconds * 1000)})
    for frame in warmup:
        recorder.frame(frame)

    plan = calibration.plan_protocol(probes, timing)
    origin_ms = messages[-1]["timestamp_ms"] + 1
    confirm_index = next(i for i, s in enumerate(plan) if s.segment == CONFIRM)
    end_ms = origin_ms + plan[-1].offset_ms + plan[-1].duration_ms
    all_frames = list(warmup)
    next_index = 0
    detector = None
    top = None
    accepted_at = None

    def issued(frame: Frame) -> int:
        return frame.message["timestamp_ms"] if clock is None else max(clock(), frame.message["timestamp_ms"])

    for frame in frames:
        ts = frame.message["timestamp_ms"]
        if ts >= end_ms:
            break
        while next_index < len(plan) and ts >= origin_ms + plan[next_index].offset_ms:
            planned = plan[next_index]
            start = origin_ms + planned.offset_ms
            if next_index == confirm_index:
                # Score from the file alone, exactly as a later re-score would.
                result = calibration.score_recording(recorder.path)
                result["probes"] = calibration.protocol_description(probes, timing)["probes"]
                prompt, top = confirm_prompt(result)
                recorder.emit(scoring_event(result))
                recorder.emit(marker(planned, start, issued(frame), prompt))
                if top is not None:
                    detector = CrossingDetector(top["threshold"]["offset"],
                                                calibration.DEFAULT_CONSTANTS.min_sustained_frames, top["direction"])
            else:
                recorder.emit(marker(planned, start, issued(frame)))
            next_index += 1
        recorder.frame(frame)
        all_frames.append(frame)
        if detector is not None and accepted_at is None:
            value = channel_values(frame.message).get(top["channel"]) if is_tracking(frame.message) else None
            deviation = None if value is None else value - top["rest"]["mean"]
            if detector.update(deviation):
                accepted_at = ts
    if next_index <= confirm_index:
        raise RuntimeError("the source ended before the protocol reached the confirm step")
    recorder.emit({"record_type": CONFIRM_RESULT, "accepted": accepted_at is not None,
                   "channel": None if top is None else top["channel"], "detected_ms": accepted_at})
    recorder.emit({"record_type": SESSION_END, "ended_ms": end_ms, "frames": len(all_frames),
                   "brightness": brightness_summary(all_frames),
                   "frame_rate_hz": calibration.frame_rate_hz([f.message for f in all_frames])})


def scoring_event(result: dict) -> dict:
    """The scoring summary event emitted before confirm."""
    usable = [r["channel"] for r in result["ranking"] if r["status"] == "usable"]
    return {"record_type": SCORING, "session_quality": result["session_quality"],
            "lighting_quality": result["lighting_quality"]["status"],
            "rest_quality": result["rest_quality"]["status"],
            "rest_quality_responsible_channels": result["rest_quality"]["responsible_channels"],
            "tier": result["tier"]["tier"], "mode": result["tier"]["mode"],
            "reason": result["tier"]["reason"], "top_channels": usable[:3]}


# --- Sources -----------------------------------------------------------------


def fake_frames(rate_hz: float, seed: int):
    """Synthetic frames. Not measured data, no image; prompts have no effect."""
    for message in fake_stream.generate_frames(rate_hz, seed, time.time_ns() // 1_000_000):
        yield Frame(message, None)


def camera_frames(source):
    """Live frames from an open mediapipe_source.CameraSource."""
    for frame in source.frames():
        yield Frame(frame.message, frame.brightness)


# --- Command line ------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Sojourner 60-second calibration and write a per-user profile.")
    parser.add_argument("--source", choices=("fake", "camera"), default="fake",
                        help="fake: synthetic frames, not measured. camera: live Face Landmarker frames (default fake)")
    parser.add_argument("--user", default="anonymous", help="user id stored in the recording and profile (default anonymous)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help="directory for the recording and profile (default captures/calibration, ignored by git)")
    parser.add_argument("--warmup", type=float, default=DEFAULT_WARMUP_SECONDS,
                        help=f"seconds of warm-up before the protocol (default {DEFAULT_WARMUP_SECONDS})")
    parser.add_argument("--events", choices=("text", "json"), default="text",
                        help="text: one readable line per event on stderr. json: one JSON event per line on stdout")
    parser.add_argument("--no-window", action="store_true", help="camera source: do not open the prompt window")
    parser.add_argument("--rescore", type=Path, metavar="PATH",
                        help="score an existing recording and write its profile; opens no source")
    fake = parser.add_argument_group("fake source")
    fake.add_argument("--rate", type=float, default=30.0, help="frames per second (default 30.0)")
    fake.add_argument("--seed", type=int, default=11, help="random seed (default 11)")
    camera = parser.add_argument_group("camera source")
    camera.add_argument("--camera", type=int, default=0, metavar="INDEX", help="camera device index (default 0)")
    camera.add_argument("--mirrored", action="store_true", help="the camera delivers a mirrored image (default off)")
    args = parser.parse_args(argv)
    if args.warmup <= 0:
        parser.error("--warmup must be positive")
    if args.rate <= 0:
        parser.error("--rate must be positive")
    return args


def profile_path_for(recording_path: Path) -> Path:
    return recording_path.with_name(recording_path.stem + "_profile.json")


EXIT_REST_SUSPECT = 2


def finish(recording_path: Path) -> int:
    """Score the recording, write the profile next to it, print the table.

    Returns 0, or EXIT_REST_SUSPECT when the rest periods disagreed. A
    suspect profile is still written, with rest_quality "suspect", no tier
    and no bindings, so the failed enrolment is on record; the exit status
    and the stderr line make sure it is never taken for a usable profile.
    """
    recording = calibration.load_recording(recording_path)
    result = calibration.score_session(recording)
    profile = calibration.build_profile(result, recording)
    profile_path = profile_path_for(recording_path)
    calibration.write_json(profile, profile_path)
    print(calibration.format_report(result))
    print(f"accepted={profile['accepted']}")
    print(f"recording {recording_path}")
    print(f"profile   {profile_path}")
    if profile["session_quality"] != calibration.REST_OK:
        print(f"session_quality=suspect (rest_quality={profile['rest_quality']}, "
              f"lighting_quality={profile['lighting_quality']}): no tier proposed; "
              f"rest channels {sorted(profile['rest_quality_responsible_channels'])}; repeat calibration", file=sys.stderr)
        return EXIT_REST_SUSPECT
    return 0


def record(args: argparse.Namespace) -> Path:
    """Run a live session from the selected source; return the recording path."""
    listeners = [JsonListener() if args.events == "json" else TextListener()]
    session_id = session_id_now()
    path = args.out_dir / f"{args.user}_{session_id}.jsonl"
    if args.source == "fake":
        recorder = Recorder(path, listeners)
        try:
            run_session(fake_frames(args.rate, args.seed), recorder, user_id=args.user, source="fake",
                        warmup_seconds=args.warmup, session_id=session_id)
        finally:
            recorder.close()
        return path

    from perception.mediapipe_source import CameraSource

    source = CameraSource(camera_index=args.camera, mirrored=args.mirrored)
    window = None
    try:
        frames = camera_frames(source)
        if not args.no_window:
            window = PromptWindow()
            window.wait_for_start(frames)
            listeners.append(window)
        recorder = Recorder(path, listeners)
        try:
            run_session(frames, recorder, user_id=args.user, source="camera", warmup_seconds=args.warmup,
                        clock=lambda: time.time_ns() // 1_000_000, session_id=session_id)
        finally:
            recorder.close()
    finally:
        if window is not None:
            window.close()
        source.close()
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.rescore is not None:
        return finish(args.rescore)
    from perception.mediapipe_source import CameraError

    try:
        path = record(args)
    except CameraError as error:
        print(f"camera source failed: {error}", file=sys.stderr)
        return 1
    except CalibrationAborted as error:
        print(f"calibration {error}; the partial recording is not scored", file=sys.stderr)
        return 130
    return finish(path)


if __name__ == "__main__":
    sys.exit(main())
