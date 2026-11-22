"""Operator commands for local gesture-interval review.

Run ``python -m ml.label``. The commands read a Schema 4 JSONL file that
``python -m perception.publish --record PATH`` already wrote. They do not
open a camera and they do not train a model.

``perception.calibrate`` writes a different, event-wrapped JSONL file. This
tool rejects that file instead of treating it as camera features.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ml.dataset import DatasetError, SplitPlanError
from ml.labeling import (
    CANDIDATE_LABELS,
    LabelingError,
    check_collection,
    clear_clip_label,
    confirm_interval,
    correct_interval,
    create_session,
    discard_session,
    inspect_recording,
    mark_interval,
    mark_uncertain,
    prepare_session,
    reclassify_split,
    remove_interval,
    session_text,
    set_clip_label,
)


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise LabelingError("operator_input_invalid")


def _clock(value: str) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise LabelingError("operator_input_invalid")
    try:
        number = int(value)
    except ValueError:
        raise LabelingError("operator_input_invalid") from None
    return number


def _index(value: str) -> int:
    return _clock(value)


def _build() -> _Parser:
    parser = _Parser(prog="python -m ml.label")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("recording", type=Path)

    init = commands.add_parser("init")
    init.add_argument("--recording", type=Path, required=True)
    init.add_argument("--out", type=Path, required=True)
    init.add_argument("--dataset", required=True)
    init.add_argument("--participant", required=True)
    init.add_argument("--session", required=True)
    init.add_argument("--recording-id", required=True)
    init.add_argument("--split", required=True)
    init.add_argument("--role", required=True)
    init.add_argument("--category", required=True)
    init.add_argument("--provenance", required=True)
    init.add_argument("--claim", required=True)
    init.add_argument("--decision", required=True)
    init.add_argument("--labels", default="")
    init.add_argument("--extra-label", action="append", default=[])
    init.add_argument("--length", type=_index, default=1)
    init.add_argument("--stride", type=_index, default=1)
    init.add_argument("--conditions", default="")
    init.add_argument("--replace", action="store_true")

    overview = commands.add_parser("overview")
    overview.add_argument("session", type=Path)

    mark = commands.add_parser("mark")
    mark.add_argument("session", type=Path)
    mark.add_argument("--start", type=_clock, required=True)
    mark.add_argument("--end", type=_clock, required=True)
    mark.add_argument("--label", default=None)
    mark.add_argument("--prompted", action="store_true")
    mark.add_argument("--uncertain", action="store_true")

    correct = commands.add_parser("correct")
    correct.add_argument("session", type=Path)
    correct.add_argument("--index", type=_index, required=True)
    correct.add_argument("--start", type=_clock)
    correct.add_argument("--end", type=_clock)
    correct.add_argument("--label")

    remove = commands.add_parser("remove")
    remove.add_argument("session", type=Path)
    remove.add_argument("--index", type=_index, required=True)

    confirm = commands.add_parser("confirm")
    confirm.add_argument("session", type=Path)
    confirm.add_argument("--index", type=_index, required=True)

    uncertain = commands.add_parser("uncertain")
    uncertain.add_argument("session", type=Path)
    uncertain.add_argument("--index", type=_index, required=True)

    clip = commands.add_parser("clip")
    clip.add_argument("session", type=Path)
    clip.add_argument("--label", required=True)

    clear_clip = commands.add_parser("clear-clip")
    clear_clip.add_argument("session", type=Path)

    reclassify = commands.add_parser("reclassify")
    reclassify.add_argument("session", type=Path)
    reclassify.add_argument("--split", required=True)
    reclassify.add_argument("--reviewed", action="store_true")

    report = commands.add_parser("prepare")
    report.add_argument("session", type=Path)

    check = commands.add_parser("check")
    check.add_argument("sessions", type=Path, nargs="+")

    discard = commands.add_parser("discard")
    discard.add_argument("session", type=Path)
    discard.add_argument("--include-recording", action="store_true")

    commands.add_parser("labels")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build().parse_args(argv)
        if args.command == "inspect":
            print(inspect_recording(args.recording).to_text(), end="")
        elif args.command == "init":
            labels = None if args.labels == "" else tuple(part for part in args.labels.split(",") if part)
            create_session(
                args.recording,
                args.out,
                dataset_id=args.dataset,
                participant_id=args.participant,
                session_id=args.session,
                recording_id=args.recording_id,
                split=args.split,
                participant_role=args.role,
                source_category=args.category,
                timestamp_provenance=args.provenance,
                capture_claim=args.claim,
                decision_id=args.decision,
                reviewed_labels=labels,
                extra_labels=tuple(args.extra_label),
                window_length=args.length,
                window_stride=args.stride,
                conditions=args.conditions,
                replace=args.replace,
            )
        elif args.command == "overview":
            print(session_text(args.session), end="")
        elif args.command == "mark":
            mark_interval(
                args.session,
                args.start,
                args.end,
                args.label,
                prompted=args.prompted,
                uncertain=args.uncertain,
            )
        elif args.command == "correct":
            correct_interval(
                args.session,
                args.index,
                start_ms=args.start,
                end_ms=args.end,
                label=args.label,
            )
        elif args.command == "remove":
            remove_interval(args.session, args.index)
        elif args.command == "confirm":
            confirm_interval(args.session, args.index)
        elif args.command == "uncertain":
            mark_uncertain(args.session, args.index)
        elif args.command == "clip":
            set_clip_label(args.session, args.label)
        elif args.command == "clear-clip":
            clear_clip_label(args.session)
        elif args.command == "reclassify":
            reclassify_split(args.session, args.split, reviewed=args.reviewed)
        elif args.command == "prepare":
            report = prepare_session(args.session)
            print(report.to_text(), end="")
            if report.block_reasons or not report.split_accepted:
                return 2
        elif args.command == "check":
            check_collection(args.sessions)
        elif args.command == "discard":
            discard_session(args.session, include_recording=args.include_recording)
        elif args.command == "labels":
            for name in CANDIDATE_LABELS:
                print(name)
        return 0
    except LabelingError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except DatasetError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except SplitPlanError as exc:
        print(",".join(exc.codes), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
