"""Regressions for the ML-2B1 round 3 audit of c653138.

``discard --include-recording`` must delete exactly the file it verified.
The recording is renamed into a private ``.label-claim-*`` directory, the
claimed file is checked, and only the claimed name is unlinked. A file that
appears at the public name at any point is never deleted.

Each test drives one interleaving deterministically by wrapping a helper at
the boundary in question. Every file is built in a disposable temporary
directory.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from tests.test_ml_labeling import _frame, _write
from tests.test_ml_labeling_codex_round1 import (
    OPTIONS,
    PLANTED,
    REPO,
    _files,
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
    confirm_interval,
    create_session,
    discard_session,
    mark_interval,
    prepare_session,
    session_text,
)

TIMEOUT = 10
REAL_RENAME = os.rename
REAL_UNLINK = os.unlink
REAL_LINK = os.link


def _session(tmp_path):
    """A prepared session whose recording lives inside the session directory."""
    recording, directory = _make(tmp_path, inside=True)
    prepare_session(directory)
    original = recording.read_bytes()
    bound = recording.stat()
    return recording, directory, original, bound


def _other(tmp_path, name="incoming.jsonl"):
    """A different recording, created outside the session directory."""
    other = tmp_path / name
    _write(other, [_frame(T0 + 100, 0.91)])
    return other


def _metadata(directory):
    found = {name: data for name, data in _files(directory).items() if name in lab._GENERATED}
    assert set(found) == set(lab._GENERATED)
    return found


def _claims(directory):
    return sorted(item for item in directory.iterdir() if item.name.startswith(lab._CLAIM_PREFIX))


def _is_claim(path):
    return Path(path).parent.name.startswith(lab._CLAIM_PREFIX)


def _rotate(recording, archive, replacement):
    """Another process archives the original and installs a new recording at its name."""
    REAL_RENAME(recording, archive)
    os.replace(replacement, recording)


def _discard_code(directory):
    with pytest.raises(LabelingError) as exc:
        discard_session(directory, include_recording=True)
    assert PLANTED not in str(exc.value)
    return exc.value.code


def _assert_blocked(directory, code):
    operations = [
        lambda: session_text(directory),
        lambda: mark_interval(directory, T0, T0 + 11, "mouth_open"),
        lambda: confirm_interval(directory, 0),
        lambda: prepare_session(directory),
        lambda: check_collection([directory]),
        lambda: discard_session(directory),
        lambda: discard_session(directory, include_recording=True),
    ]
    for operation in operations:
        with pytest.raises(LabelingError) as exc:
            operation()
        assert exc.value.code == code


def _patch_rename(monkeypatch, before=None, after=None):
    """Run ``before``/``after`` around the claim rename only."""
    seen = []

    def rename(src, dst, *args, **kwargs):
        if not _is_claim(dst) or seen:
            return REAL_RENAME(src, dst, *args, **kwargs)
        seen.append(Path(dst))
        if before:
            before()
        result = REAL_RENAME(src, dst, *args, **kwargs)
        if after:
            after(Path(dst))
        return result

    monkeypatch.setattr(lab.os, "rename", rename)
    return seen


# --------------------------------------------------------------------------
# Race 1 and 2: Codex's two interleavings
# --------------------------------------------------------------------------


def test_race1_replacement_after_verification_keeps_the_new_recording(tmp_path, monkeypatch):
    recording, directory, original, bound = _session(tmp_path)
    replacement = _other(tmp_path)
    new_bytes, new_inode = replacement.read_bytes(), replacement.stat().st_ino
    archive = tmp_path / "archived-original.jsonl"
    metadata = _metadata(directory)
    real = lab._verified_recording
    events = []

    def verified(*args, **kwargs):
        result = real(*args, **kwargs)
        assert result == recording
        _rotate(recording, archive, replacement)
        events.append("rotated")
        return result

    monkeypatch.setattr(lab, "_verified_recording", verified)
    assert _discard_code(directory) == "recording_identity_mismatch"
    assert events == ["rotated"]
    assert recording.read_bytes() == new_bytes and recording.stat().st_ino == new_inode
    assert recording.stat().st_nlink == 1
    assert archive.read_bytes() == original and archive.stat().st_ino == bound.st_ino
    assert _metadata(directory) == metadata and _claims(directory) == []


def test_race2_replacement_after_open_before_fstat_keeps_the_new_recording(tmp_path, monkeypatch):
    recording, directory, original, bound = _session(tmp_path)
    replacement = _other(tmp_path)
    new_bytes = replacement.read_bytes()
    archive = tmp_path / "archived-original.jsonl"
    metadata = _metadata(directory)
    real = os.fdopen
    events = []

    def fdopen(fd, mode="r", *args, **kwargs):
        stream = real(fd, mode, *args, **kwargs)
        info = os.fstat(fd)
        if mode == "rb" and (info.st_dev, info.st_ino) == (bound.st_dev, bound.st_ino) and not events:
            _rotate(recording, archive, replacement)
            events.append("rotated")
        return stream

    monkeypatch.setattr(lab.os, "fdopen", fdopen)
    assert _discard_code(directory) == "recording_identity_mismatch"
    assert events == ["rotated"]
    assert recording.read_bytes() == new_bytes and archive.read_bytes() == original
    assert _metadata(directory) == metadata and _claims(directory) == []


def test_race1_through_a_real_cli_subprocess(tmp_path):
    recording, directory, original, _bound = _session(tmp_path)
    replacement = _other(tmp_path)
    new_bytes = replacement.read_bytes()
    archive = tmp_path / "archived-original.jsonl"
    metadata = _metadata(directory)
    script = (
        "import os,sys\n"
        "from pathlib import Path\n"
        "import ml.labeling as lab\n"
        "real=lab._verified_recording\n"
        "def wrapped(*a,**k):\n"
        " r=real(*a,**k)\n"
        " os.rename(sys.argv[1],sys.argv[2]); os.replace(sys.argv[3],sys.argv[1])\n"
        " return r\n"
        "lab._verified_recording=wrapped\n"
        "from ml.label import main\n"
        "raise SystemExit(main(['discard',sys.argv[4],'--include-recording']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(recording), str(archive), str(replacement), str(directory)],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=TIMEOUT,
    )
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr.strip() == "recording_identity_mismatch"
    assert recording.read_bytes() == new_bytes and archive.read_bytes() == original
    assert _metadata(directory) == metadata and _claims(directory) == []


def test_new_recording_after_identity_hash_is_never_deleted(tmp_path, monkeypatch):
    """Codex's invariant: whatever the implementation, a file installed after the hash survives."""
    recording, directory, original, bound = _session(tmp_path)
    replacement = _other(tmp_path)
    new_bytes = replacement.read_bytes()
    archive = tmp_path / "archive.jsonl"
    real = lab._hash_file
    events = []

    def hashed(path, expect=None):
        result = real(path, expect)
        if expect == (bound.st_dev, bound.st_ino) and not events:
            if recording.exists():
                REAL_RENAME(recording, archive)
            os.replace(replacement, recording)
            events.append("installed")
        return result

    monkeypatch.setattr(lab, "_hash_file", hashed)
    try:
        discard_session(directory, include_recording=True)
    except LabelingError:
        pass
    assert events == ["installed"]
    assert recording.read_bytes() == new_bytes
    if archive.exists():
        assert archive.read_bytes() == original


