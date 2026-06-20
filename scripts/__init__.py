"""bewater-flow 公共常量与工具。"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# DEFAULT_EXECUTOR 是个 executor 标签（非可调 Agent），用于编排层/初始状态的默认标记。
# 无对应 agents/bewater-orchestrate.md——编排角色由 skills/bewater-build 承担。
DEFAULT_EXECUTOR = "bewater-orchestrate"

# 返工上限：累计 rework 达此次数，state.update 同事务内自动升级为 blocked。
REWORK_LIMIT = 3

# design.md 模板路径（hard link，tasks.load_with_state 用它构造 design_path 返回给 Agent）。
DESIGN_TEMPLATE_NAME = "design.md"

_project_root_cache = None


def _find_project_root():
    """从 CWD 向上查找 .bewater/flow，确定 workspace 根目录。"""
    cwd = Path.cwd()
    candidate = cwd
    while True:
        if (candidate / ".bewater" / "flow").is_file():
            return str(candidate)
        parent = candidate.parent
        if parent == candidate:
            return str(cwd)
        candidate = parent


def project_root():
    """惰性计算 project root，首次访问时扫描并缓存。"""
    global _project_root_cache
    if _project_root_cache is None:
        _project_root_cache = os.environ.get(
            "BEWATER_ROOT", _find_project_root()
        )
    return _project_root_cache


# BEWATER_BASE：环境变量优先（测试用），否则退回 project_root() 惰性扫描（仅首次磁盘 I/O）。
BEWATER_BASE = os.environ.get("BEWATER_BASE") or os.path.join(
    project_root(), ".bewater"
)

# 需求实例存放根目录，与工具自身代码（scripts/templates）隔离；覆盖 BEWATER_BASE 即一并隔离。
REQUIREMENTS_DIR = os.path.join(BEWATER_BASE, "requirements")


def utc_now():
    """返回 ISO 格式当前 UTC 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def unique_tmp(path):
    """返回基于 path 的唯一临时文件名，再由 os.replace 原子替换落盘。

    写临时文件 → os.replace 是原子的：写到一半进程崩溃，要么旧文件完整、
    要么新文件完整，绝不留半截损坏 JSON。mkstemp 给唯一名避免与同目录其他
    临时文件相撞（哪怕单进程串行写，replace 也只换它自己那个临时名）。
    """
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp",
        dir=os.path.dirname(path),
    )
    os.close(fd)
    return tmp