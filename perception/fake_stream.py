"""Synthetic camera feature stream for Sojourner.

This script publishes message 1 of docs/contracts.md, the camera feature
stream, without a camera and without MediaPipe. Every number it emits is
generated. It is not recorded human data and it is not a camera.

Frames are served over a localhost websocket, one JSON text message per
frame. The schema version and the 52 blendshape names are imported from the
contract oracle in tests/contract_schema.py so they cannot drift from it.

Only the standard library is needed to import this module. The `websockets`
package is imported inside the server function, so the tests and `--once`
run without it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from pathlib import Path

# The contract oracle lives in the tests package at the repository root.
# Running this file as a script puts only perception/ on sys.path, so the
# repository root is added here to make `tests.contract_schema` importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# column_norms and extract_angles are re-exported here so existing callers of
# fake_stream keep working; their single definition is in frame_builder.
from perception.frame_builder import (  # noqa: E402,F401
    DECIMALS,
    build_matrix,
    column_norms,
    extract_angles,
    make_feature_message,
)
from tests.contract_schema import BLENDSHAPES  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# Fixed capture epoch for the fixture files, Unix epoch milliseconds, UTC.
# It is the timestamp used in the contract's encoding illustration, so the
# fixtures regenerate byte for byte.
FIXTURE_START_MS = 1795276800000

# Resting blendshape score and gaussian noise standard deviation, both in
# score units on [0, 1].
REST_SCORE = 0.05
NOISE_SIGMA = 0.01

# Channels that pulse toward PULSE_PEAK: name -> (period s, offset s).
PULSE_PEAK = 0.7
PULSE_WIDTH_S = 1.2
PULSE_CHANNELS = {
    "browOuterUpLeft": (4.0, 0.5),
    "jawOpen": (5.0, 1.5),
    "mouthSmileLeft": (6.0, 3.0),
}

# State schedule, in seconds within each repeating cycle.
CYCLE_S = 15.0
LOST_START_S, LOST_END_S = 10.0, 11.0
PARTIAL_START_S, PARTIAL_END_S = 12.0, 12.5


# --- Motion ----------------------------------------------------------------


def sine(elapsed_s: float, amplitude: float, period_s: float, phase_rad: float = 0.0) -> float:
    """amplitude * sin(2*pi*elapsed_s/period_s + phase_rad), in amplitude's unit."""
    return amplitude * math.sin(2.0 * math.pi * elapsed_s / period_s + phase_rad)


def pulse(elapsed_s: float, period_s: float, offset_s: float) -> float:
    """Raised-cosine bump in [0, 1], PULSE_WIDTH_S wide, once per period_s.

    Zero outside the bump. Times are seconds.
    """
    t = (elapsed_s - offset_s) % period_s
    if t >= PULSE_WIDTH_S:
        return 0.0
    return 0.5 * (1.0 - math.cos(2.0 * math.pi * t / PULSE_WIDTH_S))


def clamp_unit(value: float) -> float:
    """Clamp to the blendshape score range [0, 1]."""
    return max(0.0, min(1.0, value))


def blendshape_scores(elapsed_s: float, rng: random.Random) -> dict[str, float]:
    """All 52 blendshape scores in [0, 1] for one frame.

    Every channel rests near REST_SCORE with gaussian noise. The channels in
    PULSE_CHANNELS also rise toward PULSE_PEAK and back once per period.
    """
    scores = {}
    for name in BLENDSHAPES:
        value = REST_SCORE + rng.gauss(0.0, NOISE_SIGMA)
        if name in PULSE_CHANNELS:
            period_s, offset_s = PULSE_CHANNELS[name]
            value += (PULSE_PEAK - REST_SCORE) * pulse(elapsed_s, period_s, offset_s)
        scores[name] = round(clamp_unit(value), DECIMALS)
    return scores


