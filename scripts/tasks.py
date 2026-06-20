"""tasks.json 元数据读写。"""

import json
import os
import sys

from . import DEFAULT_EXECUTOR, DESIGN_TEMPLATE_NAME, utc_now, unique_tmp
from .state import dir_path


def _validate_depends(entries):
    ids = set()
    for entry in entries:
        if "task_id" not in entry:
            raise ValueError("task 缺少必填字段 task_id: {}".format(entry))
        tid = entry["task_id"]
        if not isinstance(tid, str) or not tid:
            raise ValueError("task_id 必须为非空字符串，收到: {!r}".format(tid))
        ids.add(tid)
    if len(ids) != len(entries):
        raise ValueError("存在重复 task ID: {}".format([e.get("task_id") for e in entries]))
    for entry in entries:
        tid = entry["task_id"]
        depends = entry.get("depends", [])
        if not isinstance(depends, list):
            raise ValueError("任务 {} 的 depends 必须为列表，收到: {}".format(tid, type(depends).__name__))
        for d in depends:
            if not isinstance(d, str) or not d:
                raise ValueError("任务 {} 的 depends 元素必须为非空字符串，收到: {!r}".format(tid, d))
            if depends.count(d) > 1:
                # 源头规范：重复依赖元素无意义（运行时 _detect_cycle 已去重自鲁棒）。
                raise ValueError("任务 {} 的 depends 含重复元素: {}".format(tid, d))
            if d not in ids:
                raise ValueError("任务 {} 的 depends 引用了不存在的 task ID: {}".format(tid, d))
            if d == tid:
                raise ValueError("任务 {} 的 depends 中不能包含自身".format(tid))

    # 循环依赖检测：从每个节点 DFS，出现回溯到已访问节点即为环。
    _check_cycles(entries)


def _check_cycles(entries):
    # Plan 期结构校验：在全部已定义任务上检任意结构性环。
    # 与 orchestrate._detect_cycle 不是重复——后者是运行时死锁检测，只在
    # 未完成（pending/rework）子集上跑并且排除已 verified 节点；本处是全集
    # 三色 DFS，算法与输入域都不同。
    # 迭代式三色 DFS（避免长依赖链触发 RecursionError）：
    # WHITE=未访问，GRAY=在当前路径上，BLACK=已完成。
    dep_map = {e["task_id"]: e.get("depends", []) for e in entries}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in dep_map}

    for root in dep_map:
        if color[root] != WHITE:
            continue
        # 栈元素：(节点, 该节点待处理的依赖迭代器)
        stack = [(root, iter(dep_map[root]))]
        color[root] = GRAY
        while stack:
            tid, it = stack[-1]
            advanced = False
            for d in it:
                if color.get(d) == GRAY:
                    raise ValueError("存在循环依赖，涉及: {}".format(d))
                if color.get(d) == WHITE:
                    color[d] = GRAY
                    stack.append((d, iter(dep_map[d])))
                    advanced = True
                    break
            if not advanced:
                color[tid] = BLACK
                stack.pop()


def _validate_task_shapes(entries):
    for entry in entries:
        tid = entry["task_id"]

        for f in ("files", "steps", "signatures", "criteria"):
            val = entry.get(f, [])
            if not isinstance(val, list):
                raise ValueError("任务 {} 的 {} 必须为列表，收到: {}".format(tid, f, type(val).__name__))

        for sig in entry.get("signatures", []):
            if not isinstance(sig, dict) or "name" not in sig:
                raise ValueError("任务 {} 的 signature 缺少必填字段 name: {}".format(tid, sig))

        crit_ids = set()
        for c in entry.get("criteria", []):
            if not isinstance(c, dict) or "id" not in c or "text" not in c:
                raise ValueError("任务 {} 的 criteria 缺少必填字段 id/text: {}".format(tid, c))
            # id 必须为正整数（非 bool）——与 cli.py report 的 int 解析、state.py 的集合比对保持同类型，
            # 否则字符串 id 会过 plan 但在 report 时因 int/str 不匹配被拒。
            cid = c["id"]
            if isinstance(cid, bool) or not isinstance(cid, int) or cid < 1:
                raise ValueError("任务 {} 的 criterion id 须为正整数，收到: {!r}".format(tid, cid))
            if cid in crit_ids:
                raise ValueError("任务 {} 的 criteria 存在重复 id: {}".format(tid, cid))
            crit_ids.add(cid)


def tasks_path(name):
    return os.path.join(dir_path(name), "tasks.json")