# --------------------------------------------------------------------------
# Race 3 and 4: replacement immediately before and after the claim
# --------------------------------------------------------------------------


def test_race3_replacement_before_claim_is_detected_and_put_back(tmp_path, monkeypatch):
    recording, directory, original, _bound = _session(tmp_path)
    replacement = _other(tmp_path)
    new_bytes, new_inode = replacement.read_bytes(), replacement.stat().st_ino
    archive = tmp_path / "archived-original.jsonl"
    metadata = _metadata(directory)
    seen = _patch_rename(monkeypatch, before=lambda: _rotate(recording, archive, replacement))
    assert _discard_code(directory) == "recording_identity_mismatch"
    assert seen, "the claim rename ran"
    assert recording.read_bytes() == new_bytes and recording.stat().st_ino == new_inode
    assert recording.stat().st_nlink == 1
    assert archive.read_bytes() == original
    assert _metadata(directory) == metadata and _claims(directory) == []
    monkeypatch.undo()
    # The session is still usable; the restored file is simply not the bound one.
    assert session_text(directory)
    assert _discard_code(directory) == "recording_identity_mismatch"


def test_race4_new_file_after_claim_survives_and_only_the_original_is_deleted(tmp_path, monkeypatch):
    recording, directory, _original, _bound = _session(tmp_path)
    replacement = _other(tmp_path)
    new_bytes = replacement.read_bytes()
    claimed = []

    def after(path):
        claimed.append(path.stat().st_ino)
        os.replace(replacement, recording)

    _patch_rename(monkeypatch, after=after)
    result = discard_session(directory, include_recording=True)
    assert result.deleted_recording and result.deleted_generated == 4
    assert recording.read_bytes() == new_bytes
    assert result.unexpected_files_left == 1 and not result.directory_removed
    assert sorted(item.name for item in directory.iterdir()) == [recording.name]
    assert claimed and recording.stat().st_ino != claimed[0]


