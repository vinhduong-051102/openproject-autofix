import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa  # noqa: E402

DCFG = {"qa_db_container": "postgres_soar", "qa_db_name": "soar_ss", "qa_restore_tables": ["general_configuration"]}


def row(**kw):
    base = {"id": 1, "password_expired_time": 5184000, "time_lock": 60, "admin_ip_whitelist": "1.2.3.4"}
    base.update(kw)
    return base


class FakeDb:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if "json_agg" in cmd:
            return json.dumps(self.rows) + "\n"
        return "UPDATE 1\n"


class ConfigRestoreTest(unittest.TestCase):
    def test_restores_only_changed_columns(self):
        before = {"general_configuration": {"1": row(), "2": row(id=2)}}
        after = {"general_configuration": {"1": row(password_expired_time=1), "2": row(id=2),
                                           "9": row(id=9)}}
        sql, changes = deploy_qa.config_restore_sql(before, after)
        self.assertEqual(changes, [("general_configuration", "1", "password_expired_time", 5184000, 1)])
        self.assertIn("set password_expired_time = r.password_expired_time", sql)
        self.assertIn('"password_expired_time": 5184000', sql)
        self.assertNotIn("time_lock", sql)

    def test_no_change_no_sql(self):
        before = {"general_configuration": {"1": row()}}
        self.assertEqual(deploy_qa.config_restore_sql(before, before), ("", []))

    def test_restore_runs_update_after_qa_changed_config(self):
        before = deploy_qa.config_snapshot(DCFG, FakeDb([row()]))
        db = FakeDb([row(password_expired_time=1)])
        changes = deploy_qa.restore_config(DCFG, before, "SOAR-1", "x", "", run=db)
        self.assertEqual(len(changes), 1)
        self.assertTrue(any("begin;" in c and "update general_configuration" in c for c in db.calls))

    def test_snapshot_error_skips_restore(self):
        def boom(cmd):
            raise RuntimeError("ssh down")
        self.assertIsNone(deploy_qa.config_snapshot(DCFG, boom))
        self.assertEqual(deploy_qa.restore_config(DCFG, None, "SOAR-1", "x", ""), [])


if __name__ == "__main__":
    unittest.main()
