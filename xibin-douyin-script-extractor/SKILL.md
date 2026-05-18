---
name: xibin-douyin-script-extractor
description: 从抖音视频链接提取口播文案。用户要求“提取抖音视频文案”、转写抖音链接、运行 xibin-douyin-script-extractor skill、比较 ASR 后端，或把抖音短链接 / www.douyin.com/video 链接转成 txt/json 文案结果时使用。
---

# 抖音视频文案提取

## 范围

使用本 skill 自带的 `scripts/extract_douyin_script.py`，从单条抖音视频链接提取口播文案。

下文 `<skill-dir>` 指本 SKILL.md 所在目录，按 skill 实际安装位置替换：

- 项目级安装：`.agents/skills/xibin-douyin-script-extractor`
- 用户级安装：`~/.claude/skills/xibin-douyin-script-extractor`

保持轻量工作流：
- 对标视频文案只用于结构拆解和原创改写参考，不作为可直接复用的文案；
- 不提交 `.env`、密钥、`.tmp/`、`output_script.*`、`output_script.timestamped.*`、下载的视频音频或 ASR 缓存。

## 默认选择

如果项目根目录 `.env` 已配置火山引擎凭据，优先使用：

```powershell
python <skill-dir>/scripts/extract_douyin_script.py "<douyin-url>" --asr volcengine --output output_script.txt --json-output output_script.json
```

原因：豆包录音文件识别模型 2.0 标准版是当前样本中中文效果最好的后端。

用户要求本地离线时使用 `--asr sensevoice`。

`--asr faster-whisper --model medium` 只作为通用兜底；在本项目中文样本上更慢、同音错字更多。

## `.env` 放置约定

脚本按以下优先级加载凭据（高优先级覆盖低优先级）：

1. `--volcengine-app-id` / `--volcengine-access-token` / `--volcengine-config` 命令行参数
2. 进程环境变量（CI、临时覆盖）
3. 项目根目录 `.env`（默认；`--env-file` 可覆盖路径）
4. 用户级 `~/.config/xibin-douyin-script-extractor/.env`（兜底，适合用户级 skill 跨项目共用一份凭据）

skill 目录本身只放 `.env.example` 作为接口契约，**不放真 `.env`**——skill 可能被复制/同步到其他项目甚至公开，含密钥的 `.env` 一旦扩散就是事故。

`.env.example` 模板（同 `.env.example`）：

```text
VOLCENGINE_APP_ID=
VOLCENGINE_ACCESS_TOKEN=
VOLCENGINE_RESOURCE_ID=volc.seedasr.auc
VOLCENGINE_MODEL=bigmodel
```

如果用户要使用其他位置的配置文件：

```powershell
python <skill-dir>/scripts/extract_douyin_script.py "<douyin-url>" --asr volcengine --env-file "<path-to-env>"
```

## 常用命令

Volcengine：

```powershell
python <skill-dir>/scripts/extract_douyin_script.py "<douyin-url>" --asr volcengine --output output_script.txt --json-output output_script.json
```

SenseVoice：

```powershell
$env:UV_CACHE_DIR = (Resolve-Path .tmp).Path + "\uv-cache"
uv run --with sherpa-onnx python <skill-dir>/scripts/extract_douyin_script.py "<douyin-url>" --asr sensevoice --output output_script.txt --json-output output_script.json
```

faster-whisper：

```powershell
$env:UV_CACHE_DIR = (Resolve-Path .tmp).Path + "\uv-cache"
$env:HF_HOME = (Resolve-Path .tmp).Path + "\hf-home"
uv run --with faster-whisper python <skill-dir>/scripts/extract_douyin_script.py "<douyin-url>" --asr faster-whisper --model medium --output output_script.txt --json-output output_script.json
```

遇到网络、依赖下载或 API 调用被沙箱拦截时，按当前会话规则请求授权。

## 输出处理

运行成功后会生成三个文件（路径由 `--output` / `--timestamped-output` / `--json-output` 决定，后两个未显式指定时按 `--output` 派生）：

- `output_script.txt`：按句分段的纯文案，给用户看完整内容；
- `output_script.timestamped.txt`：每行带 `[mm:ss]` 时间戳，便于对位剪辑或回看；
- `output_script.json`：含来源、后端、视频信息和分句结构。

处理建议：
- 默认读 `output_script.txt` 输出给用户；
- 需要时间轴或定位某句话时读 `output_script.timestamped.txt`；
- 需要结构化字段（来源、后端、segments 数组）时读 `output_script.json`；
- 根据需要指出明显 ASR 错字；
- 默认不提交输出文件。

## 验证

修改脚本后运行：

```powershell
python -c "from pathlib import Path; p=Path('<skill-dir>/scripts/extract_douyin_script.py'); compile(p.read_text(encoding='utf-8'), str(p), 'exec'); print('syntax ok')"
python <skill-dir>/scripts/extract_douyin_script.py --help
git diff --check
```

验证真实后端时，优先使用短抖音链接和 `--asr volcengine`。不要打印或记录密钥。

## 参考

需要详细方案、后端对比和已验证样本时，读取 `references/final-plan.md`。