# --------------------------------------------------------------------------
# Race 5: the verified claimed file cannot be unlinked
# --------------------------------------------------------------------------


def test_race5_unlink_failure_once_puts_the_original_back_for_a_retry(tmp_path, monkeypatch):
    recording, directory, original, bound = _session(tmp_path)
    before = _files(directory)
    refused = []

    def unlink(path, *args, **kwargs):
        if _is_claim(path) and not refused:
            refused.append(path)
            raise PermissionError(PLANTED)
        return REAL_UNLINK(path, *args, **kwargs)

    monkeypatch.setattr(lab.os, "unlink", unlink)
    assert _discard_code(directory) == "recording_delete_failed"
    assert refused
    monkeypatch.undo()
    assert _files(directory) == before and _claims(directory) == []
    assert recording.stat().st_ino == bound.st_ino and recording.stat().st_nlink == 1
    result = discard_session(directory, include_recording=True)
    assert result.deleted_recording and result.directory_removed


def test_race5_unlink_failure_always_keeps_the_original_recoverable(tmp_path, monkeypatch):
    recording, directory, original, bound = _session(tmp_path)
    metadata = _metadata(directory)

    def unlink(path, *args, **kwargs):
        if _is_claim(path):
            raise PermissionError(PLANTED)
        return REAL_UNLINK(path, *args, **kwargs)

    monkeypatch.setattr(lab.os, "unlink", unlink)
    assert _discard_code(directory) == "discard_recovery_pending"
    monkeypatch.undo()
    assert _metadata(directory) == metadata
    [claim] = _claims(directory)
    assert stat.S_IMODE(claim.stat().st_mode) == 0o700
    claimed = claim / lab._CLAIMED_NAME
    assert claimed.read_bytes() == original and claimed.stat().st_ino == bound.st_ino
    _assert_blocked(directory, "discard_recovery_pending")
    # Documented recovery: the original name already holds the same file
    # (same inode), so the claimed name is the extra link and is removed.
    assert recording.stat().st_ino == claimed.stat().st_ino
    claimed.unlink()
    claim.rmdir()
    assert prepare_session(directory).accepted_frames == 3
    assert discard_session(directory, include_recording=True).deleted_recording


# --------------------------------------------------------------------------
# Race 6 and 7: the claimed file is not the bound one
# --------------------------------------------------------------------------


def test_race6_content_changed_in_place_after_claim_is_preserved(tmp_path, monkeypatch):
    recording, directory, original, bound = _session(tmp_path)
    metadata = _metadata(directory)

    def after(path):
        with open(path, "ab") as stream:
            stream.write(b"\n")

    _patch_rename(monkeypatch, after=after)
    assert _discard_code(directory) == "recording_identity_mismatch"
    assert recording.read_bytes() == original + b"\n" and recording.stat().st_ino == bound.st_ino
    assert _metadata(directory) == metadata and _claims(directory) == []


