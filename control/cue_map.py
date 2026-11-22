"""The Tier 2 cue map, chosen from the targeted probe session 20261122T073621Z.

Each entry is a control.tiers.CueBinding: one side of one calibrated
channel, relative to its measured rest mean, and the cue it gives. The
numbers below are fractions of the target's threshold under the corrected
threshold rule (perception/probe_session.py, VOLUNTARY_K = 0.33): 1.0 or
more means the target's cue fires.

    jawOpen    above rest  Go while the task is Hold, Stop while it moves
    pitch_deg  above rest  Faster   (head lift)
    pitch_deg  below rest  Slower   (head down)
    yaw_deg    above rest  Left     (head turn left: positive yaw is the nose
                                     toward the subject's left, docs/contracts.md
                                     section 1 "Angles"; positive angular is a
                                     left turn, section 3)
    yaw_deg    below rest  Right    (head turn right)
    roll_deg   unbound

Why Go and Stop share the jaw. It keeps Stop permanently on the most
reliable and fastest channel measured, which the project report requires of
a stop binding (jawOpen: repeatability CV 0.033, SNR 1131, median latency
423 ms, no false positive in 18 s of held-out rest). It also removes the
accidental-start path: no other gesture reaches above 0.13 of jaw's
threshold (the largest is head down, 0.13), so no incidental movement can
start the chair. Every other incidental crossing in the table produces only
a turn cue or Slower, and a turn cue in Hold waits for Go
(control/task.py), so Hold never moves. Any jaw press while the chair is
moving is Stop, whatever else is happening: the same rule Tier 3 uses for
"any press while moving is Stop". This is the standard single-switch
pattern, not a workaround. One press is one cue: the detector re-arms only
after the jaw is back inside its deadband, so the press that stops the
chair cannot also restart it.

Why yaw and not roll carries turning. Bound roll would fire Slower on every
deliberate tilt (tilt_left -> head_down 2.36, tilt_right -> head_down
1.69). Bound yaw: a deliberate turn reaches head_down at only 0.51 to 0.56.
Yaw's voluntary range is 48 degrees against roll's 15, and its right/left
release overshoot is 0.55 against roll's 0.73. Yaw and roll cannot both be
bound: tilt_right -> turn_right 3.84, tilt_left -> turn_left 3.40,
turn_right -> tilt_right 2.14, turn_left -> tilt_left 1.90; r = -0.93.

KNOWN RESIDUAL RISKS

1. An incidental tilt fires the matching turn cue at 3.4 to 3.8x its
   threshold and Slower at 1.7 to 2.4x. Roll being unbound does not make
   tilting harmless. While moving, a tilt turns the chair. In Hold it
   changes only the pending heading and the speed setting, and the Go
   lockout (item 6) discards both when it starts and when it ends. That
   closes this risk only around UNCUED stops. After a stop the user cued,
   there is no lockout: a tilt in Hold still queues a 15 degree increment
   and a lower speed that the next Go executes, and neither can be undone
   in Hold, because Stop and Cancel are the only cues that clear a pending
   turn and in Hold the jaw means Go (shown in tests/run_doorway_e2e.py).
2. Opening the jaw reaches 0.86 of the Faster threshold. It does not fire,
   but the margin is thin, so a Go may occasionally also step the speed up.
3. k = 0.33 sits in a window only 1.35x wide, (0.282, 0.381], derived from
   a single session. This is the most fragile constant in the system.
4. turn_left has one valid latency repetition, at 1131 ms. It is the
   slowest and least measured binding.
5. State mismatch on the shared channel, MITIGATED by the Go lockout. The
   jaw means Go or Stop according to the TASK state, not what the user
   sees; after an uncued stop the task is Hold while the chair may still be
   visibly slowing, so a jaw press meant as "stop!" would have read as Go.
   During the lockout it reads as Stop instead. What remains: a "stop!"
   press that completes more than GO_LOCKOUT_MS after the last lost tick is
   a Go.
6. Go lockout, GO_LOCKOUT_MS = 2500 ms after every uncued stop (signal loss,
   low confidence, a publisher refusal including a fault or estop),
   measured from the last tick the condition held (control/task_controller.py).
   Its duration is 250 ms (the contract's command lifetime) plus twice the
   slowest valid warned latency of any bound gesture (2 x 1131 ms); the
   factor 2 for an unwarned reaction is a judgement, and physical
   deceleration is not included because it has not been measured. Cost: a
   deliberate Go within 2.5 s of an uncued stop is ignored, which is felt
   most at the demo's closing beat, the signal-loss stop.

This map is PROVISIONAL until it is reviewed with the safety owner. It is
data only; control/tiers.py validates it against a profile, and nothing here
bypasses control/publisher.py.
"""

from __future__ import annotations

from control.cues import FASTER, GO, LEFT, RIGHT, SLOWER, STOP
from control.tiers import ABOVE, BELOW, CueBinding

SOURCE_SESSION = "20261122T073621Z"
SOURCE_VOLUNTARY_K = 0.33
UNBOUND_CHANNELS = ("roll_deg",)

CUE_MAP: tuple[CueBinding, ...] = (
    CueBinding("jawOpen", ABOVE, GO, moving_cue=STOP),
    CueBinding("pitch_deg", ABOVE, FASTER),
    CueBinding("pitch_deg", BELOW, SLOWER),
    CueBinding("yaw_deg", ABOVE, LEFT),
    CueBinding("yaw_deg", BELOW, RIGHT),
)
