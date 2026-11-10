import io
import json
import os
import tempfile
import unittest
import zipfile
import zlib
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
        self.assertIn("Job Applications/", out)
        self.assertIn('name mentions "cv"', out)
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

        code, out, _ = run(self.inbox, "--by", "type", "--apply")

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

        code, out, _ = run(self.inbox, "--by", "type", "--apply")

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

        code, out, _ = run(self.inbox, "--by", "type", "--dest", sorted_dir, "--copy", "--apply")

        self.assertEqual(code, 0, out)
        self.assertTrue((self.inbox / "cv.pdf").is_file())
        self.assertTrue((sorted_dir / "Documents" / "cv.pdf").is_file())
        self.assertIsNone(find_log(sorted_dir))
        self.assertNotIn("--undo", out)

    def test_undo_restores_files_and_removes_empty_folders(self):
        make(self.inbox / "cv.pdf", "cv")
        make(self.inbox / "photo.jpg", "img")
        run(self.inbox, "--by", "type", "--apply")
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
        run(self.inbox, "--by", "type", "--apply")
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


def minimal_pdf(text):
    stream = zlib.compress(b"BT (" + text.encode("latin-1") + b") Tj ET")
    return (
        b"%PDF-1.4\n1 0 obj<</Length " + str(len(stream)).encode()
        + b"/Filter/FlateDecode>>stream\n" + stream + b"\nendstream\nendobj\n%%EOF"
    )


class SmartModeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def classify(self, name, data=b""):
        path = self.root / name
        path.write_bytes(data)
        return sort_files.smart_classify(path)

    def test_topics_from_file_names_in_english_and_danish(self):
        cases = {
            "Jane_Doe_Resume.docx": "Job Applications",
            "CoverLetter_Acme.pdf": "Job Applications",
            "R\u00e9sum\u00e9.pdf": "Job Applications",
            "Faktura_2024-03.pdf": "Finance",
            "receipts-march.pdf": "Finance",
            "receipt.jpg": "Finance",
            "Lejekontrakt.pdf": "Contracts & Legal",
            "boarding-pass-CPH.pdf": "Travel & Tickets",
            "Eksamensbevis.pdf": "Certificates & Diplomas",
            "dishwasher manual.pdf": "Manuals & Guides",
        }
        for name, folder in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.classify(name)[0], folder)

    def test_short_keywords_only_match_whole_words(self):
        self.assertEqual(self.classify("taxi-photo-booth.pdf")[0], "Documents")
        self.assertEqual(self.classify("cvs-pharmacy.pdf")[0], "Documents")

    def test_images_split_into_screenshots_photos_and_other_images(self):
        self.assertEqual(self.classify("Sk\u00e6rmbillede 2024-03-15 kl. 10.22.31.png")[0], "Screenshots")
        self.assertEqual(self.classify("Screenshot 2024-01-02 at 09.00.00.png")[0], "Screenshots")
        self.assertEqual(self.classify("IMG_0412.HEIC")[0], "Photos")
        self.assertEqual(self.classify("20240315_123456.jpg")[0], "Photos")
        self.assertEqual(self.classify("logo.svg")[0], "Images")

    def test_non_documents_fall_back_to_their_kind(self):
        self.assertEqual(self.classify("song.mp3"), ("Audio", "MP3 file"))
        self.assertEqual(self.classify("budget.mp3")[0], "Audio")  # topics are for documents
        self.assertEqual(self.classify("README"), ("Other", "no file extension"))

    def test_topics_from_plain_text_content(self):
        folder, reason = self.classify(
            "scan001.txt", b"Invoice number 4411\nSubtotal 100\nVAT 25\nAmount due 125"
        )
        self.assertEqual(folder, "Finance")
        self.assertIn("document mentions", reason)

    def test_topics_from_docx_content(self):
        path = self.root / "draft.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                "<w:document><w:body><w:p><w:r><w:t>Curriculum Vitae</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>Work experience</w:t></w:r></w:p></w:body></w:document>",
            )
        self.assertEqual(sort_files.smart_classify(path)[0], "Job Applications")

    def test_topics_from_pdf_content(self):
        pdf = minimal_pdf("This Agreement is made between the parties. Governing law: Denmark")
        self.assertEqual(self.classify("document(3).pdf", pdf)[0], "Contracts & Legal")

    def test_a_single_content_keyword_is_not_enough(self):
        self.assertEqual(self.classify("notes.txt", b"Remember the invoice.")[0], "Documents")

    def test_unreadable_documents_fall_back_quietly(self):
        self.assertEqual(self.classify("broken.docx", b"not a zip")[0], "Documents")
        self.assertEqual(self.classify("broken.pdf", b"%PDF-1.4 stream\n\xff\xfe")[0], "Documents")

    def test_duplicates_keep_the_oldest_copy_in_place(self):
        inbox = self.root / "inbox"
        older = make(inbox / "report.pdf", "same")
        newer = make(inbox / "report (1).pdf", "same")
        make(inbox / "other.pdf", "different")
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))

        code, out, _ = run(inbox, "--duplicates", "--apply")

        self.assertEqual(code, 0, out)
        self.assertTrue((inbox / "Duplicates" / "report (1).pdf").is_file())
        self.assertTrue((inbox / "Documents" / "report.pdf").is_file())
        self.assertTrue((inbox / "Documents" / "other.pdf").is_file())

    def test_safe_folder_rejects_names_that_could_escape_or_break(self):
        for bad in ("", "..", "../escape", "/abs", "a\\b", "CON", "trailing.", "a/./b", "x" * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(sort_files.SortError):
                    sort_files.safe_folder(bad)
        self.assertEqual(sort_files.safe_folder(" Finance "), Path("Finance"))
        self.assertEqual(sort_files.safe_folder("2024/2024-03"), Path("2024", "2024-03"))


if __name__ == "__main__":
    unittest.main()
