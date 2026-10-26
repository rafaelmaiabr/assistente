"""Direct SQL for tests that must look inside the database, on whichever backend the repository uses.

A test class mixes in ``SqlTools`` and has ``self.repo``; each call is its own short transaction, so nothing here depends on
SQLite file access (the same tests run on PostgreSQL through scripts/run_tests_postgres.py).
"""


class SqlTools:
    def rows(self, sql, args=()):
        with self.repo._transaction() as conn:
            return conn.execute(sql, tuple(args)).fetchall()

    def scalar(self, sql, args=()):
        return self.rows(sql, args)[0][0]

    def execute(self, *statements):
        """Run statements (``sql`` or ``(sql, args)``) in one transaction; returns the last row count."""
        count = 0
        with self.repo._transaction() as conn:
            for statement in statements:
                sql, args = (statement, ()) if isinstance(statement, str) else statement
                count = conn.execute(sql, tuple(args)).rowcount
        return count

    def refuses(self, *statements, msg=None):
        """The database must refuse the statements (a trigger or constraint); the transaction is rolled back."""
        with self.assertRaises(self.repo.integrity_errors, msg=msg):
            self.execute(*statements)
