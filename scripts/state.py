"""state.json 读写与阶段自动推导。"""

import json
import os
import re
import sys

from . import REQUIREMENTS_DIR, utc_now, DEFAULT_EXECUTOR, REWORK_LIMIT, unique_tmp

VALID_STATUS = frozenset({"pending", "implemented", "verified", "rework", "blocked"})
VALID_EXECUTORS = frozenset({"bewater-tdd", "bewater-reviewer", DEFAULT_EXECUTOR})
# criteria 合法值：正整数（对应验收标准编号）或字符串 "?"（task 级阻塞，
# 不指向具体某条验收标准）。
ISSUE_FIELDS = {"criteria", "cause", "fix"}
MAX_HISTORY = 50
# history 裁剪：保留头部 HISTORY_HEAD 条（最早的原始问题）+ 尾部 HISTORY_TAIL 条（近期完整）
# + 1 条省略标记，合计不超过 MAX_HISTORY。
HISTORY_HEAD = 5
HISTORY_TAIL = MAX_HISTORY - HISTORY_HEAD - 1

ALLOWED_TRANSITIONS = {
    "pending":     frozenset({"pending", "implemented", "blocked"}),
    "implemented": frozenset({"implemented", "verified", "rework", "blocked"}),
    "rework":      frozenset({"rework", "implemented", "blocked"}),
    "verified":    frozenset({"verified"}),
    "blocked":     frozenset({"blocked", "pending"}),
}


def validate_name(name):
    """校验需求名。

    禁止空白字符是因为 `@agent ... "name":"<名称>" ...`
    这类指令协议按空白切分 token，含空格的名称会把指令拆断。
    """
    if not isinstance(name, str) or not name:
        raise ValueError("需求名不能为空")
    if name in (".", ".."):
        raise ValueError("需求名非法: {}".format(name))
    if name.startswith("/"):
        raise ValueError("需求名不能为绝对路径: {}".format(name))
    if "/" in name or "\\" in name:
        raise ValueError("需求名不能含路径分隔符: {}".format(name))
    if re.search(r"[\s\x00-\x1f]", name):
        raise ValueError("需求名不能含空白或控制字符: {!r}".format(name))


def dir_path(name):
    validate_name(name)
    return os.path.join(REQUIREMENTS_DIR, name)


def state_path(name):
    return os.path.join(dir_path(name), "state.json")


def list_names():
    if not os.path.isdir(REQUIREMENTS_DIR):
        return []
    names = []
    for entry in os.listdir(REQUIREMENTS_DIR):
        if entry.startswith("."):
            continue
        full = os.path.join(REQUIREMENTS_DIR, entry)
        if not os.path.isdir(full):
            continue
        try:
            has_state = os.path.exists(state_path(entry))
            has_tasks = os.path.exists(os.path.join(dir_path(entry), "tasks.json"))
        except ValueError:
            continue
        if has_state or has_tasks:
            names.append(entry)
    return sorted(names)


def load(name):
    """加载 state.json。文件不存在或损坏则回退空状态——不抛异常，
    避免 build 循环因写到一半崩溃的坏 JSON 卡死。"""
    path = state_path(name)
    if not os.path.exists(path):
        return {"tasks": {}}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        print("⚠️  state.json 损坏（{}），已回退为空状态。".format(e), file=sys.stderr)
        return {"tasks": {}}


def load_strict(name):
    """加载 state.json，文件损坏时抛 ValueError（而非静默回退）。

    供需要区分「真无进度」与「文件损坏」的守卫使用（cmd_new/cmd_plan）——
    回退空状态会让守卫误判为「无进度」而放行覆盖，丢失损坏文件中可能
    仍存在的进度。文件不存在时返回空状态（属正常初始状态）。
    """
    path = state_path(name)
    if not os.path.exists(path):
        return {"tasks": {}}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(
            "state.json 损坏（{}），无法判断是否已有进度。"
            "请人工修复或删除 .bewater/requirements/{}/state.json 后重试。".format(e, name)
        )


