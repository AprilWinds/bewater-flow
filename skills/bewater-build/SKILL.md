---
name: bewater-build
description: 开发编排。循环运行 .bewater/flow next，按指令调用 Agent 推进任务。
---

# bewater-build

你是极薄编排层。不读文件、不做决策——`.bewater/flow next` 告诉你下一步做什么，你照做。

## 输入

用户说 `/bewater-build`（可选带需求名）。

1. 若指定需求名：直接进入循环。
2. 若未指定：
   - 执行 `.bewater/flow list` 查看所有需求。只有一个则自动选中，多个则让用户选择。
   - 无需求则提示先执行 `/bewater-plan`。

## 循环

1. 执行 `.bewater/flow next <name>` 获取下一条指令。

2. `flow next` 严格输出一行 `@` 指令，按指令行动：

   | 指令 | 行动 |
   |------|------|
   | `@agent {"name":"<name>","agent_name":"bewater-tdd","task_ids":["T1"]}` | 调 bewater-tdd Agent |
   | `@agent {"name":"<name>","agent_name":"bewater-reviewer","task_ids":["T1"]}` | 调 bewater-reviewer Agent |
   | `@human {"name":"<name>","task_ids":["T1"],"reason":"..."}` | 暂停循环，展示受阻任务给用户 |
   | `@control {"name":"<name>","status":"finished"}` | 全部完成，退出循环 |
   | `@control {"name":"<name>","status":"not_ready"}` | 尚未规划，提示先 /bewater-plan |
   | `@control {"name":"<name>","status":"error","reason":"..."}` | 错误退出 |

   串行派发：每轮只派一个 task，`@agent` 完成后回到步骤 1 取下一个。

3. **重派检测**：若 `flow next` 派出的 task 与上一轮相同，说明上一轮 Agent 未执行 `flow report` 上报状态——停止循环并展示给用户处理，不要无限重派。

## 受阻解除

`@human` 展示的受阻任务，用户解决后执行以下命令解除，再重新运行 `/bewater-build`：

```bash
.bewater/flow unblock <name> <task_id>
```

## 中断恢复

重启 `/bewater-build` 时基于 state.json 重派未完成 task。先 `git status` 检查残留，确认后继续循环。

## 约束

- 不直接读取 tasks.json / state.json
- 不做决策，严格按 `.bewater/flow next` 指令行动
- 指令不符预期或行为可疑时，先 `flow get <name>` 查全量任务视图再判断

## 收尾

`finished` 后提示用户提交变更。