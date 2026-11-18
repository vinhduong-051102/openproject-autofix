import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import be_api  # noqa: E402
import deploy_qa as d  # noqa: E402

J = "SOAR/src/main/java/com/cyai/soar/"
FILES = {
    J + "controller/ReportController.java": """
package x;
@RestController
@RequestMapping("/api/v1/reports")
public class ReportController {
  private final ReportFacadeService reportFacadeService;
  @PostMapping("/export-user-implement")
  public void exportUserImplementReport(@RequestBody UserImplementFilter request) { reportFacadeService.exportUser(request); }
  @GetMapping("/{id}")
  public void get(@PathVariable Long id, @RequestParam(name = "type") Integer type) { }
}""",
    J + "facade/ReportFacadeService.java": "package x;\npublic interface ReportFacadeService { void exportUser(UserImplementFilter r); }",
    J + "facade/impl/ReportFacadeServiceImpl.java": """
package x;
public class ReportFacadeServiceImpl implements ReportFacadeService {
  public void exportUser(UserImplementFilter r) {
    queryRows(r);
  }
  private void queryRows(UserImplementFilter r) {
    int a = 1;
  }
}""",
    J + "dto/UserImplementFilter.java": "package x;\npublic class UserImplementFilter { private Long id; }",
    J + "controller/OtherController.java": """
package x;
@RestController
@RequestMapping("/api/v1/other")
public class OtherController {
  @GetMapping("/ping")
  public String ping() { return ""; }
}""",
}


def git_repo(files):
    repo = tempfile.mkdtemp()
    for p, src in files.items():
        os.makedirs(os.path.join(repo, os.path.dirname(p)), exist_ok=True)
        with open(os.path.join(repo, p), "w", encoding="utf-8") as f:
            f.write(src)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(["git", "-C", repo] + c, check=True, env=env, stdout=subprocess.DEVNULL)
    return repo


class BeRelatedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = tempfile.mkdtemp()
        cls.p = mock.patch.object(be_api, "CACHE_DIR", cls.cache)
        cls.p.start()
        be_api._MEM.clear()
        cls.repo = git_repo(FILES)

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        be_api._MEM.clear()

    def paths(self, eps):
        return {"%s %s" % (e["method"], e["path"]) for e in eps}

    def test_impl_reaches_controller_via_interface(self):
        eps = self.paths(be_api.related_endpoints(self.repo, ["ReportFacadeServiceImpl"]))
        self.assertIn("POST /api/v1/reports/export-user-implement", eps)
        self.assertNotIn("GET /api/v1/other/ping", eps)

    def test_private_method_expands_to_public_caller(self):
        eps = be_api.related_endpoints(self.repo, ["ReportFacadeServiceImpl"], methods={"queryRows"})
        self.assertIn("POST /api/v1/reports/export-user-implement", self.paths(eps))

    def test_dto_direct(self):
        eps = self.paths(be_api.related_endpoints(self.repo, ["UserImplementFilter"]))
        self.assertIn("POST /api/v1/reports/export-user-implement", eps)

    def test_precheck(self):
        self.assertIn("phương thức PUT", be_api.precheck(self.repo, "PUT", "/api/v1/reports/export-user-implement", "{}"))
        self.assertIn("thiếu body", be_api.precheck(self.repo, "POST", "/api/v1/reports/export-user-implement", ""))
        self.assertIn("type", be_api.precheck(self.repo, "GET", "/api/v1/reports/5", ""))
        self.assertEqual(be_api.precheck(self.repo, "GET", "/api/v1/reports/5?type=1", ""), "")
        self.assertEqual(be_api.precheck(self.repo, "GET", "/api/v1/unknown/5", ""), "")  # không biết → cho gửi

    def test_changed_code_from_diff(self):
        diff = ("# BE abcdef1\ndiff --git a/%sfacade/impl/ReportFacadeServiceImpl.java b/x\n"
                "@@ -5,3 +5,3 @@ public class ReportFacadeServiceImpl implements ReportFacadeService {\n"
                "@@ -8,2 +8,2 @@   private void queryRows(UserImplementFilter r) {\n-    int a = 0;\n+    int a = 1;\n" % J)
        classes, methods = d.be_changed_code(diff)
        self.assertEqual(classes, {"ReportFacadeServiceImpl"})
        self.assertIn("queryRows", methods)


class PrematureTest(unittest.TestCase):
    TICKET = "# T\n**CÁC BƯỚC THỰC HIỆN:**\n1. Mở sự việc, chỉnh sửa người thực hiện rồi Lưu\n**KẾT QUẢ MONG ĐỢI:**\nx"
    EPS = [{"method": "PUT", "path": "/api/v1/cases/{id}", "controller": "C", "handler": "update"}]

    def run_(self, verdict, executed, ticket=None, eps=None):
        with mock.patch.object(d, "be_related_endpoints", return_value=self.EPS if eps is None else eps):
            return d.premature_conclusion(verdict, executed, ticket or self.TICKET, "", {})

    def test_no_write_is_rejected(self):
        msg = self.run_("ĐẠT", [("GET", "/api/v1/cases/5", "", "HTTP 200"), ("POST", "/api/v1/cases/filter", "{}", "HTTP 200")])
        self.assertIn("CHƯA thực hiện thao tác chính", msg)
        self.assertIn("PUT /api/v1/cases/{id}", msg)

    def test_write_on_related_endpoint_ok(self):
        self.assertEqual(self.run_("ĐẠT", [("PUT", "/api/v1/cases/5", "{}", "HTTP 200")]), "")

    def test_write_elsewhere_not_enough(self):
        self.assertTrue(self.run_("KHÔNG TỰ TEST ĐƯỢC", [("POST", "/api/v1/users", "{}", "HTTP 201")]))

    def test_blocked_request_not_counted(self):
        self.assertTrue(self.run_("ĐẠT", [("PUT", "/api/v1/cases/5", "{}", "CÔNG CỤ CHƯA GỬI request này")]))

    def test_read_ticket_or_fail_verdict_not_checked(self):
        read = "# T\n**CÁC BƯỚC THỰC HIỆN:**\n1. Tìm kiếm theo Mã, Người tạo, Ng tạo\n**KẾT QUẢ MONG ĐỢI:**\nx"
        self.assertEqual(self.run_("ĐẠT", [], ticket=read), "")
        self.assertEqual(self.run_("KHÔNG ĐẠT", []), "")

    def test_ui_click_counts_when_no_be(self):
        self.assertEqual(self.run_("ĐẠT", [("UI", "/", '[{"click": "#save"}]', "ok")], eps=[]), "")


class FeCallSitesTest(unittest.TestCase):
    def test_constant_to_call_site(self):
        fe = git_repo({
            "src/constants/ServicePaths.ts": 'export const SERVICE_PATHS = {\n  COMMON: {\n    GET_HISTORY: "/api/v1/histories",\n  },\n'
                                             '  HEALTHCHECK: {\n    GET_HISTORY: "/api/v1/hc",\n  },\n};\n',
            "src/apis/common.ts": "const A = {\n  list: (f) => get(SERVICE_PATHS.COMMON.GET_HISTORY + `?type=${f.type}`),\n};\n",
            "src/apis/hc.ts": "const B = {\n  h: () => get(SERVICE_PATHS.HEALTHCHECK.GET_HISTORY),\n};\n",
        })
        out = d.fe_call_sites({"_fe_repo": fe}, "/api/v1/histories?object_id=1")
        self.assertIn("type=${f.type}", out)
        self.assertNotIn("HEALTHCHECK", out)


if __name__ == "__main__":
    unittest.main()
