# 🛠️ Agent Skills

一组可复用的 **AI Agent 技能**（Skills），旨在增强 AI 编程助手的能力。

Skills 是一种结构化的指令集，可以被 AI Agent 加载并执行，从而在特定场景下提供更专业、更高效的工作流。

## ✨ 特性

- **按说明安装** — 将 skill 文件夹复制到 Agent 的 skills 目录，再按各 skill 的依赖说明完成配置
- **结构化定义** — 每个 skill 使用 YAML frontmatter + Markdown 格式，清晰定义触发条件和执行步骤
- **可组合** — 不同 skill 可以在工作流中相互配合
- **平台边界明确** — 各 skill 分别声明支持的平台、运行时和前置依赖

## 📦 可用 Skills

| Skill | 说明 |
|-------|------|
| [dual-review](./dual-review/) | 支持 Claude Code、Codex 和 Antigravity，合并宿主内部评审与另一产品的独立意见 |
| [english-learning-card-image](./english-learning-card-image/) | 英语好句双语配图学习卡：生成、定向编辑、评估与提示词，强调清新风格、准确文字和记忆锚点 |
| [xibin-douyin-script-extractor](./xibin-douyin-script-extractor/) | 抖音口播文案提取 — 把抖音视频链接转成带时间戳的文案 txt/json，支持火山引擎豆包、SenseVoice、faster-whisper 三种 ASR 后端 |

## 🚀 快速开始

### 安装

`dual-review` 的宿主依赖：

- Claude Code：
  1. 安装 OpenAI Codex 插件，运行 `/codex:setup` 完成 Codex CLI 的安装与认证。
  2. 当前插件的用户命令 `/codex:rescue` 由 `codex:codex-rescue` agent 实现。用户可手动运行 `/codex:rescue --wait "只回答 OK，不修改文件"` 做最小只读探测；命令正常返回 `OK` 才表示该 agent 可调用。skill 运行时只检查可用 agent 列表，不靠试调用探测。
