import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import be_api  # noqa: E402

CONTROLLER = '''package x;
@RestController
@RequestMapping("/api/v1/cases")
public class CaseController {
  @PutMapping("/{id}")
  public ResponseGeneral<Void> update(@RequestBody @Valid CaseRequest request, @PathVariable Long id) { return null; }

  /** đổi người thực hiện */
  @PutMapping("/swap-user/{id}")
  @PreAuthorize("hasAuthority('CASE_UPDATE')")
  public ResponseGeneral<Void> swapUserImplement(
        @RequestBody @Valid SwapUserImplement request,
        @PathVariable Long id,
        @RequestHeader(name = LANGUAGE, defaultValue = DEFAULT_LANGUAGE) String language) { return null; }

  @PutMapping(value = "/swap-solution-batch")
  public ResponseGeneral<Void> swapSolutionBatch(@RequestBody List<Long> ids) { return null; }

  @GetMapping
  public Object list(CaseSearch search, @RequestParam(name = "lang", required = false) String lang, Pageable pageable) { return null; }
}
'''
DTO_SWAP = '''package x;
@Data
@JsonNaming(PropertyNamingStrategies.SnakeCaseStrategy.class)
public class SwapUserImplement {
  private Long userId;

  @NotNull(message = "Department cannot be empty")
  private Long departmentId;

  @JsonProperty("ly_do")
  private String reason;

  private static final long serialVersionUID = 1L;
  private Kind kind;
}
'''
DTO_CASE = '''package x;
public class CaseRequest {
  @NotBlank(message = "Case code must not be empty")
  private String code;
}
'''
DTO_SEARCH = '''package x;
public class CaseSearch {
  private Integer objectType;
  private int page;
}
'''
SLACK = '''package x;
@RestController
@RequestMapping("/api/slack")
public class SlackController {
  @PostMapping("/dm")
  public Object dm() { return null; }
}
'''
WARN = '''package x;
@RestController
@RequestMapping("/api/v1/sla-warnings")
public class SLAWarningController {
  @PostMapping("/filter")
  public Object filter() { return null; }
}
'''
ENUM = '''package x;
public enum Kind { A, B("b"), C; Kind() {} Kind(String s) {} }
'''


class BeApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["OP_WATCH_CACHE_DIR"] = cls.tmp.name + "/cache"
        be_api.CACHE_DIR = cls.tmp.name + "/cache"
        repo = cls.repo = cls.tmp.name + "/be"
        base = os.path.join(repo, "SOAR/src/main/java/x")
        os.makedirs(base)
        for name, src in (("CaseController", CONTROLLER), ("SwapUserImplement", DTO_SWAP), ("CaseRequest", DTO_CASE),
                          ("CaseSearch", DTO_SEARCH), ("Kind", ENUM), ("SlackController", SLACK),
                          ("SLAWarningController", WARN)):
            with open(os.path.join(base, name + ".java"), "w") as f:
                f.write(src)
        run = lambda *a: subprocess.run(["git", "-C", repo] + list(a), check=True, stdout=subprocess.DEVNULL)
        run("init", "-q")
        run("add", ".")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
        be_api._MEM.clear()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_swap_user_signature(self):
        out = be_api.lookup(self.repo, "swap-user")
        self.assertIn("PUT /api/v1/cases/swap-user/{id}", out)
        self.assertIn('"user_id": Long', out)
        self.assertIn('"department_id": Long — NotNull «Department cannot be empty»', out)
        self.assertIn('"ly_do": String', out)          # @JsonProperty thắng @JsonNaming
        self.assertIn("enum: A, B, C", out)
        self.assertNotIn("serial_version_uid", out)    # bỏ trường static
        self.assertNotIn("language", out)              # bỏ @RequestHeader

    def test_missing_id_prefers_swap_user_over_update(self):
        found = be_api.Index(self.repo).match("PUT /api/v1/cases/swap-user")
        self.assertEqual(found[0]["path"], "/api/v1/cases/swap-user/{id}")

    def test_concrete_id_matches(self):
        found = be_api.Index(self.repo).match("PUT /api/v1/cases/swap-user/1553")
        self.assertEqual(found[0]["path"], "/api/v1/cases/swap-user/{id}")
        found = be_api.Index(self.repo).match("PUT /api/v1/cases/1553")
        self.assertEqual(found[0]["handler"], "update")

    def test_hint_405_wrong_method(self):
        hint = be_api.hint_for(self.repo, "POST", "/api/v1/cases/swap-solution-batch", 405)
        self.assertIn("KHÔNG nhận POST", hint)
        self.assertIn("PUT /api/v1/cases/swap-solution-batch", hint)
        self.assertIn("mảng [Long", hint)

    def test_query_dto_and_dto_lookup(self):
        out = be_api.lookup(self.repo, "GET /api/v1/cases")
        self.assertIn("objectType: Integer", out)
        self.assertIn("query lang: String", out)
        self.assertNotIn("pageable", out.lower())
        self.assertIn("DTO SwapUserImplement", be_api.lookup(self.repo, "SwapUserImplement"))

    def test_short_keyword_matches_segment_not_substring(self):
        out = be_api.lookup(self.repo, "sla")
        self.assertIn("/api/v1/sla-warnings/filter", out)
        self.assertNotIn("/api/slack", out)

    def test_controller_name_lists_endpoints(self):
        out = be_api.lookup(self.repo, "SLAWarningController")
        self.assertIn("POST /api/v1/sla-warnings/filter", out)
        self.assertNotIn("DTO", out)

    def test_hint_ignores_non_api(self):
        self.assertEqual(be_api.hint_for(self.repo, "GET", "https://fe/assets/x.js", 404), "")


if __name__ == "__main__":
    unittest.main()
