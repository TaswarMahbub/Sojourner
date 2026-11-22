"""Local face-mesh preview side channel (demo/face_preview.py) and its camera tap.

Constructed inputs only: no camera, no detector, no recording.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from demo.face_preview import DEFAULT_PORT, PORT_ENV, PreviewChannel, flat_landmarks, preview_port
from demo.motor_bridge import Fanout
from perception import mediapipe_source
from perception.mediapipe_source import CameraSource


def _point(x, y):
    return SimpleNamespace(x=x, y=y, z=0.0)


def _fake_source(preview):
    """A CameraSource whose camera and detector are fakes: no hardware, the real read_frame."""
    source = object.__new__(CameraSource)
    source.mirrored = False
    source._clock = mediapipe_source.StrictlyIncreasingMs()
    source._preview = preview
    source._capture = SimpleNamespace(read=lambda: (True, "BGR"))
    result = SimpleNamespace(face_landmarks=[[_point(0.5, 0.5)] * 478], face_blendshapes=[], facial_transformation_matrixes=[])
    source._landmarker = SimpleNamespace(detect_for_video=lambda image, ts: result)
    source._cv2 = SimpleNamespace(cvtColor=lambda img, code: img, COLOR_BGR2RGB=0, COLOR_BGR2GRAY=0, mean=lambda g: (100.0,))
    source._mp = SimpleNamespace(Image=lambda **kw: kw, ImageFormat=SimpleNamespace(SRGB=1))
    return source


def test_port_default_and_override():
    assert preview_port({}) == DEFAULT_PORT == 8768
    assert preview_port({PORT_ENV: "9100"}) == 9100
    with pytest.raises(ValueError):
        preview_port({PORT_ENV: "x"})


def test_camera_message_is_identical_with_or_without_the_preview(monkeypatch):
    monkeypatch.setattr(mediapipe_source.time, "time_ns", lambda: 1795276800000 * 1_000_000)
    seen = []
    with_preview = _fake_source(lambda bgr, lm, msg: seen.append((bgr, len(lm[0]), msg["tracking_state"])))
    without = _fake_source(None)
    a, b = with_preview.read_frame(), without.read_frame()
    assert a.message == b.message and a.reasons == b.reasons
    assert "landmarks" not in json.dumps(a.message) and "image" not in a.message
    assert seen == [("BGR", 478, a.message["tracking_state"])]


def test_a_failing_preview_is_switched_off_and_the_camera_keeps_running():
    calls = []

    def broken(*_args):
        calls.append(1)
        raise RuntimeError("preview exploded")

    source = _fake_source(broken)
    first, second = source.read_frame(), source.read_frame()
    assert first.message["schema_version"] == "4" and second.message["schema_version"] == "4"
    assert calls == [1] and source._preview is None


def test_flat_landmarks():
    assert flat_landmarks([]) is None
    assert flat_landmarks([[_point(0.123456, 0.98765)]]) == [0.1235, 0.9877]


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_offer_is_latest_only_and_rate_limited():
    published = []
    gate = threading.Event()

    def slow_encode(bgr, lm, state, ts, source):
        gate.wait(2)
        return {"bgr": bgr}

    channel = PreviewChannel(published.append, max_fps=1000, encode=slow_encode)
    try:
        for i in range(50):  # the camera thread never waits on the encoder
            channel.offer(i, [], {"tracking_state": "tracking", "timestamp_ms": i})
        gate.set()
        assert _wait(lambda: len(published) >= 1)
        time.sleep(0.1)
        assert channel.offered == 50 and channel.superseded >= 48
        assert json.loads(published[-1])["bgr"] == 49  # the newest frame, never a backlog
        assert len(published) <= 2
    finally:
        channel.close()


def test_no_encoding_while_no_browser_is_connected():
    encoded = []
    channel = PreviewChannel(lambda _t: None, has_clients=lambda: False,
                             encode=lambda *a, **k: encoded.append(1) or {})
    try:
        channel.offer("img", [], {"tracking_state": "tracking", "timestamp_ms": 1})
        assert _wait(lambda: channel.skipped_no_client == 1)
        assert encoded == []
    finally:
        channel.close()


def test_encoder_errors_are_counted_not_raised():
    def boom(*_a, **_k):
        raise RuntimeError("bad image")

    channel = PreviewChannel(lambda _t: None, encode=boom)
    try:
        channel.offer("img", [], {"tracking_state": "tracking", "timestamp_ms": 1})
        assert _wait(lambda: channel.errors == 1)
    finally:
        channel.close()


def test_latest_only_fanout_drops_outdated_frames_for_a_slow_client():
    from websockets.asyncio.client import connect

    server = Fanout(0, latest_only=True)
    try:
        async def client():
            async with connect(f"ws://127.0.0.1:{server.port}") as ws:
                await asyncio.sleep(0.2)
                for i in range(20):
                    server.publish(json.dumps({"n": i}))
                await asyncio.sleep(0.3)
                got = [json.loads(await asyncio.wait_for(ws.recv(), 2))]
                while True:
                    try:
                        got.append(json.loads(await asyncio.wait_for(ws.recv(), 0.3)))
                    except asyncio.TimeoutError:
                        return got

        got = asyncio.run(client())
        assert got[-1] == {"n": 19} and len(got) < 20
    finally:
        server.close()
    with pytest.raises(ValueError):
        Fanout(0, replay=True, latest_only=True)


def test_encode_preview_downscales_and_keeps_landmarks_only_when_a_face_is_found():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from demo.face_preview import encode_preview

    img = np.zeros((480, 640, 3), dtype=np.uint8)
    face = [[_point(0.25, 0.75)] * 478]
    msg = encode_preview(img, face, "tracking", 1795276800000)
    assert msg["image"]["width"] == 320 and msg["image"]["height"] == 240
    assert msg["source"] == "camera" and len(msg["landmarks"]) == 956 and msg["landmarks"][:2] == [0.25, 0.75]
    assert encode_preview(img, face, "lost", 1)["landmarks"] is None
    with pytest.raises(ValueError):
        encode_preview(img, face, "tracking", 1, source="synthetic")