def head_matrix(elapsed_s: float) -> list[list[float]]:
    """A tracking facial transformation matrix for elapsed_s seconds in.

    Orientation: yaw within +-25 deg (valid [-90, 90]), pitch within +-15 deg
    (valid [-60, 60]), roll within +-10 deg (valid [-45, 45]). Translation:
    x within +-3 cm and y within +-2 cm (valid [-100, 100]), z in
    [-50, -40] cm (valid [-200, -5]). Scale is uniform, 1.0 +- 0.02.
    Elements are rounded to DECIMALS places. frame_builder extracts the
    published angles from this rounded matrix, so they match the contract's
    extraction exactly.
    """
    matrix = build_matrix(
        yaw_deg=sine(elapsed_s, 25.0, 11.0),
        pitch_deg=sine(elapsed_s, 15.0, 7.0, 1.0),
        roll_deg=sine(elapsed_s, 10.0, 13.0, 2.0),
        scale=1.0 + sine(elapsed_s, 0.02, 29.0),
        tx_cm=sine(elapsed_s, 3.0, 19.0),
        ty_cm=sine(elapsed_s, 2.0, 23.0, 0.5),
        tz_cm=-45.0 + sine(elapsed_s, 5.0, 17.0),
    )
    return [[round(value, DECIMALS) for value in row] for row in matrix]


def partial_scores(elapsed_s: float, rng: random.Random) -> dict[str, float]:
    """A random proper subset of the blendshape scores, 0 to 51 names.

    May be empty, as the contract allows. Values stay in [0, 1].
    """
    scores = blendshape_scores(elapsed_s, rng)
    kept = rng.sample(BLENDSHAPES, rng.randint(0, len(BLENDSHAPES) - 1))
    return {name: scores[name] for name in BLENDSHAPES if name in kept}


# --- Frames ------------------------------------------------------------------


def state_at(elapsed_ms: int) -> str:
    """Tracking state for elapsed_ms milliseconds into the run.

    Each 15 s cycle holds a 1.0 s run of "lost" from 10.0 s and a 0.5 s run
    of "partial" from 12.0 s. The other 13.5 s, 90 percent, are "tracking".
    """
    t = (elapsed_ms / 1000.0) % CYCLE_S
    if LOST_START_S <= t < LOST_END_S:
        return "lost"
    if PARTIAL_START_S <= t < PARTIAL_END_S:
        return "partial"
    return "tracking"


def make_frame(t_ms: int, state: str, rng: random.Random, elapsed_ms: int = 0) -> dict:
    """One camera feature message as a dict.

    t_ms is the frame's capture time, integer Unix epoch milliseconds, UTC,
    and is published as timestamp_ms. elapsed_ms is the integer number of
    milliseconds since the run started. It drives the motion, so a seeded
    run produces the same values whatever wall-clock time it starts at.
    state is "tracking", "partial" or "lost", and the frame follows the
    contract's tracking-state table for it. The message is assembled by
    frame_builder.make_feature_message from the same plain inputs the
    camera source uses, and RuntimeError is raised if it classifies them
    differently from state. confidence is always None: the stock Face
    Landmarker result carries no per-frame score.
    """
    elapsed_s = elapsed_ms / 1000.0
    if state == "tracking":
        face_count, scores, matrix = 1, blendshape_scores(elapsed_s, rng), head_matrix(elapsed_s)
    elif state == "partial":
        face_count, scores, matrix = 1, partial_scores(elapsed_s, rng), None
    elif state == "lost":
        face_count, scores, matrix = 0, None, None
    else:
        raise ValueError(f"unknown tracking state {state!r}")
    frame = make_feature_message(t_ms, face_count, scores, matrix)
    if frame["tracking_state"] != state:
        raise RuntimeError(
            f"generated inputs for {state!r} were classified {frame['tracking_state']!r}"
        )
    return frame


def generate_frames(rate_hz: float, seed: int, start_ms: int):
    """Yield frames forever at rate_hz frames per second.

    Frame i is captured at start_ms + round(i * 1000 / rate_hz), integer
    Unix epoch milliseconds, so timestamps never decrease. The values depend
    only on seed, rate_hz and the frame index.
    """
    rng = random.Random(seed)
    index = 0
    while True:
        elapsed_ms = round(index * 1000.0 / rate_hz)
        yield make_frame(start_ms + elapsed_ms, state_at(elapsed_ms), rng, elapsed_ms)
        index += 1


