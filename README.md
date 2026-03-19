# 🛠️ Agent Skills

一组可复用的 **AI Agent 技能**（Skills），旨在增强 AI 编程助手的能力。

Skills 是一种结构化的指令集，可以被 AI Agent 加载并执行，从而在特定场景下提供更专业、更高效的工作流。

## ✨ 特性

- **即插即用** — 将 skill 文件夹复制到 Agent 的 skills 目录即可使用
- **结构化定义** — 每个 skill 使用 YAML frontmatter + Markdown 格式，清晰定义触发条件和执行步骤
- **可组合** — 不同 skill 可以在工作流中相互配合
- **平台适配** — 自动适配 Windows / macOS / Linux 环境

## 📦 可用 Skills

| Skill | 说明 |
|-------|------|
| [dual-review](./dual-review/) | 双重评审工作流 — 同时启动内部 Agent 评审和 Codex CLI 外部评审，合并去重后呈现结果 |

## 🚀 快速开始

### 安装

将整个仓库克隆到你的 Agent skills 目录下：

```bash
git clone https://github.com/fireshort/skills.git ~/.agents/skills
```

或者只复制你需要的 skill 文件夹：

```bash
# 示例：只安装 dual-review skill
cp -r dual-review/ ~/.agents/skills/dual-review/
```

### Skill 目录结构

每个 skill 是一个独立的文件夹，至少包含一个 `SKILL.md` 文件：

```
skills/
├── dual-review/
│   └── SKILL.md          # skill 定义文件（必需）
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
