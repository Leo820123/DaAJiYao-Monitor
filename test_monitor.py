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

    def test_nested_reads_all_pages(self):
        first = [reply(i, 888, "old") for i in range(1, 21)]
        second = [reply(21, 888, "last")]
        pages = []
        def mock_get(_session, _url, params):
            pages.append(params["pn"])
            return {"page": {"count": 21}, "replies": first if params["pn"] == 1 else second}
        with patch.object(monitor, "get", side_effect=mock_get), \
             patch.object(monitor.time, "sleep", return_value=None):
            result = list(monitor.nested(None, "oid", 17, "root"))
        self.assertEqual(len(result), 21)
        self.assertEqual(pages, [1, 2])

if __name__ == "__main__":
    unittest.main()
