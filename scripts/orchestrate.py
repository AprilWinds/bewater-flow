"""状态机编排引擎。"""

import json
import re

from . import tasks, state

_REASON_MAX = 200
_LINEBREAK = re.compile(r"[\r\n\t]+")


def _flatten_reason(text):
    if not text:
        return ""
    flat = _LINEBREAK.sub(" ", text).strip()
    if len(flat) > _REASON_MAX:
        flat = flat[:_REASON_MAX - 1] + "…"
    return flat


def _scope_ready(tasks_state, task_meta):
    """依赖已满足的 pending/rework 任务，rework 优先。串行派发下调用方只取首个：
    上一轮派出的 task 在其上报状态变更前，build 不会再次调 next。"""
    result = []
    for tid, t_state in tasks_state.items():
        status = t_state.get("status")
        if status not in ("pending", "rework"):
            continue
        if not state.deps_satisfied(tasks_state, task_meta, tid):
            continue
        result.append(tid)
    # rework 排前尽快返工，其余按 ID 排序保证确定性
    result.sort(key=lambda tid: (tasks_state[tid].get("status") != "rework", tid))
    return result


def _scope_implemented(tasks_state):
    return sorted(
        tid for tid, t in tasks_state.items() if t.get("status") == "implemented"
    )


def _scope_blocked(tasks_state):
    return sorted(
        tid for tid, t in tasks_state.items() if t.get("status") == "blocked"
    )


def _detect_deadlock(tasks_state, task_meta):
    stuck = []
    # 用 meta（非仅 state）建 ID 集，确保依赖存在性检测覆盖全部已定义任务。
    all_ids = set(task_meta.keys())

    for tid, t_state in tasks_state.items():
        status = t_state.get("status")
        if status not in ("pending", "rework"):
            continue
        meta = task_meta.get(tid, {})
        deps = meta.get("depends", [])
        if not deps:
            continue

        broken = []
        for d in deps:
            if d not in all_ids:
                broken.append("{}:不存在".format(d))
            elif d == tid:
                broken.append("{}:自依赖".format(d))
        if broken:
            stuck.append("{}[{}]".format(tid, ";".join(broken)))

    if not stuck:
        stuck = _detect_cycle(tasks_state, task_meta)
    return stuck


def _detect_cycle(tasks_state, task_meta):
    # 运行时死锁检测：只在未完成（pending/rework）子集上跑、排除已 verified 节点，
    # 用 Kahn 拓扑排序找残留强连通分量。与 tasks._check_cycles 不是重复——后者
    # 是 plan 期全集结构校验（三色 DFS）；本处输入域与算法均不同，勿强行合并。
    unsatisfied = {
        tid for tid, t in tasks_state.items()
        if t.get("status") in ("pending", "rework")
    }
    if not unsatisfied:
        return []

    # 每个未完成节点的去重依赖（限定在未完成子集内）。去重以防 tasks.json 被篡改
    # 出现重复依赖元素，导致入度重复计数、误报环。
    deps_of = {
        tid: {d for d in task_meta.get(tid, {}).get("depends", []) if d in unsatisfied}
        for tid in unsatisfied
    }
    # 入度 = 子集内未满足依赖数；反向邻接表：d → 依赖 d 的未完成节点，供拓扑减入度。
    indeg = {tid: len(deps) for tid, deps in deps_of.items()}
    dependents = {tid: [] for tid in unsatisfied}
    for tid, deps in deps_of.items():
        for d in deps:
            dependents[d].append(tid)

    queue = [tid for tid, d in indeg.items() if d == 0]
    while queue:
        tid = queue.pop()
        for other in dependents[tid]:
            indeg[other] -= 1
            if indeg[other] == 0:
                queue.append(other)

    cycle_ids = [tid for tid, d in indeg.items() if d > 0]
    if cycle_ids:
        return ["{}[依赖环]".format(tid) for tid in cycle_ids]
    return []


def _format_blocked_reason(tasks_state, task_ids):
    def _cause(tid):
        issue = tasks_state.get(tid, {}).get("issue") or {}
        return issue.get("cause", "未指定原因")
    return _flatten_reason(" ".join(
        "[{}] {}".format(tid, _cause(tid)) for tid in task_ids
    ))


def _instr_agent(name, agent_name, task_ids):
    obj = {"name": name, "agent_name": agent_name, "task_ids": task_ids}
    return ["@agent {}".format(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))]


def _instr_reviewer(name, task_ids):
    return _instr_agent(name, "bewater-reviewer", task_ids)


