import io
import json
import os
import tempfile
import multiprocessing
import unittest
from contextlib import nullcontext, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from . import edits
from .mount import workspace_mount


class EditPublicationTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "client"
        self.tree = Path(folder.name) / "agent"
        self.root.mkdir()
        self.tree.mkdir()
        self.copy = edits.WorkingCopy(self.root, self.tree)
        self.target = self.root / "note.txt"
        self.target.write_bytes(b"start")
        self.copy.refresh()
        (self.tree / "note.txt").write_bytes(b"agent")

    def assert_conflict(self, conflicts):
        (path, proposal), = conflicts
        self.assertEqual(path, Path("note.txt"))
        self.assertEqual(proposal.read_bytes(), b"agent")
        self.assertEqual(self.target.read_bytes(), b"local")

    def test_clean_update_addition_deletion_and_next_turn(self):
        (self.tree / "empty").mkdir()
        (self.tree / "empty/new").write_bytes(b"created")
        self.assertEqual(self.copy.publish(), [])
        self.assertEqual(self.target.read_bytes(), b"agent")
        self.assertEqual((self.root / "empty/new").read_bytes(), b"created")
        self.target.write_bytes(b"latest")
        self.copy.refresh()
        self.assertEqual((self.tree / "note.txt").read_bytes(), b"latest")
        (self.tree / "empty/new").unlink()
        (self.tree / "empty").rmdir()
        self.assertEqual(self.copy.publish(), [])
        self.assertFalse((self.root / "empty").exists())

    def test_change_before_check_with_restored_timestamp(self):
        before = self.target.stat()
        self.target.write_bytes(b"local")
        os.utime(self.target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assert_conflict(self.copy.publish())

    def test_previous_message_cannot_overwrite_an_edit_between_turns(self):
        self.assertEqual(self.copy.publish(), [])
        self.target.write_bytes(b"local")
        self.copy.refresh()
        self.assertEqual((self.tree / "note.txt").read_bytes(), b"local")
        (self.tree / "note.txt").write_bytes(b"agent")
        self.assert_conflict(self.copy.publish())

    def test_type_change_preserves_the_original_tree(self):
        (self.root / "folder").mkdir()
        (self.root / "folder/doc").write_bytes(b"keep")
        self.copy.refresh()
        (self.tree / "folder/doc").unlink()
        (self.tree / "folder").rmdir()
        (self.tree / "folder").write_bytes(b"file")
        (path, proposal), = self.copy.publish()
        self.assertEqual(path, Path("folder"))
        self.assertEqual(proposal.read_bytes(), b"file")
        self.assertEqual((self.root / "folder/doc").read_bytes(), b"keep")

    def test_editor_saves_between_check_and_atomic_exchange(self):
        rename = edits.rename_at

        def save_then_exchange(source, parent, name, **kwargs):
            if kwargs.get("exchange"):
                other = self.root / "editor.tmp"
                other.write_bytes(b"local")
                os.replace(other, self.target)
            return rename(source, parent, name, **kwargs)

        with patch.object(edits, "rename_at", side_effect=save_then_exchange):
            self.assert_conflict(self.copy.publish())

    def test_latest_save_wins_during_conflict_restoration(self):
        rename = edits.rename_at

        def save(source, parent, name, **kwargs):
            if kwargs.get("exchange"):
                self.target.write_bytes(b"first")
            elif Path(source).name == "exchange":
                self.target.write_bytes(b"local")
            return rename(source, parent, name, **kwargs)

        with patch.object(edits, "rename_at", side_effect=save):
            self.assert_conflict(self.copy.publish())
        previous, = self.copy.archive.glob("edit-*/previous")
        self.assertEqual(previous.read_bytes(), b"first")

    def test_two_sessions_with_the_same_base(self):
        second_tree = self.tree.parent / "second"
        second_tree.mkdir()
        second = edits.WorkingCopy(self.root, second_tree)
        second.refresh()
        (second_tree / "note.txt").write_bytes(b"second")
        self.assertEqual(self.copy.publish(), [])
        (_, proposal), = second.publish()
        self.assertEqual(self.target.read_bytes(), b"agent")
        self.assertEqual(proposal.read_bytes(), b"second")

    def test_recover_editor_save_after_publisher_dies_immediately_after_exchange(self):
        rename = edits.rename_at

        def interrupt(source, parent, name, **kwargs):
            if kwargs.get("exchange"):
                self.target.write_bytes(b"local")
                rename(source, parent, name, **kwargs)
                os._exit(73)
            return rename(source, parent, name, **kwargs)

        with patch.object(edits, "rename_at", side_effect=interrupt):
            child = multiprocessing.get_context("fork").Process(target=self.copy.publish)
            child.start()
            child.join(5)
        self.assertEqual(child.exitcode, 73)
        with self.copy.locked():
            self.copy.recover()
            self.copy.recover()
        proposal, = self.copy.archive.glob("edit-*/agent/note.txt")
        self.assertEqual(proposal.read_bytes(), b"agent")
        self.assertEqual(self.target.read_bytes(), b"local")

    def test_deleted_source_stays_deleted_and_symlink_is_not_followed(self):
        self.target.unlink()
        (_, proposal), = self.copy.publish()
        self.assertEqual(proposal.read_bytes(), b"agent")
        self.assertFalse(self.target.exists())
        self.target.symlink_to(self.root / "missing")
        (_, proposal), = self.copy.publish()
        self.assertTrue(self.target.is_symlink())
        self.assertFalse((self.root / "missing").exists())
        self.assertEqual(proposal.read_bytes(), b"agent")

    def test_interrupted_message_retains_agent_work_without_publishing(self):
        with patch("runtime.mount._mount", return_value=nullcontext("mount")), redirect_stderr(io.StringIO()) as err:
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with workspace_mount(self.root, "host", None, "rw") as (_, copy):
                    (copy.tree / "note.txt").write_bytes(b"agent")
                    self.target.write_bytes(b"local")
                    raise RuntimeError("interrupted")
        draft, = self.copy.archive.glob("unfinished-*/note.txt")
        self.assertIn(str(draft.parent), err.getvalue())
        self.assertEqual(draft.read_bytes(), b"agent")
        self.assertEqual(self.target.read_bytes(), b"local")

    def test_staged_link_cannot_turn_into_a_deletion(self):
        (self.tree / "note.txt").unlink()
        (self.tree / "note.txt").symlink_to("another-file")
        with self.assertRaisesRegex(ValueError, "unsupported staged"):
            self.copy.publish()
        self.assertEqual(self.target.read_bytes(), b"start")

    def test_recovery_never_replaces_a_new_save(self):
        for recreated in (False, True):
            with self.subTest(recreated=recreated), self.copy.locked():
                entry = Path(tempfile.mkdtemp(dir=self.copy.archive, prefix="edit-"))
                (entry / "path.json").write_text(json.dumps("note.txt"))
                self.target.write_bytes(b"original")
                self.target.rename(entry / "claimed")
                if recreated:
                    self.target.write_bytes(b"local")
                self.copy.recover()
                self.assertEqual(self.target.read_bytes(), b"local" if recreated else b"original")
                if recreated:
                    self.assertEqual((entry / "previous").read_bytes(), b"original")