def save(name, data):
    path = state_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    trimmed = data
    tasks = data.get("tasks")
    if isinstance(tasks, dict):
        trimmed_tasks = {}
        for tid, t in tasks.items():
            hist = t.get("history")
            if isinstance(hist, list) and len(hist) > MAX_HISTORY:
                t = dict(t)
                t["history"] = _trim_history(hist)
            trimmed_tasks[tid] = t
        trimmed = dict(data)
        trimmed["tasks"] = trimmed_tasks

    tmp = unique_tmp(path)
    with open(tmp, "w") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _trim_history(hist):
    """裁剪超长 history：保留头部 HISTORY_HEAD 条 + 尾部 HISTORY_TAIL 条。

    多轮返工中最早的原始问题往往最有价值，纯留尾（hist[-N:]）会丢掉它。
    留头留尾，中间插入一条省略标记记录被丢弃的条数，兼顾可追溯与体积。
    """
    keep_total = HISTORY_HEAD + HISTORY_TAIL
    if len(hist) <= keep_total:
        return hist[-MAX_HISTORY:]
    head = hist[:HISTORY_HEAD]
    tail = hist[-HISTORY_TAIL:]
    omitted = len(hist) - keep_total
    return head + [{"status": "…", "ts": None, "executor": None,
                    "issue": None, "_omitted": omitted}] + tail


def _commit(st, name, tid, target_status, executor, issue):
    now = utc_now()
    prev = st["tasks"].get(tid, {})
    if "history" not in prev:
        prev["history"] = []
    # history 嵌入当时的 issue 快照，多轮 rework 时前序 cause/fix 不被清除，
    # 执行 Agent 可经 flow get 回看。非 rework/blocked 状态 issue 为 None。
    prev["history"].append({
        "status": target_status, "ts": now,
        "executor": executor, "issue": issue,
    })
    if target_status == "rework":
        prev["rework_count"] = prev.get("rework_count", 0) + 1
    prev["status"] = target_status
    prev["executor"] = executor
    prev["issue"] = issue
    save(name, st)
    return st


def current_stage(tasks_state):
    # 优先级顺序（从紧急到宽松）：
    #   rework → implemented → pending → blocked（全结束时有受阻）→ finished
    # 第一匹配跳出，故 rework 优先于 implemented（返工先修，review 后审）。
    if not tasks_state:
        return "plan"
    statuses = {t.get("status", "pending") for t in tasks_state.values()}
    if "rework" in statuses:
        return "tdd"
    if "implemented" in statuses:
        return "review"
    if "pending" in statuses:
        return "tdd"
    if "blocked" in statuses:
        # 全部任务已结束且至少一个 blocked：唯一剩余工作需要人工处理。
        return "blocked"
    return "finished"


def _validate_row(row):
    if "task_id" not in row:
        raise ValueError("缺少必填字段: task_id")
    tid = row["task_id"]
    if not isinstance(tid, str) or not tid:
        raise ValueError("task_id 必须为非空字符串，收到: {!r}".format(tid))
    if "status" not in row:
        raise ValueError("缺少必填字段: status")
    if row["status"] not in VALID_STATUS:
        raise ValueError("非法 status: {}，允许: {}".format(row["status"], sorted(VALID_STATUS)))
    if "executor" in row and row["executor"] not in VALID_EXECUTORS:
        raise ValueError("非法 executor: {}，允许: {}".format(row["executor"], sorted(VALID_EXECUTORS)))

    status = row["status"]
    issue = row.get("issue")
    if status in ("rework", "blocked"):
        if not issue or not isinstance(issue, dict):
            raise ValueError("status={} 必须附带 issue 字段".format(status))
        for key in ISSUE_FIELDS:
            if key not in issue:
                raise ValueError("issue 缺少字段: {}".format(key))
        # cause/fix 必须为非空字符串（criteria 可为 "?" 或整数，单独放过）。
        for key in ("cause", "fix"):
            v = issue.get(key)
            if not isinstance(v, str) or not v.strip():
                raise ValueError("issue.{} 不能为空".format(key))
    elif issue is not None:
        raise ValueError("非 rework/blocked 状态不应附带 issue 字段")


