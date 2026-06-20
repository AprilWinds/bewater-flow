"""bewater-flow CLI —— 子命令解析与上报守卫。

所有写操作都落到 state/tasks 模块，本文件只负责参数解析、前置校验
（任务存在性、issue.criteria 命中真实编号）与失败时退出。
"""

import argparse
import json
import os
import subprocess
import sys

from . import (
    BEWATER_BASE, DEFAULT_EXECUTOR,
    tasks, state, orchestrate,
)


def _fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def _check_workspace():
    if not os.path.isdir(BEWATER_BASE):
        _fail(
            "fatal: 不是 bewater workspace（未找到 .bewater/ 目录）。\n"
            "  执行 'flow new <名称>' 创建一个。"
        )


# ======================== handlers ========================


def cmd_new(args):
    name = args.name
    existing_state = state.state_path(name)
    if os.path.exists(existing_state):
        prev = state.load_strict(name)
        if prev.get("tasks"):
            print(
                "⚠️  {} 已存在且有进度，flow new 将重置 state.json。".format(name),
                file=sys.stderr,
            )
            old_tasks = tasks.tasks_path(name)
            if os.path.exists(old_tasks):
                os.remove(old_tasks)
            old_design = os.path.join(state.dir_path(name), "design.md")
            if os.path.exists(old_design):
                os.remove(old_design)
    os.makedirs(state.dir_path(name), exist_ok=True)
    state.save(name, {"tasks": {}})
    print("✓ {} 创建完成 → 阶段: plan".format(name))


def cmd_plan(args):
    _check_workspace()
    name = args.name
    json_file = os.path.join(state.dir_path(name), "tasks.json")

    if not os.path.exists(json_file):
        _fail("未找到 tasks.json: {}\n请先将任务规划写入该路径，然后运行 flow plan <名称>。".format(json_file))

    with open(json_file) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            _fail("JSON 解析失败（{}）: {}".format(json_file, e))

    if not isinstance(data, dict):
        _fail("plan 内容必须为 JSON 对象，收到: {}".format(type(data).__name__))

    entries = data.get("tasks", [])
    if not isinstance(entries, list):
        _fail("tasks 必须为列表，收到: {}".format(type(entries).__name__))
    if not entries:
        _fail("tasks 列表为空，请提供至少一个任务")

    prev_state = state.load(name).get("tasks", {})
    if any(t.get("status", "pending") != "pending" for t in prev_state.values()):
        print(
            "⚠️  {} 已存在运行时进度，flow plan 将覆盖规划并重置 state。".format(name),
            file=sys.stderr,
        )

    try:
        tasks.init(name, entries)
    except ValueError as e:
        _fail("校验失败: {}".format(e))
    print("✓ {} 规划写入完成，{} 个任务 → 阶段: tdd".format(name, len(entries)))


def cmd_get(args):
    _check_workspace()
    name = args.name
    task_ids = args.task_ids if args.task_ids else None

    if task_ids:
        meta = tasks.load_meta(name)
        known = {t["task_id"] for t in meta.get("tasks", [])}
        unknown = [t for t in task_ids if t not in known]
        if unknown:
            _fail(
                "任务不存在: {}，当前任务列表: {}".format(
                    ",".join(unknown), sorted(known)
                )
            )

    data = tasks.load_with_state(name, task_ids=task_ids)
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ── report 子命令 ──


def _validate_task_exists(name, task_id):
    task_ids = tasks.get_task_ids(name)
    if not task_ids:
        _fail("tasks.json 不存在或为空，无法上报。请先执行 flow plan。")
    if task_id not in task_ids:
        _fail("任务 {} 不存在，当前任务列表: {}".format(task_id, sorted(task_ids)))


def _normalize_criteria(crit):
    """字符串整数归一，? 保留原样。"""
    if crit != "?":
        try:
            return int(crit)
        except ValueError:
            _fail(
                "issue.criteria 必须是验收标准编号（整数）或 \"?\"，收到: {}".format(crit)
            )
    return crit


def _build_issue(args):
    issue = {
        "criteria": _normalize_criteria(args.criteria),
        "cause": args.cause,
        "fix": args.fix,
    }
    if getattr(args, "notes", None) is not None:
        issue["notes"] = args.notes
    return issue


# report 子命令的状态映射：status → executor。rework/blocked 带且仅带 issue。
# executor 与状态机的 ALLOWED_TRANSITIONS 对应，固化在此避免各 handler 重复写。
_REPORT_EXECUTORS = {
    "implemented": "bewater-tdd",
    "verified": "bewater-reviewer",
    "rework": "bewater-reviewer",
    "blocked": DEFAULT_EXECUTOR,
}


def _report(args):
    """通用上报：implement / verify / rework / blocked 共用。

    status 与 executor 经 argparse set_defaults 注入 args；带 issue 的两类
    （rework/blocked）需预检 issue.criteria 命中真实编号，避免 state 反向
    依赖 tasks 造成循环 import。
    """
    _check_workspace()
    _validate_task_exists(args.name, args.task_id)

    status = args.status
    with_issue = status in ("rework", "blocked")
    issue = _build_issue(args) if with_issue else None
    row = {"task_id": args.task_id, "status": status,
           "executor": _REPORT_EXECUTORS[status]}
    if issue is not None:
        row["issue"] = issue

    try:
        if issue is not None:
            tasks.validate_issue_criteria(args.name, args.task_id, issue)
        st = state.update(args.name, row)
    except ValueError as e:
        _fail("校验失败: {}".format(e))
    stage = state.current_stage(st.get("tasks", {}))
    print("✓ {} → {}  (阶段: {})".format(args.task_id, status, stage))


