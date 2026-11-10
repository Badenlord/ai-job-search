import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import file_sorter_app as app


def make(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FileSorterAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.inbox = self.root / "Downloads"
        self.inbox.mkdir()
        self.server = app.create_server(0)
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        all_headers = {"Host": self.host}
        all_headers.update(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=all_headers)
        response = conn.getresponse()
        raw = response.read()
        result = (response.status, raw, dict(response.getheaders()))
        conn.close()
        return result

    def api(self, name, body=None, **headers):
        sent = {"X-Sorter-Token": self.server.token}
        sent.update(headers)
        status, raw, _ = self.request("POST", "/api/" + name, body or {}, sent)
        return status, json.loads(raw)

    # --- access control -------------------------------------------------

    def test_page_needs_the_key_and_ships_a_strict_csp(self):
        status, _, _ = self.request("GET", "/")
        self.assertEqual(status, 403)

        status, raw, headers = self.request("GET", "/?key=" + self.server.token)
        page = raw.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(json.dumps(self.server.token), page)
        self.assertNotIn("__NONCE__", page)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

    def test_rejects_foreign_hosts_origins_and_missing_tokens(self):
        status, _, _ = self.request("GET", "/?key=" + self.server.token, headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.api("places", **{"X-Sorter-Token": "wrong"})[0], 403)
        self.assertEqual(self.api("places", Origin="https://evil.example")[0], 403)
        self.assertEqual(self.api("nope")[0], 404)
        self.assertEqual(self.api("places", Origin=f"http://{self.host}")[0], 200)

    # --- the main flow --------------------------------------------------

    def test_scan_sort_with_changes_then_undo_from_history(self):
        make(self.inbox / "invoice-march.pdf")
        make(self.inbox / "notes.txt")
        make(self.inbox / "LICENSE")
        make(self.inbox / "half.zip.crdownload")

        status, scan = self.api("scan", {"folder": str(self.inbox), "by": "smart"})
        self.assertEqual(status, 200, scan)
        by_name = {f["name"]: f for f in scan["files"]}
        self.assertEqual(set(by_name), {"invoice-march.pdf", "notes.txt", "LICENSE"})
        self.assertEqual(by_name["invoice-march.pdf"]["folder"], "Finance")
        self.assertIn("invoice", by_name["invoice-march.pdf"]["reason"])
        self.assertEqual(scan["skipped"], {"unfinished download": 1})
        self.assertIn("Job Applications", scan["folders"])

        status, result = self.api("sort", {
            "scanId": scan["scanId"],
            "choices": [
                {"id": by_name["invoice-march.pdf"]["id"], "folder": "Finance"},
                {"id": by_name["notes.txt"]["id"], "folder": "My notes"},
            ],  # LICENSE left out: stays where it is
        })
        self.assertEqual(status, 200, result)
        self.assertEqual(result["done"], 2)
        self.assertTrue((self.inbox / "Finance" / "invoice-march.pdf").is_file())
        self.assertTrue((self.inbox / "My notes" / "notes.txt").is_file())
        self.assertTrue((self.inbox / "LICENSE").is_file())

        status, history = self.api("history", {"folder": str(self.inbox)})
        self.assertEqual(status, 200)
        self.assertEqual(len(history["entries"]), 1)
        self.assertEqual(history["entries"][0]["restorable"], 2)

        status, undone = self.api("undo", {"log": history["entries"][0]["log"]})
        self.assertEqual(status, 200, undone)
        self.assertEqual(undone["restored"], 2)
        self.assertTrue((self.inbox / "invoice-march.pdf").is_file())
        self.assertFalse((self.inbox / "My notes").exists())

    def test_unsafe_folder_names_are_refused_before_anything_moves(self):
        make(self.inbox / "a.pdf")
        _, scan = self.api("scan", {"folder": str(self.inbox)})
        file_id = scan["files"][0]["id"]

        for bad in ("../escape", "/etc", "a\\b", ".."):
            status, body = self.api("sort", {"scanId": scan["scanId"], "choices": [{"id": file_id, "folder": bad}]})
            self.assertEqual(status, 400, bad)
            self.assertIn("folder name", body["error"])
        self.assertTrue((self.inbox / "a.pdf").is_file())
        self.assertFalse((self.root / "escape").exists())

        # The scan is still usable after a refused request, but only once.
        ok = {"scanId": scan["scanId"], "choices": [{"id": file_id, "folder": "Docs"}]}
        self.assertEqual(self.api("sort", ok)[0], 200)
        status, body = self.api("sort", ok)
        self.assertEqual(status, 400)
        self.assertIn("Scan again", body["error"])

    def test_invalid_selections_are_refused(self):
        make(self.inbox / "a.pdf")
        _, scan = self.api("scan", {"folder": str(self.inbox)})
        for choices in ([], [{"id": 99, "folder": "X"}], [{"id": True, "folder": "X"}],
                        [{"id": 0, "folder": "X"}, {"id": 0, "folder": "Y"}]):
            status, _ = self.api("sort", {"scanId": scan["scanId"], "choices": choices})
            self.assertEqual(status, 400, choices)
        self.assertEqual(self.api("sort", {"scanId": "made-up", "choices": [{"id": 0, "folder": "X"}]})[0], 400)

    # --- helpers --------------------------------------------------------

    def test_browse_lists_visible_subfolders_with_full_paths(self):
        (self.inbox / "Work").mkdir()
        (self.inbox / ".secret").mkdir()
        make(self.inbox / "file.txt")

        status, listing = self.api("browse", {"path": str(self.inbox)})

        self.assertEqual(status, 200)
        self.assertEqual(listing["dirs"], [{"name": "Work", "path": str(self.inbox / "Work")}])
        self.assertEqual(listing["parent"], str(self.root))
        self.assertEqual(self.api("browse", {"path": str(self.root / "missing")})[0], 400)

    def test_places_start_in_downloads(self):
        with patch.object(app.Path, "home", return_value=self.root):
            status, info = self.api("places")
        self.assertEqual(status, 200)
        self.assertEqual(info["start"], str(self.inbox))
        self.assertEqual([p["name"] for p in info["places"]], ["Downloads", "Home"])

    def test_undo_only_accepts_sorter_logs(self):
        other = make(self.root / "notes.json", "{}")
        self.assertEqual(self.api("undo", {"log": str(other)})[0], 400)
        fake = make(self.root / ".sort_files_log_x.json", json.dumps({"moves": []}))
        self.assertEqual(self.api("undo", {"log": str(fake)})[0], 400)

    def test_errors_in_scan_come_back_as_readable_messages(self):
        status, body = self.api("scan", {"folder": str(self.root / "missing")})
        self.assertEqual(status, 400)
        self.assertTrue(body["error"].startswith("Folder not found"))


if __name__ == "__main__":
    unittest.main()
