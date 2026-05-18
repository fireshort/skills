"""
Minimal Douyin speech-to-text prototype.

Usage:
    uv run --with faster-whisper python <skill-dir>/scripts/extract_douyin_script.py "https://v.douyin.com/..." --asr faster-whisper
    uv run --with sherpa-onnx python <skill-dir>/scripts/extract_douyin_script.py "https://v.douyin.com/..." --asr sensevoice
    python <skill-dir>/scripts/extract_douyin_script.py "https://v.douyin.com/..." --asr volcengine
"""

from __future__ import annotations

import argparse
import array
import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from urllib.error import HTTPError
from pathlib import Path

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)

DEFAULT_SENSEVOICE_MODEL_DIR = (
    ".tmp/sherpa-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
)
VOLCENGINE_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
VOLCENGINE_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
VOLCENGINE_PENDING_CODES = {"20000001", "20000002"}
DEFAULT_VOLCENGINE_RESOURCE_ID = "volc.seedasr.auc"
DEFAULT_VOLCENGINE_MODEL = "bigmodel"
USER_CONFIG_ENV_PATH = Path.home() / ".config" / "xibin-douyin-script-extractor" / ".env"


@dataclass
class AudioPaths:
    mp3: Path
    wav16k: Path | None = None


def run_command(cmd: list[str]) -> None:
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(message)


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
            os.environ.setdefault(key, value)
    return values


def load_env_with_fallback(project_env: Path) -> dict[str, str]:
    # 项目级 .env 优先，找不到的 key 回退到用户级 ~/.config/xibin-douyin-script-extractor/.env
    # （os.environ.setdefault 不会覆盖已有值，所以先后顺序决定优先级）
    primary = load_env_file(project_env)
    fallback = load_env_file(USER_CONFIG_ENV_PATH)
    merged = dict(fallback)
    merged.update(primary)
    return merged


class StopAtShareRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if "iesdouyin.com/share/video/" in newurl:
            raise RuntimeError(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="ignore")


def resolve_share_url(url: str) -> str:
    if "iesdouyin.com/share/video/" in url:
        return url

    video_id_match = re.search(r"/video/(\d+)", url)
    if video_id_match:
        return f"https://www.iesdouyin.com/share/video/{video_id_match.group(1)}/"

    opener = urllib.request.build_opener(StopAtShareRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
    try:
        opener.open(request, timeout=30)
    except RuntimeError as exc:
        share_url = str(exc)
        if share_url.startswith("https://www.iesdouyin.com/share/video/"):
            return share_url
        raise

    raise RuntimeError("Could not resolve Douyin short link to an iesdouyin share URL.")


def find_item_list(obj, depth: int = 0):
    if depth > 10:
        return None
    if isinstance(obj, dict):
        if "item_list" in obj:
            return obj["item_list"]
        for value in obj.values():
            result = find_item_list(value, depth + 1)
            if result:
                return result
    if isinstance(obj, list):
        for item in obj:
            result = find_item_list(item, depth + 1)
            if result:
                return result
    return None


def parse_video_info(html: str) -> dict:
    match = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.+?\})\s*</script>", html, re.DOTALL)
    if not match:
        raise RuntimeError("Could not find window._ROUTER_DATA in share page HTML.")

    data = json.loads(match.group(1))
    items = find_item_list(data)
    if not items:
        raise RuntimeError("Could not find item_list in window._ROUTER_DATA.")

    item = items[0]
    video = item.get("video", {})
    play_addr = video.get("play_addr", {})
    uri = play_addr.get("uri")
    if not uri:
        raise RuntimeError(f"Could not find video.play_addr.uri. aweme_type={item.get('aweme_type')}")

    play_url = (
        uri
        if uri.startswith("http")
        else f"https://aweme.snssdk.com/aweme/v1/play/?video_id={urllib.parse.quote(uri)}&ratio=720p&line=0"
    )

    return {
        "aweme_id": item.get("aweme_id"),
        "desc": item.get("desc", ""),
        "author": item.get("author", {}).get("nickname", ""),
        "duration_ms": video.get("duration"),
        "video_uri": uri,
        "play_url": play_url,
    }


def download_video(play_url: str, output_path: Path) -> Path:
    request = urllib.request.Request(play_url, headers={"User-Agent": MOBILE_UA})
    with urllib.request.urlopen(request, timeout=90) as response, output_path.open("wb") as file:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            file.write(chunk)
    return output_path


def extract_audio_mp3(video_path: Path, audio_path: Path) -> Path:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
        str(audio_path),
    ]
    run_command(cmd)
    return audio_path


