---
name: dual-review
description: 跨 Claude Code 与 Codex 的双重评审工作流。在 Claude Code 中运行 Claude 内部评审与 Codex 插件外部评审；在 Codex 中运行 Codex 内部评审与 Claude Code CLI 外部评审，并在 Claude 明确限额失败时自动改用 Antigravity CLI 的 Gemini 4 Neon（High）。用户提出评审、review、code review、请评审、检查代码、审查，或流程要求进行 spec review、plan review、requesting-code-review 时使用。
---

# 双重评审工作流

在当前宿主中启动一条内部评审，再调用另一产品完成独立外部评审。先识别当前宿主，再选择对应路径；严禁混用两端专属工具。

| 当前宿主 | 内部评审 | 外部评审 |
|----------|----------|----------|
| Claude Code | Claude Code Agent | 安装在 Claude Code 中的 Codex 插件 `codex:codex-rescue` |
| Codex | Codex subagent | 优先由 helper 后台运行 Claude Code CLI；明确限额失败并完成 cleanup 后，自动改用 Antigravity CLI 的 Gemini 4 Neon（High） |

本文所说的“Codex 插件”专指安装在 Claude Code 中的插件。Codex 宿主不调用该插件，也不使用 `codex:codex-rescue` 或 `--wait`；Claude Code 宿主不得调用本 skill 的 `run_claude_review.py`，避免把“Claude 评审 Claude”误报成跨产品双路评审。

## 核心规则

1. 两路必须评审同一目标和同一范围。
2. 两路 prompt 都必须写明「只评审、不修改文件、不调用 `dual-review`、不调用其他评审 agent」。
3. 启动第一路后立即启动第二路，不等待第一路完成；两路都到达成功或判定失败的终态后再汇总。
4. 外部 reviewer 是 `dual-review` 的组成部分。即使它读到“评审一律调用 dual-review”的规则，也只能输出本路独立意见，不得再次启动双路评审。
5. 只向外部 reviewer 提供指定文档、diff，以及核实所必需的 Git 跟踪源码、测试和配置。禁止发送 `.env`、密钥、token、数据库、用户数据、生产日志和无关私有内容。
6. reviewer 的只读边界必须按“可能只是指令级约束”处理。启动两路前统一记录 `git status` 和目标 diff，结束后重新核对；若 reviewer 产生任何修改，立即报告并将该路判为失败，不自动回退或清理修改。Codex 宿主的外部 Claude 已同时禁用工具并运行在私有临时目录，无需为该路单独重复 Git 核对，但流程结束时的整体核对不能省略。
7. Codex 宿主启动 Claude helper 后必须保存 `state_dir`；无论成功或失败，结果收集后都必须执行 cleanup。cleanup 未成功前不得把临时评审输出与控制状态视为已清除。

## 调用方式

Claude Code：

```text
/dual-review code --uncommitted [主题名]
/dual-review code --base <分支> [主题名]
/dual-review design <文件路径> [主题名]
/dual-review plan <文件路径> [主题名]
/dual-review
```

Codex：

```text
$dual-review code --uncommitted [主题名]
$dual-review code --base <分支> [主题名]
$dual-review design <文件路径> [主题名]
$dual-review plan <文件路径> [主题名]
$dual-review
```

`spec review` 归入 `design` 类型；`plan review` 归入 `plan` 类型。

未带参数时，根据对话上下文确定类型和目标。只有无法从当前任务、工作区和 Git 状态确定具体目标时才向用户确认，禁止启动无目标的占位 reviewer。

## 步骤 1：固定评审范围

| 评审类型 | 评审范围 |
|----------|----------|
| 代码（uncommitted） | working tree diff + staged diff + 未跟踪文件 |
| 代码（branch diff） | `git diff <分支>...HEAD` |
| 设计文档 | 指定文件，并核对相关源码、配置、测试和下游消费方 |
| 实现计划 | 指定文件，并核对计划涉及的真实文件、依赖和验证入口 |

启动前读取适用的项目规则，并用 Git 状态或指定路径确认边界。两路 prompt 使用同一份范围描述；uncommitted 评审必须显式列出未跟踪文件。

固定范围前先对未跟踪文件、目标 diff 和指定文档做敏感性预检：

- 禁止项不属于用户指定目标时，从两路范围中同时排除，并只报告排除的安全路径或类别，不暴露内容。
- 禁止项本身属于用户指定目标时，不启动外部路线；内部路线可继续，但必须按单路评审报告，明确双路验证未完成。
- 不得为了维持“两路同范围”而把敏感内容发送给外部 reviewer，也不得静默让两路评审不同范围。