# ── 其它命令 ──


def cmd_scope(args):
    _check_workspace()
    name, tid = args.name, args.task_id
    meta = tasks.meta_by_id(name)
    task_meta = meta.get(tid)
    if not task_meta:
        _fail("任务 {} 不存在，当前任务列表: {}".format(tid, sorted(meta.keys())))

    files = list(task_meta.get("files", []))
    files_empty = len(files) == 0

    try:
        out = subprocess.check_output(
            ["git", "status", "--short", "--untracked-files=all"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
        changes = [ln[3:] for ln in out.splitlines() if len(ln) > 3]
    except (OSError, subprocess.CalledProcessError):
        changes = []

    result = {
        "task_id": tid,
        "files": files,
        "files_empty": files_empty,
        "changes": changes,
    }
    if files_empty:
        result["advice"] = "files 为空：请判 blocked 交人工界定审查范围，不要扫全部变更。"
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_unblock(args):
    _check_workspace()
    _validate_task_exists(args.name, args.task_id)
    row = {"task_id": args.task_id, "status": "pending", "executor": DEFAULT_EXECUTOR}
    try:
        st = state.update(args.name, row)
    except ValueError as e:
        _fail("校验失败: {}".format(e))
    stage = state.current_stage(st.get("tasks", {}))
    print("✓ {} 已解除受阻 → pending  (阶段: {})".format(args.task_id, stage))


def cmd_list(args):
    _check_workspace()
    names = state.list_names()
    if not names:
        print("（暂无需求，使用 flow new <名称> 创建）")
        return
    print("需求:")
    for name in names:
        st = state.load(name)
        stage = state.current_stage(st.get("tasks", {}))
        print("  - {}  ({})".format(name, stage))


def cmd_next(args):
    _check_workspace()
    for line in orchestrate.next_action(args.name):
        print(line)


# ======================== argparse ========================


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="flow",
        description="bewater-flow —— 需求编排工具",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")
    sub.required = True

    # new
    p = sub.add_parser("new", help="创建需求骨架")
    p.add_argument("name", help="需求名称")
    p.set_defaults(func=cmd_new)

    # plan
    p = sub.add_parser("plan", help="写入任务规划（从标准路径 tasks.json 读取）")
    p.add_argument("name", help="需求名称")
    p.set_defaults(func=cmd_plan)

    # get
    p = sub.add_parser("get", help="读取任务元数据 + 运行时状态")
    p.add_argument("name", help="需求名称")
    p.add_argument("task_ids", nargs="*", metavar="T1", help="任务 ID（可选）")
    p.set_defaults(func=cmd_get)

    # report ── 子子命令 implement / verify / rework / blocked
    report = sub.add_parser("report", help="上报状态变更")
    rsub = report.add_subparsers(dest="report_cmd", metavar="<command>")
    rsub.required = True

    p = rsub.add_parser("implement", help="pending → implemented（TDD 实现完成）")
    p.add_argument("name", help="需求名称")
    p.add_argument("task_id", help="任务 ID")
    p.set_defaults(func=_report, status="implemented")

    p = rsub.add_parser("verify", help="implemented → verified（审查通过）")
    p.add_argument("name", help="需求名称")
    p.add_argument("task_id", help="任务 ID")
    p.set_defaults(func=_report, status="verified")

    p = rsub.add_parser("rework", help="implemented → rework（审查不通过，返工）")
    p.add_argument("name", help="需求名称")
    p.add_argument("task_id", help="任务 ID")
    p.add_argument("--criteria", required=True, help="验收标准编号或 ?")
    p.add_argument("--cause", required=True, help="问题原因")
    p.add_argument("--fix", required=True, help="修复动作")
    p.add_argument("--notes", help="推理链/触发条件（可选）")
    p.set_defaults(func=_report, status="rework")

    p = rsub.add_parser("blocked", help="标记为受阻（需人工介入）")
    p.add_argument("name", help="需求名称")
    p.add_argument("task_id", help="任务 ID")
    p.add_argument("--criteria", required=True, help="验收标准编号或 ?")
    p.add_argument("--cause", required=True, help="阻塞原因")
    p.add_argument("--fix", required=True, help="需人决策的选项")
    p.add_argument("--notes", help="推理链/触发条件（可选）")
    p.set_defaults(func=_report, status="blocked")

    # scope
    p = sub.add_parser("scope", help="审查范围：本 task 应审文件清单")
    p.add_argument("name", help="需求名称")
    p.add_argument("task_id", help="任务 ID")
    p.set_defaults(func=cmd_scope)

    # unblock
    p = sub.add_parser("unblock", help="受阻解除（blocked → pending）")
    p.add_argument("name", help="需求名称")
    p.add_argument("task_id", help="任务 ID")
    p.set_defaults(func=cmd_unblock)

    # list
    p = sub.add_parser("list", help="列出全部需求及阶段")
    p.set_defaults(func=cmd_list)

    # next
    p = sub.add_parser("next", help="编排引擎：输出下一步指令")
    p.add_argument("name", help="需求名称")
    p.set_defaults(func=cmd_next)

    return parser


def main():
    parser = _build_parser()

    # 无参数时显示帮助，而非 "error: too few arguments"
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    args = parser.parse_args()

    try:
        args.func(args)
    except ValueError as e:
        _fail("校验失败: {}".format(e))