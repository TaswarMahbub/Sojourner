"""Live camera feature source: laptop camera -> Face Landmarker -> message 1.

This is the only perception file that touches the camera or the model.
mediapipe and cv2 are imported inside the functions that use them, never at
module level, so importing this module needs only the standard library.
numpy is never imported here: OpenCV returns the frame array and MediaPipe
returns the matrix array, and .tolist() on that array yields plain lists.

The detector result is reduced to plain data (face count, a name -> score
dict, a 4x4 list of rows) and handed to frame_builder.make_feature_message,
which applies the contract's tracking-state table.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perception.frame_builder import (  # noqa: E402
    assess,
    make_feature_message,
    mirror_feature_names,
    mirror_matrix,
)

# Resolved against the repository, not the working directory, so the
# publisher finds the model wherever it is launched from.
MODEL_PATH = REPO_ROOT / "perception" / "models" / "face_landmarker.task"

FRAME_WIDTH_PX = 640
FRAME_HEIGHT_PX = 480


class CameraError(RuntimeError):
    """The camera or the model could not be opened or stopped delivering.

    The message is written for the operator and is printed without a
    traceback by the publisher.
    """


@dataclass(frozen=True)
class CameraFrame:
    """One published message plus diagnostics about the captured image.

    reasons lists the checks that kept the frame from tracking, and is empty
    for a tracking frame. brightness is grayscale_brightness of the captured
    image, 0 to 255. Both are diagnostic only and are not part of the
    published message.
    """

    message: dict
    reasons: list[str]
    brightness: float | None = None


class StrictlyIncreasingMs:
    """Integer milliseconds that always exceed the previous value returned.

    detect_for_video in VIDEO mode requires every timestamp to be strictly
    greater than the one before and raises on a repeated or decreasing
    value. The source clock is time.monotonic_ns, which never moves
    backward, but two camera reads can land in the same millisecond. When
    that happens the value is advanced to previous + 1, so the detector
    never sees a repeat. These values are fed to the detector only; the
    published timestamp_ms is the wall-clock capture time.
    """

    def __init__(self) -> None:
        self._previous: int | None = None

    def next(self) -> int:
        candidate = time.monotonic_ns() // 1_000_000
        if self._previous is not None and candidate <= self._previous:
            candidate = self._previous + 1
        self._previous = candidate
        return candidate


def import_cv2():
    """Import OpenCV, or raise CameraError naming the missing package."""
    try:
        import cv2
    except ImportError as error:
        raise CameraError(
            "OpenCV is not installed. Install the camera dependencies with "
            "`python -m pip install -r perception/requirements.txt`."
        ) from error
    return cv2


def import_mediapipe():
    """Import MediaPipe, or raise CameraError naming the missing package."""
    try:
        import mediapipe
    except ImportError as error:
        raise CameraError(
            "MediaPipe is not installed. Install the camera dependencies with "
            "`python -m pip install -r perception/requirements.txt`."
        ) from error
    return mediapipe


def grayscale_brightness(cv2, bgr) -> float:
    """Mean grayscale value of a BGR image, 0 (black) to 255 (white).

    This is the repository's single definition of frame brightness. The
    image is converted with cv2.COLOR_BGR2GRAY, which weights channels
    0.299 R + 0.587 G + 0.114 B (ITU-R BT.601 luma), and every pixel is
    averaged with equal weight. It describes the lighting of the whole
    image, not of the face.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.mean(gray)[0])


def open_camera(index: int):
    """Open camera index at 640x480 and confirm it delivers a frame.

    On Windows the DirectShow backend is used; the default backend there
    is slow to start. Raises CameraError if the device does not open or its
    first read fails.
    """
    cv2 = import_cv2()
    if sys.platform == "win32":
        capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    else:
        capture = cv2.VideoCapture(index)
    if not capture.isOpened():
        capture.release()
        raise CameraError(f"Camera {index} could not be opened. Is it connected and not in use by another program?")
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH_PX)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT_PX)
    ok, _ = capture.read()
    if not ok:
        capture.release()
        raise CameraError(f"Camera {index} opened but returned no frame.")
    return capture


