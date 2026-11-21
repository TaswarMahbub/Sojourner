# Perception

Owner: Mridhul.

This directory will hold the MediaPipe Face Landmarker integration that publishes the camera feature stream defined in `docs/contracts.md`.

Planned responsibilities:

- Capture camera frames and record `timestamp_ms` in Unix epoch milliseconds, UTC.
- Run Face Landmarker for one face, with blendshapes and the facial transformation matrix enabled.
- Publish subject-relative blendshape names, head pose, tracking state, and confidence.
- Emit `lost` or `partial` when the face, the blendshapes, or the pose do not meet schema 1. Those states are not actionable.

Raw 478-point landmarks are not part of schema 1. They can be used inside this directory later. They are not written to git as datasets or recordings.

No perception code is included in this revision.
