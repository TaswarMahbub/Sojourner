"""Scripted end-to-end run: control/ publisher against the MotorReceiver oracle.

Run from the repository root:

    python tests/run_control_e2e.py

Prints each datagram the publisher sends and the receiver's decision, for a
handshake, rearm, one motion command, a camera-loss stop, a link-loss
watchdog stop, an operator estop, and recovery through rearm. The receiver is
the contract oracle, not firmware. This is not a hardware test.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control.publisher import MotionRequest  # noqa: E402
from tests.control_rig import Rig, lost_frame  # noqa: E402

MOVE = MotionRequest("manual", 0.15, 0.0)


def banner(text: str) -> None:
    print(f"\n== {text}")


def torque(rig: Rig) -> str:
    snap = rig.receiver.snapshot()
    return (
        f"    receiver: torque={rig.receiver.motors_have_torque()} lin={snap['linear_mps']:.6f} "
        f"armed={snap['armed']} fault_latch={snap['fault_latch']} estop_latch={snap['software_estop_latch']}"
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        rig = Rig(Path(tmp), verbose=True)

        banner("receiver boot: startup done, physical stop released, cutoff restored")
        rig.ready_receiver()

        banner("handshake: status, session start, status echo")
        rig.handshake()
        print(f"    session trusted={rig.publisher.session.trusted}")

        banner("rearm (local operator authorization on the laptop)")
        rig.arm()
        print(torque(rig))

        banner("one motion command")
        rig.advance(10)
        rig.fresh_perception()
        rig.tick(motion=MOVE)
        print(torque(rig))
        rig.advance(10)
        rig.status()

        banner("signal loss 1: camera loses the face, publisher sends a stop")
        rig.advance(10)
        rig.publisher.observe_frame(lost_frame(rig.clock.unix))
        rig.tick(motion=MOVE)
        print(f"    publisher decision: {rig.publisher.last_decision}")
        print(torque(rig))
        rig.advance(10)
        rig.status()

        banner("motion resumes with fresh perception")
        rig.advance(10)
        rig.fresh_perception()
        rig.tick(motion=MOVE)
        print(torque(rig))

        banner("signal loss 2: radio link drops for 300 ms, receiver watchdog stops")
        rig.link_up = False
        for _ in range(6):
            rig.advance(50)
            rig.fresh_perception()
            rig.tick(motion=MOVE)
        rig.link_up = True
        print(torque(rig))

        banner("link back: publisher only sends stops until rearm")
        for _ in range(2):
            rig.status()
            rig.fresh_perception()
            rig.tick(motion=MOVE)
            print(f"    publisher decision: {rig.publisher.last_decision}")
            rig.advance(10)
        print(torque(rig))

        banner("operator estop")
        rig.status()
        rig.tick(operator_estop=True)
        print(torque(rig))
        rig.advance(10)
        rig.status()
        rig.fresh_perception()
        rig.tick(motion=MOVE)
        print(f"    publisher decision: {rig.publisher.last_decision}")

        banner("recovery: rearm clears both latches, then motion")
        rig.advance(10)
        rig.status()
        rig.tick(operator_rearm=True)
        print(torque(rig))
        rig.advance(10)
        rig.status()
        rig.advance(10)
        rig.fresh_perception()
        rig.tick(motion=MOVE)
        print(torque(rig))

        banner("receiver decisions in order")
        print("    " + ", ".join(rig.results))
        motion_while_unarmed = [
            m for m, r in zip(rig.sent, rig.results) if m["stop"] is False and r != "accepted_motion" and r != "lost-in-transit"
        ]
        print(f"    motion datagrams the receiver refused: {len(motion_while_unarmed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