def _instr_human_blocked(name, task_ids, reason):
    return ["@human {}".format(json.dumps({
        "name": name, "task_ids": task_ids, "reason": reason,
    }, ensure_ascii=False, separators=(",", ":")))]


def _instr_control(name, status, reason=""):
    # @control 是给编排层（bewater-build）的控制信号，区别于 @agent（派活）/ @human（受阻通知）。
    obj = {"name": name, "status": status}
    if reason:
        obj["reason"] = reason
    return ["@control {}".format(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))]


def next_action(name):
    st = state.load(name)
    tasks_state = st.get("tasks", {})

    if not tasks_state:
        return _instr_control(name, "not_ready")

    task_meta = tasks.meta_by_id(name)
    stage = state.current_stage(tasks_state)

    # state 有进度但 tasks.json 缺失/损坏：继续派发会把相互依赖的任务当成无依赖并行，
    # 输出 error 让人工介入。
    if not task_meta and tasks_state:
        return _instr_control(
            name, "error",
            "tasks_meta_missing: state.json 有 {} 个任务但 tasks.json 缺失或损坏，"
            "请人工检查 .bewater/requirements/{}/".format(len(tasks_state), name)
        )

    # state/tasks 任务集合应一一对应（tasks.init 同事务写两者）。不一致只发生于
    # 崩溃/外部编辑，此时阶段推导与派发都不可信。报错让人工介入，不静默自愈。
    meta_ids = set(task_meta.keys())
    state_ids = set(tasks_state.keys())
    if meta_ids != state_ids:
        only_meta = sorted(meta_ids - state_ids)
        only_state = sorted(state_ids - meta_ids)
        detail = []
        if only_meta:
            detail.append("tasks.json 有但 state.json 无: {}".format(",".join(only_meta)))
        if only_state:
            detail.append("state.json 有但 tasks.json 无: {}".format(",".join(only_state)))
        return _instr_control(
            name, "error",
            "task_set_mismatch: {}。请人工检查 .bewater/requirements/{}/"
            "（state.json 与 tasks.json 的任务集合不一致）".format("; ".join(detail), name)
        )

    if stage == "tdd":
        lines = _handle_active(name, tasks_state, task_meta)
    elif stage == "review":
        lines = _handle_review(name, tasks_state)
    elif stage == "finished" and not state.has_blocked(tasks_state):
        lines = _instr_control(name, "finished")
    else:
        lines = []

    return _attach_blocked(lines, name, tasks_state)


def _attach_blocked(lines, name, tasks_state):
    """受阻任务后处理：每轮严格一行。有 blocked 即以 @human 覆盖（blocked 优先，
    需人介入，冻住同轮的 tdd/reviewer 派发）；唯 error 优先于 blocked——error
    表示状态已损坏，派发与受阻通知都无意义，原样保留让人工先修损坏。"""
    if not state.has_blocked(tasks_state):
        return lines
    if _has_error(lines):
        return lines
    blocked = _scope_blocked(tasks_state)
    return _instr_human_blocked(name, blocked, _format_blocked_reason(tasks_state, blocked))


def _has_error(lines):
    # 契约保证 lines 仅含 @control/@agent/@human 指令行，检查 error 状态
    # 只需匹配 JSON payload 中的 status 字段值。
    for line in lines:
        if line.startswith("@control") and '"status":"error"' in line:
            return True
    return False


def _handle_active(name, tasks_state, task_meta):
    """就绪派 TDD → 否则派审查 → 否则查死锁或等受阻解除。"""
    tids = _scope_ready(tasks_state, task_meta)
    if tids:
        return _instr_agent(name, "bewater-tdd", [tids[0]])

    done = _scope_implemented(tasks_state)
    if done:
        return _instr_reviewer(name, [done[0]])

    # 没有就绪也没有 implemented：可能死锁（cycle）或 pending 依赖 blocked 任务。
    # 前者报 error，后者由 _attach_blocked 处理。
    unresolved = _detect_deadlock(tasks_state, task_meta)
    if unresolved:
        return _instr_control(name, "error", "deadlock_deps: {}".format(
            ",".join(unresolved)))
    return []


def _handle_review(name, tasks_state):
    done = _scope_implemented(tasks_state)
    if not done:
        # 无待审 implemented 任务——不应发生（调用方保证有 implemented 才进 review），
        # 但防御兜底避免 done[0] IndexError。返回空让调用方兜底 fallthrough。
        return []
    return _instr_reviewer(name, [done[0]])