- Antigravity
  1. 将 `dual-review` 放入工作区的 `.agents/skills/`，或全局的 `~/.gemini/config/skills/`。安装位置见 [Antigravity 官方文档](https://antigravity.google/docs/skills)。
  2. 安装 Python 3 和 Codex CLI，并完成 `codex login`。Windows 上确认宿主终端能找到 `codex.exe`，或使用已核实的可执行文件绝对路径。[OpenAI Codex CLI 文档](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
  3. 如果平时使用的 `CODEX_HOME` 包含 `AGENTS.md` 或 `AGENTS.override.md`，为评审选一个没有全局规则的目录。在单独终端将该目录设为 `CODEX_HOME` 并执行 `codex login`，然后把目录路径提供给 Antigravity。
  4. 在 Antigravity 中发起 `/dual-review`。Antigravity 负责内部评审，Codex CLI 在临时空目录中以只读模式评审经预检的快照。
- Codex：
  1. 安装 Python 3.10 及以上版本；Windows 要求 CPython 3.12.4 及以上。
  2. 安装本机 Claude Code CLI。已有旧版 CLI 时可执行 `claude install stable` 安装原生构建。
  3. Windows 运行 `py -3 --version`、`claude --version`、`claude --help` 和 `where.exe claude`；macOS/Linux 使用 `python3 --version`、`claude --version`、`claude --help` 和 `command -v claude`。help 中须包含 `--safe-mode`、`--permission-mode`、`--tools`、`--append-system-prompt`、`--no-session-persistence`、`--no-chrome`、`--output-format`、`-p` 或 `--print`，并明确 `dontAsk` 和空字符串禁用全部工具。
  4. 安装、升级或修改 helper 后运行 self-test；Windows 使用 `py -3 dual-review/scripts/run_claude_review.py self-test`，macOS/Linux 使用 `python3 dual-review/scripts/run_claude_review.py self-test`。它不会调用 Claude 或读取评审快照，也不得在 `python -O` / `PYTHONOPTIMIZE` 下运行。
  5. Windows 上，Claude 明确限额且清理完成后，外部评审自动改用 Antigravity CLI 的 Gemini 4 Neon（High）。用 `agy models` 确认当前账户有 `gemini-4-neon-high`。修改 AGY helper 后运行 `py -3 dual-review/scripts/run_antigravity_review.py self-test`，并用合成内容完成一次真实冒烟。

安全边界：helper 只接受原生 Claude Code 二进制（Windows `.exe`/`.com`，POSIX ELF/Mach-O），并会再次检查上述 CLI 契约。不能证明 `--tools ""` 空参数会完整保留的 command、shell 或 script wrapper 将被拒绝。Codex 宿主必须用 Python builder 将 prompt 的 UTF-8 bytes 编码成不换行 base64，并通过 `subprocess.run(..., input=base64_bytes)` 直接传给 `--prompt-base64-stdin`；不要使用 PowerShell 文本管道或普通 `print()`。Claude 在与控制状态无父子关系的独立随机临时工作目录运行，但这不是操作系统级文件隔离；主要边界仍是 safe mode、禁用全部工具与只传入批准快照。helper 的 stdout/stderr 固定为 UTF-8，`result` 对合法输出使用二进制 stdout 保留原始换行；调用方也必须按 UTF-8 捕获。为保留 Claude Code 的 CLI 认证与正常运行环境，Claude 子进程会继承宿主环境变量；“禁止发送密钥/token”约束的是评审 prompt 和快照内容，不代表操作系统级环境隔离。

Claude Code 与 Codex 的个人 skill 目录不同：Claude Code 使用 `~/.claude/skills`，Codex 使用 `$HOME/.agents/skills`。建议只保留一份仓库，再把目标 skill 链接到两个宿主：

```bash
git clone https://github.com/fireshort/skills.git ~/skills-public
mkdir -p ~/.claude/skills ~/.agents/skills
ln -s ~/skills-public/dual-review ~/.claude/skills/dual-review
ln -s ~/skills-public/dual-review ~/.agents/skills/dual-review
```

两个宿主都支持扫描到 skill 目录的符号链接。只使用一个宿主时，只创建对应链接即可。也可以复制文件夹，但更新仓库后要重新同步：

```bash
mkdir -p ~/.claude/skills/dual-review ~/.agents/skills/dual-review

# Claude Code
cp -R dual-review/. ~/.claude/skills/dual-review/

# Codex
cp -R dual-review/. ~/.agents/skills/dual-review/
```

Windows 推荐使用无需开发者模式或管理员权限的目录联接；若选择 `SymbolicLink`，通常需要开启开发者模式或使用管理员终端：

```powershell
New-Item -ItemType Directory -Force -Path "$HOME/.claude/skills", "$HOME/.agents/skills"
New-Item -ItemType Junction -Path "$HOME/.claude/skills/dual-review" -Target "$HOME/skills-public/dual-review"
New-Item -ItemType Junction -Path "$HOME/.agents/skills/dual-review" -Target "$HOME/skills-public/dual-review"
```

### Skill 目录结构

每个 skill 是一个独立的文件夹，至少包含一个 `SKILL.md` 文件：

```
skills/
├── dual-review/
│   ├── SKILL.md          # skill 定义文件（必需）
│   └── scripts/          # Codex 宿主调用 Claude Code 的 helper
├── another-skill/
│   ├── SKILL.md
│   ├── scripts/          # 辅助脚本（可选）
│   ├── examples/         # 示例文件（可选）
│   └── resources/        # 资源文件（可选）
└── README.md
```

### SKILL.md 格式

```markdown
---
name: skill-name
description: 简短描述，Agent 据此判断何时触发此 skill
---

# Skill 标题

详细的执行步骤和说明...
```

- **name** — skill 的唯一标识符
- **description** — 用于 Agent 匹配和触发判断的描述文字，建议包含关键触发词

## 🤝 贡献

欢迎贡献新的 skill！请遵循以下步骤：

1. Fork 本仓库
2. 创建你的 skill 分支：`git checkout -b skill/your-skill-name`
3. 在项目根目录下创建新的 skill 文件夹，包含 `SKILL.md`
4. 提交你的更改：`git commit -m 'feat: 添加 your-skill-name skill'`
5. 推送到你的分支：`git push origin skill/your-skill-name`
6. 提交 Pull Request

### 贡献指南

- 每个 skill 应当职责单一、目标明确
- `SKILL.md` 中的 `description` 要包含足够的触发词，方便 Agent 自动匹配
- 执行步骤应详细且无歧义
- 如有平台差异，请做好适配说明

## 📄 许可证

本项目采用 [MIT 许可证](./LICENSE) 开源。