def replay_frames(path: Path):
    """Yield the frames stored one JSON object per line in path, once."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def first_frame_in_state(state: str, seed: int, rate_hz: float, start_ms: int) -> dict:
    """The first frame of a seeded run whose tracking_state is state."""
    for frame in generate_frames(rate_hz, seed, start_ms):
        if frame["tracking_state"] == state:
            return frame
    raise AssertionError("unreachable: generate_frames is infinite")


def write_fixtures(directory: Path, seed: int = 11, rate_hz: float = 30.0) -> list[Path]:
    """Write frame_tracking.json, frame_partial.json and frame_lost.json.

    Each is the first frame in that state of a seeded run starting at
    FIXTURE_START_MS, pretty-printed with two-space indentation.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for state in ("tracking", "partial", "lost"):
        frame = first_frame_in_state(state, seed, rate_hz, FIXTURE_START_MS)
        path = directory / f"frame_{state}.json"
        path.write_text(json.dumps(frame, indent=2) + "\n", encoding="utf-8", newline="\n")
        written.append(path)
    return written


# --- Publishing -----------------------------------------------------------


def encode(frame: dict) -> str:
    """Wire encoding: compact JSON text, NaN and infinity refused."""
    return json.dumps(frame, separators=(",", ":"), allow_nan=False)


async def publish(frames, rate_hz: float, port: int, record: Path | None) -> None:
    """Serve frames on ws://127.0.0.1:port at rate_hz frames per second.

    Every connected client receives each frame as one JSON text message.
    A client that disconnects is dropped from the broadcast set, and
    broadcast skips connections that are closing, so the publisher keeps
    running. A generated stream runs until interrupted; a replay returns
    when the file is exhausted.
    """
    from websockets.asyncio.server import broadcast, serve

    clients = set()

    async def handler(websocket) -> None:
        clients.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            clients.discard(websocket)

    loop = asyncio.get_running_loop()
    record_handle = record.open("w", encoding="utf-8", newline="\n") if record else None
    async with serve(handler, "127.0.0.1", port):
        print(f"publishing on ws://127.0.0.1:{port} at {rate_hz} Hz", file=sys.stderr, flush=True)
        start = loop.time()
        for index, frame in enumerate(frames):
            await asyncio.sleep(max(0.0, start + index / rate_hz - loop.time()))
            message = encode(frame)
            broadcast(clients, message)
            if record_handle:
                record_handle.write(message + "\n")
                record_handle.flush()
    if record_handle:
        record_handle.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthetic Sojourner camera feature stream. Not a camera, not recorded human data."
    )
    parser.add_argument("--rate", type=float, default=30.0, help="frames per second (default 30.0)")
    parser.add_argument("--seed", type=int, default=11, help="random seed (default 11)")
    parser.add_argument("--record", type=Path, help="write every emitted frame to this file, one JSON object per line")
    parser.add_argument("--replay", type=Path, help="publish the frames in this JSON-lines file instead of generating")
    parser.add_argument("--port", type=int, default=8765, help="websocket port on 127.0.0.1 (default 8765)")
    parser.add_argument("--once", action="store_true", help="print one frame to stdout and exit")
    parser.add_argument("--write-fixtures", type=Path, metavar="DIR", help="write the three fixture frames into DIR and exit")
    args = parser.parse_args(argv)
    if args.rate <= 0:
        parser.error("--rate must be positive")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.write_fixtures:
        for path in write_fixtures(args.write_fixtures, args.seed, args.rate):
            print(path)
        return
    if args.replay:
        frames = replay_frames(args.replay)
    else:
        frames = generate_frames(args.rate, args.seed, int(time.time() * 1000))
    if args.once:
        frame = next(iter(frames))
        if args.record:
            args.record.write_text(encode(frame) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps(frame, indent=2))
        return
    asyncio.run(publish(frames, args.rate, args.port, args.record))


if __name__ == "__main__":
    main()
