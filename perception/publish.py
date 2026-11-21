"""Publish the camera feature stream, message 1 of docs/contracts.md.

One entry point and one websocket server for both sources:

    python -m perception.publish --source fake      synthetic, no camera
    python -m perception.publish --source camera    live laptop camera

Every connected client on ws://127.0.0.1:PORT receives each frame as one
compact JSON text message, the same wire format for both sources.

The fake source paces itself at --rate. The camera source is paced by the
camera: each frame is published as soon as it has been captured and
detected. Camera reads block, so they run on one dedicated worker thread
and the event loop stays free to serve clients.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perception import fake_stream  # noqa: E402
from perception.mediapipe_source import CameraError, CameraSource  # noqa: E402
from tests.contract_schema import validate_feature  # noqa: E402

DEFAULT_PORT = 8765


def describe(message: dict, reasons: list[str]) -> str:
    """One human-readable diagnostic line for a published message.

    Shows timestamp_ms, tracking_state, the feature count, yaw/pitch/roll
    in degrees (or "-" without a pose), confidence, the contract
    validator's errors, and for a non-tracking frame the checks that
    rejected it.
    """
    pose = message["head_pose"]
    if pose is None:
        angles = "yaw=      -  pitch=      -  roll=      -"
    else:
        angles = f"yaw={pose['yaw_deg']:+7.2f}  pitch={pose['pitch_deg']:+7.2f}  roll={pose['roll_deg']:+7.2f}"
    line = (
        f"ts={message['timestamp_ms']}  {message['tracking_state']:<8}  "
        f"features={len(message['features']):>2}  {angles}  "
        f"confidence={message['confidence']}  validator={validate_feature(message)}"
    )
    if reasons:
        line += f"  rejected_by={reasons}"
    return line


async def serve(frames, port: int, rate_hz: float | None, record: Path | None, print_frames: bool, limit: int | None) -> None:
    """Broadcast (message, reasons) pairs from frames on ws://127.0.0.1:port.

    rate_hz, when given, paces publication at that many frames per second;
    None publishes each frame as soon as frames yields it. limit stops
    after that many frames; None runs until frames ends or the process is
    interrupted. A client may connect and disconnect at any time.
    """
    from websockets.asyncio.server import broadcast, serve as ws_serve

    clients = set()

    async def handler(websocket) -> None:
        clients.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            clients.discard(websocket)

    loop = asyncio.get_running_loop()
    # One worker so every camera and detector call happens on the same thread.
    reader = ThreadPoolExecutor(max_workers=1)
    record_handle = record.open("w", encoding="utf-8", newline="\n") if record else None
    try:
        async with ws_serve(handler, "127.0.0.1", port):
            print(f"publishing on ws://127.0.0.1:{port}", file=sys.stderr, flush=True)
            start = loop.time()
            index = 0
            while limit is None or index < limit:
                if rate_hz is not None:
                    await asyncio.sleep(max(0.0, start + index / rate_hz - loop.time()))
                item = await loop.run_in_executor(reader, next, frames, None)
                if item is None:
                    break
                message, reasons = item
                text = fake_stream.encode(message)
                broadcast(clients, text)
                if record_handle:
                    record_handle.write(text + "\n")
                    record_handle.flush()
                if print_frames:
                    print(describe(message, reasons), flush=True)
                index += 1
    finally:
        reader.shutdown(wait=True)
        if record_handle:
            record_handle.close()


def fake_frames(rate_hz: float, seed: int):
    """Synthetic (message, reasons) pairs. reasons is always empty."""
    start_ms = time.time_ns() // 1_000_000
    for message in fake_stream.generate_frames(rate_hz, seed, start_ms):
        yield message, []


def camera_frames(source: CameraSource):
    """Live (message, reasons) pairs from an open CameraSource."""
    for frame in source.frames():
        yield frame.message, frame.reasons


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish the Sojourner camera feature stream.")
    parser.add_argument("--source", choices=("fake", "camera"), default="fake",
                        help="fake: synthetic frames, not measured. camera: live Face Landmarker frames (default fake)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"websocket port on 127.0.0.1 (default {DEFAULT_PORT})")
    parser.add_argument("--record", type=Path, help="write every published frame to this file, one JSON object per line")
    parser.add_argument("--print-frames", action="store_true",
                        help="print one diagnostic line per frame: state, feature count, angles, validator result")
    parser.add_argument("--frames", type=int, metavar="N", help="stop after N frames (default: run until Ctrl+C)")
    fake = parser.add_argument_group("fake source")
    fake.add_argument("--rate", type=float, default=30.0, help="frames per second (default 30.0)")
    fake.add_argument("--seed", type=int, default=11, help="random seed (default 11)")
    camera = parser.add_argument_group("camera source")
    camera.add_argument("--camera", type=int, default=0, metavar="INDEX", help="camera device index (default 0)")
    camera.add_argument("--mirrored", action="store_true",
                        help="the camera delivers a mirrored image: swap Left/Right blendshape names and negate translation x, yaw and roll (default off)")
    args = parser.parse_args(argv)
    if args.rate <= 0:
        parser.error("--rate must be positive")
    if args.frames is not None and args.frames <= 0:
        parser.error("--frames must be positive")
    return args


def run(args: argparse.Namespace) -> None:
    if args.source == "fake":
        asyncio.run(serve(fake_frames(args.rate, args.seed), args.port, args.rate, args.record, args.print_frames, args.frames))
        return
    source = CameraSource(camera_index=args.camera, mirrored=args.mirrored)
    try:
        asyncio.run(serve(camera_frames(source), args.port, None, args.record, args.print_frames, args.frames))
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args)
    except CameraError as error:
        print(f"camera source failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
