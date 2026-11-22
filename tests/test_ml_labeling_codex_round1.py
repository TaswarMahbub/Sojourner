"""Regressions for the ML-2B1 round 1 audit of b320110.

1. A failed session replacement keeps the previous session.
2. Temporary files are unique and exclusive, so a planted symlink is never followed.
3. A recording cannot share a path or an inode with a generated file.
4. Discard verifies every target first and stays retryable after a failure.
5. Prepare reads one private snapshot of the recording.
6. Collection checks refuse one source recording under several sessions.
7. Session and annotation metadata are validated in full on every load.
8. Malformed metadata gives a stable code and exit 2 from the real CLI.
Also: the annotation-domain review reason.

Every file here is built in a disposable temporary directory. No frame is a
participant recording and no timestamp is a measured capture.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from perception.fake_stream import encode
from tests.test_ml_labeling import _frame, _write
from tests.test_ml_pipeline import T0

from ml import labeling as lab
from ml.dataset import DatasetError, SplitPlanError
from ml.features import FEATURE_NAMES
from ml.label import main
from ml.labeling import (
    MARKER_NAME,
    LabelingError,
    check_collection,
    confirm_interval,
    create_session,
    discard_session,
    mark_interval,
    prepare_session,
    reclassify_split,
    session_text,
)

CONTROLLED = (LabelingError, DatasetError, SplitPlanError)
PLANTED = "PLANTED-ML2B1-4417"
OPTIONS = dict(
    dataset_id="ds01",
    participant_id="p01",
    session_id="s01",
    recording_id="r01",
    split="train",
    participant_role="population_train",
    source_category="constructed_fixture",
    timestamp_provenance="constructed",
    capture_claim="constructed_fixture",
    decision_id="decision-1",
)
FRAMES = [_frame(T0, 0.2), _frame(T0 + 10, 0.8), _frame(T0 + 20, 0.4)]
JAW = FEATURE_NAMES.index("jawOpen.mean")
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _private_tmp(tmp_path, monkeypatch):
    # Snapshots go to a per-test temporary root, so leftovers can be counted.
    root = tmp_path / "systmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _make(tmp_path, *, inside=False, frames=FRAMES, name="features.jsonl", folder="session", **changes):
    destination = tmp_path / folder
    recording = (destination if inside else tmp_path) / name
    recording.parent.mkdir(parents=True, exist_ok=True)
    _write(recording, frames)
    created = create_session(recording, destination, **{**OPTIONS, **changes})
    return recording.resolve(), created


def _confirmed(directory, start=T0, end=T0 + 21, label="mouth_open"):
    mark_interval(directory, start, end, label)
    count = len(json.loads((directory / "annotations.json").read_text())["intervals"])
    confirm_interval(directory, count - 1)


def _files(directory):
    return {
        item.name: item.read_bytes()
        for item in directory.iterdir()
        if item.is_file() and not item.is_symlink()
    }


def _entries(directory):
    return sorted(item.name for item in directory.iterdir())


def _ready(tmp_path, **kwargs):
    """A confirmed, prepared session plus the bytes of everything in it."""
    recording, directory = _make(tmp_path, **kwargs)
    _confirmed(directory)
    assert prepare_session(directory).examples_emitted == 3
    return recording, directory, _files(directory), recording.read_bytes()


def _assert_intact(directory, recording, before, recording_bytes):
    assert _files(directory) == before
    assert recording.read_bytes() == recording_bytes
    assert not [name for name in _entries(directory) if name.startswith((".label-", "."))]
    report = prepare_session(directory)
    assert report.examples_emitted == 3 and not report.block_reasons


def _edit(directory, name, **changes):
    document = json.loads((directory / name).read_text())
    document.update(changes)
    (directory / name).write_text(json.dumps(document))


def _fail_os_replace(monkeypatch, predicate, *, error=OSError, times=1):
    real = os.replace
    hits = []

    def fake(src, dst):
        if len(hits) < times and predicate(Path(src), Path(dst)):
            hits.append((Path(src).name, Path(dst).name))
            raise error(PLANTED)
        return real(src, dst)

    monkeypatch.setattr(lab.os, "replace", fake)
    return hits


def _in_stage(path):
    return path.parent.name.startswith(lab._STAGE_PREFIX)


def _in_backup(path):
    return path.parent.name.startswith(lab._BACKUP_PREFIX)


# --------------------------------------------------------------------------
# 1. Replacement keeps the previous session on failure
# --------------------------------------------------------------------------

REPLACE_FAULTS = {
    "stage_session": lambda src, dst: _in_stage(dst) and dst.name == "session.json",
    "stage_annotations": lambda src, dst: _in_stage(dst) and dst.name == "annotations.json",
    "stage_marker": lambda src, dst: _in_stage(dst) and dst.name == MARKER_NAME,
    "first_backup_move": lambda src, dst: _in_backup(dst) and dst.name == MARKER_NAME,
    "backup_report": lambda src, dst: _in_backup(dst) and dst.name == "quality-report.json",
    "backup_annotations": lambda src, dst: _in_backup(dst) and dst.name == "annotations.json",
    "backup_session": lambda src, dst: _in_backup(dst) and dst.name == "session.json",
    "commit_session": lambda src, dst: _in_stage(src) and not _in_stage(dst) and dst.name == "session.json",
    "commit_annotations": lambda src, dst: _in_stage(src) and not _in_stage(dst) and dst.name == "annotations.json",
    "commit_marker": lambda src, dst: _in_stage(src) and not _in_stage(dst) and dst.name == MARKER_NAME,
}


@pytest.mark.parametrize("inside", [False, True], ids=["outside", "inside"])
@pytest.mark.parametrize("fault", sorted(REPLACE_FAULTS))
def test_failed_replacement_restores_the_previous_session(tmp_path, monkeypatch, fault, inside):
    recording, directory, before, recording_bytes = _ready(tmp_path, inside=inside)
    hits = _fail_os_replace(monkeypatch, REPLACE_FAULTS[fault])
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **{**OPTIONS, "session_id": "s99"})
    assert hits, "the fault was not reached"
    assert exc.value.code == "output_write_failed"
    assert PLANTED not in str(exc.value)
    monkeypatch.undo()
    _assert_intact(directory, recording, before, recording_bytes)


def test_failure_before_any_replacement_write(tmp_path, monkeypatch):
    recording, directory, before, recording_bytes = _ready(tmp_path)
    real = tempfile.mkdtemp

    def refuse(*args, **kwargs):
        if kwargs.get("prefix") == lab._STAGE_PREFIX:
            raise OSError(PLANTED)
        return real(*args, **kwargs)

    monkeypatch.setattr(lab.tempfile, "mkdtemp", refuse)
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **OPTIONS)
    assert exc.value.code == "output_write_failed"
    monkeypatch.undo()
    _assert_intact(directory, recording, before, recording_bytes)


def test_failed_verification_of_staged_files(tmp_path, monkeypatch):
    recording, directory, before, recording_bytes = _ready(tmp_path)

    def bad(*_args):
        raise LabelingError("output_write_failed")

    monkeypatch.setattr(lab, "_verify_staged", bad)
    with pytest.raises(LabelingError):
        create_session(recording, directory, replace=True, **OPTIONS)
    monkeypatch.undo()
    _assert_intact(directory, recording, before, recording_bytes)


def test_interrupt_during_commit_restores_and_propagates(tmp_path, monkeypatch):
    recording, directory, before, recording_bytes = _ready(tmp_path)
    _fail_os_replace(monkeypatch, REPLACE_FAULTS["commit_annotations"], error=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        create_session(recording, directory, replace=True, **OPTIONS)
    monkeypatch.undo()
    _assert_intact(directory, recording, before, recording_bytes)


def test_failure_during_cleanup_after_a_failed_replacement(tmp_path, monkeypatch):
    recording, directory, before, recording_bytes = _ready(tmp_path)
    _fail_os_replace(monkeypatch, REPLACE_FAULTS["commit_marker"])
    real = lab._remove_owned_dir
    monkeypatch.setattr(
        lab,
        "_remove_owned_dir",
        lambda path: False if path is not None and path.name.startswith(lab._STAGE_PREFIX) else real(path),
    )
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **OPTIONS)
    assert exc.value.code == "replacement_cleanup_incomplete"
    monkeypatch.undo()
    leftovers = [name for name in _entries(directory) if name.startswith(lab._STAGE_PREFIX)]
    assert len(leftovers) == 1
    # The previous session is fully back and usable. Only the staging copy is left.
    assert {name: data for name, data in _files(directory).items()} == before
    assert recording.read_bytes() == recording_bytes
    assert prepare_session(directory).examples_emitted == 3
    # A leftover is refused rather than silently reused or deleted.
    with pytest.raises(LabelingError) as again:
        create_session(recording, directory, replace=True, **OPTIONS)
    assert again.value.code == "directory_not_empty"


def test_failed_rollback_keeps_the_backup_instead_of_losing_it(tmp_path, monkeypatch):
    recording, directory, before, recording_bytes = _ready(tmp_path)
    forward = REPLACE_FAULTS["commit_annotations"]

    def predicate(src, dst):
        restoring = _in_backup(src) and dst.parent == directory and dst.name == "annotations.json"
        return forward(src, dst) or restoring

    _fail_os_replace(monkeypatch, predicate, times=2)
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **OPTIONS)
    assert exc.value.code == "replacement_rollback_incomplete"
    monkeypatch.undo()
    backups = [item for item in directory.iterdir() if item.name.startswith(lab._BACKUP_PREFIX)]
    assert len(backups) == 1
    assert (backups[0] / "annotations.json").read_bytes() == before["annotations.json"]
    assert recording.read_bytes() == recording_bytes
    # Without the annotations file the directory does not load as a session.
    with pytest.raises(LabelingError):
        prepare_session(directory)


def test_cleanup_failure_after_a_successful_replacement_is_reported(tmp_path, monkeypatch):
    recording, directory, _before, recording_bytes = _ready(tmp_path)
    real = lab._remove_owned_dir
    monkeypatch.setattr(
        lab,
        "_remove_owned_dir",
        lambda path: False if path is not None and path.name.startswith(lab._BACKUP_PREFIX) else real(path),
    )
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **{**OPTIONS, "session_id": "s02"})
    assert exc.value.code == "replacement_cleanup_incomplete"
    monkeypatch.undo()
    assert json.loads((directory / "session.json").read_text())["session_id"] == "s02"
    assert json.loads((directory / "annotations.json").read_text())["intervals"] == []
    assert recording.read_bytes() == recording_bytes


def test_successful_replacement_is_complete_and_leaves_nothing_behind(tmp_path):
    recording, directory, before, recording_bytes = _ready(tmp_path)
    create_session(recording, directory, replace=True, **{**OPTIONS, "session_id": "s02"})
    assert _entries(directory) == sorted([MARKER_NAME, "annotations.json", "session.json"])
    assert json.loads((directory / "session.json").read_text())["session_id"] == "s02"
    assert prepare_session(directory).confirmed_intervals == 0
    assert recording.read_bytes() == recording_bytes


def test_replacement_with_and_without_the_cli_flag(tmp_path, monkeypatch, capsys):
    recording, directory, before, recording_bytes = _ready(tmp_path)
    argv = [
        "init", "--recording", str(recording), "--out", str(directory), "--dataset", "ds01",
        "--participant", "p01", "--session", "s01", "--recording-id", "r01", "--split", "train",
        "--role", "population_train", "--category", "constructed_fixture",
        "--provenance", "constructed", "--claim", "constructed_fixture", "--decision", "decision-1",
    ]
    plain = subprocess.run([sys.executable, "-m", "ml.label", *argv], capture_output=True, text=True, cwd=REPO)
    assert plain.returncode == 2 and plain.stderr.strip() == "output_exists"
    _assert_intact(directory, recording, _files(directory), recording_bytes)
    before = _files(directory)
    _fail_os_replace(monkeypatch, REPLACE_FAULTS["commit_session"])
    assert main([*argv, "--replace"]) == 2
    assert capsys.readouterr().err.strip() == "output_write_failed"
    monkeypatch.undo()
    _assert_intact(directory, recording, before, recording_bytes)
    replaced = subprocess.run(
        [sys.executable, "-m", "ml.label", *argv, "--replace"], capture_output=True, text=True, cwd=REPO
    )
    assert replaced.returncode == 0 and replaced.stderr == ""
    assert prepare_session(directory).confirmed_intervals == 0


def test_interrupted_first_initialization_is_not_a_session(tmp_path, monkeypatch):
    recording = tmp_path / "features.jsonl"
    _write(recording, FRAMES)
    directory = tmp_path / "session"
    _fail_os_replace(monkeypatch, REPLACE_FAULTS["commit_marker"])
    with pytest.raises(LabelingError):
        create_session(recording, directory, **OPTIONS)
    monkeypatch.undo()
    assert not directory.exists()
    assert recording.read_bytes() == "".join(encode(frame) + "\n" for frame in FRAMES).encode()


# --------------------------------------------------------------------------
# 2. Temporary files
# --------------------------------------------------------------------------


def _victim(tmp_path):
    victim = tmp_path / "unrelated.txt"
    victim.write_text("KEEP-" + PLANTED)
    return victim


@pytest.mark.parametrize(
    "name,action",
    [
        (".annotations.json.tmp", lambda d: confirm_interval(d, 0)),
        (".session.json.tmp", lambda d: reclassify_split(d, "calibration", reviewed=True)),
        (".quality-report.json.tmp", prepare_session),
    ],
)
@pytest.mark.parametrize("planted", ["symlink", "file"])
def test_predictable_temporary_names_are_never_opened(tmp_path, name, action, planted):
    _recording, directory = _make(tmp_path, participant_role="held_out", split="eval")
    mark_interval(directory, T0, T0 + 21, "select")
    victim = _victim(tmp_path)
    path = directory / name
    if planted == "symlink":
        path.symlink_to(victim)
    else:
        path.write_text("KEEP-" + PLANTED)
    action(directory)
    assert victim.read_text() == "KEEP-" + PLANTED
    if planted == "file":
        assert path.read_text() == "KEEP-" + PLANTED
    else:
        assert path.is_symlink()


def test_unsafe_destination_types_are_refused(tmp_path):
    target = tmp_path / "out.json"
    victim = _victim(tmp_path)
    target.symlink_to(victim)
    with pytest.raises(LabelingError) as link:
        lab._atomic_write(target, "{}", allow_replace=True)
    assert link.value.code == "symlink_not_allowed"
    assert victim.read_text() == "KEEP-" + PLANTED
    folder = tmp_path / "folder.json"
    folder.mkdir()
    with pytest.raises(LabelingError) as directory:
        lab._atomic_write(folder, "{}", allow_replace=True)
    assert directory.value.code == "unsafe_output_type"
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    with pytest.raises(LabelingError) as pipe:
        lab._atomic_write(fifo, "{}", allow_replace=True)
    assert pipe.value.code == "unsafe_output_type"
    assert sorted(item.name for item in tmp_path.iterdir() if item.name.startswith(".")) == []


@pytest.mark.parametrize("where", ["write", "fsync", "rename"])
def test_failed_single_file_write_keeps_the_old_file_and_removes_only_its_temp(tmp_path, monkeypatch, where):
    _recording, directory = _make(tmp_path)
    mark_interval(directory, T0, T0 + 21, "select")
    before = (directory / "annotations.json").read_bytes()
    stranger = directory / ".annotations.json.other.tmp"
    stranger.write_text("not mine")
    if where == "rename":
        _fail_os_replace(monkeypatch, lambda src, dst: dst.name == "annotations.json")
    elif where == "fsync":
        monkeypatch.setattr(lab.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError(PLANTED)))
    else:
        real = lab.os.fdopen

        class Broken:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

            def write(self, _data):
                raise OSError(PLANTED)

        made = set()
        real_mkstemp = lab.tempfile.mkstemp

        def tracked(*a, **k):
            fd, name = real_mkstemp(*a, **k)
            made.add(fd)
            return fd, name

        monkeypatch.setattr(lab.tempfile, "mkstemp", tracked)
        monkeypatch.setattr(
            lab.os,
            "fdopen",
            lambda fd, mode="r", *a, **k: Broken(real(fd, mode, *a, **k)) if fd in made else real(fd, mode, *a, **k),
        )
    with pytest.raises(LabelingError) as exc:
        confirm_interval(directory, 0)
    monkeypatch.undo()
    assert exc.value.code == "output_write_failed" and PLANTED not in str(exc.value)
    assert (directory / "annotations.json").read_bytes() == before
    assert stranger.read_text() == "not mine"
    temps = [name for name in _entries(directory) if name.endswith(".tmp")]
    assert temps == [stranger.name]


def test_written_metadata_is_private_to_the_owner(tmp_path):
    _recording, directory = _make(tmp_path)
    prepare_session(directory)
    for name in ("session.json", "annotations.json", "quality-report.json", MARKER_NAME):
        assert stat.S_IMODE(os.stat(directory / name).st_mode) == 0o600


# --------------------------------------------------------------------------
# 3. Recording and generated-file collisions
# --------------------------------------------------------------------------

COLLIDING = [
    MARKER_NAME,
    "session.json",
    "annotations.json",
    "quality-report.json",
    "Annotations.JSON",
    "SESSION.json",
    ".label-stage-abc",
    ".label-backup-abc",
    ".annotations.json.tmp",
    ".session.json.x1y2.tmp",
]


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("name", COLLIDING)
def test_every_generated_name_is_refused_as_a_recording_name(tmp_path, name, replace):
    directory = tmp_path / "session"
    directory.mkdir()
    recording = directory / name
    _write(recording, FRAMES)
    before = recording.read_bytes()
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=replace, **OPTIONS)
    assert exc.value.code == "recording_collides_with_session_file"
    assert _entries(directory) == [name]
    assert recording.read_bytes() == before


@pytest.mark.parametrize("name", ["session.json", "annotations.json", MARKER_NAME, "quality-report.json"])
def test_hard_link_from_a_generated_name_to_the_recording_is_refused(tmp_path, name):
    recording, directory = _make(tmp_path)
    prepare_session(directory)
    before = recording.read_bytes()
    (directory / name).unlink()
    os.link(recording, directory / name)
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **OPTIONS)
    assert exc.value.code == "recording_collides_with_session_file"
    assert recording.read_bytes() == before


def test_directory_alias_cannot_hide_a_collision(tmp_path):
    directory = tmp_path / "session"
    directory.mkdir()
    recording = directory / "annotations.json"
    _write(recording, FRAMES)
    before = recording.read_bytes()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(LabelingError) as exc:
        create_session(alias / "annotations.json", directory, replace=True, **OPTIONS)
    assert exc.value.code == "recording_collides_with_session_file"
    assert recording.read_bytes() == before


def test_ordinary_recording_inside_the_directory_is_still_allowed(tmp_path):
    recording, directory, before, recording_bytes = _ready(tmp_path, inside=True)
    create_session(recording, directory, replace=True, **OPTIONS)
    assert recording.read_bytes() == recording_bytes


# --------------------------------------------------------------------------
# 4. Discard
# --------------------------------------------------------------------------

MANAGED = {MARKER_NAME, "session.json", "annotations.json"}


def _managed(directory):
    return MANAGED <= set(_entries(directory))


def test_discard_deletes_a_verified_recording_inside_the_session(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    prepare_session(directory)
    result = discard_session(directory, include_recording=True)
    assert result.deleted_recording and result.directory_removed and result.deleted_generated == 4
    assert not recording.exists() and not directory.exists()


def test_discard_leaves_a_recording_outside_the_session(tmp_path):
    recording, directory = _make(tmp_path)
    result = discard_session(directory, include_recording=True)
    assert not result.deleted_recording and result.recording_outside_session
    assert recording.exists() and not directory.exists()


@pytest.mark.parametrize("kind", ["unrelated_text", "other_jsonl"])
def test_redirected_recording_path_is_refused_before_anything_is_deleted(tmp_path, kind):
    recording, directory = _make(tmp_path, inside=True)
    victim = directory / ("unrelated.txt" if kind == "unrelated_text" else "other.jsonl")
    if kind == "unrelated_text":
        victim.write_text("KEEP-" + PLANTED)
    else:
        _write(victim, [_frame(T0, 0.5)])
    before = _files(directory)
    _edit(directory, "session.json", recording_path=str(victim))
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "recording_identity_mismatch"
    assert victim.read_bytes() == before[victim.name]
    assert recording.read_bytes() == before[recording.name]
    assert _managed(directory)


def test_unrelated_files_inside_the_directory_survive(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    keep = directory / "operator-note.txt"
    keep.write_text("keep")
    nested = directory / "nested"
    nested.mkdir()
    (nested / "keep").write_text("keep")
    result = discard_session(directory, include_recording=True)
    assert result.deleted_recording and not result.directory_removed
    assert result.unexpected_files_left == 2
    assert keep.read_text() == "keep" and (nested / "keep").read_text() == "keep"


def test_changed_recording_contents_are_not_deleted(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    _write(recording, [_frame(T0, 0.9), _frame(T0 + 10, 0.9), _frame(T0 + 20, 0.9)])
    changed = recording.read_bytes()
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "recording_identity_mismatch"
    assert recording.read_bytes() == changed and _managed(directory)
    # Ordinary discard still removes only this tool's files.
    result = discard_session(directory)
    assert not result.deleted_recording and recording.read_bytes() == changed


def test_symlinked_recording_is_refused(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    real = tmp_path / "real.jsonl"
    shutil.move(recording, real)
    recording.symlink_to(real)
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "symlink_not_allowed"
    assert real.exists() and recording.is_symlink() and _managed(directory)


def test_hard_linked_recording_is_refused(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    os.link(recording, tmp_path / "second-name.jsonl")
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "recording_has_other_links"
    assert recording.exists() and _managed(directory)


def test_generated_symlink_is_refused_before_anything_is_deleted(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    victim = _victim(tmp_path)
    (directory / "quality-report.json").symlink_to(victim)
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "symlink_not_allowed"
    assert victim.read_text() == "KEEP-" + PLANTED and recording.exists() and _managed(directory)


def test_failure_before_deletion_changes_nothing(tmp_path, monkeypatch):
    recording, directory = _make(tmp_path, inside=True)
    before = _files(directory)
    monkeypatch.setattr(lab, "_hash_file", lambda *_a, **_k: (_ for _ in ()).throw(OSError(PLANTED)))
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "recording_unreadable"
    assert _files(directory) == before


def test_failed_recording_deletion_keeps_the_session_for_a_retry(tmp_path, monkeypatch):
    recording, directory = _make(tmp_path, inside=True)
    before = _files(directory)
    real = Path.unlink

    def refuse(path, *args, **kwargs):
        if path == recording:
            raise PermissionError(PLANTED)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "recording_delete_failed" and PLANTED not in str(exc.value)
    monkeypatch.undo()
    assert _files(directory) == before
    retry = discard_session(directory, include_recording=True)
    assert retry.deleted_recording and retry.directory_removed


@pytest.mark.parametrize("failing", ["annotations.json", "session.json", MARKER_NAME])
def test_failed_generated_deletion_can_be_retried(tmp_path, monkeypatch, failing):
    recording, directory = _make(tmp_path, inside=True)
    prepare_session(directory)
    real = Path.unlink

    def refuse(path, *args, **kwargs):
        if path.name == failing and path.parent == directory:
            raise PermissionError(PLANTED)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "discard_incomplete"
    monkeypatch.undo()
    assert not recording.exists()
    assert (directory / MARKER_NAME).exists()
    retry = discard_session(directory, include_recording=True if failing != MARKER_NAME else False)
    assert retry.directory_removed and not directory.exists()


def test_marker_only_retry_does_not_guess_a_recording(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    for name in ("annotations.json", "session.json"):
        (directory / name).unlink()
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "recording_identity_unavailable"
    assert recording.exists() and (directory / MARKER_NAME).exists()
    result = discard_session(directory)
    assert result.deleted_generated == 1 and recording.exists()


def test_repeated_discard(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    keep = directory / "keep.txt"
    keep.write_text("keep")
    discard_session(directory, include_recording=True)
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "not_a_label_session"
    assert keep.read_text() == "keep"


def test_malformed_metadata_blocks_discard(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    _edit(directory, "session.json", capture_claim="operator_claims_camera")
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "capture_claim_mismatch"
    assert recording.exists() and _managed(directory)


# --------------------------------------------------------------------------
# 5. Prepare reads one snapshot
# --------------------------------------------------------------------------

CHANGED = [_frame(T0, 0.9), _frame(T0 + 10, 0.9), _frame(T0 + 20, 0.9)]


def _replace_file(path):
    fresh = path.with_name("fresh.jsonl")
    _write(fresh, CHANGED)
    os.replace(fresh, path)


def _overwrite_in_place(path):
    data = "".join(encode(frame) + "\n" for frame in CHANGED).encode()
    original = path.read_bytes()
    assert len(data) == len(original)
    with open(path, "r+b") as handle:
        handle.write(data)


MUTATIONS = {"replace": _replace_file, "in_place": _overwrite_in_place}


def _assert_withheld(directory, report):
    assert report.block_reasons and report.examples_emitted == 0 and report.example_windows == ()
    stored = json.loads((directory / "quality-report.json").read_text())
    assert stored["examples_emitted"] == 0 and stored["block_reasons"]
    assert "0.9" not in report.to_json()


@pytest.mark.parametrize("mutate", sorted(MUTATIONS))
def test_change_before_prepare_blocks(tmp_path, mutate):
    recording, directory = _make(tmp_path)
    _confirmed(directory)
    MUTATIONS[mutate](recording)
    report = prepare_session(directory)
    _assert_withheld(directory, report)
    assert report.block_reasons == ("recording_changed",)


@pytest.mark.parametrize("mutate", sorted(MUTATIONS))
def test_change_during_snapshot_creation_never_approves_changed_values(tmp_path, monkeypatch, mutate):
    recording, directory = _make(tmp_path)
    _confirmed(directory)
    real = lab.os.fdopen
    done = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            data = self.stream.read(size)
            if data and not done:
                done.append(True)
                MUTATIONS[mutate](recording)
            return data

    def wrapped(fd, mode="r", *args, **kwargs):
        stream = real(fd, mode, *args, **kwargs)
        try:
            same = os.path.samestat(os.fstat(fd), os.stat(recording))
        except OSError:
            same = False
        return Reader(stream) if mode == "rb" and same and not done else stream

    monkeypatch.setattr(lab.os, "fdopen", wrapped)
    report = prepare_session(directory)
    monkeypatch.undo()
    assert done
    _assert_withheld(directory, report)
    assert "recording_changed_during_prepare" in report.block_reasons


@pytest.mark.parametrize("mutate", sorted(MUTATIONS))
def test_change_immediately_before_ingestion(tmp_path, monkeypatch, mutate):
    recording, directory = _make(tmp_path)
    _confirmed(directory)
    real = lab.ingest_recording
    seen = []

    def swap(path, *args, **kwargs):
        MUTATIONS[mutate](recording)
        info = os.stat(path)
        assert not os.path.samestat(info, os.stat(recording))
        assert info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o400
        assert REPO not in Path(path).resolve().parents and directory not in Path(path).resolve().parents
        result = real(path, *args, **kwargs)
        seen.extend(item.example.features[JAW] for item in result.examples)
        return result

    monkeypatch.setattr(lab, "ingest_recording", swap)
    report = prepare_session(directory)
    # Ingestion read the snapshot, which holds the fingerprinted values.
    assert seen == pytest.approx([0.2, 0.8, 0.4])
    _assert_withheld(directory, report)
    assert report.block_reasons == ("recording_changed_during_prepare",)


@pytest.mark.parametrize("target", ["live", "snapshot"])
def test_change_after_validation_before_the_report(tmp_path, monkeypatch, target):
    recording, directory = _make(tmp_path)
    _confirmed(directory)
    real = lab._snapshot_unchanged

    def tamper(snap):
        if target == "live":
            _overwrite_in_place(recording)
        else:
            os.chmod(snap.path, 0o600)
            _overwrite_in_place(snap.path)
        return real(snap)

    monkeypatch.setattr(lab, "_snapshot_unchanged", tamper)
    report = prepare_session(directory)
    _assert_withheld(directory, report)
    expected = "recording_changed_during_prepare" if target == "live" else "snapshot_changed"
    assert expected in report.block_reasons


def test_snapshot_is_removed_on_success_and_failure(tmp_path, monkeypatch, _private_tmp):
    recording, directory = _make(tmp_path)
    _confirmed(directory)
    prepare_session(directory)
    assert list(_private_tmp.iterdir()) == []

    def boom(*_args, **_kwargs):
        raise RuntimeError("programming failure")

    monkeypatch.setattr(lab, "ingest_recording", boom)
    with pytest.raises(RuntimeError):
        prepare_session(directory)
    assert list(_private_tmp.iterdir()) == []


def test_unchanged_recording_still_prepares(tmp_path):
    recording, directory = _make(tmp_path)
    _confirmed(directory)
    report = prepare_session(directory)
    assert report.examples_emitted == 3 and not report.block_reasons


# --------------------------------------------------------------------------
# 6. Collection-wide source reuse
# --------------------------------------------------------------------------


def _held(tmp_path, recording, folder, session_id, recording_id, split):
    destination = tmp_path / folder
    create_session(
        recording,
        destination,
        **{
            **OPTIONS,
            "participant_id": "p02",
            "session_id": session_id,
            "recording_id": recording_id,
            "split": split,
            "participant_role": "held_out",
        },
    )
    return destination


def _collection_codes(directories):
    with pytest.raises(SplitPlanError) as exc:
        check_collection(directories)
    return set(exc.value.codes)


def test_same_path_in_calibration_and_eval_is_refused(tmp_path):
    recording = tmp_path / "features.jsonl"
    _write(recording, FRAMES)
    cal = _held(tmp_path, recording, "cal", "sC", "rC", "calibration")
    ev = _held(tmp_path, recording, "eval", "sE", "rE", "eval")
    assert _collection_codes([cal, ev]) == {
        "recording_source_reused",
        "recording_source_reused_across_splits",
        "calibration_eval_source_leakage",
    }


def test_hard_linked_recording_in_calibration_and_eval_is_refused(tmp_path):
    recording = tmp_path / "features.jsonl"
    _write(recording, FRAMES)
    linked = tmp_path / "linked.jsonl"
    os.link(recording, linked)
    cal = _held(tmp_path, recording, "cal", "sC", "rC", "calibration")
    ev = _held(tmp_path, linked, "eval", "sE", "rE", "eval")
    assert {"recording_source_reused", "calibration_eval_source_leakage"} <= _collection_codes([cal, ev])


def test_copied_recording_in_calibration_and_eval_is_refused(tmp_path):
    recording = tmp_path / "features.jsonl"
    _write(recording, FRAMES)
    copy = tmp_path / "copy.jsonl"
    shutil.copyfile(recording, copy)
    cal = _held(tmp_path, recording, "cal", "sC", "rC", "calibration")
    ev = _held(tmp_path, copy, "eval", "sE", "rE", "eval")
    assert _collection_codes([cal, ev]) == {
        "recording_content_duplicated",
        "recording_source_reused_across_splits",
        "calibration_eval_source_leakage",
    }
    # Removing the copy does not hide it: the stored fingerprints still match.
    copy.unlink()
    assert "recording_content_duplicated" in _collection_codes([cal, ev])


def test_reuse_within_one_split_is_also_refused(tmp_path):
    recording = tmp_path / "features.jsonl"
    _write(recording, FRAMES)
    first = _make(tmp_path / "a", session_id="sA", recording_id="rA")[1]
    second = tmp_path / "b" / "session"
    create_session(tmp_path / "a" / "features.jsonl", second, **{**OPTIONS, "session_id": "sB", "recording_id": "rB"})
    assert _collection_codes([first, second]) == {"recording_source_reused"}


def test_independent_sessions_still_pass(tmp_path):
    train = _make(tmp_path / "t", participant_id="p01")[1]
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    _write(a, [_frame(T0 + 1, 0.1)])
    _write(b, [_frame(T0 + 2, 0.1)])
    cal = _held(tmp_path, a, "cal", "sC", "rC", "calibration")
    ev = _held(tmp_path, b, "eval", "sE", "rE", "eval")
    plan = check_collection([train, cal, ev])
    assert plan.report.held_out_participants == ("p02",)


def test_cli_check_reports_reuse(tmp_path):
    recording = tmp_path / "features.jsonl"
    _write(recording, FRAMES)
    cal = _held(tmp_path, recording, "cal", "sC", "rC", "calibration")
    ev = _held(tmp_path, recording, "eval", "sE", "rE", "eval")
    result = subprocess.run(
        [sys.executable, "-m", "ml.label", "check", str(cal), str(ev)], capture_output=True, text=True, cwd=REPO
    )
    assert result.returncode == 2 and "calibration_eval_source_leakage" in result.stderr
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------
# 7. Metadata validation on every load
# --------------------------------------------------------------------------

OPERATIONS = {
    "prepare": prepare_session,
    "overview": session_text,
    "mark": lambda d: mark_interval(d, T0, T0 + 5, "select"),
    "confirm": lambda d: confirm_interval(d, 0),
    "reclassify": lambda d: reclassify_split(d, "calibration", reviewed=True),
    "discard": lambda d: discard_session(d, include_recording=True),
    "check": lambda d: check_collection([d]),
}


def _assert_refused_everywhere(directory, code=None):
    before = _files(directory)
    for name, operation in OPERATIONS.items():
        with pytest.raises(LabelingError) as exc:
            operation(directory)
        if code is not None:
            assert exc.value.code == code, name
        assert PLANTED not in str(exc.value)
    assert _files(directory) == before
    assert "quality-report.json" not in before


def _fresh_with_candidate(tmp_path):
    recording, directory = _make(tmp_path)
    mark_interval(directory, T0, T0 + 21, "select")
    return recording, directory


@pytest.mark.parametrize(
    "file,old,new",
    [
        ("annotations.json", '"status":"candidate"', '"status":"candidate","status":"confirmed"'),
        ("annotations.json", '"label":"select"', '"label":"select","label":"mouth_open"'),
        ("session.json", '"split":"train"', '"split":"train","split":"eval"'),
        ("session.json", '"capture_claim":"constructed_fixture"', '"capture_claim":"constructed_fixture","capture_claim":"x"'),
    ],
)
def test_duplicate_keys_are_refused(tmp_path, file, old, new):
    _recording, directory = _fresh_with_candidate(tmp_path)
    path = directory / file
    raw = path.read_text()
    assert raw.count(old) == 1
    path.write_text(raw.replace(old, new))
    _assert_refused_everywhere(directory, "duplicate_metadata_key")


def test_duplicate_key_inside_split_history_is_refused(tmp_path):
    _recording, directory = _make(tmp_path, participant_role="held_out", split="calibration")
    reclassify_split(directory, "eval", reviewed=True)
    path = directory / "session.json"
    raw = path.read_text()
    path.write_text(raw.replace('"reviewed":true', '"reviewed":true,"reviewed":false'))
    _assert_refused_everywhere(directory, "duplicate_metadata_key")


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"source_category": "consented_local"}, "constructed_data_is_not_a_measurement"),
        ({"authorization_status": "human_authorized"}, "constructed_data_is_not_a_measurement"),
        ({"timestamp_provenance": "measured_capture"}, "constructed_data_is_not_a_measurement"),
        (
            {"source_category": "consented_local", "authorization_status": "human_authorized", "timestamp_provenance": "measured_capture"},
            "constructed_data_is_not_a_measurement",
        ),
        ({"capture_claim": "operator_claims_camera"}, "capture_claim_mismatch"),
        ({"capture_claim": "operator_claims_camera", "source_category": "consented_local"}, "capture_claim_mismatch"),
        (
            {"capture_claim": "operator_claims_camera", "source_category": "consented_local", "authorization_status": "human_authorized"},
            "capture_claim_mismatch",
        ),
        (
            {
                "capture_claim": "operator_claims_camera",
                "source_category": "authorized_public",
                "authorization_status": "human_authorized",
                "timestamp_provenance": "measured_capture",
            },
            "capture_claim_mismatch",
        ),
        (
            {
                "capture_claim": "operator_claims_camera",
                "source_category": "consented_local",
                "authorization_status": "not_authorized",
                "timestamp_provenance": "measured_capture",
            },
            "capture_claim_mismatch",
        ),
    ],
)
def test_contradictory_provenance_is_refused(tmp_path, changes, code):
    _recording, directory = _fresh_with_candidate(tmp_path)
    _edit(directory, "session.json", **changes)
    if "timestamp_provenance" in changes:
        _edit(directory, "annotations.json", timestamp_provenance=changes["timestamp_provenance"])
    _assert_refused_everywhere(directory, code)


def test_consistent_operator_claim_still_loads(tmp_path):
    recording, directory = _make(
        tmp_path,
        source_category="consented_local",
        timestamp_provenance="measured_capture",
        capture_claim="operator_claims_camera",
    )
    _confirmed(directory)
    report = prepare_session(directory)
    assert report.examples_emitted == 3 and report.timestamps_are_measured_capture
    assert report.authorization_is_proof is False


@pytest.mark.parametrize("claim", [PLANTED, "", None, [], {}, 1, "Constructed_Fixture"])
def test_invalid_capture_claims_are_refused(tmp_path, claim):
    _recording, directory = _fresh_with_candidate(tmp_path)
    _edit(directory, "session.json", capture_claim=claim)
    _assert_refused_everywhere(directory, "invalid_capture_claim")


WRONG_SESSION = [
    ("participant_role", []), ("participant_role", "guest"), ("split", {}), ("split", "test"),
    ("recording_path", []), ("recording_path", "relative.jsonl"), ("recording_path", ""),
    ("reviewed_labels", [{}]), ("reviewed_labels", []), ("reviewed_labels", "select"),
    ("reviewed_labels", ["select", "select"]), ("reviewed_labels", ["unknown"]),
    ("timestamp_provenance", {}), ("source_category", []), ("authorization_status", None),
    ("authorization_decision_id", PLANTED + "@example.com"), ("dataset_id", 7),
    ("participant_id", None), ("window_length", 0), ("window_length", True), ("window_stride", 1.5),
    ("conditions", None), ("accepted_frames", -1), ("accepted_frames", "3"),
    ("first_timestamp_ms", None), ("last_timestamp_ms", 1.5), ("byte_length", None),
    ("sha256", "ABC"), ("split_history", {}), ("split_history", [{"from_split": "train"}]),
    ("split_history", [{"from_split": "train", "to_split": "train", "reviewed": True}]),
    ("split_history", [{"from_split": "train", "to_split": "eval", "reviewed": "yes"}]),
    ("session_format", "other"), ("source_format", []),
]


@pytest.mark.parametrize("field,value", WRONG_SESSION, ids=[f"{f}-{i}" for i, (f, _v) in enumerate(WRONG_SESSION)])
def test_wrong_session_field_types_and_values_are_refused(tmp_path, field, value):
    _recording, directory = _fresh_with_candidate(tmp_path)
    _edit(directory, "session.json", **{field: value})
    _assert_refused_everywhere(directory)


@pytest.mark.parametrize("file", ["session.json", "annotations.json"])
def test_missing_and_unknown_fields_are_refused(tmp_path, file):
    _recording, directory = _fresh_with_candidate(tmp_path)
    document = json.loads((directory / file).read_text())
    original = dict(document)
    document.pop(sorted(document)[0])
    (directory / file).write_text(json.dumps(document))
    _assert_refused_everywhere(directory, "missing_metadata_field")
    (directory / file).write_text(json.dumps({**original, "email": PLANTED}))
    _assert_refused_everywhere(directory, "unexpected_metadata_field")


WRONG_INTERVAL = [
    ({"status": []}, "invalid_annotation_status"),
    ({"status": "approved"}, "invalid_annotation_status"),
    ({"label": None, "status": "confirmed"}, "label_required"),
    ({"label": "unknown", "status": "confirmed"}, "unknown_is_not_an_annotation"),
    ({"label": {}}, "invalid_annotation_label"),
    ({"prompted": "yes"}, "invalid_annotation"),
    ({"start_ms": T0 + 30, "end_ms": T0 + 30, "status": "confirmed"}, "annotation_interval_is_empty"),
    ({"start_ms": 1.5}, "invalid_annotation_time"),
    ({"extra": PLANTED}, "unexpected_metadata_field"),
]


@pytest.mark.parametrize("changes,code", WRONG_INTERVAL)
def test_hand_edited_interval_rows_are_refused(tmp_path, changes, code):
    _recording, directory = _fresh_with_candidate(tmp_path)
    document = json.loads((directory / "annotations.json").read_text())
    document["intervals"][0].update(changes)
    (directory / "annotations.json").write_text(json.dumps(document))
    _assert_refused_everywhere(directory, code)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"participant_id": "p99"}, "annotation_session_mismatch"),
        ({"session_id": "s99"}, "annotation_session_mismatch"),
        ({"recording_id": []}, "invalid_recording_id"),
        ({"intervals": {}}, "invalid_annotation"),
        ({"clip_label": "unknown"}, "unknown_is_not_an_annotation"),
        ({"timestamp_provenance": []}, "invalid_timestamp_provenance"),
        ({"annotation_format": "v0"}, "metadata_unreadable"),
    ],
)
def test_inconsistent_annotation_documents_are_refused(tmp_path, changes, code):
    _recording, directory = _fresh_with_candidate(tmp_path)
    _edit(directory, "annotations.json", **changes)
    _assert_refused_everywhere(directory, code)


@pytest.mark.parametrize(
    "file,raw,code",
    [
        ("session.json", b"\xff\xfe", "metadata_invalid_utf8"),
        ("annotations.json", b'{"a":' * 10000 + b"0" + b"}" * 10000, "metadata_too_deep"),
        ("session.json", b'{"x":NaN}', "metadata_non_finite_number"),
        ("annotations.json", b" " * (4 * 1024 * 1024 + 1), "metadata_too_large"),
        ("session.json", b"{", "metadata_unreadable"),
        ("session.json", b"[]", "metadata_not_an_object"),
    ],
)
def test_undecodable_metadata_is_refused(tmp_path, file, raw, code):
    _recording, directory = _fresh_with_candidate(tmp_path)
    (directory / file).write_bytes(raw)
    _assert_refused_everywhere(directory, code)


def test_marker_problems_are_refused(tmp_path):
    _recording, directory = _fresh_with_candidate(tmp_path)
    marker = directory / MARKER_NAME
    for raw in (b"\xff", b"", (lab.SESSION_FORMAT + "\n\n").encode()):
        marker.write_bytes(raw)
        _assert_refused_everywhere(directory, "not_a_label_session")


# --------------------------------------------------------------------------
# 8. Real CLI error handling
# --------------------------------------------------------------------------

CLI_CASES = [
    ("annotations.json", "status", []),
    ("session.json", "participant_role", []),
    ("session.json", "recording_path", []),
    ("session.json", "reviewed_labels", [{}]),
    ("session.json", "timestamp_provenance", {}),
    ("session.json", "capture_claim", PLANTED),
]


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "ml.label", *map(str, args)], capture_output=True, text=True, cwd=REPO
    )


def _assert_clean_failure(result):
    assert result.returncode == 2
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1
    for leak in ("Traceback", "Error:", PLANTED, "jawOpen", "0.2", "features.jsonl"):
        assert leak not in result.stderr


@pytest.mark.parametrize("file,field,value", CLI_CASES)
@pytest.mark.parametrize("command", ["prepare", "overview", "confirm", "discard", "check"])
def test_real_cli_returns_a_code_for_malformed_metadata(tmp_path, file, field, value, command):
    _recording, directory = _fresh_with_candidate(tmp_path)
    if file == "annotations.json":
        document = json.loads((directory / file).read_text())
        document["intervals"][0][field] = value
        (directory / file).write_text(json.dumps(document))
    else:
        _edit(directory, file, **{field: value})
    extra = ["--index", "0"] if command == "confirm" else []
    _assert_clean_failure(_cli(command, directory, *extra))


@pytest.mark.parametrize(
    "file,raw",
    [
        (MARKER_NAME, b"\xff"),
        ("session.json", (b'{"' + PLANTED.encode() + b'":') * 10000 + b"0" + b"}" * 10000),
        ("annotations.json", b"\xc3\x28"),
        ("annotations.json", None),
    ],
    ids=["marker-utf8", "deep", "annotations-utf8", "duplicate"],
)
def test_real_cli_returns_a_code_for_undecodable_files(tmp_path, file, raw):
    _recording, directory = _fresh_with_candidate(tmp_path)
    if raw is None:
        text = (directory / file).read_text()
        raw = text.replace('"status":"candidate"', '"status":"candidate","status":"confirmed"').encode()
    (directory / file).write_bytes(raw)
    for command in ("prepare", "overview", "discard"):
        _assert_clean_failure(_cli(command, directory))


def test_real_cli_operator_input_errors(tmp_path):
    recording, directory = _make(tmp_path)
    _assert_clean_failure(_cli("mark", directory, "--start", "x", "--end", "1", "--label", "select"))
    _assert_clean_failure(_cli("inspect", tmp_path / "missing.jsonl"))
    _assert_clean_failure(_cli("frobnicate"))


def test_programming_failures_are_not_swallowed(tmp_path, monkeypatch):
    _recording, directory = _make(tmp_path)

    def broken(_directory):
        raise RuntimeError("programming failure")

    monkeypatch.setattr("ml.label.prepare_session", broken)
    with pytest.raises(RuntimeError):
        main(["prepare", str(directory)])


# --------------------------------------------------------------------------
# Annotation domain (nonblocking policy)
# --------------------------------------------------------------------------

DOMAIN = [_frame(1000, 0.1), _frame(1010, 0.2), _frame(1020, 0.3)]


@pytest.mark.parametrize(
    "start,end,flagged",
    [
        (1000, 1021, False),
        (1000, 1011, False),
        (1010, 1021, False),
        (999, 1021, True),
        (1000, 1022, True),
        (0, 1011, True),
        (0, 9007199254740991, True),
    ],
)
def test_intervals_past_the_observed_frames_are_flagged_not_extended(tmp_path, start, end, flagged):
    _recording, directory = _make(tmp_path, frames=DOMAIN)
    _confirmed(directory, start, end)
    report = prepare_session(directory)
    assert ("interval_boundary_not_observed" in report.review_reasons) is flagged
    assert not report.block_reasons and report.examples_emitted > 0
    stamps = [window[0] for window in report.example_windows]
    assert stamps == [stamp for stamp in (1000, 1010, 1020) if start <= stamp < end]


def test_last_frame_at_the_safe_integer_ceiling(tmp_path):
    top = 9007199254740991
    _recording, directory = _make(tmp_path, frames=[_frame(top - 1, 0.1)])
    _confirmed(directory, top - 1, top)
    report = prepare_session(directory)
    assert "interval_boundary_not_observed" not in report.review_reasons
    assert report.example_windows == ((top - 1, top - 1, "mouth_open", 1),)
