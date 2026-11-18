import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as d  # noqa: E402

BAD = 'HTTP 400\nContent-Type: application/json\n\n{"status":400,"data":{"code":"CODE_INVALIDATE","detail":"Mã không hợp lệ"}}'
GEN = 'HTTP 200\nContent-Type: application/json\n\n{"status":200,"message":"success","data":"CA12345678"}'
OK = 'HTTP 201\nContent-Type: application/json\n\n{"status":201,"data":{"id":9}}'


class RegenCodeTest(unittest.TestCase):
    def run_call(self, call, responses):
        sent = []

        def fake_run(cmd, **kw):
            sent.append(cmd)
            return mock.Mock(stdout=responses.pop(0))
        with mock.patch.object(d.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(d, "clean_output", side_effect=lambda x: x), \
                mock.patch.object(d, "be_api_precheck", return_value=""), \
                mock.patch.object(d, "be_api_hint", return_value=""):
            out = d.soar_call(tempfile.mkdtemp(), call, {"server_retry_minutes": 1})
        return out, sent

    def test_retries_with_generated_code(self):
        call = {"method": "POST", "target": "/api/v1/cases", "body": json.dumps({"name": "QA_AUTO_x", "code": "QA_1"})}
        out, sent = self.run_call(call, [BAD, GEN, OK])
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1][-1], "/api/v1/cases/gen-code")
        self.assertEqual(json.loads(sent[2][-1])["code"], "CA12345678")
        self.assertIn("HTTP 201", out)
        self.assertIn("gen-code", out)

    def test_intentional_invalid_not_retried(self):
        call = {"method": "POST", "target": "/api/v1/cases", "intentional_invalid": True,
                "body": json.dumps({"code": "bad"})}
        out, sent = self.run_call(call, [BAD])
        self.assertEqual(len(sent), 1)
        self.assertIn("CODE_INVALIDATE", out)

    def test_no_gen_code_endpoint_keeps_original(self):
        call = {"method": "POST", "target": "/api/v1/things", "body": json.dumps({"code": "x"})}
        out, sent = self.run_call(call, [BAD, 'HTTP 404\n\n{"status":404}'])
        self.assertEqual(len(sent), 2)
        self.assertIn("CODE_INVALIDATE", out)


if __name__ == "__main__":
    unittest.main()
