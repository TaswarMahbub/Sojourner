"""Constructed face-preview stream for testing the PiP without a camera. NOT a camera feed.

    python -m demo.preview_fixture [--pattern tracking|lost|cycle] [--port N]

Every message carries "source": "fixture", and the PiP labels it TEST FIXTURE,
never LIVE CAMERA. The image is drawn here: a grey frame with a red square
centred on each ANCHOR landmark. The landmarks are constructed: all 478 points
on a face-shaped ellipse grid, with the anchors at exact known positions. A
correctly aligned overlay puts each anchor's mesh vertex in the middle of its
red square.

Patterns: tracking (always a face), lost (never), cycle (3 s tracking, 3 s lost).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo.face_preview import encode_preview, preview_port  # noqa: E402
from demo.motor_bridge import Fanout  # noqa: E402

LANDMARKS = 478
WIDTH, HEIGHT = 640, 480
# Anchor landmark index -> normalized (x, y). 1 nose tip, 33/263 eye corners, 61/291 mouth corners.
ANCHORS = {1: (0.50, 0.55), 33: (0.38, 0.42), 263: (0.62, 0.42), 61: (0.42, 0.70), 291: (0.58, 0.70)}
MARKER_HALF_PX = 12  # in the 640 px source image; 6 px in the 320 px preview


class _Point:
    def __init__(self, x, y):
        self.x, self.y = x, y


def fixture_landmarks() -> list[_Point]:
    points = []
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(LANDMARKS):
        r = math.sqrt((i + 0.5) / LANDMARKS)
        a = i * golden
        points.append(_Point(0.5 + 0.34 * r * math.cos(a), 0.52 + 0.42 * r * math.sin(a)))
    for index, (x, y) in ANCHORS.items():
        points[index] = _Point(x, y)
    return points


def fixture_image():
    import numpy as np

    img = np.full((HEIGHT, WIDTH, 3), 90, dtype=np.uint8)
    for x, y in ANCHORS.values():
        cx, cy = round(x * WIDTH), round(y * HEIGHT)
        img[cy - MARKER_HALF_PX:cy + MARKER_HALF_PX, cx - MARKER_HALF_PX:cx + MARKER_HALF_PX] = (0, 0, 255)  # BGR red
    return img


def message(tracking: bool, now_ms: int) -> dict:
    face = [fixture_landmarks()] if tracking else []
    return encode_preview(fixture_image(), face, "tracking" if tracking else "lost", now_ms, source="fixture")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pattern", choices=("tracking", "lost", "cycle"), default="tracking")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--fps", type=float, default=10.0)
    args = parser.parse_args(argv)
    server = Fanout(args.port if args.port is not None else preview_port(), latest_only=True)
    print(f"FIXTURE | constructed face preview, NOT a camera, on ws://127.0.0.1:{server.port} ({args.pattern})", flush=True)
    start = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - start
            tracking = args.pattern == "tracking" or (args.pattern == "cycle" and int(elapsed // 3) % 2 == 0)
            server.publish(json.dumps(message(tracking, time.time_ns() // 1_000_000), separators=(",", ":")))
            time.sleep(1 / args.fps)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