每路 prompt 至少包含：

- 评审目标和精确范围。
- 「你是 `dual-review` 的一条独立 reviewer。只评审，不修改任何文件；不要调用 `dual-review`、其他 skill 或其他评审 agent；不得读取或外传 `.env`、密钥、token、数据库、用户数据、生产日志和无关私有内容。」
- 对有仓库读取能力的 reviewer，要求对照真实代码、配置、测试、样例输出和下游消费方，不只检查 diff 或文档自洽性。Codex 宿主的外部 Claude 只能依据自包含快照；快照缺少结论所必需的材料时，必须单列「上下文不足」，不得臆测。
- 中文输出，按 Critical / Important / Suggestion 分级；每条写清「问题 → 影响 → 建议改法」并附文件路径和行号证据。
- 若 reviewer 处于 plan mode，明确要求它不创建计划文件、不调用 `ExitPlanMode`，直接输出评审结论。
- 最后给出单一 gate 结论：可进入实现 / 需修改后再进入实现；代码评审对应为可合并 / 需修改后再合并。

设计文档另加关注点「设计合理性、风险、遗漏」；实现计划另加「可行性、遗漏、风险」。

## 步骤 2：识别宿主并启动两路

根据当前系统身份和实际可调用工具识别宿主，不根据仓库名、skill 路径或猜测判断：

- 系统声明当前为 Claude Code，或提供 Claude Code 的 subagent 工具（如 `Agent` / `Task`）→ 走 Claude Code 路径；`codex:codex-rescue` 是否可用只决定外部路能否启动，不改变宿主判断。
- 系统声明当前为 Codex，或提供 Codex subagent/collaboration 工具（如 `spawn_agent`、`wait_agent`）→ 走 Codex 路径。
- 工具与身份冲突时，以系统声明的当前宿主为准，并报告缺失的那一路；不要试调用另一宿主的专属入口。
- 无法判定为 Claude Code 或 Codex 时，只运行可确认的内部评审，按步骤 4 降级为单路并说明宿主未知；不得猜测外部入口。

### Claude Code 路径

在同一条消息中并行发出两个 Agent tool call。当前 Agent tool 支持 `run_in_background` 时，两路均设为后台；不支持时，在同一条消息中并行发出两个前台调用，不先等待任一路完成：

1. **Claude 内部评审**：使用 `general-purpose`；若系统列出的可用 agent 中有专职 reviewer，则优先使用。不要靠试错探测 agent 名。
2. **Codex 外部评审**：若 `codex:codex-rescue` 出现在当前可用 agent 列表中，使用该 `subagent_type`；若不可用，仍启动内部评审，把外部路记为“未安装/不可用”，按步骤 4 降级并提示 `/codex:setup`，不要靠试调用探测。

给 `codex:codex-rescue` 的请求还必须满足：

- 带 `--wait` 路由标志，让 rescue subagent 前台等待并返回完整 stdout。`--wait` 由 Claude Code 执行层消费，不属于转交给 Codex 的任务正文。
- 当前 OpenAI Codex 插件的用户命令 `/codex:rescue` 由 `codex:codex-rescue` agent 实现；README 中的最小探测由用户手动执行。skill 运行时仍只按可用 agent 列表判定，不靠试调用探测。
- 明确「只评审、不修改任何文件，输出按 Critical / Important / Suggestion 分级」。
- 不经 Skill 工具调用 `codex:review` 或 `codex:adversarial-review`。这些入口带 `disable-model-invocation: true`，只能由用户手动触发；模型可调用入口是 `codex:codex-rescue`。

若当前 Claude Code Agent tool 支持工具 allowlist，必须收窄为只读工具；否则适用核心规则中的指令级边界与评审前后 Git 核对。

### Codex 路径

本节仅供 Codex 宿主执行；Claude Code 宿主不得调用本节 helper。

