---
name: dual-review
description: 双重评审工作流——同时启动 Claude Code 内部评审和 Codex 插件外部评审。触发词：评审、review、code review、请评审、检查代码、审查。当执行 brainstorming 的 spec review 或 requesting-code-review 流程时同样必须调用。
---

# 双重评审工作流

一次调用，同时启动两路独立评审。依赖 codex 插件（`/codex:setup` 验证）处理所有 Codex CLI 细节。

## 核心规则

**在同一条回复消息中，发出两个 Agent tool call（均设置 `run_in_background: true`）：**

1. **Agent A** → Claude Code 内部评审（`general-purpose`；若系统提示的可用 agent 列表中有专职评审 agent 则优先用——以列表为准判定，不靠试错）
2. **Agent B** → Codex 外部评审（`subagent_type: codex:codex-rescue`）

每个 agent 的 prompt 必须包含具体评审目标（文件路径 / git 范围）和输出要求；目标不明确时先向用户确认，禁止启动无具体目标的占位 agent。

必须等两路都到达终态（成功或判定失败）再汇总；一路长期未完成时，向用户报告 pending 状态并声明该路未完成验证。

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

评审范围按类型定义，**两路必须审同一范围**（否则合并结果有盲区）：

| 评审类型 | 评审范围 |
|----------|---------|
| 代码（uncommitted） | git diff（含 staged）+ 未跟踪文件 |
| 代码（branch diff） | git diff <分支>...HEAD |
| 设计文档 / 实现计划 | 指定文件路径 |

### 步骤 2：同时启动两路评审

在同一条消息中发出两个 Agent tool call，均设置 `run_in_background: true`：

#### Agent A：Claude Code 内部评审

prompt 要求：
- 按上表写明评审范围（uncommitted 必须明确覆盖未跟踪文件）
- 声明「只评审、不修改文件、不调用其他评审 agent」
- 输出以中文按 Critical / Important / Suggestion 分级

#### Agent B：Codex 外部评审

使用 Agent tool，`subagent_type: codex:codex-rescue`，prompt 直接写评审请求（rescue agent 是转发器，基本原样转交 Codex，仅允许收紧措辞）。

> **不要**让子代理经 Skill 工具调用 `codex:review` / `codex:adversarial-review`——这些命令带 `disable-model-invocation: true`，只能由用户手动触发，模型或子代理调用必然失败。`codex:codex-rescue` 才是插件提供的模型可调用入口。

prompt 必须包含三个要素：

1. **评审范围**：按上表写明；设计文档另加关注点「设计合理性、风险、遗漏」，实现计划另加「可行性、遗漏、风险」。
2. **`--wait`**：必须带。否则 rescue 对较大的评审会自行转后台执行，而 `codex:status` / `codex:result` 都是用户专用命令，编排方永远拿不回结果，该路评审会静默丢失。
3. **「只评审、不修改任何文件，输出按 Critical/Important/Suggestion 分级」**：rescue 契约对 review 类请求本就不加写权限，此句显式加固该判定并约束 Codex 行为。注意这是指令级约束，不是沙箱隔离。

### 步骤 3：汇总结果

两路都到达终态后：
1. 合并去重：同一问题两路严重级别不同时取更高级别；两路结论冲突时并列展示并标注来源，不擅自丢弃任何一方
2. 按 Critical → Important → Suggestion 排序，向用户呈现合并结果

### 步骤 4：处理失败

**失败判定**：Agent B 返回为空或不含评审内容时，一律视为该路失败（rescue 契约规定调用失败时返回空）。失败信息含限额/重置时间（usage limit / session limit / rate limit）的按限额类处理，否则按其他失败处理。

- **单路限额失败**：暂时性失败。向用户报告重置时间；重置后补跑同一路，**每路最多补跑 1 次**，补跑仍失败则按其失败类型报告并终止。补跑完成前必须明确声明「该路评审未完成，不算已完成验证」——限额失败不允许静默以单路结果收场。
- **单路其他失败**：报告错误原因，用另一路结果收场，并明确说明本次只有单路评审通过（仅非限额失败允许单路收场）。错误指向 Codex 未安装/未认证时，提示用户运行 `/codex:setup`。
- **两路都失败**：
  - 均为限额失败 → 报告两路重置时间，重置后各补跑一次（同样受最多 1 次限制）。
  - 含非限额失败 → 报告错误，建议用户手动补评审：`/codex:review` 只能补外部一路，内部评审需重新发起。
