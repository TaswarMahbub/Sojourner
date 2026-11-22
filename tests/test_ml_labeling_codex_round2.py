"""Regressions for the ML-2B1 round 2 audit of 9910a3b.

A. An incomplete rollback never leaves a session that loads.
B. Discard deletes only the recording bound at init, by name and inode.
C. A snapshot directory that cannot be created is a stable code.
D. A FIFO in place of metadata is refused at once, not waited on.

Every file is built in a disposable temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from tests.test_ml_labeling import _write
from tests.test_ml_labeling_codex_round1 import (
    FRAMES,
    OPTIONS,
    PLANTED,
    REPO,
    _confirmed,
    _edit,
    _entries,
    _fail_os_replace,
    _files,
    _in_backup,
    _in_stage,
    _make,
    _private_tmp,  # noqa: F401  (autouse fixture)
)
from tests.test_ml_pipeline import T0

from ml import labeling as lab
from ml.label import main
from ml.labeling import (
    MARKER_NAME,
    LabelingError,
    check_collection,
    create_session,
    discard_session,
    prepare_session,
    session_text,
)

HELD = dict(OPTIONS, participant_role="held_out", split="calibration")
TIMEOUT = 10


def _cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "ml.label", *map(str, args)],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=TIMEOUT,
        env=env,
    )


def _calibration(tmp_path):
    """A held-out calibration session with three confirmed examples and a report."""
    recording, directory = _make(tmp_path, participant_role="held_out", split="calibration")
    _confirmed(directory)
    assert prepare_session(directory).examples_emitted == 3
    return recording, directory, _files(directory), recording.read_bytes()


def _backup(directory):
    found = [item for item in directory.iterdir() if item.name.startswith(lab._BACKUP_PREFIX)]
    assert len(found) == 1
    return found[0]


def _recover(directory):
    """The manual recovery documented in ml/LABELING.md."""
    backup = _backup(directory)
    for item in backup.iterdir():
        os.replace(item, directory / item.name)
    backup.rmdir()


def _assert_not_authorizing(directory):
    with pytest.raises(LabelingError) as exc:
        prepare_session(directory)
    assert exc.value.code in {"replacement_recovery_pending", "not_a_label_session", "metadata_unreadable"}
    result = _cli("prepare", directory)
    assert result.returncode == 2 and result.stdout == "" and "Traceback" not in result.stderr
    return exc.value.code


def _assert_calibration_restored(directory, before):
    assert _files(directory) == before
    session = json.loads((directory / "session.json").read_text())
    assert session["split"] == "calibration"
    report = prepare_session(directory)
    assert report.split == "calibration" and report.confirmed_intervals == 1
    assert report.examples_emitted == 3 and not report.block_reasons


# --------------------------------------------------------------------------
# A. Rollback
# --------------------------------------------------------------------------


def test_codex_sequence_calibration_to_eval_is_not_authorizing(tmp_path, monkeypatch):
    recording, directory, before, recording_bytes = _calibration(tmp_path)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("KEEP")

    def predicate(src, dst):
        commit = _in_stage(src) and not _in_stage(dst) and dst.name == "annotations.json"
        move_new_out = src == directory / "session.json" and _in_stage(dst)
        restore_old = _in_backup(src) and dst == directory / "session.json"
        return commit or move_new_out or restore_old

    hits = _fail_os_replace(monkeypatch, predicate, error=PermissionError, times=3)
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **dict(HELD, split="eval"))
    monkeypatch.undo()
    assert exc.value.code == "replacement_rollback_incomplete" and len(hits) == 3
    # The new eval metadata is stuck in place, so the old marker stays in the backup.
    assert json.loads((directory / "session.json").read_text())["split"] == "eval"
    assert not (directory / MARKER_NAME).exists()
    backup = _backup(directory)
    assert (backup / "session.json").read_bytes() == before["session.json"]
    assert (backup / MARKER_NAME).read_bytes() == before[MARKER_NAME]
    assert _assert_not_authorizing(directory) == "replacement_recovery_pending"
    for operation in (session_text, lambda d: check_collection([d]), lambda d: discard_session(d)):
        with pytest.raises(LabelingError) as refused:
            operation(directory)
        assert refused.value.code == "replacement_recovery_pending"
    assert recording.read_bytes() == recording_bytes and unrelated.read_text() == "KEEP"
    _recover(directory)
    _assert_calibration_restored(directory, before)


COMMITS = ["session.json", "annotations.json", MARKER_NAME]
RESTORES = [MARKER_NAME, "session.json", "annotations.json", "quality-report.json"]


@pytest.mark.parametrize("commit", COMMITS)
@pytest.mark.parametrize("restore", RESTORES)
def test_every_incomplete_rollback_is_not_authorizing_and_recoverable(tmp_path, monkeypatch, commit, restore):
    recording, directory, before, recording_bytes = _calibration(tmp_path)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("KEEP")

    def predicate(src, dst):
        failed_commit = _in_stage(src) and not _in_stage(dst) and dst.name == commit
        failed_restore = _in_backup(src) and dst == directory / restore
        return failed_commit or failed_restore

    hits = _fail_os_replace(monkeypatch, predicate, times=2)
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **dict(HELD, split="eval"))
    monkeypatch.undo()
    assert len(hits) == 2, hits
    assert exc.value.code == "replacement_rollback_incomplete"
    backup = _backup(directory)
    assert (backup / restore).read_bytes() == before[restore]
    _assert_not_authorizing(directory)
    if (directory / MARKER_NAME).exists():
        # A marker may be back only when no new file is left, and then the
        # non-empty backup still blocks every load.
        for name in ("session.json", "annotations.json"):
            if (directory / name).exists():
                assert (directory / name).read_bytes() == before[name]
    assert recording.read_bytes() == recording_bytes and unrelated.read_text() == "KEEP"
    _recover(directory)
    _assert_calibration_restored(directory, before)


def test_marker_is_withheld_while_a_new_file_is_stuck(tmp_path, monkeypatch):
    recording, directory, before, _bytes = _calibration(tmp_path)

    def predicate(src, dst):
        commit = _in_stage(src) and not _in_stage(dst) and dst.name == MARKER_NAME
        move_new_out = src == directory / "annotations.json" and _in_stage(dst)
        restore_old = _in_backup(src) and dst == directory / "annotations.json"
        return commit or move_new_out or restore_old

    _fail_os_replace(monkeypatch, predicate, times=3)
    with pytest.raises(LabelingError):
        create_session(recording, directory, replace=True, **dict(HELD, split="eval"))
    monkeypatch.undo()
    assert not (directory / MARKER_NAME).exists()
    assert json.loads((directory / "annotations.json").read_text())["intervals"] == []
    _recover(directory)
    _assert_calibration_restored(directory, before)


@pytest.mark.parametrize(
    "commit,stuck",
    [
        ("annotations.json", "session.json"),
        (MARKER_NAME, "session.json"),
        (MARKER_NAME, "annotations.json"),
    ],
)
def test_a_stuck_new_file_never_gets_the_old_marker(tmp_path, monkeypatch, commit, stuck):
    """The mixed-generation case: a new file cannot leave and its old copy cannot return."""
    recording, directory, before, recording_bytes = _calibration(tmp_path)

    def predicate(src, dst):
        failed_commit = _in_stage(src) and not _in_stage(dst) and dst.name == commit
        move_new_out = src == directory / stuck and _in_stage(dst)
        restore_old = _in_backup(src) and dst == directory / stuck
        return failed_commit or move_new_out or restore_old

    hits = _fail_os_replace(monkeypatch, predicate, times=3)
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **dict(HELD, split="eval"))
    monkeypatch.undo()
    assert len(hits) == 3 and exc.value.code == "replacement_rollback_incomplete"
    assert (directory / stuck).read_bytes() != before[stuck]
    assert not (directory / MARKER_NAME).exists()
    assert (_backup(directory) / MARKER_NAME).read_bytes() == before[MARKER_NAME]
    assert _assert_not_authorizing(directory) == "replacement_recovery_pending"
    # The backup alone blocks even if someone puts a marker back by hand.
    (directory / MARKER_NAME).write_bytes(before[MARKER_NAME])
    assert _assert_not_authorizing(directory) == "replacement_recovery_pending"
    (directory / MARKER_NAME).unlink()
    assert recording.read_bytes() == recording_bytes
    _recover(directory)
    _assert_calibration_restored(directory, before)


def test_ordinary_rollback_restores_split_and_confirmed_intervals(tmp_path, monkeypatch):
    recording, directory, before, recording_bytes = _calibration(tmp_path)
    for commit in COMMITS:
        _fail_os_replace(monkeypatch, lambda src, dst, c=commit: _in_stage(src) and not _in_stage(dst) and dst.name == c)
        with pytest.raises(LabelingError) as exc:
            create_session(recording, directory, replace=True, **dict(HELD, split="eval"))
        monkeypatch.undo()
        assert exc.value.code == "output_write_failed"
        assert not [name for name in _entries(directory) if name.startswith(".")]
        _assert_calibration_restored(directory, before)
    assert recording.read_bytes() == recording_bytes


def test_a_marker_beside_a_non_empty_backup_is_still_refused(tmp_path):
    recording, directory, before, _bytes = _calibration(tmp_path)
    backup = directory / (lab._BACKUP_PREFIX + "manual")
    backup.mkdir()
    (backup / "quality-report.json").write_bytes(before["quality-report.json"])
    assert _assert_not_authorizing(directory) == "replacement_recovery_pending"
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == "replacement_recovery_pending"
    assert _files(directory) == before
    # A file, not a directory, under the backup prefix is refused too.
    shutil.rmtree(backup)
    (directory / (lab._BACKUP_PREFIX + "file")).write_text("x")
    assert _assert_not_authorizing(directory) == "replacement_recovery_pending"


def test_success_with_a_leftover_backup_waits_for_the_operator(tmp_path, monkeypatch):
    recording, directory, _before, _bytes = _calibration(tmp_path)
    real = lab._remove_owned_dir
    monkeypatch.setattr(
        lab,
        "_remove_owned_dir",
        lambda path: False if path is not None and path.name.startswith(lab._BACKUP_PREFIX) else real(path),
    )
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **dict(HELD, split="eval"))
    monkeypatch.undo()
    assert exc.value.code == "replacement_cleanup_incomplete"
    assert _assert_not_authorizing(directory) == "replacement_recovery_pending"
    shutil.rmtree(_backup(directory))
    report = prepare_session(directory)
    assert report.split == "eval" and report.confirmed_intervals == 0 and report.examples_emitted == 0


def test_an_empty_backup_directory_is_only_a_leftover(tmp_path):
    _recording, directory, _before, _bytes = _calibration(tmp_path)
    (directory / (lab._BACKUP_PREFIX + "empty")).mkdir()
    assert prepare_session(directory).examples_emitted == 3


def test_missing_directory_is_still_not_a_session(tmp_path):
    with pytest.raises(LabelingError) as exc:
        prepare_session(tmp_path / "absent")
    assert exc.value.code == "not_a_label_session"


# --------------------------------------------------------------------------
# B. Deletion identity
# --------------------------------------------------------------------------


def _inside(tmp_path):
    recording, directory = _make(tmp_path, inside=True)
    return recording, directory, _files(directory)


def _refused(directory, code, before):
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert exc.value.code == code
    assert _files(directory) == before


def test_init_binds_name_device_and_inode_from_the_fingerprinted_file(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    bound = json.loads((directory / "session.json").read_text())["recording_identity"]
    info = os.stat(recording)
    assert bound == {"name": recording.name, "device": info.st_dev, "inode": info.st_ino}
    result = discard_session(directory, include_recording=True)
    assert result.deleted_recording and not recording.exists() and not directory.exists()


def test_same_bytes_under_another_name_are_refused(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    copy = directory / "unrelated-preserve.jsonl"
    shutil.copyfile(recording, copy)
    before = _files(directory)
    _edit(directory, "session.json", recording_path=str(copy))
    before["session.json"] = (directory / "session.json").read_bytes()
    _refused(directory, "recording_identity_mismatch", before)
    assert copy.exists() and recording.exists()


def test_same_bytes_with_a_forged_name_are_refused_by_inode(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    copy = directory / "copy.jsonl"
    shutil.copyfile(recording, copy)
    document = json.loads((directory / "session.json").read_text())
    document["recording_path"] = str(copy)
    document["recording_identity"]["name"] = copy.name
    (directory / "session.json").write_text(json.dumps(document))
    _refused(directory, "recording_identity_mismatch", _files(directory))


def test_same_bytes_replacing_the_original_path_are_refused(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    fresh = tmp_path / "fresh.jsonl"
    shutil.copyfile(recording, fresh)
    os.replace(fresh, recording)
    _refused(directory, "recording_identity_mismatch", _files(directory))
    # Prepare is bound to content, so the same bytes still prepare.
    assert not prepare_session(directory).block_reasons


def test_different_content_redirect_is_refused(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    other = directory / "other.jsonl"
    _write(other, [FRAMES[0]])
    _edit(directory, "session.json", recording_path=str(other))
    _refused(directory, "recording_identity_mismatch", _files(directory))


def test_changed_content_at_the_bound_inode_is_refused(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    with open(recording, "r+b") as handle:
        handle.write(b" ")
    _refused(directory, "recording_identity_mismatch", _files(directory))


def test_hard_linked_and_symlinked_targets_are_refused(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    os.link(recording, tmp_path / "second.jsonl")
    _refused(directory, "recording_has_other_links", _files(directory))
    (tmp_path / "second.jsonl").unlink()
    real = tmp_path / "real.jsonl"
    shutil.move(recording, real)
    recording.symlink_to(real)
    _refused(directory, "symlink_not_allowed", _files(directory))
    assert real.exists()


def test_swap_between_lstat_and_open_is_refused(tmp_path, monkeypatch):
    recording, directory, _before = _inside(tmp_path)
    real = lab._hash_file

    def swap(path, expect=None):
        fresh = tmp_path / "fresh.jsonl"
        shutil.copyfile(recording, fresh)
        os.replace(fresh, recording)
        return real(path, expect)

    monkeypatch.setattr(lab, "_hash_file", swap)
    _refused(directory, "recording_identity_mismatch", _files(directory))


def test_recording_outside_the_session_is_left_in_place(tmp_path):
    recording, directory = _make(tmp_path)
    result = discard_session(directory, include_recording=True)
    assert not result.deleted_recording and result.recording_outside_session and recording.exists()


def test_legacy_session_without_identity_keeps_its_recording(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    document = json.loads((directory / "session.json").read_text())
    document.pop("recording_identity")
    (directory / "session.json").write_text(json.dumps(document))
    assert prepare_session(directory).accepted_frames == 3
    _refused(directory, "recording_identity_unavailable", _files(directory))
    result = discard_session(directory)
    assert not result.deleted_recording and recording.exists()


@pytest.mark.parametrize(
    "identity",
    [
        [],
        {"name": "x", "device": 1},
        {"name": "x", "device": 1, "inode": 2, "extra": 3},
        {"name": "", "device": 1, "inode": 2},
        {"name": "a/b", "device": 1, "inode": 2},
        {"name": "..", "device": 1, "inode": 2},
        {"name": 5, "device": 1, "inode": 2},
        {"name": "x", "device": -1, "inode": 2},
        {"name": "x", "device": 1, "inode": "2"},
        {"name": "x", "device": True, "inode": 2},
    ],
)
def test_malformed_identity_is_refused_on_load(tmp_path, identity):
    _recording, directory, _before = _inside(tmp_path)
    _edit(directory, "session.json", recording_identity=identity)
    with pytest.raises(LabelingError) as exc:
        prepare_session(directory)
    assert exc.value.code in {"invalid_recording_identity", "metadata_not_an_object", "missing_metadata_field", "unexpected_metadata_field"}


def test_failed_identity_check_keeps_metadata_and_retry_works(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    saved = recording.read_bytes()
    with open(recording, "r+b") as handle:
        handle.write(b" ")
    _refused(directory, "recording_identity_mismatch", _files(directory))
    with open(recording, "r+b") as handle:
        handle.write(saved[:1])
    result = discard_session(directory, include_recording=True)
    assert result.deleted_recording and not directory.exists()


def test_replace_rebinds_to_the_current_file(tmp_path):
    recording, directory, _before = _inside(tmp_path)
    fresh = tmp_path / "fresh.jsonl"
    shutil.copyfile(recording, fresh)
    os.replace(fresh, recording)
    create_session(recording, directory, replace=True, **OPTIONS)
    assert json.loads((directory / "session.json").read_text())["recording_identity"]["inode"] == os.stat(recording).st_ino
    assert discard_session(directory, include_recording=True).deleted_recording


# --------------------------------------------------------------------------
# C. Snapshot directory failures
# --------------------------------------------------------------------------

DENY_SNAPSHOT = """
import sys, tempfile
real = tempfile.mkdtemp
def deny(*a, **k):
    if k.get("prefix") == "sojourner-label-snapshot-":
        raise {error}("{marker} /private/secret/path")
    return real(*a, **k)
