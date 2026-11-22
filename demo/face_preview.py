"""Local face-mesh preview: the demo's own camera image and Face Landmarker landmarks, for a PiP view.

A side channel only. It reuses the image the demo already captured and the
landmarks its one Face Landmarker already produced (perception/mediapipe_source.py
calls PreviewChannel.offer after building each Schema 4 message). Nothing here
opens a camera, runs a detector, or touches the Schema 4 message, the control
loop or the receiver.

* The camera thread only stores references (offer is O(1)). A worker thread
  does the resizing and JPEG encoding, at most MAX_FPS, and only while a
  browser is connected. A frame that is not sent before a newer one arrives
  is dropped, never queued.
* Served on ws://127.0.0.1:${SOJOURNER_PREVIEW_PORT:-8768}, localhost only,
  latest-only per client (motor_bridge.Fanout).
* Nothing is written to disk: no image, video or landmark is recorded.

Message (one JSON object per WebSocket text message):

    {"type": "face_preview", "schema": 1, "source": "camera" | "fixture",
     "capture_ms": int, "tracking_state": "tracking" | "partial" | "lost",
     "image": {"mime": "image/jpeg", "width": int, "height": int, "b64": str},
     "landmarks": [x0, y0, x1, y1, ...] | null}

Landmarks are Face Landmarker's normalized image coordinates for the first
face, in the captured (unmirrored) image, rounded to 1e-4. They are null when
the frame is lost, so a stale mesh is never drawn.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time

PORT_ENV = "SOJOURNER_PREVIEW_PORT"
DEFAULT_PORT = 8768
MAX_FPS = 10.0
WIDTH_PX = 320
JPEG_QUALITY = 60
SCHEMA = 1
SOURCES = ("camera", "fixture")


def preview_port(environ=os.environ) -> int:
    raw = environ.get(PORT_ENV)
    if raw is None:
        return DEFAULT_PORT
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise ValueError(f"{PORT_ENV} must be a port number")
    return int(raw)


def flat_landmarks(face_landmarks) -> list[float] | None:
    """The first face's normalized (x, y) pairs, flattened and rounded, or None."""
    if not face_landmarks:
        return None
    out: list[float] = []
    for point in face_landmarks[0]:
        out.append(round(float(point.x), 4))
        out.append(round(float(point.y), 4))
    return out


def encode_preview(bgr, face_landmarks, tracking_state: str, capture_ms: int, *, source: str = "camera",
                   width: int = WIDTH_PX, quality: int = JPEG_QUALITY) -> dict:
    """Downscale and JPEG-encode one BGR image and attach its landmarks. Needs OpenCV."""
    import cv2

    if source not in SOURCES:
        raise ValueError("unknown preview source")
    h, w = bgr.shape[:2]
    height = max(1, round(h * width / w))
    small = cv2.resize(bgr, (width, height), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    landmarks = None if tracking_state == "lost" else flat_landmarks(face_landmarks)
    return {
        "type": "face_preview", "schema": SCHEMA, "source": source, "capture_ms": int(capture_ms),
        "tracking_state": tracking_state,
        "image": {"mime": "image/jpeg", "width": width, "height": height,
                  "b64": base64.b64encode(jpeg.tobytes()).decode("ascii")},
        "landmarks": landmarks,
    }


class PreviewChannel:
    """Latest-only hand-off from the camera thread to a rate-limited encoder thread."""

    def __init__(self, publish, *, has_clients=lambda: True, max_fps: float = MAX_FPS, source: str = "camera",
                 encode=encode_preview, clock=time.monotonic):
        self._publish = publish
        self._has_clients = has_clients
        self._period = 1.0 / max_fps
        self._source = source
        self._encode = encode
        self._clock = clock
        self._latest = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.offered = 0
        self.superseded = 0
        self.sent = 0
        self.skipped_no_client = 0
        self.errors = 0
        self.encode_ms: list[float] = []
        self.offer_ns: list[int] = []
        self._thread = threading.Thread(target=self._work, name="face-preview", daemon=True)
        self._thread.start()

    def offer(self, bgr, face_landmarks, message: dict) -> None:
        """Called on the camera thread. Stores references and returns; never blocks or encodes."""
        t0 = time.perf_counter_ns()
        if self._latest is not None:
            self.superseded += 1
        self._latest = (bgr, face_landmarks, message.get("tracking_state"), message.get("timestamp_ms"))
        self.offered += 1
        self._wake.set()
        if len(self.offer_ns) < 10000:
            self.offer_ns.append(time.perf_counter_ns() - t0)

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2)

    def _work(self) -> None:
        last = -1e9
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            wait = last + self._period - self._clock()
            if wait > 0:
                time.sleep(wait)  # newer frames keep replacing _latest meanwhile
            item, self._latest = self._latest, None
            if item is None or self._stop.is_set():
                continue
            if not self._has_clients():
                self.skipped_no_client += 1
                continue
            last = self._clock()
            try:
                t0 = time.perf_counter()
                text = json.dumps(self._encode(item[0], item[1], item[2], item[3], source=self._source),
                                  separators=(",", ":"))
                self.encode_ms.append((time.perf_counter() - t0) * 1000.0)
                self._publish(text)
                self.sent += 1
            except Exception:
                self.errors += 1  # the preview may fail; nothing else depends on it

    def summary(self) -> str:
        enc = sorted(self.encode_ms)
        med = f"{enc[len(enc) // 2]:.1f} ms" if enc else "-"
        offer = sorted(self.offer_ns)
        offer_med = f"{offer[len(offer) // 2] / 1000:.1f} us" if offer else "-"
        return (f"face preview: offered {self.offered}, sent {self.sent}, superseded {self.superseded}, "
                f"no client {self.skipped_no_client}, errors {self.errors}; encode median {med}; "
                f"camera-thread offer median {offer_med}")