def load_meta(name):
    path = tasks_path(name)
    if not os.path.exists(path):
        return {"name": name, "tasks": []}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        print("⚠️  tasks.json 损坏（{}），已回退为空任务列表。".format(e), file=sys.stderr)
        return {"name": name, "tasks": []}


def meta_by_id(name, meta=None):
    """按 task_id 索引 tasks.json 元数据。

    已加载的 meta 可经 meta 入参复用，避免重复读盘。tasks.json 缺失/损坏
    返回空 dict——与 load_meta 的静默回退一致。
    """
    if meta is None:
        meta = load_meta(name)
    return {t["task_id"]: t for t in meta.get("tasks", [])}


def _slim_history(history):
    """精简 history：只暴露 rework/blocked 条目的 status/ts/cause/fix
    （过滤 None-issue 条目），供执行 Agent 在多轮 rework 时回看前序原因。"""
    slim = []
    for h in history:
        if h.get("status") not in ("rework", "blocked"):
            continue
        issue = h.get("issue") or {}
        entry = {
            "status": h.get("status"),
            "ts": h.get("ts"),
            "cause": issue.get("cause"),
            "fix": issue.get("fix"),
        }
        if "notes" in issue:
            entry["notes"] = issue["notes"]
        slim.append(entry)
    return slim


def load_with_state(name, task_ids=None, meta=None, st=None):
    from .state import load as load_state

    if meta is None:
        meta = load_meta(name)
    if st is None:
        st = load_state(name)
    tasks_state = st.get("tasks", {})

    all_tasks = meta.get("tasks", [])
    by_id = meta_by_id(name, meta)

    if task_ids:
        ids = task_ids
    else:
        ids = [t["task_id"] for t in all_tasks]

    design_path = os.path.join(dir_path(name), DESIGN_TEMPLATE_NAME)
    design_path = design_path if os.path.exists(design_path) else None

    result = []
    for tid in ids:
        m = by_id.get(tid, {})
        s = tasks_state.get(tid, {})
        result.append({
            "task_id": tid,
            "name": m.get("name", tid),
            "depends": m.get("depends", []),
            "files": m.get("files", []),
            "signatures": m.get("signatures", []),
            "steps": m.get("steps", []),
            "criteria": m.get("criteria", []),
            "status": s.get("status", "pending"),
            "issue": s.get("issue"),
            "design_path": design_path,
            "history": _slim_history(s.get("history", [])),
        })

    return {"tasks": result}


def init(name, entries):
    from .state import save as save_state, _default_task_state

    _validate_depends(entries)
    _validate_task_shapes(entries)

    path = tasks_path(name)
    data = {"name": name, "tasks": entries}

    os.makedirs(os.path.dirname(path), exist_ok=True)

    now = utc_now()
    initial_history = [{"status": "pending", "ts": now, "executor": DEFAULT_EXECUTOR}]

    tasks_state = {}
    for entry in entries:
        tasks_state[entry["task_id"]] = _default_task_state(history=initial_history)

    # 先写 tasks.json 再重置 state.json：任一崩溃窗口下要么 tasks.json 已是新内容
    # （state 仍是旧进度，但能读到任务，不会 fail-open 派发空任务），要么两者都已更新，
    # 避免「有 state 无 tasks.json」的危险中间态。
    if os.path.exists(path):
        print("⚠️  tasks.json 已存在，将被覆盖。", file=sys.stderr)

    tmp = unique_tmp(path)
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)

    save_state(name, {"tasks": tasks_state})


def get_task_ids(name):
    data = load_meta(name)
    return [t["task_id"] for t in data.get("tasks", [])]


def validate_issue_criteria(name, tid, issue):
    """校验 issue.criteria 命中本 task 的真实验收标准编号。

    criteria 属性元数据（tasks.json），故校验归属 tasks 模块而非 state——
    避免 state 反向依赖 tasks 造成循环 import。cli 在调 state.update 前预检。
    "?" 表示 task 级问题（不指向具体验收标准），合法放行。
    """
    if not issue:
        return
    crit_field = issue.get("criteria")
    if crit_field in (None, "?"):
        return
    by_id = meta_by_id(name)
    meta = by_id.get(tid)
    valid_ids = set()
    if isinstance(meta, dict):
        valid_ids = {c["id"] for c in meta.get("criteria", []) if isinstance(c, dict)}
    if crit_field not in valid_ids:
        raise ValueError(
            "任务 {} 的 issue.criteria={} 不在验收标准编号中: {}".format(
                tid, crit_field, sorted(valid_ids) if valid_ids else "[]"
            )
        )