tempfile.mkdtemp = deny
from ml.label import main
raise SystemExit(main(sys.argv[1:]))
"""


def _deny_snapshot(monkeypatch, error=PermissionError):
    real = tempfile.mkdtemp

    def deny(*args, **kwargs):
        if kwargs.get("prefix") == "sojourner-label-snapshot-":
            raise error(PLANTED)
        return real(*args, **kwargs)

    monkeypatch.setattr(lab.tempfile, "mkdtemp", deny)


@pytest.mark.parametrize("error", [PermissionError, OSError, FileNotFoundError])
def test_snapshot_directory_failure_is_a_code(tmp_path, monkeypatch, capsys, error):
    recording, directory = _make(tmp_path)
    _confirmed(directory)
    before = _files(directory)
    recording_bytes = recording.read_bytes()
    _deny_snapshot(monkeypatch, error)
    for operation in (prepare_session, session_text, lambda d: lab.inspect_recording(recording)):
        with pytest.raises(LabelingError) as exc:
            operation(directory)
        assert exc.value.code == "snapshot_unavailable" and PLANTED not in str(exc.value)
    with pytest.raises(LabelingError):
        create_session(recording, tmp_path / "other", **OPTIONS)
    assert not (tmp_path / "other").exists()
    assert main(["prepare", str(directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip() == "snapshot_unavailable"
    assert _files(directory) == before and recording.read_bytes() == recording_bytes


def test_snapshot_directory_failure_in_a_real_subprocess(tmp_path):
    recording, directory = _make(tmp_path)
    _confirmed(directory)
    before = _files(directory)
    for command in (["prepare", directory], ["overview", directory], ["inspect", recording]):
        script = DENY_SNAPSHOT.format(error="PermissionError", marker=PLANTED)
        result = subprocess.run(
            [sys.executable, "-c", script, *map(str, command)],
            capture_output=True, text=True, cwd=REPO, timeout=TIMEOUT,
        )
        assert result.returncode == 2 and result.stdout == ""
        assert result.stderr.strip() == "snapshot_unavailable"
        assert PLANTED not in result.stderr and "/private/secret" not in result.stderr
        assert "Traceback" not in result.stderr
    assert _files(directory) == before


def test_programming_error_from_mkdtemp_is_not_swallowed(tmp_path, monkeypatch):
    _recording, directory = _make(tmp_path)
    _deny_snapshot(monkeypatch, RuntimeError)
    with pytest.raises(RuntimeError):
        prepare_session(directory)


def test_failure_after_the_directory_exists_removes_it(tmp_path, monkeypatch, _private_tmp):
    recording, directory = _make(tmp_path)
    real = os.open

    def refuse(path, flags, *args):
        if str(path).endswith("recording.jsonl") and flags & os.O_CREAT:
            raise PermissionError(PLANTED)
        return real(path, flags, *args)

    monkeypatch.setattr(lab.os, "open", refuse)
    with pytest.raises(LabelingError) as exc:
        prepare_session(directory)
    assert exc.value.code == "recording_unreadable"
    assert list(_private_tmp.iterdir()) == []


# --------------------------------------------------------------------------
# D. Non-regular metadata
# --------------------------------------------------------------------------

FIFO_CODES = {
    MARKER_NAME: "not_a_label_session",
    "session.json": "metadata_unreadable",
    "annotations.json": "metadata_unreadable",
}


@pytest.mark.parametrize("command", ["prepare", "overview", "confirm", "discard", "check"])
@pytest.mark.parametrize("name", sorted(FIFO_CODES))
def test_fifo_metadata_fails_promptly_in_a_real_subprocess(tmp_path, name, command):
    recording, directory = _make(tmp_path)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("KEEP")
    (directory / name).unlink()
    os.mkfifo(directory / name)
    before = _entries(directory)
    extra = ["--index", "0"] if command == "confirm" else []
    try:
        result = _cli(command, directory, *extra)
    except subprocess.TimeoutExpired:
        pytest.fail("the CLI waited on a FIFO")
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr.strip() == FIFO_CODES[name]
    assert "Traceback" not in result.stderr
    assert _entries(directory) == before and unrelated.read_text() == "KEEP"
    assert recording.exists()


def test_descriptor_check_catches_a_fifo_swapped_in_after_lstat(tmp_path):
    _recording, directory = _make(tmp_path)
    (directory / "session.json").unlink()
    os.mkfifo(directory / "session.json")
    # Pretend lstat saw a regular file, so only the non-blocking open and fstat protect.
    script = (
        "import sys, ml.labeling as l\n"
        "real = l._existing_kind\n"
        "l._existing_kind = lambda p: 'file' if p.name == 'session.json' else real(p)\n"
        "from ml.label import main\n"
        "raise SystemExit(main(sys.argv[1:]))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, "prepare", str(directory)],
            capture_output=True, text=True, cwd=REPO, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("open blocked on a FIFO")
    assert result.returncode == 2 and result.stderr.strip() == "metadata_unreadable"


def test_fifo_recording_fails_promptly(tmp_path):
    fifo = tmp_path / "features.jsonl"
    os.mkfifo(fifo)
    result = _cli("inspect", fifo)
    assert result.returncode == 2 and result.stderr.strip() == "path_not_a_file"


def test_regular_files_and_symlinks_keep_their_behaviour(tmp_path):
    _recording, directory = _make(tmp_path)
    assert _cli("overview", directory).returncode == 0
    target = tmp_path / "session-copy.json"
    shutil.copyfile(directory / "session.json", target)
    (directory / "session.json").unlink()
    (directory / "session.json").symlink_to(target)
    result = _cli("prepare", directory)
    assert result.returncode == 2 and result.stderr.strip() == "metadata_unreadable"
    marker_target = tmp_path / "marker"
    shutil.copyfile(directory / MARKER_NAME, marker_target)
    (directory / MARKER_NAME).unlink()
    (directory / MARKER_NAME).symlink_to(marker_target)
    assert _cli("prepare", directory).stderr.strip() == "not_a_label_session"
