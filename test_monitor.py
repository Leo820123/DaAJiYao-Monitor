"""Offline tests: historical import, duplicate protection and correct nested pagination."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import monitor

def reply(rpid, uid, content):
    return {"rpid": rpid, "mid": uid, "member": {"mid": uid, "uname": "author"},
            "content": {"message": content}, "ctime": 1700000000, "parent": 1}

class HistoryTests(unittest.TestCase):
    def test_import_before_first_launch_and_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(monitor, "DB", Path(directory) / "test.sqlite3"), \
                 patch.object(monitor, "CSV", Path(directory) / "export.csv"):
                conn = monitor.database()
                top = reply(10, 123, "pinned before launch")
                historical = [reply(11, 888, "old UP reply"),
                              reply(12, 999, "not UP"),
                              reply(13, 888, "another old UP reply")]
                with patch.object(monitor, "pinned", return_value=top), \
                     patch.object(monitor, "nested", side_effect=lambda *args, **kwargs: iter(historical)):
                    first = monitor.check(None, conn, "999", 17, "888", "unused")
                    second = monitor.check(None, conn, "999", 17, "888", "unused")
                self.assertEqual(first["scanned"], 3)
                self.assertEqual(first["matched"], 2)
                self.assertEqual(first["added"], 2)
                self.assertEqual(second["added"], 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM records").fetchone()[0], 3)
                self.assertTrue((Path(directory) / "export.csv").exists())
                conn.close()

    def test_up_reply_includes_exact_user_original_and_excludes_unrelated(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(monitor, "DB", Path(directory) / "test.sqlite3"), \
                 patch.object(monitor, "CSV", Path(directory) / "export.csv"):
                conn = monitor.database()
                top = reply(100, 999, "pinned question")
                original = reply(101, 123, "What about tomorrow?")
                unrelated = reply(102, 234, "unrelated message")
                author_answer = reply(103, 888, "Watch the volume")
                author_answer["parent"] = 101
                batch = [unrelated, author_answer, original]
                with patch.object(monitor, "pinned", return_value=top), \
                     patch.object(monitor, "nested", side_effect=lambda *args, **kwargs: iter(batch)):
                    summary = monitor.check(None, conn, "999", 17, "888", "unused")
                self.assertEqual(summary["matched"], 1)
                self.assertEqual(summary["context"], 1)
                self.assertEqual(summary["unresolved"], 0)
                stored = dict(conn.execute("SELECT id,kind FROM records"))
                self.assertEqual(stored["101"], "被UP主回复的用户原评论")
                self.assertEqual(stored["103"], "UP主楼中楼")
                self.assertNotIn("102", stored)
                conn.close()

    def test_missing_parent_is_not_falsely_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(monitor, "DB", Path(directory) / "test.sqlite3"), \
                 patch.object(monitor, "CSV", Path(directory) / "export.csv"):
                conn = monitor.database()
                top = reply(100, 777, "pinned root")
                answer = reply(103, 888, "reply to missing 999")
                answer["parent"] = 999
                with patch.object(monitor, "pinned", return_value=top), \
                     patch.object(monitor, "nested", side_effect=lambda *args, **kwargs: iter([answer])):
                    summary = monitor.check(None, conn, "999", 17, "888", "unused")
                self.assertEqual(summary["unresolved"], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM records").fetchone()[0], 2)
                conn.close()

    def test_sparse_first_page_does_not_stop_before_newer_replies(self):
        """Regression: old code stopped if the first page had fewer than 20."""
        pages = []
        batch1 = [reply(i, 123, "older") for i in range(1, 11)]
        batch2 = [reply(i, 888, "newer UP comment") for i in range(11, 26)]
        def fake_get(_session, _url, params):
            pn = params["pn"]
            pages.append(pn)
            return {"page": {"count": 25},
                    "replies": {1: batch1, 2: batch2}.get(pn, [])}
        with patch.object(monitor, "get", side_effect=fake_get), \
             patch.object(monitor.time, "sleep", return_value=None):
            items = list(monitor.nested(None, "oid", 17, "root"))
        self.assertEqual(len(items), 25)
        self.assertIn("25", [monitor.rid(item) for item in items])
        self.assertGreaterEqual(len(pages), 3)

    def test_sparse_pages_all_scanned_even_when_first_page_has_one_item(self):
        pages = []
        def fake_get(_session, _url, params):
            pn = params["pn"]
            pages.append(pn)
            data = {1: [reply(1, 123, "old")],
                    2: [reply(2, 888, "newer")],
                    3: [reply(3, 888, "newest")]}
            return {"page": {"count": 50}, "replies": data.get(pn, [])}
        with patch.object(monitor, "get", side_effect=fake_get), \
             patch.object(monitor.time, "sleep", return_value=None):
            records = list(monitor.nested(None, "oid", 17, "root"))
        self.assertEqual([monitor.rid(x) for x in records], ["1","2","3"])
        self.assertIn(3, pages)

    def test_nested_reads_all_pages(self):
        first = [reply(i, 888, "old") for i in range(1, 21)]
        second = [reply(21, 888, "last")]
        pages = []
        def mock_get(_session, _url, params):
            pages.append(params["pn"])
            return {"page": {"count": 21}, "replies": {1: first, 2: second}.get(params["pn"], [])}
        with patch.object(monitor, "get", side_effect=mock_get), \
             patch.object(monitor.time, "sleep", return_value=None):
            result = list(monitor.nested(None, "oid", 17, "root"))
        self.assertEqual(len(result), 21)
        self.assertEqual(pages, [1, 2, 3])

if __name__ == "__main__":
    unittest.main()
