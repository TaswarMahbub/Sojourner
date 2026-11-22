"""Generate the live-mirror fixture: one scripted control session, recorded as the bridge streams it.

Runs demo/run_live_sim.py's synthetic script on a simulated clock through the
unmodified control/ pipeline and the tapped receiver oracle, and writes the
hello plus every operation (gzipped JSON lines). The frontend test
src/live/liveMirror.test.js replays it through the JavaScript receiver.

Run from the repository root:

    python3 frontend/scripts/live_mirror_fixture.py

Synthetic frames and the labelled test key from tests/control_auth.py. Not a
recording, and the key is not a rover secret. The same code writes the same file.
"""

from __future__ import annotations

import gzip
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from demo.motor_bridge import OpLog  # noqa: E402
from demo.run_live_sim import run_scripted  # noqa: E402
from tests.session_harness import SimClock  # noqa: E402

OUT = ROOT / "frontend" / "src" / "live" / "__fixtures__" / "live-session.jsonl.gz"


def generate() -> list[str]:
    clock = SimClock()
    log = OpLog(clock_ms=lambda: clock.unix)
    with tempfile.TemporaryDirectory() as tmp:
        run_scripted(clock, Path(tmp), log, out=lambda _line: None)
    return log.history()


def main() -> int:
    lines = generate()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(OUT, "wb", mtime=0) as handle:
        handle.write(("\n".join(lines) + "\n").encode("utf-8"))
    print(f"wrote {OUT.relative_to(ROOT)}: {len(lines)} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