def test_race6_different_content_claimed_is_preserved(tmp_path, monkeypatch):
    recording, directory, original, _bound = _session(tmp_path)
    replacement = _other(tmp_path)
    new_bytes = replacement.read_bytes()
    archive = tmp_path / "archive.jsonl"
    metadata = _metadata(directory)
    _patch_rename(monkeypatch, before=lambda: _rotate(recording, archive, replacement))
    assert _discard_code(directory) == "recording_identity_mismatch"
    assert recording.read_bytes() == new_bytes and archive.read_bytes() == original
    assert _metadata(directory) == metadata


def test_race7_same_content_different_inode_is_refused(tmp_path, monkeypatch):
    recording, directory, original, bound = _session(tmp_path)
    copy = tmp_path / "copy.jsonl"
    shutil.copyfile(recording, copy)
    copy_inode = copy.stat().st_ino
    archive = tmp_path / "archive.jsonl"
    metadata = _metadata(directory)
    hashed = []
    real = lab._hash_file

    def tracking(path, expect=None):
        hashed.append(Path(path))
        return real(path, expect)

    monkeypatch.setattr(lab, "_hash_file", tracking)
    _patch_rename(monkeypatch, before=lambda: _rotate(recording, archive, copy))
    assert _discard_code(directory) == "recording_identity_mismatch"
    assert recording.read_bytes() == original and recording.stat().st_ino == copy_inode
    assert archive.stat().st_ino == bound.st_ino
    assert _metadata(directory) == metadata
    # Refused on the inode, before the claimed copy's bytes were even hashed.
    assert not any(_is_claim(path) for path in hashed)


def test_claimed_symlink_is_kept_aside_and_its_target_untouched(tmp_path, monkeypatch):
    recording, directory, original, _bound = _session(tmp_path)
    target = tmp_path / "target.jsonl"
    target.write_bytes(b"KEEP-" + PLANTED.encode())
    archive = tmp_path / "archive.jsonl"
    metadata = _metadata(directory)

    def swap():
        REAL_RENAME(recording, archive)
        recording.symlink_to(target)

    _patch_rename(monkeypatch, before=swap)
    assert _discard_code(directory) == "discard_recovery_pending"
    assert target.read_bytes() == b"KEEP-" + PLANTED.encode() and archive.read_bytes() == original
    [claim] = _claims(directory)
    assert (claim / lab._CLAIMED_NAME).is_symlink()
    assert _metadata(directory) == metadata


# --------------------------------------------------------------------------
# Race 8: an existing claim directory
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["file_inside", "not_a_directory", "subdirectory_inside"])
def test_race8_pending_claim_blocks_every_operation_and_is_untouched(tmp_path, kind):
    recording, directory, original, _bound = _session(tmp_path)
    pending = directory / (lab._CLAIM_PREFIX + "earlier")
    if kind == "not_a_directory":
        pending.write_bytes(b"KEEP")
    else:
        pending.mkdir(mode=0o700)
        if kind == "file_inside":
            (pending / lab._CLAIMED_NAME).write_bytes(b"KEEP")
        else:
            (pending / "inner").mkdir()
    snapshot = sorted(str(item.relative_to(directory)) for item in directory.rglob("*"))
    metadata = _metadata(directory)
    _assert_blocked(directory, "discard_recovery_pending")
    assert sorted(str(item.relative_to(directory)) for item in directory.rglob("*")) == snapshot
    assert recording.read_bytes() == original and _metadata(directory) == metadata
    assert main(["discard", str(directory), "--include-recording"]) == 2


def test_race8_backup_pending_takes_precedence_over_claim_pending(tmp_path):
    _recording, directory, _original, _bound = _session(tmp_path)
    (directory / (lab._CLAIM_PREFIX + "a")).write_bytes(b"x")
    (directory / (lab._BACKUP_PREFIX + "b")).write_bytes(b"x")
    _assert_blocked(directory, "replacement_recovery_pending")


def test_race8_empty_claim_directory_is_only_a_leftover(tmp_path):
    recording, directory, _original, _bound = _session(tmp_path)
    (directory / (lab._CLAIM_PREFIX + "empty")).mkdir()
    assert prepare_session(directory).accepted_frames == 3
    result = discard_session(directory, include_recording=True)
    assert result.deleted_recording and not recording.exists()
    assert result.unexpected_files_left == 1 and not result.directory_removed