1. **Codex 内部评审**：使用当前 Codex 提供的 `spawn_agent` 或等价 subagent 工具启动独立 reviewer。传入完整评审目标与范围；要求只读、禁止再次调用 reviewer。保留足够的任务上下文，但不要把主 agent 的预设结论写进 prompt。若可收窄 subagent 工具或权限则必须收窄，否则适用核心规则中的评审前后 Git 核对。
2. **Claude Code 外部评审**：先确定可用的 Python 解释器（Windows 仅接受 CPython 3.12.4 及以上版本，优先 `py -3`，再尝试 `python`；macOS/Linux 要求 Python 3.10 及以上版本，优先 `python3`，再尝试 `python`），将实际调用记为 `<python-exe>`；再运行 `<python-exe> --version`、`claude --version` 和 `claude --help`。除确认 flag 存在，还必须确认 `dontAsk` 是 `--permission-mode` 的合法值，并且 `--tools` 帮助明确说明空字符串会禁用全部工具；helper 会用相同检查再次 fail closed。所有平台都必须解析到原生 Claude Code 二进制：Windows 只接受 `.exe`/`.com`，POSIX 只接受 ELF/Mach-O；shell、command 和 script wrapper 可能丢弃空参数，helper 会以 `unsupported_claude_launcher` 拒绝启动，不得绕过。CLI 帮助文案无法证明该契约时也必须 fail closed；helper 的 `cli_contract` 错误会附带实际 `claude --version` 供诊断，但不提供绕过开关。缺失或语义不符时按步骤 4 报告。后续所有 helper 命令必须复用同一个 `<python-exe>`。`<skill-dir>` 取本次已加载的 `SKILL.md` 所在目录，不从当前仓库路径猜测。
3. 用临时 Python builder 在内存中构造完整 review prompt，将其 UTF-8 bytes 编码为不换行的 base64，并由 builder 通过 `subprocess.run(..., input=base64_bytes)` 直接交给对应 helper 的 stdin；任何时候都不得把完整 prompt、base64 或源码快照写入调用方临时文件。内部 reviewer 启动后立即执行 builder：

```text
<python-exe> <builder.py>
```

4. **Antigravity 限额 fallback**：只有 Claude helper 已到 `failed` 终态、`classify-failure --state-dir <state_dir>` 返回 `quota_limited: true` 和本轮 `prompt_sha256`，并且上一轮 `cleanup` 成功后，才由 builder 用完全相同的 prompt bytes 自动调用 `<python-exe> <skill-dir>/scripts/run_antigravity_review.py run --prompt-base64-stdin --expected-prompt-sha256 <prompt_sha256>`。默认模型在 helper 内固定为 `gemini-4-neon-high`，不存在公开模型覆盖参数；禁止静默切换到默认路由、其他推理档位或模型。认证、网络、CLI 契约、回执、编码、超时、空输出等非限额失败不得触发 fallback。Antigravity 每轮最多尝试 1 次，失败后降级为单路并报告原因。当前 Antigravity fallback 仅支持 Windows；其他平台 fail closed，不影响 Claude 主路径。

使用临时 builder 脚本从已批准路径读取材料、在内存中构造 prompt。工作区与暂存区 diff 必须由 builder 通过 `subprocess.run([...], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)` 直接捕获在内存中；Git argv 必须使用 `git --no-pager -c core.fsmonitor= -c core.pager=cat -c diff.external= diff --no-ext-diff --no-textconv ...` 这一固定安全前缀，禁止触发仓库可配置的 pager、fsmonitor、external diff 或 textconv。已批准的未跟踪文件用 `Path.read_bytes()` 读取并严格按 UTF-8 解码。随后执行 `subprocess.run([<python-exe>, "<skill-dir>/scripts/run_claude_review.py", "start", "--cwd", <仓库根目录>, "--prompt-base64-stdin"], input=base64.b64encode(prompt.encode("utf-8")), stdout=subprocess.PIPE, stderr=subprocess.PIPE)`，再把 helper 的 stdout/stderr 以二进制转发给宿主。这样 Windows 与 POSIX 都不依赖 shell 管道。禁止 `git diff > 临时文件` 或其他源码快照落盘。builder 只能包含模板、已批准路径和固定 argv，不能嵌入源码、diff 或敏感内容，执行后立即删除；禁止用 PowerShell 字符串管道、普通 `print()` 或依赖 `$OutputEncoding` 的文本输出生成传输流。base64 必须是不插入任何空白的不换行形式；helper 会先严格校验并解码，再把原始 UTF-8 bytes 经二进制 pipe 传给 worker 与 Claude。非法 base64、非法 UTF-8、空输入、超过解码后上限的输入，以及因换行或空白导致 base64 传输尺寸超过上限，都会在启动 Claude 前 fail closed。helper 不接受 prompt 文件，也不把快照或 base64 写入 state directory。