def extract_audio_wav16k(video_path: Path, audio_path: Path) -> Path:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-acodec",
        "pcm_s16le",
        str(audio_path),
    ]
    run_command(cmd)
    return audio_path


def extract_audio(video_path: Path, run_dir: Path, asr_backend: str) -> AudioPaths:
    mp3_path = extract_audio_mp3(video_path, run_dir / "audio.mp3")
    if asr_backend == "sensevoice":
        wav_path = extract_audio_wav16k(video_path, run_dir / "audio-16k.wav")
        return AudioPaths(mp3=mp3_path, wav16k=wav_path)
    return AudioPaths(mp3=mp3_path)


def transcribe_faster_whisper(audio_path: Path, model_name: str) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Missing dependency: faster-whisper. Run with: uv run --with faster-whisper ...") from exc

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(str(audio_path), language="zh", vad_filter=True)
    segments = [
        {
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text.strip(),
        }
        for segment in segments_iter
        if segment.text.strip()
    ]

    return {
        "asr_backend": "faster-whisper",
        "model": model_name,
        "language": info.language,
        "duration": info.duration,
        "text": "".join(segment["text"] for segment in segments),
        "segments": segments,
    }


def wav_duration(audio_path: Path) -> float:
    with wave.open(str(audio_path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def read_wav_float_samples(audio_path: Path) -> tuple[int, list[float]]:
    with wave.open(str(audio_path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise RuntimeError("SenseVoice expects 16-bit mono WAV audio.")
        sample_rate = wav.getframerate()
        data = wav.readframes(wav.getnframes())

    samples = array.array("h")
    samples.frombytes(data)
    return sample_rate, [sample / 32768.0 for sample in samples]


def transcribe_sensevoice(audio_path: Path, model_dir: Path, model_file: str, tokens_file: str) -> dict:
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise RuntimeError("Missing dependency: sherpa-onnx. Run with: uv run --with sherpa-onnx ...") from exc

    model_path = model_dir / model_file
    tokens_path = model_dir / tokens_file
    if not model_path.exists():
        raise RuntimeError(f"SenseVoice model file not found: {model_path}")
    if not tokens_path.exists():
        raise RuntimeError(f"SenseVoice tokens file not found: {tokens_path}")

    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_path),
        tokens=str(tokens_path),
        num_threads=4,
        language="zh",
        use_itn=True,
        provider="cpu",
    )
    sample_rate, samples = read_wav_float_samples(audio_path)
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    recognizer.decode_stream(stream)
    text = stream.result.text.strip()

    return {
        "asr_backend": "sensevoice",
        "model": str(model_path),
        "language": "zh",
        "duration": wav_duration(audio_path),
        "text": text,
        "segments": [{"start": 0.0, "end": round(wav_duration(audio_path), 3), "text": text}] if text else [],
    }


def load_volcengine_credentials(args: argparse.Namespace) -> tuple[str, str]:
    load_env_with_fallback(Path(args.env_file))
    app_id = args.volcengine_app_id or os.environ.get("VOLCENGINE_APP_ID")
    access_token = args.volcengine_access_token or os.environ.get("VOLCENGINE_ACCESS_TOKEN")

    if args.volcengine_config:
        config = json.loads(Path(args.volcengine_config).read_text(encoding="utf-8"))
        volcengine = config.get("asr", {}).get("volcengine", {})
        app_id = app_id or volcengine.get("app_id")
        access_token = access_token or volcengine.get("access_token")

    if not app_id or not access_token:
        raise RuntimeError(
            "Missing Volcengine credentials. Set VOLCENGINE_APP_ID/VOLCENGINE_ACCESS_TOKEN "
            f"in project .env, in {USER_CONFIG_ENV_PATH}, in environment variables, "
            "or pass --volcengine-config."
        )
    return app_id, access_token


def get_volcengine_resource_id(args: argparse.Namespace) -> str:
    load_env_with_fallback(Path(args.env_file))
    return args.volcengine_resource_id or os.environ.get("VOLCENGINE_RESOURCE_ID") or DEFAULT_VOLCENGINE_RESOURCE_ID


def get_volcengine_model(args: argparse.Namespace) -> str:
    load_env_with_fallback(Path(args.env_file))
    return args.volcengine_model or os.environ.get("VOLCENGINE_MODEL") or DEFAULT_VOLCENGINE_MODEL


def volcengine_headers(app_id: str, access_token: str, resource_id: str, request_id: str) -> dict[str, str]:
    return {
        "X-Api-App-Key": app_id,
        "X-Api-Access-Key": access_token,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
        "Content-Type": "application/json",
    }


def post_json(url: str, headers: dict[str, str], body: dict) -> tuple[dict[str, str], dict]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            content = response.read().decode("utf-8", errors="ignore")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return response_headers, json.loads(content) if content else {}
    except HTTPError as exc:
        content = exc.read().decode("utf-8", errors="ignore")
        try:
            error = json.loads(content)
        except json.JSONDecodeError:
            error = {"message": content}
        message = error.get("header", {}).get("message") or error.get("message") or exc.reason
        raise RuntimeError(f"Volcengine request failed: {message}") from exc


def normalize_volcengine_utterances(utterances: list[dict] | None) -> list[dict]:
    raw = []
    for utterance in utterances or []:
        text = utterance.get("text", "").strip()
        if not text:
            continue
        start = float(utterance.get("start_time", utterance.get("start", 0)) or 0)
        end = float(utterance.get("end_time", utterance.get("end", 0)) or 0)
        raw.append((start, end, text))

    # Volcengine bigmodel ASR returns timestamps in milliseconds; detect once for the whole batch
    # so that short opening utterances (e.g. start_time=80ms) aren't mistaken for seconds.
    max_value = max((max(start, end) for start, end, _ in raw), default=0)
    divisor = 1000.0 if max_value >= 1000 else 1.0

    segments = [
        {"start": round(start / divisor, 3), "end": round(end / divisor, 3), "text": text}
        for start, end, text in raw
    ]
    segments.sort(key=lambda segment: segment["start"])
    return segments


def transcribe_volcengine(audio_path: Path, args: argparse.Namespace) -> dict:
    app_id, access_token = load_volcengine_credentials(args)
    resource_id = get_volcengine_resource_id(args)
    model_name = get_volcengine_model(args)
    request_id = str(uuid.uuid4())
    headers = volcengine_headers(app_id, access_token, resource_id, request_id)
    audio_data = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    context = json.dumps(
        {"hotwords": [{"word": word} for word in args.volcengine_hotwords]},
        ensure_ascii=False,
    )
    submit_body = {
        "user": {"uid": app_id},
        "audio": {"data": audio_data, "format": "mp3"},
        "request": {
            "model_name": model_name,
            "enable_itn": True,
            "show_utterances": True,
            "context": context,
        },
    }

    submit_headers, _ = post_json(VOLCENGINE_SUBMIT_URL, headers, submit_body)
    submit_code = submit_headers.get("x-api-status-code")
    if submit_code != "20000000":
        message = submit_headers.get("x-api-message", "unknown error")
        raise RuntimeError(f"Volcengine submit failed: {submit_code} {message}")

    result_json = None
    for _ in range(args.volcengine_max_polls):
        time.sleep(args.volcengine_poll_interval)
        query_headers, query_json = post_json(VOLCENGINE_QUERY_URL, headers, {})
        query_code = query_headers.get("x-api-status-code")
        if query_code == "20000000":
            result_json = query_json
            break
        if query_code not in VOLCENGINE_PENDING_CODES:
            message = query_headers.get("x-api-message", "unknown error")
            raise RuntimeError(f"Volcengine query failed: {query_code} {message}")

    if result_json is None:
        raise RuntimeError("Volcengine transcription timed out.")

    result = result_json.get("result", {})
    text = result.get("text", "").strip()
    return {
        "asr_backend": "volcengine",
        "model": model_name,
        "resource_id": resource_id,
        "request_id": request_id,
        "language": "zh",
        "duration": None,
        "text": text,
        "segments": normalize_volcengine_utterances(result.get("utterances")),
    }


def transcribe(audio_paths: AudioPaths, args: argparse.Namespace) -> dict:
    if args.asr == "faster-whisper":
        return transcribe_faster_whisper(audio_paths.mp3, args.model)
    if args.asr == "sensevoice":
        if not audio_paths.wav16k:
            raise RuntimeError("SenseVoice requires 16k mono WAV audio.")
        return transcribe_sensevoice(
            audio_paths.wav16k,
            Path(args.sensevoice_model_dir),
            args.sensevoice_model_file,
            args.sensevoice_tokens_file,
        )
    if args.asr == "volcengine":
        return transcribe_volcengine(audio_paths.mp3, args)
    raise RuntimeError(f"Unsupported ASR backend: {args.asr}")


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def render_plain_text(result: dict) -> str:
    segments = result.get("segments") or []
    lines = [segment["text"].strip() for segment in segments if segment.get("text", "").strip()]
    if lines:
        return "\n".join(lines) + "\n"
    return result.get("text", "").strip() + "\n"


def render_timestamped_text(result: dict) -> str:
    segments = result.get("segments") or []
    lines = []
    for segment in segments:
        text = segment.get("text", "").strip()
        if not text:
            continue
        lines.append(f"[{format_timestamp(segment.get('start', 0))}] {text}")
    if lines:
        return "\n".join(lines) + "\n"
    return result.get("text", "").strip() + "\n"


def derive_timestamped_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.timestamped{output_path.suffix}")


def write_outputs(
    result: dict,
    output_path: Path,
    timestamped_path: Path,
    json_path: Path | None,
) -> None:
    output_path.write_text(render_plain_text(result), encoding="utf-8")
    timestamped_path.write_text(render_timestamped_text(result), encoding="utf-8")
    if json_path:
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def make_work_root(path: str) -> Path:
    work_root = Path(path)
    work_root.mkdir(parents=True, exist_ok=True)
    return work_root


def make_run_dir(work_root: Path) -> Path:
    run_dir = work_root / f"douyin-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract spoken script from a Douyin video URL.")
    parser.add_argument("url", help="Douyin video URL, including v.douyin.com short links.")
    parser.add_argument("--output", default="output_script.txt", help="Plain text output path (one segment per line).")
    parser.add_argument(
        "--timestamped-output",
        help="Timestamped text output path. Default: derived from --output (insert .timestamped).",
    )
    parser.add_argument("--json-output", help="Optional JSON output path with timestamped segments.")
    parser.add_argument(
        "--asr",
        choices=["faster-whisper", "sensevoice", "volcengine"],
        default="faster-whisper",
        help="ASR backend. Default: faster-whisper.",
    )
    parser.add_argument("--model", default="medium", help="faster-whisper model name. Default: medium.")
    parser.add_argument(
        "--sensevoice-model-dir",
        default=DEFAULT_SENSEVOICE_MODEL_DIR,
        help="sherpa-onnx SenseVoice model directory.",
    )
    parser.add_argument("--sensevoice-model-file", default="model.int8.onnx", help="SenseVoice ONNX file name.")
    parser.add_argument("--sensevoice-tokens-file", default="tokens.txt", help="SenseVoice tokens file name.")
    parser.add_argument("--env-file", default=".env", help="Local .env file for credentials. Default: .env.")
    parser.add_argument("--volcengine-config", help="Legacy local config JSON containing asr.volcengine credentials.")
    parser.add_argument("--volcengine-app-id", help="Volcengine App ID. Prefer env var VOLCENGINE_APP_ID.")
    parser.add_argument(
        "--volcengine-access-token",
        help="Volcengine Access Token. Prefer env var VOLCENGINE_ACCESS_TOKEN.",
    )
    parser.add_argument("--volcengine-resource-id", help="Volcengine resource ID. Default: volc.seedasr.auc.")
    parser.add_argument("--volcengine-model", help="Volcengine model name. Default: bigmodel.")
    parser.add_argument(
        "--volcengine-hotwords",
        nargs="*",
        default=[],
        help="Hotwords sent to Volcengine ASR (domain-specific terms to improve recognition).",
    )
    parser.add_argument("--volcengine-poll-interval", type=float, default=3.0, help="Query interval in seconds.")
    parser.add_argument("--volcengine-max-polls", type=int, default=20, help="Maximum Volcengine query attempts.")
    parser.add_argument("--work-dir", default=".tmp", help="Temporary working directory. Default: .tmp.")
    parser.add_argument("--keep-media", action="store_true", help="Keep downloaded video/audio files for inspection.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = Path(args.output)
    timestamped_path = Path(args.timestamped_output) if args.timestamped_output else derive_timestamped_path(output_path)
    json_path = Path(args.json_output) if args.json_output else None
    run_dir = make_run_dir(make_work_root(args.work_dir))

    print(f"Work dir: {run_dir.resolve()}")
    print("[1/4] Resolving Douyin share page...")
    share_url = resolve_share_url(args.url)
    video_info = parse_video_info(request_text(share_url))

    print("[2/4] Downloading video and extracting audio...")
    video_path = download_video(video_info["play_url"], run_dir / "video.mp4")
    audio_paths = extract_audio(video_path, run_dir, args.asr)

    print(f"[3/4] Transcribing speech with {args.asr}...")
    result = transcribe(audio_paths, args)
    result["source_url"] = args.url
    result["share_url"] = share_url
    result["video_info"] = video_info

    print("[4/4] Writing output...")
    write_outputs(result, output_path, timestamped_path, json_path)

    if not args.keep_media:
        shutil.rmtree(run_dir)

    print(f"Text written to: {output_path.resolve()}")
    print(f"Timestamped text written to: {timestamped_path.resolve()}")
    if json_path:
        print(f"JSON written to: {json_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
