---
name: dual-review
description: 双重评审工作流——同时启动 Claude Code 内部评审和 Codex CLI 外部评审。当进入任何评审环节时必须使用此技能：设计文档评审、实现计划评审、代码评审、spec review。即使你已经准备好启动 code-reviewer agent，也必须先调用此技能以确保 Codex CLI 评审同时启动。触发词：评审、review、code review、请评审、检查代码、审查。如果你正在执行 brainstorming 的 spec review 步骤或 requesting-code-review 流程，同样必须调用此技能。
---

# 双重评审工作流

一次调用，同时启动两路独立评审。解决"容易忘记启动 Codex CLI 外部评审"的问题。

## 核心规则

**在同一条回复消息中，同时发出两个 tool call（均设置 `run_in_background: true`）：**

1. **Agent 工具** → Claude Code 内部评审（后台）
2. **Bash 工具** → Codex CLI 外部评审（后台）

两路评审完成后汇总去重，再向用户呈现合并结果。

## 调用方式

```
/dual-review design <文件路径> [主题名]
/dual-review plan <文件路径> [主题名]
/dual-review code --uncommitted [主题名]
/dual-review code --base <分支> [主题名]
```

也可无参调用 `/dual-review`，根据上下文自动判断。

## 参数解析

从 args 字符串中提取：

| 参数 | 说明 | 示例 |
|------|------|------|
| 评审类型 | `design` / `plan` / `code` | design |
| 评审目标 | 文件路径 或 `--uncommitted` 或 `--base <分支>` | docs/specs/v4-design.md |
| 主题名 | 可选，用于输出文件名；未提供则从文件名/分支名推断 | v4-design |

**日期**：使用当天日期，格式 `YYYY-MM-DD`。

## 输出路径

```
docs/reviews/{日期}-{主题名}-codex-review.md
```

## 执行步骤

### 步骤 1：确定参数并准备上下文

根据 args 或对话上下文确定评审类型、目标、主题名。如果信息不足，向用户确认。

**代码评审额外准备**：在启动评审前，先运行 git 命令获取 SHA：

```bash
# --uncommitted 模式
BASE_SHA=$(git rev-parse HEAD)
# HEAD_SHA 不适用，code-reviewer 会直接看 working tree

# --base <分支> 模式
BASE_SHA=$(git merge-base <分支> HEAD)
HEAD_SHA=$(git rev-parse HEAD)
```

### 步骤 2：构造两路评审并同时启动

**在同一条消息中**发出以下两个 tool call：

---

#### 路线 A：Claude Code 内部评审

使用 Agent 工具，`run_in_background: true`。

这个 skill 不绑定特定的 agent 类型或 prompt 模板。根据当前环境中可用的评审相关 skill/agent 自行选择最合适的方式。例如：

- 如果有 `superpowers:code-reviewer` agent → 用它做代码评审，按其配套的 `requesting-code-review` skill 模板构造 prompt
- 如果有其他代码评审 agent（如 bmad 等）→ 用该 agent
- 如果没有专用评审 agent → 用 `general-purpose` agent

**唯一的硬性要求**：
1. `run_in_background: true`
2. prompt 中明确评审目标（文件路径 / git 范围）和评审重点
3. 要求输出按 Critical / Important / Suggestion 分级

---

#### 路线 B：Codex CLI 外部评审

使用 Bash 工具，`run_in_background: true`。

根据评审类型构造 codex 命令。先确保 `docs/reviews/` 目录存在，再执行 codex。

**评审类型与 codex 命令对应关系：**

| 评审类型 | codex 命令 | 输出方式 |
|----------|-----------|----------|
| 设计文档 | `codex exec -s read-only -o {输出路径} "{评审 prompt}"` | `-o` 直接写文件 |
| 实现计划 | `codex exec -s read-only -o {输出路径} "{评审 prompt}"` | `-o` 直接写文件 |
| 代码（uncommitted） | `codex review --uncommitted` | 管道重定向保存 |
| 代码（branch diff） | `codex review --base {分支}` | 管道重定向保存 |

**codex exec 的评审 prompt**：要求以中文评审，说明评审目标、评审重点、按 Critical/Important/Suggestion 分级输出，声明只读不修改文件。

**关键约束：**
- `codex review` 不支持 `-o`，需要通过管道保存输出
- `codex review` 不支持自定义 prompt，如需自定义请改用 `codex exec -s read-only`

**平台适配：**
根据系统环境信息中的 Platform 和 Shell 构造命令：
- **Windows（Git Bash 调 PowerShell）**：codex 通过 `powershell -Command "..."` 调用，管道用 `Tee-Object`
- **Mac/Linux/WSL**：直接调用 `codex`，管道用 `tee`

### 步骤 3：等待结果并汇总

两路评审都在后台运行。完成后：

1. 读取 Codex CLI 评审报告（`docs/reviews/{日期}-{主题名}-codex-review.md`）
2. 获取 Claude Code agent 返回的评审结果
3. 合并去重，按 Critical → Important → Suggestion 排序
4. 向用户呈现合并后的评审意见

### 步骤 4：处理失败

- **Codex CLI 失败**：报告错误，继续使用 Claude Code 评审结果，不阻塞流程
- **Claude Code agent 失败**：报告错误，等待并使用 Codex CLI 结果
- **两路都失败**：向用户报告，建议手动评审

## 注意事项

- 评审报告文件名不要有空格，用连字符连接
- codex 命令如果执行超时或失败，不阻塞整体流程——报告错误即可

## 示例

### 示例 1：设计文档评审

**用户说**：评审一下设计文档 docs/superpowers/specs/2026-03-17-v4-design.md

**执行**：在同一条消息中发出两个 tool call——

Tool call 1（内部评审，选择当前可用的评审 agent）:
```
Agent(
  run_in_background: true,
  description: "评审 v4-design",
  prompt: "Review the design document at docs/superpowers/specs/2026-03-17-v4-design.md. Focus on: ..."
)
```

Tool call 2（Codex CLI，根据平台构造命令）:
```
Bash(
  run_in_background: true,
  command: <mkdir -p docs/reviews && 平台适配的 codex exec 命令>
)
```

### 示例 2：代码评审

**用户说**：评审一下当前未提交的改动

**执行**：先获取 git SHA，再在同一条消息中发出两个 tool call——

Tool call 1（内部评审，选择当前可用的评审 agent）:
```
Agent(
  run_in_background: true,
  description: "代码评审 uncommitted",
  prompt: "Review all uncommitted changes. BASE_SHA=HEAD. Focus on: code quality, edge cases, test coverage. Categorize as Critical/Important/Suggestion."
)
```

Tool call 2（Codex CLI，根据平台构造命令）:
```
Bash(
  run_in_background: true,
  command: <mkdir -p docs/reviews && 平台适配的 codex review --uncommitted 命令，管道保存到输出路径>
)
```