Antigravity fallback 必须重建完全相同的 prompt bytes，并把 `classify-failure` 返回的 SHA-256 传给 helper；hash 不同就拒绝启动。builder 可通过 `--antigravity-bin <绝对路径>` 指定新安装但尚未进入当前 PATH 的 `agy`；helper 在 Windows 还会检查 `%LOCALAPPDATA%/agy/bin/agy.exe`。helper 会验证 `agy --help` 与 `agy models`，固定 Neon High slug，在私有空目录中启用 Windows AppContainer `--sandbox` 和 `--disable-slash-commands`。启动前还会读取全局 Antigravity 设置：只接受 `request-review` / `strict`，且 `permissions.allow` 必须为空；启动后的 `init.cwd`、`permission_mode`、模型和事件类型继续严格核对。私有工作区用于本轮运行和日志，结束后删除。AGY 1.5.12 实测在 `request-review` 下仍可能写入私有工作区，`init.permission_mode` 也不证明运行时 `agentMode`。headless 会拒绝需要交互审批的工具调用，并继续本轮运行（soft-deny）。输出使用固定 `--json-schema`，要求非空 `review` 与中性枚举 `decision: pass|revise`，并同时核对 `init.json_schema`、`result.json_schema` 和只含这两个字段的 `structured_output`。自由文本 `response` 不作为评审来源：agy 1.1.21 实测会在其中注入 schema 外的内部展示字段，而 `structured_output` 才是 CLI 按 schema 解析后的权威值。主流程按当前评审类型确定性映射 decision：代码对应“可合并 / 需修改后再合并”，设计与计划对应“可进入实现 / 需修改后再进入实现”，不得让模型自由改写 Gate 语义。`checkpoint` / `finish` 仅在状态合法且没有 `tool_name`、`tool_info`、`subagent_info` 时接受；任何未知 step、真实工具/子 agent、非成功终态、schema 不一致、无效结构化输出或非 UTF-8 输出都判失败。它不会把仓库作为 Antigravity workspace，也不会复用 Claude 的 state directory。

Antigravity helper 是同步进程；用宿主的 exec session 或等价机制等待，仍按 10–30 秒间隔获取新输出，不启动第二个 fallback。Windows 下先把 helper 自身加入 kill-on-close Job Object，再创建 agy；子进程在创建时原子继承 Job，消除 `Popen` 返回到事后分配 Job 之间的强杀窗口。helper 正常结束时由系统关闭句柄，宿主强杀时 Job 同时终止 agy 进程树；超时或取消仍先收口直属 `Popen`。官方 headless 契约默认不续接会话；完整 prompt、base64 和源码快照仍不得进入 argv、调用方日志或临时文件。helper 自有 `agy.log` 位于带随机 ownership 的私有 work dir：state owner 与 work marker 必须用同一 token、协议版本和规范化路径双向绑定，正常终态核对后立即删除；宿主被强杀导致 `finally` 未执行时，下次可由 `sweep` 在核实 ownership 与 helper/agy PID 创建 identity 都已终止后安全清理。

当前会话首次调用 Antigravity fallback 前运行 `<python-exe> "<skill-dir>/scripts/run_antigravity_review.py" sweep --older-than 86400`。它只扫描系统 TEMP 下 `dual-review-antigravity-*` state 目录，校验 owner token、协议版本、work dir 反向 marker 和进程 identity；处于 `spawning_child` 却没有原子 Job 保证的记录一律拒绝清理。`cleaned` 可记录，`refused` 必须报告，不能手工批量删除。`run` 正常失败只向 stderr 输出一个 JSON；若同时 cleanup 失败，该 JSON 的 `error_kind=cleanup_failed`，并包含 `primary_error_kind`、`state_dir`、`work_dir`，不得只解析或报告主错误。

helper 以独立后台进程运行 `claude -p`，启用 `--safe-mode`、`--permission-mode dontAsk`、`--no-session-persistence`、`--no-chrome` 和 `--output-format text`，并用 `--tools ""` 禁用全部工具。`--cwd` 只用于记录被评审仓库；Claude 子进程实际运行在系统 TEMP 下另一个与 state directory 没有父子关系、名称随机且初始为空的私有工作目录。该目录分离能避免从当前工作目录直接看到控制文件，但不是操作系统级文件系统隔离；安全边界仍依赖 safe mode、禁用全部工具以及只向 prompt 放入已批准快照。stdout/stderr 在 cleanup 前仍保存在 state directory。helper 用首尾两个随机回执检测投递截断与首尾输出截断，并严格按 UTF-8 解码 Claude 输出；首尾非空行可使用裸回执 token、原样带 helper 发出的固定指令前缀，或仅在最外层带 Markdown 的反引号/强调符号；附加其他文本、缺少任一回执、输出解码失败或回执之间没有评审正文时该路直接失败。`result` 通过二进制 stdout 原样返回合法 UTF-8 结果，避免 Windows 文本换行转换；回执不用于防护快照自身的 prompt injection，也不能证明模型正文中间没有自行省略。

