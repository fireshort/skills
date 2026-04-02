---
name: dual-review
description: 双重评审工作流——同时启动 Claude Code 内部评审和 Codex 插件外部评审。触发词：评审、review、code review、请评审、检查代码、审查。当执行 brainstorming 的 spec review 或 requesting-code-review 流程时同样必须调用。
---

# 双重评审工作流

一次调用，同时启动两路独立评审。依赖 codex 插件（`/codex:setup` 验证）处理所有 Codex CLI 细节。

## 核心规则

**在同一条回复消息中，发出两个 Agent tool call（均设置 `run_in_background: true`）：**

1. **Agent A** → Claude Code 内部评审
2. **Agent B** → 通过 codex 插件进行外部评审

两路完成后汇总去重，向用户呈现合并结果。

## 调用方式

```
/dual-review code --uncommitted [主题名]
/dual-review code --base <分支> [主题名]
/dual-review design <文件路径> [主题名]
/dual-review plan <文件路径> [主题名]
/dual-review  # 根据上下文自动判断
```

## 执行步骤

### 步骤 1：确定参数

从 args 或对话上下文确定评审类型和目标。信息不足时向用户确认。

### 步骤 2：同时启动两路评审

在同一条消息中发出两个 Agent tool call，均设置 `run_in_background: true`：

#### Agent A：Claude Code 内部评审

选择当前可用的评审 agent（优先 `superpowers:code-reviewer`，否则 `general-purpose`）。

prompt 要求：
- 明确评审目标（文件路径 / git 范围）
- 输出按 Critical / Important / Suggestion 分级

#### Agent B：Codex 插件外部评审

使用 `general-purpose` agent，prompt 中指示使用 Skill 工具调用对应的 codex 插件命令：

| 评审类型 | Skill 调用 | args |
|----------|-----------|------|
| 代码（uncommitted） | `codex:review` | `--wait` |
| 代码（branch diff） | `codex:review` | `--wait --base <分支>` |
| 设计文档 | `codex:rescue` | `--wait 以中文评审设计文档 <路径>，关注设计合理性、风险、遗漏，按 Critical/Important/Suggestion 分级` |
| 实现计划 | `codex:rescue` | `--wait 以中文评审实现计划 <路径>，关注可行性、遗漏、风险，按 Critical/Important/Suggestion 分级` |

**关键**：args 中必须包含 `--wait`，确保在 agent 内前台执行（agent 本身已在后台）。

### 步骤 3：汇总结果

两路完成后：
1. 合并去重，按 Critical → Important → Suggestion 排序
2. 向用户呈现合并后的评审意见

### 步骤 4：处理失败

- 一路失败：报告错误，使用另一路结果
- 两路都失败：报告错误，建议手动评审
