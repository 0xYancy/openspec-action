"""Store 自动化（observe / archive / store-check / shared）的单元测试。

运行：python3 -m unittest discover -s tests
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ROOT / ".github" / "actions"
sys.path.insert(0, str(ACTIONS / "shared"))

import store_lib  # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


write = load("observe_write", ACTIONS / "observe" / "write.py")
prepare = load("observe_prepare", ACTIONS / "observe" / "prepare.py")
archive = load("store_archive", ACTIONS / "archive" / "archive.py")
CHECK = ACTIONS / "store-check" / "check.py"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


def write_file(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


class WriteGuardTest(unittest.TestCase):
    def test_allows_only_changes_specs_and_wiki(self) -> None:
        self.assertTrue(store_lib.path_allowed("openspec/changes/x/.openspec.yaml"))
        self.assertTrue(store_lib.path_allowed("openspec/specs/cap/spec.md"))
        self.assertTrue(store_lib.path_allowed("wiki/published/page.md"))

    def test_rejects_protected_files(self) -> None:
        for path in (
            ".github/workflows/openspec-observe.yml",
            ".github/CODEOWNERS",
            ".openspec-store/store.yaml",
            "openspec/repos.yaml",
            "openspec/framework.lock",
            "openspec/schemas/store/schema.yaml",
            ".claude/skills/x/SKILL.md",
            "README.md",
            "openspec/changes/../../.github/x.yml",
        ):
            self.assertFalse(store_lib.path_allowed(path), path)


class MarkerTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        body = "正文\n" + store_lib.render_marker({"change": "a", "prs": ["o/r#1"]})
        self.assertEqual(store_lib.parse_marker(body), {"change": "a", "prs": ["o/r#1"]})

    def test_missing_or_broken_marker(self) -> None:
        self.assertIsNone(store_lib.parse_marker("没有标记"))
        self.assertIsNone(store_lib.parse_marker("<!-- openspec-obs: {broken -->"))


class ValidateAgentResultTest(unittest.TestCase):
    DATA = {
        "changes": [{"change": "early-redeem"}, {"change": "tvl"}],
        "merged": [
            {"pr": "o/dapp#1", "url": "u1", "title": "t1", "merged_at": "2026-10-20T00:00:00Z"},
            {"pr": "o/dapp#2", "url": "u2", "title": "t2", "merged_at": "2026-10-20T00:00:00Z"},
            {"pr": "o/dapp#3", "url": "u3", "title": "t3", "merged_at": "2026-10-20T00:00:00Z"},
        ],
        "rejected": [{"pr": "o/dapp#2", "change": "early-redeem"}],
    }

    def link(self, change: str, pr: str) -> dict:
        return {"change": change, "prs": [{"pr": pr, "basis": "b", "hint": False, "confidence": "中"}], "diffs": []}

    def test_rejected_pair_is_not_proposed_again(self) -> None:
        result, notes = write.validate({"links": [self.link("early-redeem", "o/dapp#2")], "unregistered": [], "no_draft": []}, self.DATA)
        self.assertEqual(result["links"], [])
        self.assertTrue(any("已被需求侧拒绝" in note for note in notes))

    def test_rejected_pr_can_link_to_another_change(self) -> None:
        result, _ = write.validate({"links": [self.link("tvl", "o/dapp#2")], "unregistered": [], "no_draft": []}, self.DATA)
        self.assertEqual([p["pr"] for p in result["links"][0]["prs"]], ["o/dapp#2"])

    def test_unknown_change_and_pr_are_dropped(self) -> None:
        result, notes = write.validate(
            {"links": [self.link("nope", "o/dapp#1"), self.link("tvl", "o/dapp#99")], "unregistered": [], "no_draft": []}, self.DATA
        )
        self.assertEqual(result["links"], [])
        self.assertEqual(len([n for n in notes if n.startswith("忽略")]), 2)

    def test_each_pr_classified_once(self) -> None:
        result, notes = write.validate(
            {
                "links": [self.link("tvl", "o/dapp#1")],
                "unregistered": [{"pr": "o/dapp#1", "behavior": "x", "suggestion": "y"}],
                "no_draft": [{"pr": "o/dapp#3", "reason": "依赖升级"}],
            },
            self.DATA,
        )
        self.assertEqual(len(result["links"]), 1)
        self.assertEqual(result["unregistered"], [])
        self.assertEqual([n["pr"] for n in result["no_draft"]], ["o/dapp#3"])
        self.assertTrue(any("o/dapp#2 未被 Agent 归类" in note for note in notes))

    def test_diffs_only_for_kept_prs(self) -> None:
        link = self.link("tvl", "o/dapp#1")
        link["diffs"] = [{"scenario": "AC-1", "pr": "o/dapp#1", "detail": "d"}, {"scenario": "AC-2", "pr": "o/dapp#9", "detail": "d"}]
        result, _ = write.validate({"links": [link], "unregistered": [], "no_draft": []}, self.DATA)
        self.assertEqual([d["scenario"] for d in result["links"][0]["diffs"]], ["AC-1"])


class PrefilterTest(unittest.TestCase):
    def test_technical_prs_skip_agent(self) -> None:
        self.assertIsNotNone(prepare.technical_reason({"user": {"login": "dependabot[bot]"}, "title": "Bump x"}, []))
        self.assertIsNotNone(prepare.technical_reason({"user": {"login": "dev"}, "title": "chore(deps): bump"}, []))
        self.assertIsNotNone(prepare.technical_reason({"user": {"login": "dev"}, "title": "x", "labels": [{"name": "dependencies"}]}, []))
        files = [{"filename": "package-lock.json"}, {"filename": "src/__tests__/a.test.ts"}]
        self.assertIsNotNone(prepare.technical_reason({"user": {"login": "dev"}, "title": "update"}, files))

    def test_product_change_goes_to_agent(self) -> None:
        files = [{"filename": "src/pages/redeem.tsx"}, {"filename": "package-lock.json"}]
        self.assertIsNone(prepare.technical_reason({"user": {"login": "dev"}, "title": "feat: early redeem"}, files))

    def test_hints_find_change_name_and_id(self) -> None:
        changes = [{"name": "early-redeem", "meta": {"ID": "20261021-aa01"}}, {"name": "tvl", "meta": {}}]
        hints = prepare.hints_for("实现 D-42，见 20261021-aa01", changes, "o/spec")
        self.assertEqual([h["change"] for h in hints], ["early-redeem"])
        self.assertEqual(prepare.hints_for("feat: something", changes, "o/spec"), [])


class DedupeTest(unittest.TestCase):
    def obs(self, number: int, state: str, merged: bool, labels: list[str], change: str | None, prs: list[str]) -> dict:
        return {"number": number, "state": state, "merged": merged, "labels": labels, "branch": f"obs/{number}",
                "url": f"u{number}", "marker": {"change": change, "prs": prs}}

    def test_handled_from_main_and_obs_prs(self) -> None:
        changes = [{"implementations": [{"pr": "o/dapp#1"}]}, {"implementations": []}]
        obs = [
            self.obs(10, "open", False, ["obs"], "tvl", ["o/dapp#2"]),
            self.obs(11, "closed", False, ["obs", "obs:rejected"], "early", ["o/dapp#3"]),
            self.obs(12, "closed", True, ["obs"], "early", ["o/dapp#4"]),
        ]
        handled, rejected, drafts = prepare.collect_handled(changes, obs)
        self.assertEqual(handled, {"o/dapp#1", "o/dapp#2", "o/dapp#3", "o/dapp#4"})
        self.assertEqual(rejected, [{"pr": "o/dapp#3", "change": "early"}])
        self.assertEqual(list(drafts), ["tvl"])
        self.assertEqual(drafts["tvl"]["branch"], "obs/10")


class StoreCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_file(self.root, "openspec/changes/ok/.openspec.yaml", "schema: store\nstatus: 已批准\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")
        git(self.root, "checkout", "-qb", "feat")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_check(self, author: str = "yancy") -> subprocess.CompletedProcess:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "change", "--allow-empty")
        return subprocess.run([sys.executable, str(CHECK), "--store", str(self.root), "--base-ref", "main", "--author", author], capture_output=True, text=True)

    def test_draft_cannot_merge(self) -> None:
        write_file(self.root, "openspec/changes/new/.openspec.yaml", "schema: store\nstatus: 草稿\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("草稿", result.stdout)

    def test_approved_passes(self) -> None:
        write_file(self.root, "openspec/changes/new/.openspec.yaml", "schema: store\nstatus: 已批准\n")
        self.assertEqual(self.run_check().returncode, 0)

    def test_progress_field_rejected(self) -> None:
        write_file(self.root, "openspec/changes/new/.openspec.yaml", 'schema: store\nstatus: 已批准\nprogress: "1/2"\n')
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("progress", result.stdout)

    def test_bot_cannot_touch_protected_files(self) -> None:
        write_file(self.root, ".github/workflows/x.yml", "name: x\n")
        self.assertEqual(self.run_check("github-actions[bot]").returncode, 1)
        self.assertEqual(self.run_check("yancy").returncode, 0)


class ArchiveTest(unittest.TestCase):
    def test_delta_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "spec.md"
            spec.write_text(
                "## ADDED Requirements\n\n### Requirement: 新需求\nx\n\n## MODIFIED Requirements\n\n### Requirement: 旧需求\ny\n",
                encoding="utf-8",
            )
            self.assertEqual(archive.delta_summary(spec), ["- [新增] 需求：新需求", "- [修改] 需求：旧需求"])

    def test_fill_purpose_from_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "openspec/specs/cap-a/spec.md", "# cap-a\n\n## Purpose\nTBD - created by archiving change x. Update Purpose after archive.\n")
            write_file(root, "openspec/specs/cap-b/spec.md", "# cap-b\n\n## Purpose\nTBD - created by archiving change x. Update Purpose after archive.\n")
            proposal = "- **能力甲**（标识 `cap-a`）：用户可以提前赎回并支付手续费\n"
            missing = archive.fill_purposes(root, proposal, ["cap-a", "cap-b"])
            self.assertEqual(missing, ["cap-b"])
            self.assertIn("能力甲：用户可以提前赎回并支付手续费", (root / "openspec/specs/cap-a/spec.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
