"""engine/workers.py: the installed worker CLIs a job trusts (the manifest, the PATH rule and the digest check, F3).

Every test works on its own temporary tree; no test touches the real PATH or a real install.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import nolive  # noqa: F401  (no test sweeps the live /proc)
from engine import workers
from engine.job import JobError


def write_tree(root: Path) -> None:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "tool").write_text("#!/bin/sh\necho 1.2.3\n", encoding="utf-8")
    (root / "bin" / "tool").chmod(0o755)
    (root / "lib").mkdir()
    (root / "lib" / "data.txt").write_text("data\n", encoding="utf-8")


def manifest_for(root: Path, name: str = "tool", bin_rel: str = "bin/tool") -> dict:
    real = os.path.realpath(root)
    return {"schema": 1, "workers": {name: {"root": real, "bin": os.path.join(real, bin_rel), "digest": workers.digest(root)}}}


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="workers-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "install"
        write_tree(self.root)

    def test_the_same_tree_has_the_same_digest(self):
        self.assertEqual(workers.digest(self.root), workers.digest(self.root))

    def test_a_changed_byte_changes_the_digest(self):
        before = workers.digest(self.root)
        (self.root / "lib" / "data.txt").write_text("changed\n", encoding="utf-8")
        self.assertNotEqual(workers.digest(self.root), before)

    def test_a_changed_mode_changes_the_digest(self):
        before = workers.digest(self.root)
        (self.root / "bin" / "tool").chmod(0o700)
        self.assertNotEqual(workers.digest(self.root), before)

    def test_an_added_or_removed_entry_changes_the_digest(self):
        before = workers.digest(self.root)
        (self.root / "lib" / "extra").write_text("x", encoding="utf-8")
        added = workers.digest(self.root)
        self.assertNotEqual(added, before)
        (self.root / "lib" / "extra").unlink()
        self.assertEqual(workers.digest(self.root), before)

    def test_a_symlink_is_recorded_by_its_text_and_never_followed(self):
        outside = self.dir / "outside.txt"
        outside.write_text("first\n", encoding="utf-8")
        link = self.root / "lib" / "link"
        os.symlink(outside, link)
        before = workers.digest(self.root)
        outside.write_text("second\n", encoding="utf-8")  # the link's target changes; the install's tree does not
        self.assertEqual(workers.digest(self.root), before)
        link.unlink()
        os.symlink(self.dir / "elsewhere", link)  # the link's text changes
        self.assertNotEqual(workers.digest(self.root), before)

    def test_a_fifo_in_the_install_is_refused(self):
        os.mkfifo(self.root / "lib" / "pipe")
        with self.assertRaises(JobError):
            workers.digest(self.root)


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="workers-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "install"
        write_tree(self.root)
        self.file = self.dir / "pivotal-workers.json"

    def write(self, data) -> str:
        self.file.write_text(json.dumps(data), encoding="utf-8")
        return str(self.file)

    def test_an_empty_path_means_no_manifest(self):
        self.assertIsNone(workers.load(""))

    def test_a_good_manifest_is_loaded_into_pins(self):
        pins = workers.load(self.write(manifest_for(self.root)))
        self.assertEqual(set(pins), {"tool"})
        self.assertEqual(pins["tool"].digest, workers.digest(self.root))

    def test_a_missing_file_refuses_the_job(self):
        with self.assertRaises(JobError):
            workers.load(str(self.dir / "absent.json"))

    def test_a_link_to_the_manifest_refuses_the_job(self):
        target = self.write(manifest_for(self.root))
        link = self.dir / "link.json"
        os.symlink(target, link)
        with self.assertRaises(JobError):
            workers.load(str(link))

    def test_a_wrong_schema_or_an_extra_key_refuses_the_job(self):
        good = manifest_for(self.root)
        for bad in ({**good, "schema": 2}, {**good, "extra": 1}, {"schema": 1}, []):
            with self.subTest(bad=bad):
                with self.assertRaises(JobError):
                    workers.load(self.write(bad))

    def test_a_bin_outside_its_root_or_a_short_digest_refuses_the_job(self):
        good = manifest_for(self.root)
        entry = good["workers"]["tool"]
        for changed in ({**entry, "bin": "/usr/bin/tool"}, {**entry, "digest": "abc"}, {**entry, "root": "relative"}):
            with self.subTest(changed=changed):
                with self.assertRaises(JobError):
                    workers.load(self.write({"schema": 1, "workers": {"tool": changed}}))

    def test_a_worker_name_that_is_not_a_plain_word_refuses_the_job(self):
        with self.assertRaises(JobError):
            workers.load(self.write({"schema": 1, "workers": {"../tool": manifest_for(self.root)["workers"]["tool"]}}))


class PresenceTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="workers-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "install"
        write_tree(self.root)
        self.path = self.dir / "bin-dir"
        self.path.mkdir()
        os.symlink(self.root / "bin" / "tool", self.path / "tool")

    def test_without_a_manifest_a_worker_is_present_when_the_path_has_it(self):
        self.assertEqual(workers.absent(["tool", "other"], None, str(self.path)), frozenset({"other"}))

    def test_with_a_manifest_an_unpinned_worker_is_not_installed_even_on_the_path(self):
        pins = workers.load(self._manifest({"tool": manifest_for(self.root)["workers"]["tool"]}))
        self.assertEqual(workers.absent(["tool", "pi"], pins, str(self.path)), frozenset({"pi"}))

    def test_a_pinned_worker_is_present_whatever_the_path_resolves_it_to(self):
        # A pinned worker runs by its manifest's absolute bin (command), so a same-named CLI first on PATH neither makes it
        # absent nor refuses the job: the manifest decides, the PATH does not.
        pins = workers.load(self._manifest({"tool": manifest_for(self.root)["workers"]["tool"]}))
        other = self.dir / "planted"
        other.mkdir()
        (other / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
        (other / "tool").chmod(0o755)
        planted = self.dir / "planted-path"
        planted.mkdir()
        os.symlink(other / "tool", planted / "tool")
        self.assertEqual(workers.absent(["tool"], pins, str(planted)), frozenset())

    def _manifest(self, workers_map) -> str:
        path = self.dir / "pivotal-workers.json"
        path.write_text(json.dumps({"schema": 1, "workers": workers_map}), encoding="utf-8")
        return str(path)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="workers-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "install"
        write_tree(self.root)
        self.pins = workers.load(self._write(manifest_for(self.root)))

    def _write(self, data) -> str:
        path = self.dir / "pivotal-workers.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_a_pinned_worker_runs_by_its_absolute_pinned_binary(self):
        self.assertEqual(workers.command(["tool", "-p", "x"], self.pins), [os.path.join(os.path.realpath(self.root), "bin/tool"), "-p", "x"])

    def test_without_a_manifest_the_argv_is_unchanged_and_resolved_by_name(self):
        self.assertEqual(workers.command(["tool", "-p"], None), ["tool", "-p"])

    def test_an_unpinned_worker_refuses_the_call(self):
        with self.assertRaises(JobError):
            workers.command(["pi", "-p"], self.pins)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="workers-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "install"
        write_tree(self.root)
        self.pins = workers.load(self._write(manifest_for(self.root)))

    def _write(self, data) -> str:
        path = self.dir / "pivotal-workers.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_an_unchanged_install_verifies(self):
        self.assertEqual(workers.verify("tool", self.pins), "")

    def test_a_changed_install_is_modified(self):
        # The same mutation an earlier entry's agent could make: the next entry would run the changed file.
        (self.root / "bin" / "tool").write_text("#!/bin/sh\nenv > /tmp/x\n", encoding="utf-8")
        self.assertEqual(workers.verify("tool", self.pins), workers.MODIFIED)

    def test_a_missing_install_is_modified(self):
        shutil.rmtree(self.root)
        self.assertEqual(workers.verify("tool", self.pins), workers.MODIFIED)

    def test_without_a_manifest_there_is_nothing_to_compare(self):
        self.assertEqual(workers.verify("tool", None), "")

    def test_a_worker_with_no_pin_is_not_installed(self):
        self.assertEqual(workers.verify("pi", self.pins), workers.NOT_INSTALLED)

    def test_the_three_verdicts_are_distinct_so_each_caller_compares_its_own(self):
        # NOT_INSTALLED is a non-empty string: a caller that tests the verdict for truth would call an unpinned worker changed (S2).
        self.assertTrue(workers.NOT_INSTALLED)
        self.assertEqual(len({"", workers.MODIFIED, workers.NOT_INSTALLED}), 3)
        self.assertEqual(workers.verify("tool", self.pins), "")
        self.assertEqual(workers.verify("pi", self.pins), workers.NOT_INSTALLED)

    def test_an_install_that_cannot_be_digested_is_modified_not_trusted(self):
        # The digest raises (a FIFO is not a file, a link or a directory): the install is refused, never verified.
        os.mkfifo(self.root / "lib" / "pipe")
        self.assertEqual(workers.verify("tool", self.pins), workers.MODIFIED)

    def test_a_pinned_binary_reached_through_a_link_is_modified(self):
        # The manifest names the binary by a path that is not its real path: a link in the path could redirect the run.
        os.symlink(self.root / "bin", self.root / "alias")
        pins = workers.load(self._write(manifest_for(self.root, bin_rel="alias/tool")))
        self.assertEqual(workers.verify("tool", pins), workers.MODIFIED)

    def test_a_pinned_binary_that_is_gone_is_modified(self):
        (self.root / "bin" / "tool").unlink()
        self.assertEqual(workers.verify("tool", self.pins), workers.MODIFIED)


if __name__ == "__main__":
    unittest.main()