def test_race8_init_refuses_a_directory_holding_a_claim(tmp_path):
    recording, directory, _original, _bound = _session(tmp_path)
    (directory / (lab._CLAIM_PREFIX + "earlier")).mkdir()
    with pytest.raises(LabelingError) as exc:
        create_session(recording, directory, replace=True, **OPTIONS)
    assert exc.value.code == "directory_not_empty"


def test_race8_recording_name_with_claim_prefix_is_reserved():
    assert lab._reserved_name(lab._CLAIM_PREFIX + "x.jsonl")


def test_each_discard_creates_a_new_claim_directory(tmp_path, monkeypatch):
    recording, directory, _original, _bound = _session(tmp_path)
    (directory / (lab._CLAIM_PREFIX + "empty")).mkdir()
    seen = _patch_rename(monkeypatch)
    discard_session(directory, include_recording=True)
    assert seen and seen[0].parent.name != lab._CLAIM_PREFIX + "empty"
    assert (directory / (lab._CLAIM_PREFIX + "empty")).is_dir()


# --------------------------------------------------------------------------
# Race 9: the original name is taken when the claim is put back
# --------------------------------------------------------------------------


def test_race9_occupied_original_name_is_never_overwritten(tmp_path, monkeypatch):
    recording, directory, original, _bound = _session(tmp_path)
    replacement = _other(tmp_path)
    claimed_bytes = replacement.read_bytes()
    archive = tmp_path / "archive.jsonl"
    occupant = b"OCCUPANT-" + PLANTED.encode()
    metadata = _metadata(directory)

    def after(_path):
        recording.write_bytes(occupant)

    _patch_rename(monkeypatch, before=lambda: _rotate(recording, archive, replacement), after=after)
    assert _discard_code(directory) == "discard_recovery_pending"
    monkeypatch.undo()
    assert recording.read_bytes() == occupant
    assert archive.read_bytes() == original
    [claim] = _claims(directory)
    assert (claim / lab._CLAIMED_NAME).read_bytes() == claimed_bytes
    assert _metadata(directory) == metadata
    _assert_blocked(directory, "discard_recovery_pending")
    result = subprocess.run(
        [sys.executable, "-m", "ml.label", "prepare", str(directory)],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=TIMEOUT,
    )
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr.strip() == "discard_recovery_pending"


def test_restore_without_hard_link_support_keeps_the_claim(tmp_path, monkeypatch):
    recording, directory, original, _bound = _session(tmp_path)
    replacement = _other(tmp_path)
    claimed_bytes = replacement.read_bytes()
    archive = tmp_path / "archive.jsonl"
    metadata = _metadata(directory)
    monkeypatch.setattr(lab.os, "link", lambda *_a, **_k: (_ for _ in ()).throw(OSError(PLANTED)))
    _patch_rename(monkeypatch, before=lambda: _rotate(recording, archive, replacement))
    assert _discard_code(directory) == "discard_recovery_pending"
    monkeypatch.undo()
    assert not recording.exists() and archive.read_bytes() == original
    [claim] = _claims(directory)
    assert (claim / lab._CLAIMED_NAME).read_bytes() == claimed_bytes
    assert _metadata(directory) == metadata
    # Documented recovery: the name is free, so the claimed file goes back.
    REAL_RENAME(claim / lab._CLAIMED_NAME, recording)
    claim.rmdir()
    assert recording.read_bytes() == claimed_bytes
    assert session_text(directory)


# --------------------------------------------------------------------------
# Claim and verification failures (cases C and D)
# --------------------------------------------------------------------------


def test_claim_rename_failure_changes_nothing(tmp_path, monkeypatch):
    recording, directory, _original, _bound = _session(tmp_path)
    before = _files(directory)

    def rename(src, dst, *args, **kwargs):
        if _is_claim(dst):
            raise PermissionError(PLANTED)
        return REAL_RENAME(src, dst, *args, **kwargs)

    monkeypatch.setattr(lab.os, "rename", rename)
    assert _discard_code(directory) == "recording_claim_failed"
    assert _files(directory) == before and _claims(directory) == []


