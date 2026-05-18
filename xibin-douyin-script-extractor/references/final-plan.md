# 抖音视频文案提取最终方案

## 链路

```text
抖音短链接或 www.douyin.com/video 链接
  -> iesdouyin 分享页
  -> window._ROUTER_DATA
  -> video.play_addr.uri
  -> aweme/v1/play mp4
  -> ffmpeg audio
  -> ASR
  -> output_script.txt / output_script.json
```

默认不使用 `yt-dlp + browser cookies`。实测它对抖音链接不稳定，且浏览器 cookie 读取容易受 Chrome 占用影响。

## 后端选择

| 后端 | 命令参数 | 说明 |
|---|---|---|
| 豆包录音文件识别模型 2.0 标准版 | `--asr volcengine` | 中文效果最好，默认推荐 |
| SenseVoice int8 | `--asr sensevoice` | 本地离线，CPU 可接受 |
| faster-whisper medium | `--asr faster-whisper --model medium` | 通用兜底，中文错字更多且慢 |

## 配置

Volcengine 默认读取项目根目录 `.env`：

```text
VOLCENGINE_APP_ID=
VOLCENGINE_ACCESS_TOKEN=
VOLCENGINE_RESOURCE_ID=volc.seedasr.auc
VOLCENGINE_MODEL=bigmodel
```

`.env` 必须保持本地私有；仓库只提交 `.env.example`。

## 已验证样本

`v.douyin.com/<short-id>/` 短链接形式：已跑通下载、音频提取、SenseVoice、Volcengine。

`www.douyin.com/video/<aweme-id>` 直接链接形式：已跑通，会被规范化为 `https://www.iesdouyin.com/share/video/<aweme-id>/`。

## 合规

提取文案只用于结构拆解和原创改写参考。禁止逐字复制、复用竞品画面/声音/肖像、批量爬取、自动发布或跳过人工审核。
