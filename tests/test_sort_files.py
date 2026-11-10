import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tools import sort_files


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = sort_files.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


def make(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def find_log(folder):
    logs = sorted(Path(folder).glob(sort_files.LOG_PREFIX + "*.json"))
    return logs[-1] if logs else None


class NamingTests(unittest.TestCase):
    def test_categories_are_case_insensitive_and_handle_compound_extensions(self):
        self.assertEqual(sort_files.category_for("CV final.PDF"), "Documents")
        self.assertEqual(sort_files.category_for("photo.HEIC"), "Images")
        self.assertEqual(sort_files.category_for("backup.tar.gz"), "Archives")
        self.assertEqual(sort_files.category_for("README"), "Other")
        self.assertEqual(sort_files.category_for("weird.xyz"), "Other")

    def test_split_extension(self):
        self.assertEqual(sort_files.split_extension("a.tar.gz"), ("a", ".tar.gz"))
        self.assertEqual(sort_files.split_extension("report.v2.pdf"), ("report.v2", ".pdf"))
        self.assertEqual(sort_files.split_extension("Makefile"), ("Makefile", ""))
        self.assertEqual(sort_files.split_extension(".bashrc"), (".bashrc", ""))


class SortTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_preview_changes_nothing(self):
        make(self.inbox / "cv.pdf")
        make(self.inbox / "photo.jpg")

        code, out, _ = run(self.inbox)

        self.assertEqual(code, 0)
        self.assertIn("Preview only", out)
        self.assertIn("Documents/", out)
        self.assertEqual(
            sorted(p.name for p in self.inbox.iterdir()), ["cv.pdf", "photo.jpg"]
        )

    def test_apply_sorts_by_type_and_skips_what_it_should(self):
        make(self.inbox / "cv.pdf")
        make(self.inbox / "budget.xlsx")
        make(self.inbox / "song.mp3")
        make(self.inbox / "notes")
        make(self.inbox / ".hidden.pdf")
        make(self.inbox / "movie.mp4.crdownload")
        make(self.inbox / "Thumbs.db")
        make(self.inbox / "existing_folder" / "inner.pdf")

        code, out, _ = run(self.inbox, "--apply")

        self.assertEqual(code, 0, out)
        self.assertTrue((self.inbox / "Documents" / "cv.pdf").is_file())
        self.assertTrue((self.inbox / "Spreadsheets" / "budget.xlsx").is_file())
        self.assertTrue((self.inbox / "Audio" / "song.mp3").is_file())
        self.assertTrue((self.inbox / "Other" / "notes").is_file())
        # Skipped: hidden, unfinished download, system file, and subfolders (not recursive).
        self.assertTrue((self.inbox / ".hidden.pdf").is_file())
        self.assertTrue((self.inbox / "movie.mp4.crdownload").is_file())
        self.assertTrue((self.inbox / "Thumbs.db").is_file())
        self.assertTrue((self.inbox / "existing_folder" / "inner.pdf").is_file())
        self.assertIn("unfinished download: 1", out)
        self.assertIsNotNone(find_log(self.inbox))
        self.assertIn("--undo", out)

    def test_never_overwrites_existing_files(self):
        make(self.inbox / "Documents" / "cv.pdf", "old")
        make(self.inbox / "cv.pdf", "new")

        code, out, _ = run(self.inbox, "--apply")

        self.assertEqual(code, 0)
        self.assertEqual((self.inbox / "Documents" / "cv.pdf").read_text(), "old")
        self.assertEqual((self.inbox / "Documents" / "cv (1).pdf").read_text(), "new")
        self.assertIn("renamed cv (1).pdf", out)

    def test_recursive_dedupes_within_run_and_leaves_projects_alone(self):
        make(self.inbox / "a" / "report.pdf", "a")
        make(self.inbox / "b" / "report.pdf", "b")
        make(self.inbox / "project" / ".git" / "HEAD")
        make(self.inbox / "project" / "main.py")
        make(self.inbox / ".cache" / "blob.zip")
        make(self.inbox / "Images" / "already.png")

        code, out, _ = run(self.inbox, "--recursive", "--apply")

        self.assertEqual(code, 0, out)
        docs = self.inbox / "Documents"
        self.assertEqual(
            sorted((docs / n).read_text() for n in ("report.pdf", "report (1).pdf")),
            ["a", "b"],
        )
        self.assertTrue((self.inbox / "project" / "main.py").is_file())
        self.assertTrue((self.inbox / ".cache" / "blob.zip").is_file())
        self.assertTrue((self.inbox / "Images" / "already.png").is_file())
        self.assertIn("1 already in place", out)

    def test_sort_by_date_uses_modified_time(self):
        photo = make(self.inbox / "photo.jpg")
        stamp = datetime(2024, 3, 15, 12, 0).timestamp()
        os.utime(photo, (stamp, stamp))

        code, _, _ = run(self.inbox, "--by", "date", "--apply")

        self.assertEqual(code, 0)
        self.assertTrue((self.inbox / "2024" / "2024-03" / "photo.jpg").is_file())

    def test_sort_by_extension(self):
        make(self.inbox / "a.pdf")
        make(self.inbox / "b.tar.gz")
        make(self.inbox / "README")

        code, _, _ = run(self.inbox, "--by", "extension", "--apply")

        self.assertEqual(code, 0)
        self.assertTrue((self.inbox / "PDF" / "a.pdf").is_file())
        self.assertTrue((self.inbox / "TAR.GZ" / "b.tar.gz").is_file())
        self.assertTrue((self.inbox / "No extension" / "README").is_file())

    def test_copy_with_dest_leaves_originals_and_writes_no_log(self):
        make(self.inbox / "cv.pdf")
        sorted_dir = self.root / "sorted"

        code, out, _ = run(self.inbox, "--dest", sorted_dir, "--copy", "--apply")

        self.assertEqual(code, 0, out)
        self.assertTrue((self.inbox / "cv.pdf").is_file())
        self.assertTrue((sorted_dir / "Documents" / "cv.pdf").is_file())
        self.assertIsNone(find_log(sorted_dir))
        self.assertNotIn("--undo", out)

    def test_undo_restores_files_and_removes_empty_folders(self):
        make(self.inbox / "cv.pdf", "cv")
        make(self.inbox / "photo.jpg", "img")
        run(self.inbox, "--apply")
        log = find_log(self.inbox)

        code, out, _ = run("--undo", log)
        self.assertEqual(code, 0)
        self.assertIn("Preview", out)
        self.assertFalse((self.inbox / "cv.pdf").exists())

        code, out, _ = run("--undo", log, "--apply")

        self.assertEqual(code, 0, out)
        self.assertEqual((self.inbox / "cv.pdf").read_text(), "cv")
        self.assertEqual((self.inbox / "photo.jpg").read_text(), "img")
        self.assertFalse((self.inbox / "Documents").exists())
        self.assertFalse((self.inbox / "Images").exists())

    def test_undo_refuses_to_overwrite_a_new_file_in_the_original_spot(self):
        make(self.inbox / "cv.pdf", "sorted")
        run(self.inbox, "--apply")
        make(self.inbox / "cv.pdf", "newer")

        code, _, err = run("--undo", find_log(self.inbox), "--apply")

        self.assertEqual(code, 1)
        self.assertIn("something else now occupies", err)
        self.assertEqual((self.inbox / "cv.pdf").read_text(), "newer")
        self.assertEqual((self.inbox / "Documents" / "cv.pdf").read_text(), "sorted")

    def test_rejects_a_file_that_is_not_an_undo_log(self):
        bogus = make(self.root / "bogus.json", json.dumps({"moves": []}))

        code, _, err = run("--undo", bogus)

        self.assertEqual(code, 2)
        self.assertIn("not an undo log", err)

    def test_refuses_recursive_sort_of_home_folder(self):
        with patch.object(sort_files.Path, "home", return_value=self.inbox):
            code, _, err = run(self.inbox, "--recursive")

        self.assertEqual(code, 2)
        self.assertIn("entire home folder", err)

    def test_missing_folder_is_an_error(self):
        code, _, err = run(self.root / "nope")

        self.assertEqual(code, 2)
        self.assertIn("not a folder", err)


if __name__ == "__main__":
    unittest.main()