def test_claim_directory_creation_failure_changes_nothing(tmp_path, monkeypatch):
    recording, directory, _original, _bound = _session(tmp_path)
    before = _files(directory)
    real = lab.tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        if kwargs.get("prefix") == lab._CLAIM_PREFIX:
            raise PermissionError(PLANTED)
        return real(*args, **kwargs)

    monkeypatch.setattr(lab.tempfile, "mkdtemp", mkdtemp)
    assert _discard_code(directory) == "recording_claim_failed"
    assert _files(directory) == before and _claims(directory) == []
    assert main(["discard", str(directory), "--include-recording"]) == 2


def test_recording_vanishing_before_claim_is_refused_not_reported_deleted(tmp_path, monkeypatch):
    recording, directory, original, _bound = _session(tmp_path)
    archive = tmp_path / "archive.jsonl"
    metadata = _metadata(directory)
    _patch_rename(monkeypatch, before=lambda: REAL_RENAME(recording, archive))
    assert _discard_code(directory) == "recording_claim_failed"
    assert archive.read_bytes() == original
    assert _metadata(directory) == metadata and _claims(directory) == []


def test_hash_failure_on_the_claimed_file_puts_it_back(tmp_path, monkeypatch):
    recording, directory, _original, _bound = _session(tmp_path)
    before = _files(directory)
    real = lab._hash_file

    def hashed(path, expect=None):
        if _is_claim(path):
            raise OSError(PLANTED)
        return real(path, expect)

    monkeypatch.setattr(lab, "_hash_file", hashed)
    assert _discard_code(directory) == "recording_unreadable"
    assert _files(directory) == before and _claims(directory) == []


def test_lstat_failure_on_the_claimed_file_puts_it_back(tmp_path, monkeypatch):
    recording, directory, _original, _bound = _session(tmp_path)
    before = _files(directory)
    real = os.lstat
    failed = []

    def lstat(path, *args, **kwargs):
        if _is_claim(path) and not failed:
            failed.append(path)
            raise PermissionError(PLANTED)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(lab.os, "lstat", lstat)
    assert _discard_code(directory) == "recording_unreadable"
    assert failed and _files(directory) == before and _claims(directory) == []


def test_claimed_file_has_a_private_neutral_location(tmp_path, monkeypatch):
    recording, directory, _original, _bound = _session(tmp_path)
    seen = []
    real = lab._hash_file

    def hashed(path, expect=None):
        if _is_claim(path):
            seen.append((Path(path).name, stat.S_IMODE(Path(path).parent.stat().st_mode), Path(path).parent.parent))
        return real(path, expect)

    monkeypatch.setattr(lab, "_hash_file", hashed)
    assert discard_session(directory, include_recording=True).deleted_recording
    [(name, mode, parent)] = seen
    assert name == lab._CLAIMED_NAME and recording.name not in name
    assert mode == 0o700 and parent == directory


# --------------------------------------------------------------------------
# Case F: recording deleted, metadata deletion failed
# --------------------------------------------------------------------------


