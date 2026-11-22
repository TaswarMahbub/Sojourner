"""Local operator reset of lost or corrupt persisted control state.

docs/contracts.md section 4, "Persisted status-gate state", ends with:
"Recovering from a lost or corrupt state is an operator decision outside this
contract." Without a recovery path, a corrupt file locks the gate forever and
the rover can never be commanded again. This module is that operator
decision, made explicit and logged.

What a reset does:

* It requires a NEW controller id, different from the one in the current
  records. The status gate's challenge marks restart at 0, and old statuses
  that echo old challenges are signed for the old controller id. With a new
  id, every old status and every old command fails the controller check, so
  a restart from 0 cannot replay a challenge id that was already used.
* It renames the existing gate and session files to *.reset-<UTC>.bak. It
  deletes nothing.
* It writes a fresh status-gate object and a fresh session record for the new
  id, each with the same atomic durable write used everywhere else.
* It appends one JSON line to control_reset.log naming the operator, the
  reason, the old and new ids, and the time.

After a reset the rover must be re-provisioned out of band with the new
controller id (section 3, "Command authentication"), and the laptop's
SOJOURNER_CONTROLLER_ID set to it. Until then every status fails the
controller check and nothing is authorized.

A reset cannot be triggered from the network. No module that handles network
input imports this one, and tests/test_control.py checks that. The command
line entry point requires an interactive terminal and asks the operator to
type the new id back.

    python -m control.reset --state-dir PATH --new-controller-id ID --operator NAME --reason TEXT
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path

from control import durable
from control.auth import CONTROLLER_ID_PATTERN
from control.session import fresh_session_state
from control.status_gate import fresh_state

LOG = logging.getLogger("control.reset")

GATE_FILE = "status_gate.json"
SESSION_FILE = "session.json"
RESET_LOG = "control_reset.log"


class ResetRefused(RuntimeError):
    """The reset request does not meet its preconditions. Nothing was changed."""


def _recorded_controller(path: Path) -> str | None:
    """Best-effort read of the old controller id, for the log and the differs check."""
    text = durable.read_text_or_none(path)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if isinstance(value, dict) and isinstance(value.get("controller_id"), str):
        return value["controller_id"]
    return None


def _id_used_before(directory: Path, controller_id: str) -> bool:
    """True when the id appears in any existing record, backup, or the reset log.

    A corrupt file may not parse, so its raw text is searched for the quoted
    id as well. Reusing an old id with challenge marks restarted at 0 would
    let an old status be replayed, which is what the new identity prevents.
    """
    if not directory.exists():
        return False
    needle = json.dumps(controller_id)
    for path in directory.iterdir():
        if path.is_file() and (path.name.startswith((GATE_FILE, SESSION_FILE)) or path.name == RESET_LOG):
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                return True
    return False


def operator_reset(state_dir, new_controller_id: str, *, operator: str, reason: str, now=None) -> dict:
    """Start a fresh controller identity. Returns the log record that was written.

    Raises ResetRefused, before touching any file, when the new id is invalid,
    matches an id found in the existing records, or operator/reason is empty.
    """
    directory = Path(state_dir)
    if not isinstance(new_controller_id, str) or CONTROLLER_ID_PATTERN.fullmatch(new_controller_id) is None:
        raise ResetRefused("new controller id does not match ^[A-Za-z0-9_-]{1,32}$")
    if not operator or not operator.strip():
        raise ResetRefused("an operator name is required")
    if not reason or not reason.strip():
        raise ResetRefused("a reason is required")
    gate_path = directory / GATE_FILE
    session_path = directory / SESSION_FILE
    old_ids = {_recorded_controller(gate_path), _recorded_controller(session_path)} - {None}
    if new_controller_id in old_ids or _id_used_before(directory, new_controller_id):
        raise ResetRefused("the new controller id must differ from every id used before in this state directory")
    moment = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, exist_ok=True)
    backups = []
    for path in (gate_path, session_path):
        if path.exists():
            backup = path.with_name(f"{path.name}.reset-{moment}.bak")
            if backup.exists():
                raise ResetRefused(f"backup {backup.name} already exists")
            path.rename(backup)
            backups.append(backup.name)
    durable.atomic_write_json(gate_path, fresh_state(new_controller_id))
    durable.atomic_write_json(session_path, fresh_session_state(new_controller_id))
    record = {
        "event": "operator_reset",
        "time_utc": moment,
        "operator": operator.strip(),
        "reason": reason.strip(),
        "old_controller_ids": sorted(old_ids),
        "new_controller_id": new_controller_id,
        "backups": backups,
    }
    with open(directory / RESET_LOG, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
    LOG.warning("operator reset: %s", record)
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Start a fresh controller identity after lost or corrupt state.")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--new-controller-id", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print("refused: an operator reset needs an interactive terminal", file=sys.stderr)
        return 2
    print("This replaces the persisted control state with a new controller identity.")
    print("The rover must then be re-provisioned with that controller id.")
    typed = input(f"Type the new controller id ({args.new_controller_id}) to confirm: ")
    if typed != args.new_controller_id:
        print("refused: confirmation did not match", file=sys.stderr)
        return 2
    record = operator_reset(args.state_dir, args.new_controller_id, operator=args.operator, reason=args.reason)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
