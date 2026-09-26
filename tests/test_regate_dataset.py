import sqlite3
import unittest

from scripts.regate_dataset import purge_nonaccepted_comments


class RegateDatasetTests(unittest.TestCase):
    def test_purge_removes_comments_without_accepted_context(self):
        con = sqlite3.connect(":memory:")
        con.executescript(
            """
            CREATE TABLE contexts (platform TEXT, context_id TEXT, verdict TEXT);
            CREATE TABLE comments (platform TEXT, context_id TEXT);
            INSERT INTO contexts VALUES ('youtube', 'accepted', 'accept');
            INSERT INTO contexts VALUES ('youtube', 'rejected', 'reject');
            INSERT INTO comments VALUES ('youtube', 'accepted');
            INSERT INTO comments VALUES ('youtube', 'rejected');
            INSERT INTO comments VALUES ('youtube', 'missing');
            """
        )

        self.assertEqual(purge_nonaccepted_comments(con), 2)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