def test_retry_after_metadata_failure_never_deletes_a_new_file(tmp_path, monkeypatch):
    recording, directory, _original, _bound = _session(tmp_path)
    real = Path.unlink

    def refuse(path, *args, **kwargs):
        if path == directory / "session.json":
            raise PermissionError(PLANTED)
        return real(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", refuse)
        assert _discard_code(directory) == "discard_incomplete"
    assert not recording.exists() and _claims(directory) == []
    fresh = b"NEW-" + PLANTED.encode()
    recording.write_bytes(fresh)
    assert _discard_code(directory) == "recording_identity_mismatch"
    assert recording.read_bytes() == fresh
    result = discard_session(directory)
    assert not result.deleted_recording and recording.read_bytes() == fresh
    assert not (directory / MARKER_NAME).exists()


# --------------------------------------------------------------------------
# Case G: an interrupted discard
# --------------------------------------------------------------------------


def test_interrupted_claim_blocks_until_the_documented_recovery(tmp_path):
    recording, directory, original, _bound = _session(tmp_path)
    claim = directory / (lab._CLAIM_PREFIX + "crash")
    claim.mkdir(mode=0o700)
    REAL_RENAME(recording, claim / lab._CLAIMED_NAME)  # The state a crash after the claim leaves.
    _assert_blocked(directory, "discard_recovery_pending")
    assert main(["prepare", str(directory)]) == 2
    # Recovery: the original name is free, so the claimed file is moved back.
    REAL_RENAME(claim / lab._CLAIMED_NAME, recording)
    claim.rmdir()
    assert recording.read_bytes() == original
    assert prepare_session(directory).accepted_frames == 3
    result = discard_session(directory, include_recording=True)
    assert result.deleted_recording and result.directory_removed


def test_crash_after_unlink_leaves_only_an_empty_claim(tmp_path):
    recording, directory, _original, _bound = _session(tmp_path)
    (directory / (lab._CLAIM_PREFIX + "crash")).mkdir()
    recording.unlink()  # The claimed file was deleted; the process stopped there.
    result = discard_session(directory, include_recording=True)
    assert not result.deleted_recording and result.deleted_generated == 4


# --------------------------------------------------------------------------
# Race 10 and preserved behaviour
# --------------------------------------------------------------------------


def test_race10_ordinary_discard_through_the_cli(tmp_path, capsys):
    recording, directory, _original, _bound = _session(tmp_path)
    assert main(["discard", str(directory), "--include-recording"]) == 0
    assert not recording.exists() and not directory.exists()


def test_race10_ordinary_discard_through_a_cli_subprocess(tmp_path):
    recording, directory, _original, _bound = _session(tmp_path)
    keep = tmp_path / "unrelated.txt"
    keep.write_text("KEEP")
    result = subprocess.run(
        [sys.executable, "-m", "ml.label", "discard", str(directory), "--include-recording"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=TIMEOUT,
    )
    assert result.returncode == 0 and "Traceback" not in result.stderr
    assert not recording.exists() and not directory.exists() and keep.read_text() == "KEEP"


def test_plain_discard_never_claims_the_recording(tmp_path, monkeypatch):
    recording, directory, original, _bound = _session(tmp_path)
    seen = _patch_rename(monkeypatch)
    result = discard_session(directory)
    assert not seen and not result.deleted_recording and recording.read_bytes() == original
    assert sorted(item.name for item in directory.iterdir()) == [recording.name]


def test_outside_recording_is_never_claimed(tmp_path, monkeypatch):
    recording, directory = _make(tmp_path)
    seen = _patch_rename(monkeypatch)
    result = discard_session(directory, include_recording=True)
    assert not seen and result.recording_outside_session and recording.exists()


def test_missing_recording_is_not_claimed(tmp_path, monkeypatch):
    recording, directory, _original, _bound = _session(tmp_path)
    recording.unlink()
    seen = _patch_rename(monkeypatch)
    result = discard_session(directory, include_recording=True)
    assert not seen and not result.deleted_recording and result.directory_removed


def test_legacy_session_is_never_claimed(tmp_path, monkeypatch, capsys):
    recording, directory, original, _bound = _session(tmp_path)
    document = json.loads((directory / "session.json").read_text())
    document.pop("recording_identity")
    (directory / "session.json").write_text(json.dumps(document))
    seen = _patch_rename(monkeypatch)
    assert main(["discard", str(directory), "--include-recording"]) == 2
    assert capsys.readouterr().err.strip() == "recording_identity_unavailable"
    assert not seen and recording.read_bytes() == original and _claims(directory) == []


@pytest.mark.parametrize("kind", ["hardlink", "symlink", "copy"])
def test_static_refusals_happen_before_any_claim(tmp_path, monkeypatch, kind):
    recording, directory, original, _bound = _session(tmp_path)
    if kind == "hardlink":
        os.link(recording, tmp_path / "second.jsonl")
    elif kind == "symlink":
        real = tmp_path / "real.jsonl"
        REAL_RENAME(recording, real)
        recording.symlink_to(real)
    else:
        fresh = tmp_path / "fresh.jsonl"
        shutil.copyfile(recording, fresh)
        os.replace(fresh, recording)
    seen = _patch_rename(monkeypatch)
    with pytest.raises(LabelingError):
        discard_session(directory, include_recording=True)
    assert not seen and _claims(directory) == []
    assert Path(os.path.realpath(recording)).read_bytes() == original
