"""Store 自动化（observe / archive / store-check / shared）的单元测试。

运行：python3 -m unittest discover -s tests
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
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
llm = load("observe_llm", ACTIONS / "observe" / "llm.py")
archive = load("store_archive", ACTIONS / "archive" / "archive.py")
draft = load("observe_draft", ACTIONS / "observe" / "draft.py")
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
        self.assertTrue(any("o/dapp#2 未被模型归类" in note for note in notes))

    def test_pr_can_link_to_several_changes(self) -> None:
        result, notes = write.validate(
            {"links": [self.link("tvl", "o/dapp#1"), self.link("early-redeem", "o/dapp#1"), self.link("tvl", "o/dapp#1")],
             "unregistered": [], "no_draft": [{"pr": "o/dapp#1", "reason": "x"}]},
            self.DATA,
        )
        self.assertEqual(sorted(l["change"] for l in result["links"]), ["early-redeem", "tvl"])
        self.assertEqual([len(l["prs"]) for l in result["links"]], [1, 1])
        self.assertEqual(result["no_draft"], [])
        self.assertTrue(any("关联重复" in note for note in notes))

    def test_new_change_id_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            write_file(store, "openspec/changes/a/.openspec.yaml", "ID: 20261021-0001\n")
            now = dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc)
            used: set[str] = set()
            ids = [write.new_change_id(store, now, used) for _ in range(50)]
            self.assertEqual(len(set(ids)), 50)
            self.assertNotIn("20261021-0001", ids)
            self.assertTrue(all(i.startswith("20261021-") for i in ids))

    def test_unregistered_carries_merge_time(self) -> None:
        result, _ = write.validate(
            {"links": [], "unregistered": [{"pr": "o/dapp#3", "behavior": "x", "suggestion": "y"}], "no_draft": []}, self.DATA
        )
        self.assertEqual(result["unregistered"][0]["merged_at"], "2026-10-20T00:00:00Z")

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


class LlmBatchTest(unittest.TestCase):
    def test_split_units_by_count_and_size(self) -> None:
        units = [{"id": i, "body": "x" * 15000} for i in range(10)]
        self.assertEqual([len(b) for b in llm.split_units(units)], [3, 3, 3, 1])
        self.assertEqual(llm.split_units([]), [[]])

    def test_merge_and_settle_archived(self) -> None:
        merged = llm.merge_results([
            {"links": [{"change": "a", "prs": [{"pr": "p1", "basis": "x"}], "diffs": []}], "unregistered": [], "no_draft": []},
            {"links": [{"change": "a", "prs": [{"pr": "p2", "basis": "y"}], "diffs": [{"scenario": "s"}]},
                       {"change": "2026-10-15-old", "prs": [{"pr": "p3", "basis": "z"}], "diffs": []}],
             "unregistered": [{"pr": "p4"}], "no_draft": []},
        ])
        result = llm.settle_archived(merged, {"2026-10-15-old"})
        self.assertEqual([(l["change"], [p["pr"] for p in l["prs"]]) for l in result["links"]], [("a", ["p1", "p2"])])
        self.assertEqual(result["no_draft"], [{"pr": "p3", "reason": "已归档 change `2026-10-15-old` 已覆盖：z"}])

    def test_relevant_changes(self) -> None:
        changes = [{"change": "a", "repositories": ["o/vault"]}, {"change": "b", "repositories": ["o/dapp"]},
                   {"change": "c", "repositories": []}, {"change": "d", "repositories": ["o/dapp"]},
                   {"change": "e", "repositories": [], "archived": True}]
        units = [{"pr": "o/vault@1..2", "hints": []}, {"pr": "o/svc#3", "hints": [{"change": "d"}]}]
        self.assertEqual([c["change"] for c in llm.relevant_changes(changes, units)], ["a", "c", "d"])

    def test_archived_on(self) -> None:
        self.assertEqual(str(prepare.archived_on("2026-10-15-brand")), "2026-10-15")
        self.assertEqual(prepare.archived_on("no-date"), prepare.dt.date.min)


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
            self.obs(13, "open", False, ["obs", "obs:archive"], "done", ["o/dapp#5"]),
            self.obs(14, "closed", False, ["obs", "obs:redo"], None, ["o/dapp#6"]),
        ]
        handled, rejected, drafts = prepare.collect_handled(changes, obs)
        self.assertEqual(handled, {"o/dapp#1", "o/dapp#2", "o/dapp#3", "o/dapp#4", "o/dapp#5"})
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
            write_file(root, "openspec/specs/cap-c/spec.md", "# cap-c\n\n## Purpose\nTBD - created by archiving change x. Update Purpose after archive.\n")
            long_text = "用户可以在锁定期内提前赎回本金，按剩余天数支付手续费，手续费归入协议金库，赎回到账前份额被冻结且不再计息"
            proposal = (f"- **能力甲**（标识 `cap-a`）：{long_text}\n"
                        "- **能力丙**（标识 `cap-c`）：用户可以提前赎回并支付手续费\n")
            missing = archive.fill_purposes(root, proposal, ["cap-a", "cap-b", "cap-c"])
            self.assertEqual(missing, ["cap-b", "cap-c"])
            self.assertIn(f"能力甲：{long_text}", (root / "openspec/specs/cap-a/spec.md").read_text(encoding="utf-8"))
            self.assertIn("能力丙：用户可以提前赎回并支付手续费", (root / "openspec/specs/cap-c/spec.md").read_text(encoding="utf-8"))

    def test_pr_target(self) -> None:
        self.assertEqual(archive.pr_target(["openspec/changes/tvl/.openspec.yaml"]), {"change": "tvl"})
        self.assertEqual(archive.pr_target(["openspec/changes/obs-dapp-28/proposal.md", "openspec/changes/obs-dapp-28/specs/a/spec.md"]),
                         {"change": "obs-dapp-28"})
        self.assertEqual(archive.pr_target(["openspec/changes/archive/2026-10-24-tvl/.openspec.yaml", "openspec/specs/a/spec.md"]),
                         {"archived": ["2026-10-24-tvl"]})
        # 已在本 PR 归档：原目录以删除形式出现在 diff 中
        self.assertEqual(archive.pr_target(["openspec/changes/tvl/.openspec.yaml", "openspec/changes/archive/2026-10-24-tvl/.openspec.yaml"]),
                         {"archived": ["2026-10-24-tvl"]})
        self.assertIn("没有改动", archive.pr_target(["openspec/specs/a/spec.md"])["reason"])
        self.assertIn("`a`、`b`", archive.pr_target(["openspec/changes/a/x.md", "openspec/changes/b/y.md"])["reason"])

    def test_archive_on_pr_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-q", "-b", "main")
            write_file(root, "openspec/specs/cap/spec.md", "# cap Specification\n\n## Purpose\n" + "x" * 60 + "\n\n## Requirements\n")
            git(root, "add", "-A"); git(root, "commit", "-qm", "base")
            git(root, "checkout", "-qb", "obs/tvl")
            write_file(root, "openspec/changes/tvl/.openspec.yaml", "schema: store\ntitle: T\nstatus: 已实现\n")
            git(root, "add", "-A"); git(root, "commit", "-qm", "obs")
            out = subprocess.run([sys.executable, str(ACTIONS / "archive" / "archive.py"), "target", "--store", str(root), "--base", "main"],
                                 capture_output=True, text=True, check=True).stdout
            self.assertEqual(out.strip(), '{"change": "tvl"}')

    def test_wiki_uncovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(archive.wiki_uncovered(root, "o/s", ["cap-a"]), [])
            write_file(root, "wiki/structure.yaml", "pages:\n  - id: p\n    capabilities:\n      - o/s/cap-a\n      - o/s/cap-ab\n")
            self.assertEqual(archive.wiki_uncovered(root, "o/s", ["cap-a", "cap-b", "cap"]), ["cap-b", "cap"])

    def test_changelog_merges_uppercase_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "openspec/specs/cap-a/CHANGELOG.md", "# cap-a 变更记录\n\n## v1\n- 旧条目\n")
            archive.append_changelog(root, "cap-a", {"created": "2026-10-23", "ID": "x", "title": "T"}, "c", ["- [修改] 需求：R"])
            names = sorted(p.name for p in (root / "openspec/specs/cap-a").iterdir())
            self.assertEqual(names, ["changelog.md"])
            text = (root / "openspec/specs/cap-a/changelog.md").read_text(encoding="utf-8")
            self.assertIn("## 迁入前的变更记录", text)
            self.assertIn("- 旧条目", text)
            self.assertIn("- [修改] 需求：R", text)



class PatchDraftTest(unittest.TestCase):
    SPEC = textwrap.dedent("""\
        # cap Specification

        ## Purpose
        定义质押页的赎回流程，包括赎回申请、等待期与领取规则，供前端与合约侧共同遵循，并说明各类异常情况下用户能看到的提示与可执行的操作。

        ## Requirements

        ### Requirement: 赎回确认
        用户提交赎回时 SHALL 弹出二次确认。

        #### Scenario: 提交赎回
        - **当** 用户提交赎回
        - **则** 弹出二次确认
        """)

    def test_check_draft(self) -> None:
        specs = {"cap": self.SPEC}
        ok = {"deltas": [{"capability": "cap", "markdown": "## MODIFIED Requirements\n\n### Requirement: 赎回确认\nx SHALL y\n\n#### Scenario: 提交赎回\n- **当** a\n- **则** b\n"}]}
        self.assertEqual(draft.check_draft(ok, specs, set()), "")
        self.assertIn("不存在", draft.check_draft({"deltas": [{"capability": "cap", "markdown": "## MODIFIED Requirements\n\n### Requirement: 无此需求\n"}]}, specs, set()))
        self.assertIn("ADDED / MODIFIED", draft.check_draft({"deltas": [{"capability": "cap", "markdown": "## REMOVED Requirements\n"}]}, specs, set()))
        self.assertIn("未选定", draft.check_draft({"deltas": [{"capability": "other", "markdown": "## ADDED Requirements\n"}]}, specs, set()))
        self.assertIn("Purpose", draft.check_draft({"deltas": [{"capability": "fresh", "purpose": "  ", "markdown": "## ADDED Requirements\n"}]}, specs, {"fresh"}))
        self.assertEqual(draft.check_draft({"deltas": [{"capability": "fresh", "purpose": "负责质押入口。", "markdown": "## ADDED Requirements\n"}]}, specs, {"fresh"}), "")
        self.assertIn("没有给出", draft.check_draft({"deltas": []}, specs, set()))
        dropped = {"deltas": [{"capability": "cap", "markdown": "## MODIFIED Requirements\n\n### Requirement: 赎回确认\nx SHALL y\n\n#### Scenario: 新场景\n- **当** a\n- **则** b\n"}]}
        self.assertIn("漏掉了原有场景：提交赎回", draft.check_draft(dropped, specs, set()))
        mixed = {"deltas": [{"capability": "cap", "markdown": "## ADDED Requirements\n\n### Requirement: 新需求\nx SHALL y\n\n## MODIFIED Requirements\n\n### Requirement: 赎回确认\nx SHALL y\n\n#### Scenario: 提交赎回\n- **当** a\n- **则** b\n"}]}
        self.assertEqual(draft.check_draft(mixed, specs, set()), "")

    def test_draft_retries_once_with_reason(self) -> None:
        bad = {"title": "t", "summary": "s", "deltas": [{"capability": "cap", "markdown": "## MODIFIED Requirements\n\n### Requirement: 赎回确认\nx SHALL y\n"}]}
        good = {"title": "t", "summary": "s", "deltas": [{"capability": "cap", "markdown": "## MODIFIED Requirements\n\n### Requirement: 赎回确认\nx SHALL y\n\n#### Scenario: 提交赎回\n- **当** a\n- **则** b\n"}]}
        replies = iter([{"capabilities": [{"name": "cap"}]}, bad, good])
        sent = []
        original = draft.call
        draft.call = lambda messages, key, model: (sent.append(messages), json.dumps(next(replies), ensure_ascii=False))[1]
        try:
            result = draft.draft_one({"behavior": "b"}, {"pr": "o/d#1"}, {"cap": self.SPEC}, "k", "m")
        finally:
            draft.call = original
        self.assertNotIn("error", result)
        self.assertIn("漏掉了原有场景", sent[-1][-1]["content"])

    def test_draft_widens_to_unselected_capabilities(self) -> None:
        other = self.SPEC.replace("cap Specification", "other Specification")
        purpose = "定义一个新的界面规范能力，说明设计系统中颜色、字号与间距的取值规则，以及各页面引用这些取值时需要遵守的约束。"
        added = "## ADDED Requirements\n\n### Requirement: R\nx SHALL y\n\n#### Scenario: S\n- **当** a\n- **则** b\n"
        bad = {"title": "t", "summary": "s", "deltas": [
            {"capability": "other", "markdown": "## MODIFIED Requirements\n\n### Requirement: 赎回确认\nx SHALL y\n"},
            {"capability": "fresh-cap", "purpose": purpose, "markdown": added}]}
        good = {"title": "t", "summary": "s", "deltas": [
            {"capability": "other", "markdown": PatchRollupTest.modified()},
            {"capability": "fresh-cap", "purpose": purpose, "markdown": added}]}
        replies = iter([{"capabilities": [{"name": "cap"}]}, bad, good])
        sent = []
        original = draft.call
        draft.call = lambda messages, key, model: (sent.append(messages), json.dumps(next(replies), ensure_ascii=False))[1]
        try:
            result = draft.draft_one({"behavior": "b"}, {"pr": "o/d#1"}, {"cap": self.SPEC, "other": other}, "k", "m")
        finally:
            draft.call = original
        self.assertNotIn("error", result)
        self.assertEqual([d["capability"] for d in result["deltas"]], ["other", "fresh-cap"])
        self.assertEqual(result["deltas"][1]["purpose"], purpose)
        self.assertIn("other Specification", sent[-1][-1]["content"])  # 重写时提供了新纳入能力的主 spec

    def test_merge_draft_updates_main_spec_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-q", "-b", "main")
            write_file(root, "openspec/specs/cap/spec.md", self.SPEC)
            git(root, "add", "-A"); git(root, "commit", "-qm", "base")
            item = {"pr": "o/dapp@abc1234..def5678", "url": "https://x", "title": "T", "behavior": "去掉二次确认"}
            plan = {"title": "赎回不再二次确认", "summary": "s", "deltas": [
                {"capability": "cap", "purpose": "", "markdown": textwrap.dedent("""\
                    ## MODIFIED Requirements

                    ### Requirement: 赎回确认
                    用户提交赎回时 SHALL 直接提交，MUST NOT 弹出二次确认。

                    #### Scenario: 提交赎回
                    - **当** 用户提交赎回
                    - **则** 直接提交，不弹出二次确认
                    """)},
                {"capability": "net-menu", "purpose": "定义移动端网络切换菜单的展示范围与交互规则，保证菜单在各种屏幕尺寸与横竖屏切换下都能完整显示且可以正常操作。",
                 "markdown": textwrap.dedent("""\
                    ## ADDED Requirements

                    ### Requirement: 菜单限制在视口内
                    移动端网络菜单 SHALL 完整显示在视口内。

                    #### Scenario: 小屏打开菜单
                    - **当** 用户在小屏设备打开网络菜单
                    - **则** 菜单完整显示在视口内
                    """)},
            ]}
            summaries, reason = write.merge_draft(root, item, plan, dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc))
            self.assertEqual(reason, "")
            self.assertEqual(sorted(summaries), ["cap", "net-menu"])
            self.assertIn("MUST NOT 弹出二次确认", (root / "openspec/specs/cap/spec.md").read_text(encoding="utf-8"))
            net = (root / "openspec/specs/net-menu/spec.md").read_text(encoding="utf-8")
            self.assertIn("定义移动端网络切换菜单", net)
            self.assertNotIn("TBD", net)
            self.assertIn("## 2026-10-25 · 补登 · o/dapp@abc1234..def5678", (root / "openspec/specs/cap/changelog.md").read_text(encoding="utf-8"))
            changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root, capture_output=True, text=True).stdout
            self.assertNotIn("openspec/changes", changed)

    def test_merge_draft_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-q", "-b", "main")
            write_file(root, "openspec/specs/cap/spec.md", self.SPEC)
            git(root, "add", "-A"); git(root, "commit", "-qm", "base")
            item = {"pr": "o/dapp#9", "url": "https://x", "title": "T", "behavior": "b"}
            bad = {"deltas": [{"capability": "cap", "purpose": "", "markdown": "## MODIFIED Requirements\n\n### Requirement: 赎回确认\n没有关键词也没有场景\n"}]}
            _, reason = write.merge_draft(root, item, bad, dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc))
            self.assertTrue(reason)
            self.assertFalse((root / "openspec/changes/obs-patch-9").exists())


class StrictSpecTest(unittest.TestCase):
    """Purpose 偏短只提醒；为空、占位或其他严格问题照常失败。需要本机有 openspec CLI。"""

    def run_case(self, purpose: str, scenario: bool = True) -> tuple[bool, list[str], list[str]]:
        import strict_spec
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "openspec/config.yaml", "schema: spec-driven\n")
            body = "\n#### Scenario: 打开页面\n- **WHEN** 打开\n- **THEN** 展示\n" if scenario else ""
            write_file(root, "openspec/specs/demo/spec.md",
                       f"# demo\n\n## Purpose\n{purpose}\n\n## Requirements\n\n### Requirement: 展示仓位\n系统 SHALL 展示仓位。\n{body}")
            return strict_spec.check("demo", tmp)

    def test_brief_purpose_is_note(self) -> None:
        ok, failures, notes = self.run_case("负责质押入口与仓位展示。")
        self.assertTrue(ok, failures)
        self.assertEqual(len(notes), 1)

    def test_empty_placeholder_and_other_warnings_fail(self) -> None:
        self.assertFalse(self.run_case("")[0])
        self.assertFalse(self.run_case("TBD")[0])
        self.assertFalse(self.run_case("负责质押入口与仓位展示。", scenario=False)[0])


class PatchRollupTest(unittest.TestCase):
    SPEC = PatchDraftTest.SPEC

    @staticmethod
    def modified(*scenarios: str) -> str:
        body = "".join(f"\n#### Scenario: {name}\n- **当** a\n- **则** b\n" for name in ("提交赎回", *scenarios))
        return "## MODIFIED Requirements\n\n### Requirement: 赎回确认\n用户提交赎回时 SHALL 弹出二次确认。\n" + body

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.remote = base / "remote.git"
        git(base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.root = base / "store"
        git(base, "clone", "-q", str(self.remote), str(self.root))
        git(self.root, "config", "user.name", "t"); git(self.root, "config", "user.email", "t@t")
        write_file(self.root, "openspec/specs/cap/spec.md", self.SPEC)
        git(self.root, "add", "-A"); git(self.root, "commit", "-qm", "base"); git(self.root, "push", "-q", "origin", "main")
        self.gh_calls: list[tuple] = []
        self.original = (write.run, write.draft_one)
        real_run = write.run

        def fake_run(store, *args, check=True, input_text=None):
            if args[0] == "gh":
                self.gh_calls.append((args, input_text))
                if args[1:3] == ("pr", "view"):
                    return json.dumps({"body": "## 补登汇总\n旧内容\n<!-- openspec-obs: {\"change\": null, \"prs\": [\"o/d#1\"]} -->", "isDraft": False})
                return "https://github.com/o/s/pull/9" if args[1:3] == ("pr", "create") else ""
            return real_run(store, *args, check=check, input_text=input_text)

        write.run = fake_run
        os.environ["DEEPSEEK_API_KEY"] = "k"

    def tearDown(self) -> None:
        write.run, write.draft_one = self.original
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self.tmp.cleanup()

    def item(self, ref: str) -> dict:
        return {"pr": ref, "url": "https://x", "title": ref, "behavior": "b"}

    def test_sequential_units_and_append(self) -> None:
        seen = []

        def fake_draft(item, unit, specs, key, model):
            seen.append(specs["cap"])
            scenarios = {"o/d#1": ("场景甲",), "o/d#2": ("场景甲", "场景乙"), "o/d#3": ("场景甲", "场景乙", "场景丙")}[item["pr"]]
            return {"title": item["pr"], "summary": "s", "deltas": [{"capability": "cap", "purpose": "", "markdown": self.modified(*scenarios)}]}

        write.draft_one = fake_draft
        args = type("A", (), {"base": "main"})()
        now = dt.datetime(2026, 10, 25, 8, 0, tzinfo=dt.timezone.utc)
        lines, fallbacks = write.apply_patch_rollup(self.root, args, [self.item("o/d#1"), self.item("o/d#2")], {}, None, now)
        self.assertEqual(fallbacks, [])
        self.assertEqual(len(lines), 2)
        self.assertIn("场景甲", seen[1])  # 第二个单元基于第一个补登后的主 spec 起草
        create = [c for c in self.gh_calls if c[0][1:3] == ("pr", "create")][0]
        self.assertIn("obs:patch", create[0])
        self.assertIn('"prs": ["o/d#1", "o/d#2"]', create[1])

        # 主分支前进后追加：汇总分支重放到最新主分支，新单元追加到原 PR
        git(self.root, "checkout", "-q", "main")
        write_file(self.root, "openspec/specs/other/spec.md", "x")
        git(self.root, "add", "-A"); git(self.root, "commit", "-qm", "main moves"); git(self.root, "push", "-q", "origin", "main")
        self.gh_calls.clear()
        patch_pr = {"number": 9, "url": "u9", "branch": "obs/patch-202610250800", "prs": ["o/d#1", "o/d#2"]}
        lines, _ = write.apply_patch_rollup(self.root, args, [self.item("o/d#3")], {}, patch_pr, now)
        self.assertEqual(len(lines), 1)
        spec = (self.root / "openspec/specs/cap/spec.md").read_text(encoding="utf-8")
        for name in ("场景甲", "场景乙", "场景丙"):
            self.assertIn(name, spec)
        self.assertTrue((self.root / "openspec/specs/other/spec.md").is_file())
        edit = [c for c in self.gh_calls if c[0][1:3] == ("pr", "edit")][0]
        self.assertIn('"prs": ["o/d#1", "o/d#2", "o/d#3"]', edit[1])
        self.assertEqual(edit[1].count("openspec-obs"), 1)
        self.assertTrue(any(c[0][1:4] == ("pr", "ready", "9") for c in self.gh_calls))
        log = subprocess.run(["git", "log", "--format=%s", "origin/main..HEAD"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertEqual(log.count("obs: 补登"), 3)

    def test_failed_unit_falls_back(self) -> None:
        write.draft_one = lambda item, unit, specs, key, model: {"error": "模型没有选出需要更新的能力"}
        args = type("A", (), {"base": "main"})()
        lines, fallbacks = write.apply_patch_rollup(self.root, args, [self.item("o/d#1")], {}, None,
                                                    dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc))
        self.assertEqual(lines, [])
        self.assertEqual([f[1] for f in fallbacks], ["模型没有选出需要更新的能力"])
        self.assertEqual(self.gh_calls, [])


class RestoreEntryKeysTest(unittest.TestCase):
    def test_restores_changed_keys_by_title(self) -> None:
        restore = load("restore_entry_keys", ACTIONS / "wiki-sync" / "restore_entry_keys.py").restore
        skeleton = "# P\n\n## V2-a\n<!-- entry: 0:o/s:2 -->\n\n待撰写\n\n## V1-b\n<!-- entry: 0:o/s:1 -->\n"
        page = "# P\n\n## V2-a\n<!-- entry: 0:o/s:9 -->\n\n正文\n\n## V1-b\n<!-- entry: 0:o/s:1 -->\n\n## V0-x\n<!-- entry: 0:o/s:0 -->\n"
        text, fixed = restore(skeleton, page)
        self.assertEqual(fixed, 1)
        self.assertIn("## V2-a\n<!-- entry: 0:o/s:2 -->\n\n正文", text)
        self.assertIn("## V0-x\n<!-- entry: 0:o/s:0 -->", text)

    def test_restores_changed_source_line(self) -> None:
        restore = load("restore_entry_keys", ACTIONS / "wiki-sync" / "restore_entry_keys.py").restore
        skeleton = "## V1-a\n<!-- entry: 0:o/s:1 -->\n\n待撰写\n\n来源：[[change:o/s/1]]\n"
        page = "## V1-a\n<!-- entry: 0:o/s:1 -->\n\n正文\n\n来源：[[change:o/s:1]]\n"
        text, fixed = restore(skeleton, page)
        self.assertEqual(fixed, 1)
        self.assertIn("来源：[[change:o/s/1]]", text)
        overview_skeleton = "## 简介\n\n来源：[[o/s/cap#旧]]\n"
        self.assertEqual(restore(overview_skeleton, "## 简介\n\n来源：[[o/s/cap#新]]\n")[1], 0)


class CommitBatchTest(unittest.TestCase):
    """直接推送的提交：同一天内连续的直接推送合成一个实现单元，PR 的提交切断连续。"""

    @staticmethod
    def commit(sha: str, when: str, parents: int = 1, message: str = "feat: x") -> dict:
        return {"sha": sha * 7, "commit": {"committer": {"date": when}, "author": {"name": "dev"}, "message": message},
                "parents": [{"sha": "p"}] * parents, "author": {"login": "dev"}}

    def test_batches(self) -> None:
        c = self.commit
        commits = [
            c("a", "2026-10-20T02:00:00Z"), c("b", "2026-10-20T03:00:00Z"),
            c("p", "2026-10-20T04:00:00Z"),            # 属于已合并 PR
            c("d", "2026-10-20T05:00:00Z"),
            c("e", "2026-10-20T17:00:00Z"),            # 北京时间已是次日
        ]
        batches = prepare.commit_batches(commits, {"p" * 7})
        self.assertEqual([[x["sha"][0] for x in b] for b in batches], [["a", "b"], ["d"], ["e"]])
        self.assertEqual(prepare.batch_ref("o/r", batches[0]), "o/r@aaaaaaa..bbbbbbb")
        self.assertEqual(prepare.batch_ref("o/r", batches[1]), "o/r@ddddddd")

    def test_technical(self) -> None:
        c = self.commit
        self.assertEqual(prepare.batch_technical([c("a", "2026-10-20T02:00:00Z", message="chore: bump")], []), "提交说明均为技术类前缀")
        merge_only_tech = [c("a", "2026-10-20T02:00:00Z", parents=2, message="chore: merge feat/x"), c("b", "2026-10-20T03:00:00Z")]
        self.assertIsNone(prepare.batch_technical(merge_only_tech, [{"filename": "src/app.ts"}]))

    def test_ref_helpers(self) -> None:
        self.assertEqual(write.ref_repo("o/r@abc1234..def5678"), "o/r")
        self.assertEqual(write.ref_suffix("o/r@abc1234..def5678"), "def5678")
        self.assertEqual(write.ref_repo("o/r#12"), "o/r")
        self.assertEqual(write.ref_suffix("o/r#12"), "12")


if __name__ == "__main__":
    unittest.main()