def _default_task_state(history=None):
    """新建 task 的初始运行时状态骨架。tasks.init 与 update 首次上报两处都需补建，
    抽到一处避免加字段时漏改。rework_count 独立维护、不依赖会被 _trim_history 裁剪的 history。"""
    return {
        "status": "pending",
        "executor": DEFAULT_EXECUTOR,
        "issue": None,
        "history": history if history is not None else [],
        "rework_count": 0,
    }


def update(name, row):
    _validate_row(row)

    st = load(name)
    tasks_state = st.get("tasks", {})
    tid = row["task_id"]
    target_status = row["status"]

    if tid not in tasks_state:
        tasks_state[tid] = _default_task_state()

    prev = tasks_state[tid]
    current_status = prev.get("status", "pending")

    allowed = ALLOWED_TRANSITIONS.get(current_status, frozenset())
    if target_status not in allowed:
        raise ValueError(
            "非法状态流转: {} → {}，允许的流转: {}".format(
                current_status, target_status, sorted(allowed)
            )
        )

    # 受阻解除（blocked → pending）：人工已介入处理，重新开始一轮实现-审查。
    # rework_count 是「累计返工」记账，不随解阻归零会让任务解阻后再次返工即
    # 触发上限（rework_count 仍 ≥ REWORK_LIMIT）→ 立刻又自动 blocked，等于
    # 白解阻。解阻即重置计数，给修复后的新一轮实现-审查完整额度。
    # 必须在 _commit 前改 prev——_commit 是唯一落盘点，之后改内存不落盘。
    if target_status == "pending" and current_status == "blocked":
        prev["rework_count"] = 0

    # criteria 合法性由 tasks 在 cli.cmd_report 预检，避免 state 反向依赖 tasks。
    issue = row.get("issue")
    executor = row.get("executor", prev.get("executor", DEFAULT_EXECUTOR))
    st = _commit(st, name, tid, target_status, executor, issue)

    # 返工达上限：同事务内升级 blocked，沿用本次 issue 并在 cause 追加注记。
    if target_status == "rework" and st["tasks"][tid].get("rework_count", 0) >= REWORK_LIMIT:
        issue_block = dict(issue or {})
        issue_block.setdefault("criteria", "?")
        cause = issue_block.get("cause", "") or ""
        note = "（累计返工 {} 次已达上限，已自动升级为 blocked）".format(REWORK_LIMIT)
        if note not in cause:
            issue_block["cause"] = (cause + " " + note).strip() if cause else note
        st = _commit(st, name, tid, "blocked", DEFAULT_EXECUTOR, issue_block)
        print("⚠️  {} 累计返工≥{}次，已自动升级为 blocked".format(
            tid, REWORK_LIMIT), file=sys.stderr)

    return st


def has_blocked(tasks_state):
    return any(t.get("status") == "blocked" for t in tasks_state.values())


def deps_satisfied(tasks_state, task_meta, task_id):
    """检查前置依赖是否全部 verified。

    只认 verified——implemented 仍可能被打回返工，不应作为开工的充分条件。

    若 task_id 不在 task_meta 中（tasks.json 缺失/损坏/不一致），保守返回
    False 而非当成无依赖——避免在元数据残缺时把相互依赖的任务并行派发。
    """
    meta = task_meta.get(task_id)
    if meta is None:
        return False
    deps = meta.get("depends", [])
    if not deps:
        return True
    return all(
        tasks_state.get(d, {}).get("status") == "verified"
        for d in deps
    )