调用要求：

- base64 解码后的 prompt 不得包含敏感性预检排除的内容。命令、argv、builder 脚本和 state directory 中都不得出现完整 prompt 或 base64；若无法在不落盘的前提下完成传输，则不启动外部路线，按步骤 4 降级。
- 外部 Claude 没有任何工具，不能自行读取工作区或生成 diff。Codex 主 agent 必须把固定后的 diff、目标文件，以及核实所必需的规则、跟踪源码、测试和配置内容整理成自包含 prompt 流；内容必须经过同一轮敏感性预检。不得通过恢复任何工具来省略这一步。helper 拒绝超过 500,000 UTF-8 bytes 的 prompt；超限时应删减无关上下文或拆成多组双路评审；每组的两路仍须使用相同范围，且最终必须合并各组结论，不得依赖模型静默截断。
- 默认超时为 1800 秒；确需缩短时可在 `start` 后追加 `--timeout <秒>`。只有使用非默认可执行文件时才传 `--claude-bin <路径>`，不得借此换用权限更宽松的包装器。Windows 的 `where.exe claude` 若先列出 `.cmd` 但同时存在原生 `.exe`，允许把该 `.exe` 的绝对路径传给 `--claude-bin`；找不到任何原生二进制时才停止。
- Windows 上，helper 的私有目录由 Python `tempfile` 创建为受保护 DACL，Codex restricted-token sandbox 无法访问以宿主权限创建的目录；反过来先在沙箱内调用 `start`，又可能出现“目录已创建但无法写入 `owner.json`”的孤儿目录。因此本节的 `sweep`、`status`、`result`、`stop`、`cleanup` 和按下文要求运行的 `self-test`，必须从第一次调用起就按宿主审批机制使用只覆盖该 helper 命令的最小范围提权，所有参数保持不变，不要先在沙箱内试跑。
- 任一平台需要提权运行 `start` 时，宿主实际审批对象必须是本次一次性、固定路径的 `<python-exe> <builder.py>` 顶层命令；builder 必须建在被评审仓库之外的系统临时或会话 scratchpad 中，不得申请宽泛的 Python 前缀。该 builder 仍只能包含模板、已批准路径和固定 helper argv，只能对已批准路径执行只读读取或使用上文固定安全 argv 的 Git diff，并用 `finally` 保证无论成功失败都在同一轮删除。申请审批时必须同时说明 builder 的固定 argv 与已批准读取范围、它会派生在 sandbox 外运行的 detached worker/Claude 进程树，以及后续 `status`、`result`、`stop`、`cleanup` 仍需同样的最小提权；正常情况下 helper 在 `--timeout` 到达后开始终止进程树并需要额外时间收口，若终止或监督失败则可能继续作为 pending 遗留。restricted-token sandbox 不再是该外部路的额外安全边界；必须继续依赖 `--safe-mode`、`--tools ""`、私有工作目录和仅传入已批准快照来维持只读边界，不得改用更宽松的 Claude 权限模式。
- 所有平台的“提权”都仅指在宿主审批机制下以同一用户帐户、相同系统临时目录根在 sandbox 外运行原命令，不得使用会切换用户或临时目录根的管理员提升或 `sudo`。`start` 前审批被拒时不得回到沙箱内试跑；无论审批是否通过都必须删除已写入的 builder，外部路视为未启动，按步骤 4 明确降级为单路评审并说明双路验证未完成。`start` 成功后若任一后续 helper 命令的审批被拒，不得退回沙箱重试或拼接通用杀进程命令；按 pending 上报，列出 `start` 已返回的 `state_dir` 与 `worker_pid`，`work_dir` 和 Claude PID 若无法获批读取则明确标为未知，并说明系统临时目录下可能遗留带 helper 前缀的工作目录与评审输出，后续仅按步骤 3 的进程终态核实与用户确认规则处置。
- 当前会话首次启动外部路前，运行 `<python-exe> "<skill-dir>/scripts/run_claude_review.py" sweep --older-than 86400`。sweep 只扫描系统 TEMP 下本 helper 前缀的直属目录，并复用 cleanup 的 ownership 与进程终态门禁；缺失或非法 `created_at` 的目录会被拒绝，而不是被当成近期状态跳过。`cleaned` 可直接记录，`refused` 必须报告路径与原因，不得手动批量删除。
- `start` 的成功与失败都输出 JSON。成功时读取并原样保存 `state_dir` 与 `worker_pid`，不自行推导或改写路径；早期校验失败时 `state_dir` 和 `worker_pid` 均为 `null`，按 `error_kind` 报告且无需 cleanup。helper 返回成功后，内部 Codex subagent 与外部 Claude 进程即并行执行。
- `--safe-mode` 隔离自动加载的 skills、plugins、hooks 和 `CLAUDE.md`，避免递归触发；外部 Claude 又没有文件工具，因此 Codex 主 agent 必须把评审范围内适用且通过预检的 `AGENTS.md` / `CLAUDE.md` 相关规则内容放进自包含快照。
- 外部 Claude 使用 `dontAsk` 权限模式并禁用全部工具，不进入 plan mode；system guard 和 review prompt 仍必须明确要求不创建计划、不调用 `ExitPlanMode`，直接输出评审结论。
- Codex 路径不调用 Codex 插件，不给 Claude CLI 传 `--wait`，也不使用 Claude TUI 的近期日志代替完整 stdout。
- worker 启动后、读取 prompt 前先持久化 `bootstrapping` PID/identity；创建 Claude 前再写 `spawning_child`，创建后立即记录 PID/PGID，再建立 Job/watchdog。任何启动窗口缺少可核验 worker 记录时，cleanup 都必须保留 state directory 与独立工作目录。POSIX 下在读取 Claude identity 前就记录 PGID；active leader 只有 identity 与仅由该子进程继承的随机环境标记都匹配时才能触发组信号。无法核验进程组、identity 或 marker 时不得发送组信号，但 worker 仍须先安全终止自己持有且尚未回收的直属 Claude 子进程。Linux 会把 zombie 视为已退出并在组探测前回收直属 leader；只有 watchdog 监督路径在 zombie leader 的 identity 仍匹配且同组后代仍存活时，才允许不依赖已不可读的 marker 继续监督原进程组。leader 已被回收、identity 无法再验证时不得仅凭旧 PGID 发信号。watchdog 监督进程关闭超时但未确认退出时必须保留其 PID/identity，不能因引用丢失而放宽 cleanup。
- 安装、升级或修改 helper 后运行 `<python-exe> "<skill-dir>/scripts/run_claude_review.py" self-test`。不得使用 `python -O` 或设置 `PYTHONOPTIMIZE`；helper 会拒绝关闭断言的运行方式。该命令不调用 Claude、不读取评审快照，只验证 base64/UTF-8、回执、规范化后的成对目录、startup grace，以及当前 POSIX 平台可用时带后代的进程组终止、leader 已回收后的 fail-closed 与 Linux zombie 识别。
- 安装、升级或修改 Antigravity helper 后还必须运行 `<python-exe> "<skill-dir>/scripts/run_antigravity_review.py" self-test`。该自测不调用 `agy`；交付前若本机已安装并认证 `agy`，再用无敏感内容的最小 prompt 做一次真实冒烟。真实冒烟因网络、认证或额度不可用时必须明确报告，不得用离线自测冒充。
- 非 Windows 平台若 Codex 沙箱阻止启动独立后台进程或写入系统临时目录，在遵守上面“同一用户、同一临时目录根”的前提下，按宿主审批机制申请最小范围提权后原样重试；Windows 必须遵守上面的首次调用即最小提权规则。