def create_landmarker(model_path: Path):
    """Face Landmarker in VIDEO mode for one face, blendshapes and matrix on.

    The detection, presence and tracking thresholds are left at their
    library defaults and are never copied into the message's confidence.
    Raises CameraError if the model file is missing.
    """
    if not model_path.is_file():
        raise CameraError(f"Face Landmarker model not found at {model_path}.")
    mp = import_mediapipe()
    vision = mp.tasks.vision
    options = vision.FaceLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=1,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
    )
    return vision.FaceLandmarker.create_from_options(options)


def result_to_plain(result) -> tuple[int, dict[str, float] | None, list[list[float]] | None]:
    """Reduce a FaceLandmarkerResult to (face_count, blendshapes, matrix).

    face_count is the number of faces with landmarks. blendshapes maps
    category name to score for the first face, or is None if the result
    carries none. matrix is the first facial transformation matrix as four
    rows of Python floats, translation in centimeters, or None.
    """
    face_count = len(result.face_landmarks)
    blendshapes = None
    if result.face_blendshapes:
        blendshapes = {c.category_name: c.score for c in result.face_blendshapes[0]}
    matrix = None
    if result.facial_transformation_matrixes:
        matrix = result.facial_transformation_matrixes[0].tolist()
    return face_count, blendshapes, matrix


def unmirror(blendshapes: dict[str, float] | None, matrix: list[list[float]] | None):
    """Restore subject-relative output from a detector fed a mirrored image.

    Swaps every *Left/*Right blendshape name, and conjugates the matrix so
    translation x, yaw and roll regain their unmirrored signs. The angles
    are not touched here: make_feature_message extracts them from the
    corrected matrix, so the published angles and matrix agree.
    """
    if blendshapes is not None:
        blendshapes = mirror_feature_names(blendshapes)
    if matrix is not None:
        matrix = mirror_matrix(matrix)
    return blendshapes, matrix


class CameraSource:
    """An open camera and landmarker that yield CameraFrame objects.

    Opening happens in the constructor, so a missing camera or model fails
    before any server starts. Call close() to release both.
    """

    def __init__(self, camera_index: int = 0, mirrored: bool = False, model_path: Path = MODEL_PATH,
                 preview=None) -> None:
        self.mirrored = mirrored
        # Optional local preview tap: called as preview(bgr, face_landmarks, message) after the
        # message is built. It must only store references (it runs on the camera thread). It never
        # changes the message, and if it raises it is switched off; the camera keeps running.
        self._preview = preview
        self._clock = StrictlyIncreasingMs()
        self._landmarker = create_landmarker(model_path)
        try:
            self._capture = open_camera(camera_index)
        except CameraError:
            self._landmarker.close()
            raise
        self._cv2 = import_cv2()
        self._mp = import_mediapipe()

    def read_frame(self) -> CameraFrame:
        """Capture, detect and assemble one message.

        timestamp_ms is wall-clock Unix epoch milliseconds, UTC, taken as
        soon as the read returns. The buffer given to the detector is the
        camera image as captured, not mirrored. brightness is measured on
        that same image. Raises CameraError if the camera stops delivering
        frames.
        """
        ok, bgr = self._capture.read()
        capture_ms = time.time_ns() // 1_000_000
        if not ok:
            raise CameraError("The camera stopped delivering frames.")
        rgb = self._cv2.cvtColor(bgr, self._cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, self._clock.next())
        face_count, blendshapes, matrix = result_to_plain(result)
        if self.mirrored:
            blendshapes, matrix = unmirror(blendshapes, matrix)
        reasons = assess(face_count, blendshapes, matrix).reasons
        message = make_feature_message(capture_ms, face_count, blendshapes, matrix)
        if self._preview is not None:
            try:
                self._preview(bgr, result.face_landmarks, message)
            except Exception:  # a preview fault must never reach the control path
                logging.getLogger(__name__).warning("face preview disabled after an error", exc_info=True)
                self._preview = None
        return CameraFrame(message, reasons, grayscale_brightness(self._cv2, bgr))

    def frames(self):
        """Yield CameraFrame objects until the camera fails or close() is called."""
        while True:
            yield self.read_frame()

    def close(self) -> None:
        """Release the camera and the landmarker."""
        self._capture.release()
        self._landmarker.close()
