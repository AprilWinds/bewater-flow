"""bewater-flow 核心逻辑单元测试。

用 BEWATER_BASE 环境变量把运行时数据隔离到临时目录，避免污染真实 workspace。
运行: python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import tempfile
import unittest

# 在导入 scripts 之前设置 BEWATER_BASE，使所有 IO 指向临时目录。
_TMP_BASE = tempfile.mkdtemp(prefix="bewater-test-")
os.environ["BEWATER_BASE"] = os.path.join(_TMP_BASE, ".bewater")
os.environ["BEWATER_ROOT"] = _TMP_BASE

from scripts import state, tasks, orchestrate, REQUIREMENTS_DIR  # noqa: E402


def _entry(tid, depends=None, criteria=None):
    """构造一个最小合法 task 条目。"""
    return {
        "task_id": tid,
        "name": tid,
        "depends": depends or [],
        "files": [],
        "signatures": [],
        "steps": [],
        "criteria": criteria or [{"id": 1, "text": "c1"}],
    }


def _plan(name, entries):
    """快捷：写入规划。"""
    tasks.init(name, entries)


class TestValidateName(unittest.TestCase):
    def test_valid_name(self):
        state.validate_name("demo")
        state.validate_name("需求-1")

    def test_empty(self):
        for bad in ["", None, 123]:
            with self.assertRaises(ValueError):
                state.validate_name(bad)

    def test_dot_and_dotdot(self):
        for bad in [".", ".."]:
            with self.assertRaises(ValueError):
                state.validate_name(bad)

    def test_path_separators(self):
        for bad in ["a/b", "a\\b", "/abs", "a b", "a\tb", "a\nb"]:
            with self.assertRaises(ValueError):
                state.validate_name(bad)

    def test_control_chars(self):
        with self.assertRaises(ValueError):
            state.validate_name("a\x00b")


class TestCurrentStage(unittest.TestCase):
    """state.current_stage 的状态组合矩阵——编排引擎的核心调度依据。"""

    def test_empty_is_plan(self):
        self.assertEqual(state.current_stage({}), "plan")
        self.assertEqual(state.current_stage(None), "plan")

    def test_pending_is_tdd(self):
        st = {"T1": {"status": "pending"}}
        self.assertEqual(state.current_stage(st), "tdd")

    def test_implemented_is_review(self):
        st = {"T1": {"status": "implemented"}}
        self.assertEqual(state.current_stage(st), "review")

    def test_rework_beats_implemented(self):
        st = {"T1": {"status": "implemented"}, "T2": {"status": "rework"}}
        self.assertEqual(state.current_stage(st), "tdd")

    def test_all_verified_is_finished(self):
        # S4: 全 verified 直接 finished
        st = {"T1": {"status": "verified"}, "T2": {"status": "verified"}}
        self.assertEqual(state.current_stage(st), "finished")

    def test_verified_plus_blocked_is_blocked(self):
        # 全部结束但有一个 blocked：唯一剩余工作需人工
        st = {"T1": {"status": "verified"}, "T2": {"status": "blocked"}}
        self.assertEqual(state.current_stage(st), "blocked")

    def test_all_blocked_is_blocked(self):
        st = {"T1": {"status": "blocked"}, "T2": {"status": "blocked"}}
        self.assertEqual(state.current_stage(st), "blocked")


class TestAllowedTransitions(unittest.TestCase):
    """update 的 ALLOWED_TRANSITIONS 流转校验。"""

    def setUp(self):
        _plan("t", [_entry("T1")])

    def test_pending_to_implemented(self):
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})

    def test_implemented_to_verified(self):
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "verified", "executor": "bewater-reviewer"})

    def test_implemented_to_rework_requires_issue(self):
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        with self.assertRaises(ValueError):
            state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer"})

    def test_illegal_skip_pending_to_verified(self):
        with self.assertRaises(ValueError):
            state.update("t", {"task_id": "T1", "status": "verified", "executor": "x"})

    def test_verified_is_terminal(self):
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "verified", "executor": "bewater-reviewer"})
        for bad in ["pending", "implemented", "rework", "blocked"]:
            with self.assertRaises(ValueError):
                state.update("t", {"task_id": "T1", "status": bad, "executor": "bewater-tdd"})

    def test_rework_to_implemented_directly(self):
        # rework 修复后直接上报 implemented，不经 pending
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "x", "fix": "y"}})
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})


class TestIssueCriteriaValidation(unittest.TestCase):
    """issue.criteria 必须命中 tasks.json 中真实验收标准 id。

    校验归属 tasks 模块（持有 criteria 元数据），cli 在 state.update 前预检。
    """

    def test_valid_criteria_id(self):
        _plan("t", [_entry("T1", criteria=[{"id": 1, "text": "c1"}, {"id": 2, "text": "c2"}])])
        tasks.validate_issue_criteria("t", "T1", {"criteria": 2, "cause": "x", "fix": "y"})

    def test_invalid_criteria_id_rejected(self):
        _plan("t", [_entry("T1", criteria=[{"id": 1, "text": "c1"}])])
        with self.assertRaises(ValueError):
            tasks.validate_issue_criteria("t", "T1", {"criteria": 99, "cause": "x", "fix": "y"})

    def test_question_mark_criteria_allowed(self):
        _plan("t", [_entry("T1")])
        tasks.validate_issue_criteria("t", "T1", {"criteria": "?", "cause": "x", "fix": "y"})

    def test_none_issue_passes(self):
        # 非 rework/blocked 状态 issue 为 None，校验直接放行。
        _plan("t", [_entry("T1")])
        tasks.validate_issue_criteria("t", "T1", None)


class TestCriterionIdType(unittest.TestCase):
    """B1: criterion id 必须为正整数——与 report 的 int 解析保持同类型，避免字符串 id 过 plan 却在 report 时被拒。"""

    def test_string_id_rejected_at_plan(self):
        with self.assertRaises(ValueError):
            tasks._validate_task_shapes([_entry("T1", criteria=[{"id": "1", "text": "c"}])])

    def test_bool_id_rejected(self):
        # bool 是 int 子类，须显式排除
        with self.assertRaises(ValueError):
            tasks._validate_task_shapes([_entry("T1", criteria=[{"id": True, "text": "c"}])])

    def test_zero_and_negative_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                tasks._validate_task_shapes([_entry("T1", criteria=[{"id": bad, "text": "c"}])])

    def test_positive_int_accepted(self):
        tasks._validate_task_shapes([_entry("T1", criteria=[{"id": 1, "text": "c"}])])


class TestIssueNonEmpty(unittest.TestCase):
    """B2: cause/fix 必须为非空字符串——不能只查 key 存在。"""

    def setUp(self):
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})

    def test_empty_cause_rejected(self):
        with self.assertRaises(ValueError):
            state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                               "issue": {"criteria": 1, "cause": "", "fix": "y"}})

    def test_whitespace_cause_rejected(self):
        with self.assertRaises(ValueError):
            state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                               "issue": {"criteria": 1, "cause": "   ", "fix": "y"}})

    def test_empty_fix_rejected(self):
        with self.assertRaises(ValueError):
            state.update("t", {"task_id": "T1", "status": "blocked", "executor": "bewater-tdd",
                               "issue": {"criteria": "?", "cause": "x", "fix": ""}})

    def test_non_string_cause_rejected(self):
        with self.assertRaises(ValueError):
            state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                               "issue": {"criteria": 1, "cause": None, "fix": "y"}})


class TestValidateDepends(unittest.TestCase):
    def test_duplicate_id(self):
        with self.assertRaises(ValueError):
            tasks._validate_depends([_entry("T1"), _entry("T1")])

    def test_self_dependency(self):
        with self.assertRaises(ValueError):
            tasks._validate_depends([_entry("T1", depends=["T1"])])

    def test_missing_dependency(self):
        with self.assertRaises(ValueError):
            tasks._validate_depends([_entry("T1", depends=["T99"])])

    def test_cycle_detected(self):
        entries = [_entry("T1", depends=["T2"]), _entry("T2", depends=["T1"])]
        with self.assertRaises(ValueError):
            tasks._validate_depends(entries)

    def test_long_chain_no_recursion_error(self):
        # 2000 个任务的线性链，原递归实现会触发 RecursionError
        entries = [_entry("T0")]
        for i in range(1, 2000):
            entries.append(_entry("T{}".format(i), depends=["T{}".format(i - 1)]))
        tasks._validate_depends(entries)  # 不应抛异常

    def test_depends_must_be_list(self):
        with self.assertRaises(ValueError):
            tasks._validate_depends([{"task_id": "T1", "depends": "T2", "files": [],
                                     "signatures": [], "steps": [], "criteria": []}])

    def test_empty_depends_element(self):
        with self.assertRaises(ValueError):
            tasks._validate_depends([_entry("T1", depends=[""])])

    def test_duplicate_depends_element_rejected(self):
        # 重复依赖元素会让编排层 _detect_cycle 入度重复计数、误报依赖环，须在 plan 源头拒绝。
        with self.assertRaises(ValueError):
            tasks._validate_depends([_entry("T1"), _entry("T2", depends=["T1", "T1"])])


class TestDepsSatisfied(unittest.TestCase):
    def test_no_deps_satisfied(self):
        self.assertTrue(state.deps_satisfied({}, {"T1": {"depends": []}}, "T1"))

    def test_dep_not_verified(self):
        ts = {"T1": {"status": "pending"}, "T2": {"status": "verified"}}
        meta = {"T1": {"depends": ["T2"]}}
        self.assertTrue(state.deps_satisfied(ts, meta, "T1"))

    def test_dep_pending_not_satisfied(self):
        ts = {"T1": {"status": "pending"}, "T2": {"status": "pending"}}
        meta = {"T1": {"depends": ["T2"]}}
        self.assertFalse(state.deps_satisfied(ts, meta, "T1"))

    def test_dep_implemented_not_sufficient(self):
        # implemented 仍可能被打回，不作为开工充分条件
        ts = {"T1": {"status": "pending"}, "T2": {"status": "implemented"}}
        meta = {"T1": {"depends": ["T2"]}}
        self.assertFalse(state.deps_satisfied(ts, meta, "T1"))

    def test_task_not_in_meta_returns_false(self):
        # fail-open 修复：meta 缺失时保守返回 False
        self.assertFalse(state.deps_satisfied({}, {}, "T1"))
        self.assertFalse(state.deps_satisfied({"T1": {"status": "pending"}}, {}, "T1"))


class TestNextAction(unittest.TestCase):
    def test_empty_state_not_ready(self):
        lines = orchestrate.next_action("empty")
        self.assertIn("@control", lines[0])
        self.assertIn("not_ready", lines[0])

    def test_tdd_dispatches_ready(self):
        _plan("t", [_entry("T1"), _entry("T2")])
        lines = orchestrate.next_action("t")
        joined = "\n".join(lines)
        self.assertIn("bewater-tdd", joined)
        # 串行派发：每轮只派一个，按 ID 取首个 T1
        self.assertIn("T1", joined)
        self.assertNotIn('"T2"', joined)

    def test_serial_dispatches_one_at_a_time(self):
        # 串行：10 个 ready 只派首个；T1 完成后下一轮才派 T2
        _plan("t", [_entry("T{}".format(i)) for i in range(10)])
        lines = orchestrate.next_action("t")
        agents = [l for l in lines if l.startswith("@agent")]
        self.assertEqual(len(agents), 1)
        self.assertIn("T0", agents[0])
        # T0 verified 后，下一轮派 T1
        state.update("t", {"task_id": "T0", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T0", "status": "verified", "executor": "bewater-reviewer"})
        lines = orchestrate.next_action("t")
        agents = [l for l in lines if l.startswith("@agent")]
        self.assertEqual(len(agents), 1)
        self.assertIn("T1", agents[0])

    def test_serial_review_one_at_a_time(self):
        # 串行审查：2 个 implemented 只审首个
        _plan("t", [_entry("T1"), _entry("T2")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T2", "status": "implemented", "executor": "bewater-tdd"})
        lines = orchestrate.next_action("t")
        agents = [l for l in lines if l.startswith("@agent")]
        self.assertEqual(len(agents), 1)
        self.assertIn("bewater-reviewer", agents[0])
        self.assertIn("T1", agents[0])
        self.assertNotIn("T2", agents[0])

    def test_blocked_dependency_not_dispatched(self):
        _plan("t", [_entry("T1"), _entry("T2", depends=["T1"])])
        lines = orchestrate.next_action("t")
        joined = "\n".join(lines)
        self.assertIn("T1", joined)
        self.assertNotIn('"T2"', joined)  # T2 依赖 T1 未 verified，不派发

    def test_blocked_takes_priority_single_line(self):
        # blocked 优先于同轮的 tdd/reviewer 派发，每轮严格一行：
        # T1 rework（本可派 tdd）+ T2 blocked 同存时，只输出 @human T2，
        # 不再追加 @agent T1。blocked 需人介入，冻住同轮派发。
        _plan("t", [_entry("T1"), _entry("T2")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "x", "fix": "y"}})
        state.update("t", {"task_id": "T2", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T2", "status": "blocked", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "blk", "fix": "decide"}})
        lines = orchestrate.next_action("t")
        self.assertEqual(len(lines), 1, "有 blocked 时每轮严格一行")
        self.assertIn("@human", lines[0])
        self.assertIn("T2", lines[0])
        self.assertNotIn("@agent", lines[0], "blocked 优先，同轮不派 tdd/reviewer")

    def test_review_dispatches_reviewer(self):
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        lines = orchestrate.next_action("t")
        self.assertIn("bewater-reviewer", "\n".join(lines))

    def test_all_verified_emits_finished(self):
        # S4: 全 verified 后 next 直接输出 finished。
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "verified", "executor": "bewater-reviewer"})
        lines = orchestrate.next_action("t")
        joined = "\n".join(lines)
        self.assertIn("@control", joined)
        self.assertIn("finished", joined)

    def test_cycle_deadlock_error(self):
        # 手动构造循环依赖（绕过 plan 校验）
        os.makedirs(os.path.join(REQUIREMENTS_DIR, "dl"), exist_ok=True)
        base = os.path.join(REQUIREMENTS_DIR, "dl")
        with open(os.path.join(base, "state.json"), "w") as f:
            json.dump({"tasks": {"T1": {"status": "pending", "executor": "bewater-orchestrate",
                                        "issue": None, "history": []},
                                 "T2": {"status": "pending", "executor": "bewater-orchestrate",
                                        "issue": None, "history": []}}}, f)
        with open(os.path.join(base, "tasks.json"), "w") as f:
            json.dump({"name": "dl", "tasks": [_entry("T1", depends=["T2"]), _entry("T2", depends=["T1"])]}, f)
        lines = orchestrate.next_action("dl")
        joined = "\n".join(lines)
        self.assertIn("error", joined)
        self.assertIn("deadlock", joined)

    def test_missing_tasks_meta_error(self):
        # state 有进度但 tasks.json 缺失 → 应输出 error（fail-open 修复）
        os.makedirs(os.path.join(REQUIREMENTS_DIR, "nometa"), exist_ok=True)
        base = os.path.join(REQUIREMENTS_DIR, "nometa")
        with open(os.path.join(base, "state.json"), "w") as f:
            json.dump({"tasks": {"T1": {"status": "pending", "executor": "bewater-orchestrate",
                                        "issue": None, "history": []}}}, f)
        # 不写 tasks.json
        lines = orchestrate.next_action("nometa")
        joined = "\n".join(lines)
        self.assertIn("error", joined)
        self.assertIn("tasks_meta_missing", joined)

    def test_duplicate_depends_not_misreported_as_cycle(self):
        # 运行时检测自鲁棒：tasks.json 被外部篡改出现重复依赖元素，
        # 不应因入度重复计数误报依赖环，而应正常派发。
        os.makedirs(os.path.join(REQUIREMENTS_DIR, "dup"), exist_ok=True)
        base = os.path.join(REQUIREMENTS_DIR, "dup")
        with open(os.path.join(base, "state.json"), "w") as f:
            json.dump({"tasks": {
                "T1": {"status": "verified", "executor": "bewater-orchestrate", "issue": None, "history": []},
                "T2": {"status": "pending", "executor": "bewater-orchestrate", "issue": None, "history": []}}}, f)
        with open(os.path.join(base, "tasks.json"), "w") as f:
            json.dump({"name": "dup", "tasks": [
                _entry("T1"), _entry("T2", depends=["T1", "T1"])]}, f)  # 重复依赖（绕过 plan 校验）
        lines = orchestrate.next_action("dup")
        joined = "\n".join(lines)
        self.assertIn("bewater-tdd", joined)  # T2 依赖 T1 已 verified，应派发
        self.assertNotIn("deadlock", joined)

    def test_meta_has_task_not_in_state_reports_mismatch(self):
        # 回归 #5：tasks.json 有 T2 但 state.json 没登记 T2（崩溃/外部编辑导致不一致）。
        # 旧实现：current_stage 只看 state 的 {verified} → 误判「全 verified」跳过 T2
        os.makedirs(os.path.join(REQUIREMENTS_DIR, "m1"), exist_ok=True)
        base = os.path.join(REQUIREMENTS_DIR, "m1")
        with open(os.path.join(base, "state.json"), "w") as f:
            json.dump({"tasks": {"T1": {"status": "verified", "executor": "bewater-reviewer",
                                        "issue": None, "history": []}}}, f)
        with open(os.path.join(base, "tasks.json"), "w") as f:
            json.dump({"name": "m1", "tasks": [_entry("T1"), _entry("T2")]}, f)
        lines = orchestrate.next_action("m1")
        joined = "\n".join(lines)
        self.assertIn("error", joined)
        self.assertIn("task_set_mismatch", joined)
        self.assertIn("T2", joined)
        # 不应误判为 finished（旧 bug 的危害正是静默跳过 T2）

    def test_state_has_task_not_in_meta_reports_mismatch(self):
        # 回归 #5：state.json 残留 T3 但 tasks.json 没有 T3。
        # 应报 task_set_mismatch error。
        os.makedirs(os.path.join(REQUIREMENTS_DIR, "m2"), exist_ok=True)
        base = os.path.join(REQUIREMENTS_DIR, "m2")
        with open(os.path.join(base, "state.json"), "w") as f:
            json.dump({"tasks": {
                "T1": {"status": "verified", "executor": "bewater-reviewer", "issue": None, "history": []},
                "T3": {"status": "pending", "executor": "bewater-orchestrate", "issue": None, "history": []}}}, f)
        with open(os.path.join(base, "tasks.json"), "w") as f:
            json.dump({"name": "m2", "tasks": [_entry("T1")]}, f)
        lines = orchestrate.next_action("m2")
        joined = "\n".join(lines)
        self.assertIn("error", joined)
        self.assertIn("task_set_mismatch", joined)
        self.assertIn("T3", joined)

    def test_next_action_always_single_line(self):
        # 契约：每轮 flow next 严格输出一行（@agent/@human/@control 之一）。
        # build 提示词以此契约做「逐行处理」解析，多行会破坏重派判定与单步派发。
        # 覆盖所有会输出指令的分支，断言行数恒 ≤ 1，把「碰巧单行」钉成「强制单行」。
        cases = []

        # tdd 派发（pending）+ rework 同存：rework 优先，单行 @agent
        _plan("sl_tdd", [_entry("T1"), _entry("T2", depends=["T1"])])
        cases.append(("tdd pending 派发", orchestrate.next_action("sl_tdd")))
        state.update("sl_tdd", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("sl_tdd", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                                "issue": {"criteria": 1, "cause": "x", "fix": "y"}})
        cases.append(("rework 优先派发", orchestrate.next_action("sl_tdd")))

        # review 派发
        _plan("sl_rev", [_entry("T1")])
        state.update("sl_rev", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        cases.append(("review 派发", orchestrate.next_action("sl_rev")))

        # blocked 优先：同轮有 implemented 可审 + blocked，只出 @human 一行
        _plan("sl_blk", [_entry("T1"), _entry("T2")])
        state.update("sl_blk", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("sl_blk", {"task_id": "T2", "status": "implemented", "executor": "bewater-tdd"})
        state.update("sl_blk", {"task_id": "T2", "status": "blocked", "executor": "bewater-reviewer",
                                "issue": {"criteria": "?", "cause": "b", "fix": "decide"}})
        cases.append(("blocked 优先单行", orchestrate.next_action("sl_blk")))

        # finished：全 verified → 直接 finished
        _plan("sl_done", [_entry("T1")])
        state.update("sl_done", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("sl_done", {"task_id": "T1", "status": "verified", "executor": "bewater-reviewer"})
        cases.append(("finished 单行", orchestrate.next_action("sl_done")))

        for label, lines in cases:
            self.assertLessEqual(
                len(lines), 1,
                "{} 应严格输出一行，实际 {} 行: {}".format(label, len(lines), lines))


class TestAutoBlockReworkLimit(unittest.TestCase):
    """返工上限自动升级：state.update 提交 rework 后同事务内升级 blocked。

    自动升级内联在 update 的锁内完成，不再依赖编排层 sweep——故无 TOCTOU 窗口：
    判定与提交在同一锁内，不存在「持陈旧快照」的问题。
    """

    def test_rework_limit_auto_blocks_and_preserves_issue(self):
        _plan("t", [_entry("T1")])
        issue = {"criteria": 1, "cause": "原始返工原因 src/x.ts:5", "fix": "原始修复方向"}
        for _ in range(3):
            state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
            # 第三次 rework 提交时即自动升级为 blocked（update 内同事务完成）
            state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                               "issue": dict(issue)})
        st = state.load("t")["tasks"]["T1"]
        self.assertEqual(st["status"], "blocked")
        # 原始 cause/fix 应保留，不被覆写
        self.assertIn("原始返工原因", st["issue"]["cause"])
        self.assertIn("自动升级", st["issue"]["cause"])
        self.assertEqual(st["issue"]["fix"], "原始修复方向")

    def test_auto_block_happens_at_third_rework_not_second(self):
        _plan("t", [_entry("T1")])
        # 两次 rework 不触发升级
        for _ in range(2):
            state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
            state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                               "issue": {"criteria": 1, "cause": "r", "fix": "y"}})
        self.assertEqual(state.load("t")["tasks"]["T1"]["status"], "rework")
        # 第三次 rework 触发升级
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "r3", "fix": "y3"}})
        self.assertEqual(state.load("t")["tasks"]["T1"]["status"], "blocked")

    def test_below_limit_rework_stays_rework(self):
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "r", "fix": "y"}})
        # 仅一次 rework，不应升级
        self.assertEqual(state.load("t")["tasks"]["T1"]["status"], "rework")

    def test_rework_limit_survives_history_trimming(self):
        # 回归 #3：早期实现数 history 里 rework 条目计返工次数，但 _trim_history
        # 会丢中间段导致计数归零、上限失效（死循环）。改用独立 rework_count 后，
        # 即使 history 被裁剪到 rework 条目全丢，第三次 rework 仍应升级 blocked。
        _plan("t", [_entry("T1")])
        # 2 次 rework（未达上限 3）——走 update，rework_count 此时已被 _commit
        # 累加到 2 并落盘，与 history 解耦。
        for i in (1, 2):
            state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
            state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                               "issue": {"criteria": 1, "cause": "r%d" % i, "fix": "f"}})
        self.assertEqual(state.load("t")["tasks"]["T1"]["rework_count"], 2)
        # 人为往 history 塞大量条目，把早期 rework(r1/r2) 推到被裁剪的中间段：
        # 头部 5 条 padding + 原 history + 尾部 45 条 padding > MAX_HISTORY(50)
        st = state.load("t")
        hist = list(st["tasks"]["T1"]["history"])
        pad_head = [{"status": "pending", "ts": "h%d" % i, "executor": "bewater-orchestrate",
                     "issue": None} for i in range(5)]
        pad_tail = [{"status": "implemented", "ts": "t%d" % i, "executor": "bewater-tdd",
                     "issue": None} for i in range(45)]
        st["tasks"]["T1"]["history"] = pad_head + hist + pad_tail
        state.save("t", st)
        # 裁剪后早期 rework 条目应已丢失（证明 history 确实被裁了）
        trimmed = state.load("t")["tasks"]["T1"]
        causes = [h.get("issue", {}).get("cause") if isinstance(h.get("issue"), dict) else None
                  for h in trimmed["history"]]
        self.assertNotIn("r1", causes)
        self.assertNotIn("r2", causes)
        # 但 rework_count 独立维护，不受 history 裁剪影响，仍为 2
        self.assertEqual(trimmed["rework_count"], 2)
        # 第三次 rework：rework_count 从 2 → 3，达上限触发升级
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "r3", "fix": "f"}})
        final = state.load("t")["tasks"]["T1"]
        # 修复后：history 裁剪丢了 r1/r2，但独立 rework_count 仍正确累计，
        # 第三次 rework 触发 blocked（旧实现会因计数归零卡在 rework 死循环）
        self.assertEqual(final["status"], "blocked",
                         "history 裁剪不应使返工上限失效")
        self.assertEqual(final["rework_count"], 3)


class TestUnblockResetsReworkCount(unittest.TestCase):
    """受阻解除（blocked → pending）须重置 rework_count。

    否则解阻后任务第一次返工即累计到 REWORK_LIMIT 又被自动 blocked，等于
    人工解阻无效——给修复后的新一轮实现-审查完整返工额度。
    """

    def _to_blocked(self, name="t"):
        issue = {"criteria": 1, "cause": "x", "fix": "y"}
        for _ in range(3):
            state.update(name, {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
            state.update(name, {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                                "issue": dict(issue)})

    def test_unblock_resets_rework_count(self):
        _plan("t", [_entry("T1")])
        self._to_blocked()
        self.assertEqual(state.load("t")["tasks"]["T1"]["rework_count"], 3)
        # 人工解除受阻
        state.update("t", {"task_id": "T1", "status": "pending", "executor": "bewater-orchestrate"})
        st = state.load("t")["tasks"]["T1"]
        self.assertEqual(st["status"], "pending")
        self.assertEqual(st["rework_count"], 0)
        # 解阻后旧 issue 应清除（_commit 用传入的 None 覆盖）
        self.assertIsNone(st["issue"])

    def test_rework_after_unblock_gets_full_quota(self):
        # 关键回归：解阻后重新实现，仅 1 次返工不应再次触发自动 blocked。
        _plan("t", [_entry("T1")])
        self._to_blocked()
        state.update("t", {"task_id": "T1", "status": "pending", "executor": "bewater-orchestrate"})
        # 重新实现 → reviewer 判一次 rework
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "新原因", "fix": "f"}})
        st = state.load("t")["tasks"]["T1"]
        self.assertEqual(st["status"], "rework", "解阻后首次返工不应立即自动 blocked")
        self.assertEqual(st["rework_count"], 1)


class TestHistoryTrimming(unittest.TestCase):
    """history 裁剪：超长时留头留尾，最早原始问题不丢。"""

    def _write_long_history(self, name, n):
        """经 state.save 写入含 n 条 history 的 state，触发裁剪（绕过状态机避免返工上限）。"""
        hist = []
        for i in range(n):
            hist.append({"status": "implemented" if i % 2 else "rework",
                         "ts": "t{}".format(i), "executor": "bewater-tdd", "issue": None})
        state.save(name, {"tasks": {"T1": {
            "status": "rework", "executor": "bewater-orchestrate",
            "issue": None, "history": hist}}})

    def test_history_capped_when_exceeds_max(self):
        _plan("t", [_entry("T1")])
        self._write_long_history("t", 80)
        st = state.load("t")["tasks"]["T1"]
        self.assertLessEqual(len(st["history"]), state.MAX_HISTORY)

    def test_head_and_tail_preserved(self):
        _plan("t", [_entry("T1")])
        self._write_long_history("t", 80)
        st = state.load("t")["tasks"]["T1"]
        hist = st["history"]
        # 头部第一条（t0）与尾部最后一条（t79）都应保留
        self.assertEqual(hist[0]["ts"], "t0")
        self.assertEqual(hist[-1]["ts"], "t79")
        # 中间应有省略标记
        omitted = [h for h in hist if h.get("_omitted")]
        self.assertEqual(len(omitted), 1)
        # 丢弃条数 = 80 - (5 + 44) = 31
        self.assertEqual(omitted[0]["_omitted"], 31)

    def test_short_history_not_trimmed(self):
        _plan("t", [_entry("T1")])
        self._write_long_history("t", 20)
        st = state.load("t")["tasks"]["T1"]
        # 未超阈值，无省略标记
        self.assertFalse(any(h.get("_omitted") for h in st["history"]))


class TestHistoryPersistsIssue(unittest.TestCase):
    """history 嵌入 issue 快照——多轮 rework 前序 cause 不丢，flow get 可回看。"""

    def test_rework_history_visible_via_flow_get(self):
        _plan("t", [_entry("T1", criteria=[{"id": 1, "text": "c1"}, {"id": 2, "text": "c2"}])])
        # 第一轮 rework：cause A
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "第一轮 src/a.ts:1", "fix": "改A"}})
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        # 第二轮 rework：cause B
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 2, "cause": "第二轮 src/b.ts:2", "fix": "改B"}})

        data = tasks.load_with_state("t", ["T1"])
        entry = data["tasks"][0]
        # 当前 issue 是第二轮
        self.assertEqual(entry["issue"]["cause"], "第二轮 src/b.ts:2")
        # history 暴露两轮 rework，前序 cause A 仍在
        rework_hist = entry["history"]
        causes = [h["cause"] for h in rework_hist]
        self.assertIn("第一轮 src/a.ts:1", causes)
        self.assertIn("第二轮 src/b.ts:2", causes)
        # 非 rework/blocked 条目被过滤
        self.assertTrue(all(h["status"] in ("rework", "blocked") for h in rework_hist))

    def test_notes_persisted_in_history(self):
        """B3: notes 随 issue 快照嵌入 history，多轮 rework 前序 notes 不丢。"""
        _plan("t", [_entry("T1", criteria=[{"id": 1, "text": "c1"}])])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        # 第一轮 rework 带 notes
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "x", "fix": "y",
                                     "notes": "触发条件:缓存命中时第5行"}})
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        # 第二轮 rework 不带 notes
        state.update("t", {"task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
                           "issue": {"criteria": 1, "cause": "z", "fix": "w"}})

        data = tasks.load_with_state("t", ["T1"])
        hist = data["tasks"][0]["history"]
        # 第一轮有 notes，第二轮无 notes 则不带该键
        first = [h for h in hist if h["cause"] == "x"][0]
        second = [h for h in hist if h["cause"] == "z"][0]
        self.assertEqual(first["notes"], "触发条件:缓存命中时第5行")
        self.assertNotIn("notes", second)


class TestNotesFieldPassthrough(unittest.TestCase):
    """issue 额外 notes 字段不经 ISSUE_FIELDS 拒绝，可落库并经 flow get 返回。"""

    def test_notes_persists_and_returns(self):
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        state.update("t", {
            "task_id": "T1", "status": "rework", "executor": "bewater-reviewer",
            "issue": {"criteria": 1, "cause": "x", "fix": "y", "notes": "触发条件:缓存命中"},
        })
        data = tasks.load_with_state("t", ["T1"])
        self.assertEqual(data["tasks"][0]["issue"]["notes"], "触发条件:缓存命中")


class TestDesignPath(unittest.TestCase):
    """load_with_state 返回 design_path（存在则路径，否则 None）。"""

    def test_design_path_null_when_absent(self):
        _plan("t", [_entry("T1")])
        data = tasks.load_with_state("t", ["T1"])
        self.assertIsNone(data["tasks"][0]["design_path"])

    def test_design_path_set_when_present(self):
        _plan("t", [_entry("T1")])
        design = os.path.join(state.dir_path("t"), "design.md")
        with open(design, "w") as f:
            f.write("# t\n")
        data = tasks.load_with_state("t", ["T1"])
        self.assertEqual(data["tasks"][0]["design_path"], design)


def _run_argv(argv):
    """运行 'flow <argv>' 并捕获 stdout/stderr/exit_code。"""
    import io
    from contextlib import redirect_stdout, redirect_stderr
    from scripts.cli import _build_parser

    buf = io.StringIO()
    err = io.StringIO()
    parser = _build_parser()
    with redirect_stdout(buf), redirect_stderr(err):
        try:
            args = parser.parse_args(argv)
            args.func(args)
            return buf.getvalue(), err.getvalue(), 0
        except SystemExit as e:
            return buf.getvalue(), err.getvalue(), e.code


class TestCliPlan(unittest.TestCase):
    """cmd_plan：从标准路径读 tasks.json。"""

    def test_plan_from_standard_path(self):
        name = "p1"
        path = tasks.tasks_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"tasks": [_entry("T1")]}, f)
        out, err, code = _run_argv(["plan", name])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(tasks.get_task_ids(name), ["T1"])

    def test_plan_overwrites_existing_progress(self):
        name = "p2"
        _plan(name, [_entry("T1")])
        state.update(name, {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        path = tasks.tasks_path(name)
        with open(path, "w") as f:
            json.dump({"tasks": [_entry("T1"), _entry("T2")]}, f)
        out, err, code = _run_argv(["plan", name])
        self.assertEqual(code, 0, msg=err)
        self.assertIn("已存在运行时进度", err)
        st = state.load(name)["tasks"]
        self.assertEqual(st["T1"]["status"], "pending")
        self.assertIn("T2", st)

    def test_plan_overwrites_corrupt_state(self):
        name = "p3"
        _plan(name, [_entry("T1")])
        with open(os.path.join(REQUIREMENTS_DIR, name, "state.json"), "w") as f:
            f.write("{ broken json")
        path = tasks.tasks_path(name)
        with open(path, "w") as f:
            json.dump({"tasks": [_entry("T1"), _entry("T2")]}, f)
        out, err, code = _run_argv(["plan", name])
        self.assertEqual(code, 0, msg=err)
        self.assertIn("损坏", err)

    def test_plan_rejects_empty_tasks(self):
        name = "p4"
        path = tasks.tasks_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"tasks": []}, f)
        out, err, code = _run_argv(["plan", name])
        self.assertNotEqual(code, 0)
        self.assertIn("为空", err)


class TestCliReportImplement(unittest.TestCase):
    """flow report implement：pending → implemented。"""

    def test_implement(self):
        _plan("t", [_entry("T1")])
        out, err, code = _run_argv(["report", "implement", "t", "T1"])
        self.assertEqual(code, 0, msg=err)
        st = state.load("t")["tasks"]["T1"]
        self.assertEqual(st["status"], "implemented")
        self.assertEqual(st["executor"], "bewater-tdd")

    def test_implement_rejects_unknown_flags(self):
        _plan("t", [_entry("T1")])
        out, err, code = _run_argv(["report", "implement", "t", "T1", "--criteria", "1"])
        self.assertNotEqual(code, 0)


class TestCliReportVerify(unittest.TestCase):
    """flow report verify：implemented → verified。"""

    def test_verify(self):
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        out, err, code = _run_argv(["report", "verify", "t", "T1"])
        self.assertEqual(code, 0, msg=err)
        st = state.load("t")["tasks"]["T1"]
        self.assertEqual(st["status"], "verified")
        self.assertEqual(st["executor"], "bewater-reviewer")


class TestCliReportRework(unittest.TestCase):
    """flow report rework：内联 --criteria/--cause/--fix/--notes。"""

    def setUp(self):
        _plan("t", [_entry("T1", criteria=[{"id": 1, "text": "c1"}, {"id": 2, "text": "c2"}])])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})

    def test_rework_with_issue(self):
        out, err, code = _run_argv([
            "report", "rework", "t", "T1",
            "--criteria", "1",
            "--cause", '当 x == "null" 时第 42 行',
            "--fix", '加 if (x == null) return 401',
            "--notes", "触发条件:缓存命中",
        ])
        self.assertEqual(code, 0, msg=err)
        issue = state.load("t")["tasks"]["T1"]["issue"]
        self.assertEqual(issue["criteria"], 1)
        self.assertEqual(issue["cause"], '当 x == "null" 时第 42 行')
        self.assertEqual(issue["fix"], '加 if (x == null) return 401')
        self.assertEqual(issue["notes"], "触发条件:缓存命中")

    def test_rework_string_int_criteria_normalized(self):
        out, err, code = _run_argv([
            "report", "rework", "t", "T1",
            "--criteria", "2", "--cause", "x", "--fix", "y",
        ])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(state.load("t")["tasks"]["T1"]["issue"]["criteria"], 2)

    def test_rework_invalid_criteria_rejected(self):
        out, err, code = _run_argv([
            "report", "rework", "t", "T1",
            "--criteria", "99", "--cause", "x", "--fix", "y",
        ])
        self.assertNotEqual(code, 0)
        self.assertIn("不在验收标准编号中", err)

    def test_rework_missing_criteria_rejected(self):
        out, err, code = _run_argv([
            "report", "rework", "t", "T1",
            "--cause", "x", "--fix", "y",
        ])
        self.assertNotEqual(code, 0)

    def test_rework_missing_cause_rejected(self):
        out, err, code = _run_argv([
            "report", "rework", "t", "T1",
            "--criteria", "1", "--fix", "y",
        ])
        self.assertNotEqual(code, 0)


class TestCliReportBlocked(unittest.TestCase):
    """flow report blocked：内联 --criteria/--cause/--fix/--notes。"""

    def test_blocked_with_question_mark(self):
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        out, err, code = _run_argv([
            "report", "blocked", "t", "T1",
            "--criteria", "?", "--cause", "需决策", "--fix", "409 or 423",
        ])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(state.load("t")["tasks"]["T1"]["issue"]["criteria"], "?")
        self.assertEqual(state.load("t")["tasks"]["T1"]["status"], "blocked")

    def test_blocked_with_notes(self):
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        out, err, code = _run_argv([
            "report", "blocked", "t", "T1",
            "--criteria", "?", "--cause", "x", "--fix", "y",
            "--notes", "设计级问题:缓存方案不确定",
        ])
        self.assertEqual(code, 0, msg=err)
        issue = state.load("t")["tasks"]["T1"]["issue"]
        self.assertEqual(issue["notes"], "设计级问题:缓存方案不确定")


class TestCliScope(unittest.TestCase):
    """cmd_scope：给 reviewer 的审查范围清单。"""

    def test_scope_returns_files(self):
        entries = [_entry("T1")]
        entries[0]["files"] = ["src/a.py", "tests/test_a.py"]
        _plan("s", entries)
        out, err, code = _run_argv(["scope", "s", "T1"])
        self.assertEqual(code, 0, msg=err)
        data = json.loads(out)
        self.assertEqual(data["files"], ["src/a.py", "tests/test_a.py"])
        self.assertFalse(data["files_empty"])
        self.assertNotIn("advice", data)

    def test_scope_empty_files_gives_blocked_advice(self):
        _plan("t", [_entry("T1")])  # files 默认 []
        out, err, code = _run_argv(["scope", "t", "T1"])
        self.assertEqual(code, 0, msg=err)
        data = json.loads(out)
        self.assertTrue(data["files_empty"])
        self.assertIn("advice", data)
        self.assertIn("blocked", data["advice"])

    def test_scope_unknown_task_rejected(self):
        _plan("t", [_entry("T1")])
        out, err, code = _run_argv(["scope", "t", "T99"])
        self.assertNotEqual(code, 0)
        self.assertIn("不存在", err)


class TestCliUnblock(unittest.TestCase):
    """cmd_unblock：受阻解除 blocked → pending。"""

    def test_unblock_blocked_to_pending(self):
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "blocked", "executor": "bewater-tdd",
                           "issue": {"criteria": "?", "cause": "x", "fix": "y"}})
        out, err, code = _run_argv(["unblock", "t", "T1"])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(state.load("t")["tasks"]["T1"]["status"], "pending")

    def test_unblock_non_blocked_rejected(self):
        _plan("t", [_entry("T1")])
        state.update("t", {"task_id": "T1", "status": "implemented", "executor": "bewater-tdd"})
        out, err, code = _run_argv(["unblock", "t", "T1"])
        self.assertNotEqual(code, 0)
        self.assertIn("非法状态流转", err)

    def test_unblock_unknown_task_rejected(self):
        _plan("t", [_entry("T1")])
        out, err, code = _run_argv(["unblock", "t", "T99"])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP_BASE, ignore_errors=True)