## 步骤 3：收集并汇总

### Claude Code 宿主

采用后台调用时，保存两个 Agent 返回的 task ID，使用当前宿主提供的后台任务结果工具（如 `TaskOutput`）分别等待并读取最终输出；采用步骤 2 的前台并行 fallback 时，两个 Agent 的直接返回内容就是终态输出，不需要 task ID 或 `TaskOutput`。不要用用户专用的 `codex:status` / `codex:result` 代替 Agent 结果回收。只有成功取得两路内容，或已按步骤 4 判定某一路失败，才进入汇总。

### Codex 宿主

用 `wait_agent` 或当前 Codex 的等价工具等待内部 subagent 终态。helper 的 stdout/stderr 固定为 UTF-8；Codex 宿主必须按 UTF-8 捕获，不得交给 Windows 默认 GBK 解码。Windows 必须从首次调用起、其他平台一旦进入提权路径后，以下所有 helper 调用都继续复用步骤 2 的最小提权方式，不能在生命周期中途退回 sandbox。外部 Claude 路按以下方式取回完整结果：

1. 每 10–30 秒轮询 `<python-exe> "<skill-dir>/scripts/run_claude_review.py" status --state-dir "<state_dir>"`。`starting`、`running` 都不是终态；`starting` 在 30 秒 startup grace 内不得 cleanup。超过 grace 后，只有三种早期退出证据允许安全清理：`worker.json` 为 `bootstrapping` 且该 worker identity 已确认退出；`worker.json` 缺失，但 `launch.json` 的 protocol version ≥ 2、已记录 worker PID/identity、`worker_record_before_child: true` 且该 identity 已确认退出；或启动失败结果带 `safe_to_cleanup: true`。前两种可按 `stale: true` 清理；launch 侧也缺少可核验 worker 身份时继续 fail closed。helper 默认在 1800 秒后自动失败并终止 Claude，不重复启动外部评审。从 `start` 发出起累计超过所用 `--timeout`（默认 1800 秒）再加 120 秒仍无终态时停止轮询，按步骤 4 报告长期 pending。
2. 状态为 `unknown` 或带 `orphaned: true` 时，按返回的 PID、权限提示和 state directory 核实 worker/Claude 子进程；若同时有 `stale_heartbeat: true`，先按正常间隔再轮询一次，连续两次才升级核实。若是权限不足，用宿主审批机制以相同参数最小提权重试；Windows 已按步骤 2 提权后仍为 `unknown` 时不再扩大提权范围或改变审批对象，但后续命令仍沿用步骤 2 的同一提权方式，继续核实并在无法确定时报告 pending。不得把它当作成功、失败或重新启动的依据。
3. 状态为 `done` 后运行 `<python-exe> "<skill-dir>/scripts/run_claude_review.py" result --state-dir "<state_dir>"`，按 UTF-8 读取完整 stdout；状态为 `failed` 时也运行同一命令，读取已保存的完整或部分 stdout、stderr。Claude 输出不是 UTF-8 时，`result` 会在 stdout 给出带替换字符的诊断副本、在 stderr 标明编码错误并返回非零；不得把该副本当作成功评审。极少数回执已验证、但随后写回结果或监督收口发生异常的路径会出现 `failed` 且 `receipt_verified: true`；此时正文首尾完整，可作为带失败标记的降级材料，但不能把该路判为成功。评审成败只按 `status` 输出 JSON 的 `state` 判断；命令非零退出码只表示 helper 命令自身未完整回收结果，不能代替 `state`。`done` 同时带 `supervision_error` 时，评审正文和回执仍可用，但必须报告监督进程异常；在 cleanup 的进程终态门禁通过前不能声称监督链已完整收口。
4. 用户取消评审或主流程中止时，运行 `<python-exe> "<skill-dir>/scripts/run_claude_review.py" stop --state-dir "<state_dir>"`；该命令只写入带随机 ownership token 的取消请求，由原 worker 终止自己创建的 Claude 进程树，不按外部传入的 PID 发信号。stop 返回 `unknown` 时不要据此判定成功或失败，也不要直接拼接通用杀进程命令；继续按第 1 项轮询 status，直到终态或按 pending 上报。
5. 结果收集完成后运行 `<python-exe> "<skill-dir>/scripts/run_claude_review.py" cleanup --state-dir "<state_dir>"`。cleanup 会校验随机 ownership token、系统临时目录边界、独立工作目录边界、进程创建身份和 worker/Claude/watchdog 进程组终态；正常终态会短暂等待 worker 退出，机器重启或 PID 复用不会把无关进程误判为本轮进程，通过全部门禁后同时移除 state directory 与独立工作目录。startup grace 内的 `starting`、`running`、`unknown`、`spawning_child` 或遗留子进程未结束时不得清理；超过 grace 的 `starting` 仍须按第 1 项核验三种早期退出证据，launch 侧也无法核验 worker 身份时不得清理。`stale: true` 表示已证明 worker 在创建 Claude 前退出，`incomplete: true` 表示状态目录在初始化时中断，两者都已安全清理。cleanup 被拒绝时，helper 会报告 `state_dir`、已校验的 `work_dir`（无法安全解析时为 `null`）和拒绝原因；必须向用户说明其中可能仍含评审输出或控制状态。只有用户确认且进程终态已人工核实后，才可同时手动删除这两个精确目录；不得只删除 state directory 而把独立工作目录遗留在 TEMP 中。

Claude helper 到达 `failed` 后，在 cleanup 前先运行 `classify-failure --state-dir "<state_dir>"`。分类器扫描 Claude stderr 与 worker error；stdout 不做宽泛关键词扫描，只在 `claude_exit`、回执未验证、文件不超过 512 bytes、严格 UTF-8 完整读取后恰好一个非空物理行，且该行精确符合本机已实测的 Claude Code weekly-limit + reset time/timezone 格式时认定 quota，避免把正文里讨论“rate limit”的评审误判为额度失败。其他限额文案在未经实测并加入回归前 fail closed。分类命令只有退出码 0 才产生有效分类；任何非零退出（包括 stdout JSON 写着 `reason: result_missing`）都表示分类器自身未完成，不得据其触发 fallback。只有它输出 `quota_limited: true` 且 `prompt_sha256` 是严格小写 64 位十六进制值才记录 fallback 条件；随后仍必须成功执行 cleanup。重新构造快照后把同一个 SHA-256 交给 Antigravity helper，hash 不同不得发送。Antigravity 成功 stdout 是 helper 从已校验 `structured_output.review` 与 `decision` 确定性拼出的完整外部评审；主流程再按当前评审类型映射最终 Gate。非零退出时 stderr 的 JSON `error_kind` 是该路失败原因，不把部分事件流、自由文本 response 或日志当作评审结果。

状态字段按以下边界处理，不能把诊断字段当成成功信号：

- `failure_reason=stopped_by_caller` 表示已取消，不自动重试；`timeout` 表示超过 helper 限时；`claude_exit` 表示 Claude 非零退出，先读 stderr；`output_encoding`、`receipt_missing`、`empty_review`、`worker_error` 都表示该路失败。
- `receipt_missing` 或 `empty_review` 时，已保存 stdout 只能作为未验证的诊断材料，不能作为成功评审；`output_encoding` 的替换字符副本同理。
- `safe_to_cleanup: true` 只说明缺少 worker 记录的早期启动失败可以安全清理，不代表评审成功。
- `orphaned: true` 或 `state=unknown` 表示进程身份或生命周期仍不确定，必须按上面的 PID/权限流程核实，不能重启、清理或判定成功。

任一路输出为空、缺少所要求的分级或 gate、或不含完整评审结论时，视为该路失败。

两路成功后：

1. 合并去重。同一问题严重级别不同时取更高等级。
2. 两路结论冲突时并列展示并标注来源，不擅自丢弃任何一方。
3. 按 Critical → Important → Suggestion 排序。
4. 根据合并后的最高风险给出一次总 gate 结论，不把任一路自报的“通过”直接当作最终结论。
5. 任一路单列的「上下文不足」必须原样保留在汇总中，并作为总 gate 的未覆盖风险；不得用另一路的推测静默填补。

## 步骤 4：处理失败

- **Claude 限额失败**：以 helper 的 `classify-failure` 为唯一自动 fallback 判据，不靠主 agent 自由解释错误文本。分类为限额且 cleanup 成功后，不等待 Claude 重置、不重跑 Claude，自动调用 Antigravity 的 Gemini 4 Neon（High）一次。分类不是限额、分类命令失败或 cleanup 未完成时不得调用 Antigravity。
- **Antigravity 失败**：禁止自动改用其他模型、推理档位或 provider。报告单一错误 JSON 的 `error_kind`、实际 stderr/退出码；`cleanup_failed` 还必须报告 `primary_error_kind` 与精确 state/work dir，并在确认进程终态后交给 `sweep`，不得直接删除。`antigravity_missing` 时给出安装或 PATH 恢复方式，认证失败时提示先交互登录，模型不可用时报告固定 slug，不得删掉模型固定或放宽工具边界。该路评审未完成，不算已完成验证。
- **Claude Code 宿主的 Codex 外部路失败**：若提示插件未安装或未认证，提示用户运行 `/codex:setup`。
- **Codex 宿主的 Claude 外部路失败**：先报告 `start` 的 `error_kind`、实际 stderr 或退出码。命令不存在时说明需安装 Claude Code；未认证时先核对当前 `claude --help` 和 `claude auth --help`，再提示其中实际存在的登录入口，不写死或猜测子命令；Python 运行时过旧、base64/UTF-8 输入无效、CLI 契约不兼容或只找到非原生 launcher 时报告支持边界并停止。`cli_contract` 还应报告 helper 附带的 Claude 版本，不能以文案漂移为由跳过 fail-closed 检查；输入无效时检查 builder 是否用 `subprocess.run(..., input=base64.b64encode(...))` 直接传入不含换行或空白的 bytes，不得改用 PowerShell 文本管道、宽松解码或绕过工具禁用检查。`start` 返回非空 `state_dir` 时，仍须按步骤 3 清理或报告 cleanup 被拒绝。
- **单路非限额失败**：可以用另一路结果收场，但必须明确降级为单路评审，并说明失败原因和未覆盖风险。
- **两路都失败**：不输出“通过”结论；报告两路错误和恢复方式。
- **一路长期 pending**：向用户报告 pending 状态，并明确该路尚未完成验证。
