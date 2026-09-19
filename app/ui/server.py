"""Web UI gateway for Realtime speech and selectable avatar rendering."""

from __future__ import annotations

import asyncio
import array
import base64
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
import wave
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from ui.affinity import Affinity

ROOT = Path(__file__).resolve().parents[1]
MODELS_ROOT = ROOT.parent / "AI-Girlfriend-Models"
STATIC_ROOT = Path(__file__).resolve().parent / "static"
DATA_ROOT = ROOT / "heygem-data"
INPUT_ROOT = DATA_ROOT / "input"
TEMP_ROOT = DATA_ROOT / "temp"
RESULT_ROOT = DATA_ROOT / "result"
OUTPUT_ROOT = ROOT / "output"
OUTPUT_AUDIO_ROOT = OUTPUT_ROOT / "audio"
OUTPUT_VIDEO_ROOT = OUTPUT_ROOT / "video"
LIVEACT_RESULT_ROOT = OUTPUT_ROOT / "liveact"
STATE_FILE = DATA_ROOT / "state.json"
LLM_PROVIDER_FILE = DATA_ROOT / "llm-provider.json"
AFFINITY_FILE = DATA_ROOT / "affinity.json"
VOICES_ROOT = ROOT / "voices"
OMNIVOICE_URL = os.getenv("OMNIVOICE_URL", "http://127.0.0.1:8791")
affinity = Affinity(AFFINITY_FILE)
CHARACTERS_CONFIG_FILE = ROOT / "config" / "characters.json"

REALTIME_URL = os.getenv("CYBER_REALTIME_URL", "ws://127.0.0.1:8766/v1/realtime")
_realtime_url = urlsplit(REALTIME_URL)
REALTIME_POOL_URL = os.getenv(
    "CYBER_REALTIME_POOL_URL",
    (
        f"{'https' if _realtime_url.scheme == 'wss' else 'http'}://"
        f"{_realtime_url.netloc}{_realtime_url.path.rsplit('/', 1)[0]}/pool"
    ),
)
HEYGEM_URL = os.getenv("CYBER_HEYGEM_URL", "http://127.0.0.1:8383")
LIVEACT_URL = os.getenv("CYBER_LIVEACT_URL", "http://127.0.0.1:8390")
LLM_HEALTH_URL = os.getenv("CYBER_LLM_HEALTH_URL", "http://127.0.0.1:8080/health")
LOCAL_LLM_API_BASE_URL = os.getenv(
    "CYBER_LLM_API_BASE_URL",
    "http://127.0.0.1:8080/v1",
)
LOCAL_LLM_SERVER = Path(
    os.getenv(
        "CYBER_LLM_SERVER",
        str(ROOT / "llama.cpp" / "llama-server.exe"),
    )
)
LOCAL_LLM_MODEL = Path(
    os.getenv(
        "CYBER_LLM_MODEL",
        str(
            MODELS_ROOT
            / "llm"
            / "qwen3.5-9b-GGUF"
            / "Qwen_Qwen3.5-9B-Q5_K_M.gguf"
        ),
    )
)
LOCAL_LLM_PORT = int(os.getenv("CYBER_LLM_PORT", "8080"))
STARTUP_LOCAL_LLM_PID = int(os.getenv("CYBER_LLM_PID", "0") or "0")
REALTIME_PORT = int(os.getenv("CYBER_REALTIME_PORT", "8766"))
HEYGEM_PORT = int(os.getenv("CYBER_HEYGEM_PORT", "8383"))
LIVEACT_PORT = int(os.getenv("CYBER_LIVEACT_PORT", "8390"))
LIVEACT_TIMEOUT_SECONDS = int(os.getenv("CYBER_LIVEACT_TIMEOUT_SECONDS", "3600"))
CHARACTER = os.getenv("CYBER_CHARACTER", "xiaoman")
HEYGEM_ENABLED = os.getenv("CYBER_HEYGEM_ENABLED", "1") == "1"
# 图片驱动（云端 LiveAct）已移除：我们只做视频驱动
LIVEACT_ENABLED = False
DEFAULT_AVATAR_SOURCE = Path(
    os.getenv(
        "CYBER_DEFAULT_AVATAR",
        str(INPUT_ROOT / "avatar-demo.mp4"),
    )
)
SAMPLE_RATE = 16000
MAX_AVATAR_BYTES = 2 * 1024 * 1024 * 1024
MAX_AVATAR_IMAGE_BYTES = 20 * 1024 * 1024
MAX_SYSTEM_PROMPT_CHARACTERS = 16000
HEYGEM_LONG_EDGE = 1280
# 只保留视频驱动
AVATAR_DRIVERS = {"video"}
ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_EXTERNAL_LLM_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_EXTERNAL_LLM_MODEL = "deepseek-chat"
LLM_MODEL_PRESETS = [
    "gpt-4o-mini",
    "gpt-4.1-mini",
    "qwen-plus",
    "qwen-turbo",
    "deepseek-chat",
]


def public_engine_text(value: Any) -> str:
    return str(value).replace("HeyGem", "视频驱动")


for directory in (
    INPUT_ROOT,
    TEMP_ROOT,
    RESULT_ROOT,
    OUTPUT_AUDIO_ROOT,
    OUTPUT_VIDEO_ROOT,
    LIVEACT_RESULT_ROOT,
):
    directory.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("ui")

app = FastAPI(title="AI Girlfriend", docs_url=None, redoc_url=None)
heygem_lock = asyncio.Lock()
liveact_lock = asyncio.Lock()
llm_runtime_lock = asyncio.Lock()
rendered_jobs: dict[str, Path] = {}
local_llm_process: subprocess.Popen[bytes] | None = None
startup_local_llm_pid = STARTUP_LOCAL_LLM_PID
active_chat_client: Any | None = None
active_chat_client_id = ""


@app.middleware("http")
async def prevent_stale_frontend_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/index.html", "/app.js", "/styles.css"}:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def read_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_json_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_config(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# ── 语言模型：多提供方（我们改的）──────────────────────────────
# 原版只有"本地 Qwen / 一个三方 API"两选一。改成可以存多个提供方，各自带
# Base URL / Key / 模型，点一个就切过去——参考 DSH 那套模型设置的做法。
#
# 注意我们这套架构里所有请求最终都走本机的 DSH bridge（它负责记忆），
# 用户在这里选的 Base URL 是**上游厂商**的地址，会通过请求头转给 bridge，
# 由 bridge 转交给 DSH。所以这里不再有"本地 Qwen"那一档。
DSH_BRIDGE_URL = os.getenv("CYBER_DSH_BRIDGE_URL", "http://127.0.0.1:8790/v1")


def default_llm_provider_config() -> dict[str, Any]:
    return {"active": "", "providers": []}


def read_llm_provider_config() -> dict[str, Any]:
    if not LLM_PROVIDER_FILE.exists():
        return default_llm_provider_config()
    try:
        saved = read_json_config(LLM_PROVIDER_FILE)
    except (OSError, json.JSONDecodeError):
        return default_llm_provider_config()
    if not isinstance(saved, dict):
        return default_llm_provider_config()

    # 兼容旧格式（单一 provider），自动升级成列表
    if "providers" not in saved and saved.get("base_url"):
        legacy = {
            "id": "legacy",
            "label": "已有配置",
            "base_url": str(saved.get("base_url", "")),
            "api_key": str(saved.get("api_key", "")),
            "model": str(saved.get("model", "")),
            "custom": True,
        }
        return {"active": "legacy", "providers": [legacy]}

    providers = [p for p in saved.get("providers", []) if isinstance(p, dict) and p.get("id")]
    active = str(saved.get("active", ""))
    if providers and not any(p["id"] == active for p in providers):
        active = providers[0]["id"]
    return {"active": active, "providers": providers}


def public_llm_provider_config() -> dict[str, Any]:
    """给前端看的版本：密钥只报告有无，不回传内容。"""
    config = read_llm_provider_config()
    return {
        "active": config["active"],
        "providers": [
            {
                "id": p["id"],
                "label": p.get("label", p["id"]),
                "baseUrl": p.get("base_url", ""),
                "model": p.get("model", ""),
                "custom": bool(p.get("custom")),
                "hasApiKey": bool(p.get("api_key")),
            }
            for p in config["providers"]
        ],
        "presets": LLM_MODEL_PRESETS,
    }


def active_llm_provider() -> dict[str, Any] | None:
    config = read_llm_provider_config()
    for provider in config["providers"]:
        if provider["id"] == config["active"]:
            return provider
    return None


def normalize_llm_base_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Base URL 必须是文本。")
    base_url = value.strip().rstrip("/")
    if not base_url or len(base_url) > 2048:
        raise ValueError("Base URL 不能为空或超过 2048 个字符。")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Base URL 必须是有效的 HTTP(S) API 地址。")
    return base_url


def apply_provider_action(payload: dict[str, Any]) -> dict[str, Any]:
    """按前端发来的动作改提供方列表，返回新的配置。"""
    config = read_llm_provider_config()
    providers: list[dict[str, Any]] = config["providers"]
    action = str(payload.get("action", "upsert"))
    target = str(payload.get("id", "")).strip()

    def find(pid: str) -> dict[str, Any] | None:
        return next((p for p in providers if p["id"] == pid), None)

    if action == "activate":
        if not find(target):
            raise ValueError("要启用的提供方不存在。")
        config["active"] = target

    elif action == "remove":
        if not find(target):
            raise ValueError("要删除的提供方不存在。")
        providers[:] = [p for p in providers if p["id"] != target]
        if config["active"] == target:
            config["active"] = providers[0]["id"] if providers else ""

    elif action == "clear-key":
        provider = find(target)
        if not provider:
            raise ValueError("提供方不存在。")
        provider["api_key"] = ""

    elif action == "upsert":
        base_url = normalize_llm_base_url(payload.get("baseUrl"))
        model = str(payload.get("model", "")).strip()
        if not model or len(model) > 200 or any(c.isspace() for c in model):
            raise ValueError("模型名称不能为空或包含空格。")

        label = str(payload.get("label", "")).strip()
        pid = target or re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or uuid.uuid4().hex[:8]

        provider = find(pid)
        if provider is None:
            provider = {"id": pid, "api_key": ""}
            providers.append(provider)
        provider["label"] = label or provider.get("label") or pid
        provider["base_url"] = base_url
        provider["model"] = model
        provider["custom"] = bool(payload.get("custom", provider.get("custom", False)))

        api_key = payload.get("apiKey")
        if isinstance(api_key, str) and api_key.strip():
            if len(api_key) > 4096:
                raise ValueError("API Key 不能超过 4096 个字符。")
            provider["api_key"] = api_key.strip()
        if not provider["api_key"]:
            raise ValueError("请先填写 API Key。")

        if payload.get("activate", True):
            config["active"] = pid

    else:
        raise ValueError(f"未知操作：{action}")

    return config


def persist_llm_provider_config(config: dict[str, Any]) -> None:
    LLM_PROVIDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json_config(LLM_PROVIDER_FILE, config)


# 新会话开场时，下一次请求要不要带上"你们处到哪一步了"。
# 由 WebSocket 连上时置位（见 chat_socket），发出去一次就清掉。
_affinity_opening_pending = False


def mark_affinity_opening() -> None:
    global _affinity_opening_pending
    _affinity_opening_pending = True


def prepare_llm_payload(payload: dict[str, Any], provider: dict[str, Any]) -> dict[str, Any]:
    """背景独立传递，用户原话及管线附加指令保持原样。"""
    global _affinity_opening_pending
    prepared = dict(payload)
    prepared["model"] = provider.get("model", "")
    if _affinity_opening_pending:
        prepared["companion_context"] = affinity.opening_context()
        _affinity_opening_pending = False
    prepared.pop("chat_template_kwargs", None)
    prepared.pop("reasoning_effort", None)
    return prepared


def pipeline_warmup_response(payload: dict[str, Any]) -> dict[str, Any] | None:
    messages = payload.get("messages")
    is_pipeline_warmup = (
        payload.get("stream") is not True
        and messages
        == [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Hello"},
        ]
    )
    if not is_pipeline_warmup:
        return None
    return {
        "id": f"chatcmpl-warmup-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": 0,
        "model": "warmup",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def llm_completion_target(provider: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """请求一律发给本机的 DSH bridge——记忆在它那儿。

    用户选的上游厂商地址走请求头带过去，由 bridge 转交给 DSH；
    密钥照 OpenAI 的惯例放 Authorization。
    """
    endpoint = DSH_BRIDGE_URL.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.get('api_key', '')}",
        "Content-Type": "application/json",
        "X-Upstream-Base-Url": provider.get("base_url", ""),
        "X-Upstream-Label": provider.get("label", ""),
        # 以服务端保存的开关为准，bridge 用它选择本轮回复篇幅。
        "X-Companion-Output-Mode": "digital-human" if lipsync_enabled() else "voice-only",
    }
    return endpoint, headers


def normalize_system_prompt(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("System Prompt 必须是文本。")
    prompt = value.strip()
    if len(prompt) < 20:
        raise ValueError("System Prompt 至少需要二十个字符。")
    if len(prompt) > MAX_SYSTEM_PROMPT_CHARACTERS:
        raise ValueError(
            f"System Prompt 不能超过 {MAX_SYSTEM_PROMPT_CHARACTERS} 个字符。"
        )
    return prompt


def current_system_prompt() -> str:
    characters = read_json_config(CHARACTERS_CONFIG_FILE)
    character = characters.get(CHARACTER, {})
    return str(character.get("system_prompt", "")).strip()


def persist_system_prompt(prompt: str) -> None:
    characters = read_json_config(CHARACTERS_CONFIG_FILE)
    character = characters.setdefault(CHARACTER, {})
    character["system_prompt"] = prompt
    write_json_config(CHARACTERS_CONFIG_FILE, characters)


def system_prompt_update_event(prompt: str) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": prompt,
        },
    }


def ensure_default_avatar() -> None:
    state = read_state()
    if current_avatar(state, "video") is not None:
        return
    if not DEFAULT_AVATAR_SOURCE.exists():
        return

    target = INPUT_ROOT / "avatar-demo.mp4"
    if not target.exists():
        shutil.copy2(DEFAULT_AVATAR_SOURCE, target)
    avatars = state.setdefault("avatars", {})
    avatars["video"] = {
        "filename": target.name,
        "original_name": DEFAULT_AVATAR_SOURCE.name,
    }
    state["avatar"] = target.name
    state["original_name"] = DEFAULT_AVATAR_SOURCE.name
    write_state(state)


def current_avatar_driver(state: dict[str, Any] | None = None) -> str:
    state = state or read_state()
    stored = str(state.get("avatar_driver", "video")).strip().lower()
    return "image" if stored in {"image", "liveact"} else "video"


def normalize_avatar_driver(value: Any) -> str:
    driver = str(value or "").strip().lower()
    if driver not in AVATAR_DRIVERS:
        raise ValueError("口型方案必须选择视频驱动或图片驱动。")
    return driver


def persist_avatar_driver(driver: str) -> None:
    state = read_state()
    state["avatar_driver"] = normalize_avatar_driver(driver)
    write_state(state)


def avatar_state(
    state: dict[str, Any] | None = None,
    driver: str | None = None,
) -> dict[str, str]:
    state = state or read_state()
    selected = normalize_avatar_driver(driver or current_avatar_driver(state))
    avatars = state.get("avatars", {})
    stored = avatars.get(selected, {}) if isinstance(avatars, dict) else {}
    if selected == "video" and (not isinstance(stored, dict) or not stored.get("filename")):
        stored = avatars.get("heygem", {}) if isinstance(avatars, dict) else {}
    if isinstance(stored, dict) and stored.get("filename"):
        return {
            "filename": str(stored["filename"]),
            "original_name": str(stored.get("original_name", "")),
        }
    if selected == "video" and state.get("avatar"):
        return {
            "filename": str(state["avatar"]),
            "original_name": str(state.get("original_name", "")),
        }
    return {}


def current_avatar(
    state: dict[str, Any] | None = None,
    driver: str | None = None,
) -> Path | None:
    filename = Path(avatar_state(state, driver).get("filename", "")).name
    if not filename:
        return None
    candidate = INPUT_ROOT / filename
    return candidate if candidate.exists() else None


# ── 画面焦点（我们加的）──────────────────────────────────────────────
# 舞台是竖高的，素材是横幅，object-fit: cover 会裁掉左右两边。默认居中裁，
# 人物偏在画面一侧时就会被切到边上，甚至切掉半张脸。
#
# 所以按素材里人脸的实际位置算一个 object-position，让脸落在舞台中间。
# 结果缓存在 state.json 里，同一个文件只算一次。
FOCUS_CACHE_KEY = "media_focus"


def media_focus(path: Path) -> dict[str, float]:
    """返回 {"x": 0~1, "y": 0~1}：素材里人脸中心的相对位置。"""
    state = read_state()
    cache = state.get(FOCUS_CACHE_KEY)
    if not isinstance(cache, dict):
        cache = {}
    hit = cache.get(path.name)
    if isinstance(hit, dict) and "x" in hit and "y" in hit:
        return {"x": float(hit["x"]), "y": float(hit["y"])}

    focus = {"x": 0.5, "y": 0.5}
    try:
        import cv2  # 只在这里用，缺了就退回居中

        # opencv-python-headless 5.x 不再随包提供 haar 级联，所以我们自己带一份。
        # 找不到就退回 opencv 自带路径（旧版本有），再找不到就居中裁。
        bundled = Path(__file__).resolve().parent / "assets" / "haarcascade_frontalface_default.xml"
        cascade_path = str(bundled) if bundled.exists() else (
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            raise RuntimeError(f"级联文件加载失败：{cascade_path}")
        capture = cv2.VideoCapture(str(path))
        centers: list[tuple[float, float]] = []
        index = 0
        while index < 90 and len(centers) < 12:
            ok, frame = capture.read()
            if not ok:
                break
            if index % 7 == 0:
                height, width = frame.shape[:2]
                faces = cascade.detectMultiScale(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), 1.2, 5, minSize=(40, 40)
                )
                if len(faces):
                    x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
                    centers.append(((x + w / 2) / width, (y + h / 2) / height))
            index += 1
        capture.release()
        if centers:
            focus = {
                "x": round(sum(c[0] for c in centers) / len(centers), 4),
                "y": round(sum(c[1] for c in centers) / len(centers), 4),
            }
    except Exception:  # noqa: BLE001
        logger.debug("人脸定位失败，画面按居中裁剪：%s", path.name, exc_info=True)

    cache[path.name] = focus
    state[FOCUS_CACHE_KEY] = cache
    write_state(state)
    return focus


def avatar_payload(driver: str | None = None) -> dict[str, Any] | None:
    state = read_state()
    selected = normalize_avatar_driver(driver or current_avatar_driver(state))
    path = current_avatar(state, selected)
    if path is None:
        return None
    stored = avatar_state(state, selected)
    return {
        "name": stored.get("original_name") or path.name,
        "url": f"/media/avatar/{path.name}",
        "kind": selected,
        "driver": selected,
        "focus": media_focus(path),
    }


def idle_video_state(state: dict[str, Any] | None = None) -> dict[str, str]:
    state = state or read_state()
    stored = state.get("idle_video", {})
    if not isinstance(stored, dict) or not stored.get("filename"):
        return {}
    return {
        "filename": str(stored["filename"]),
        "original_name": str(stored.get("original_name", "")),
    }


def current_idle_video(state: dict[str, Any] | None = None) -> Path | None:
    filename = Path(idle_video_state(state).get("filename", "")).name
    if not filename:
        return None
    candidate = INPUT_ROOT / filename
    return candidate if candidate.exists() else None


def idle_video_payload() -> dict[str, Any] | None:
    state = read_state()
    path = current_idle_video(state)
    if path is None:
        return None
    stored = idle_video_state(state)
    return {
        "name": stored.get("original_name") or path.name,
        "url": f"/media/avatar/{path.name}",
        "kind": "video",
        "focus": media_focus(path),
    }


BUNDLED_FFMPEG = ROOT.parent / "runtime" / "ffmpeg" / "ffmpeg.exe"


def resolve_ffmpeg() -> str | None:
    """先用整合包自带的，找不到才退回 PATH。

    自带优先而不是 PATH 优先：用户机器上那个可能是任意版本、任意编译选项，
    甚至是别的软件塞进 PATH 的残缺构建。自带这份是我们验过能转的。
    环境变量 AI_GIRLFRIEND_FFMPEG 可以指定别的路径，优先级最高。
    """
    override = os.getenv("AI_GIRLFRIEND_FFMPEG", "").strip()
    if override and Path(override).is_file():
        return override
    if BUNDLED_FFMPEG.is_file():
        return str(BUNDLED_FFMPEG)
    return shutil.which("ffmpeg")


FFMPEG_MISSING = (
    "找不到 ffmpeg。整合包应该自带一份在 runtime\\ffmpeg\\ffmpeg.exe，"
    "缺了的话去 ffmpeg.org 下一个，或用环境变量 AI_GIRLFRIEND_FFMPEG 指向它。"
)


async def normalize_heygem_video(source: Path, target: Path) -> None:
    """Create a browser-friendly, high-quality MP4 with a 1280px long edge."""
    ffmpeg = resolve_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError(FFMPEG_MISSING)

    scale = (
        f"scale='if(gte(iw,ih),{HEYGEM_LONG_EDGE},-2)':"
        f"'if(gte(iw,ih),-2,{HEYGEM_LONG_EDGE})':flags=lanczos"
    )
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        scale,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "15",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(target),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=30 * 60)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("视频驱动素材转码超过三十分钟。") from None
    if process.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"视频无法解码或转码：{message or 'ffmpeg 未生成输出'}")


def normalize_liveact_image(source: Path, target: Path) -> None:
    """Decode and re-encode a single portrait image without metadata."""
    try:
        with Image.open(source) as opened:
            opened.load()
            width, height = opened.size
            if width < 1 or height < 1:
                raise ValueError("图片尺寸无效。")
            if width * height > 64_000_000:
                raise ValueError("图片像素过大，请使用不超过 6400 万像素的图片。")
            image = opened.convert("RGB")
            image.save(target, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise RuntimeError(f"图片无法解码：{error}") from error


async def tcp_ready(host: str, port: int) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=0.8,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def claim_chat_client(
    client: Any,
    client_id: str,
) -> tuple[bool, Any | None]:
    global active_chat_client, active_chat_client_id

    if not client_id:
        return False, None
    previous = active_chat_client
    active_chat_client = client
    active_chat_client_id = client_id
    return True, previous if previous is not client else None


async def release_chat_client(client: Any) -> None:
    global active_chat_client, active_chat_client_id

    if active_chat_client is client:
        active_chat_client = None
        active_chat_client_id = ""


async def wait_for_realtime_slot(timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(REALTIME_POOL_URL)
                payload = response.json()
                if (
                    response.is_success
                    and int(payload.get("in_use", 0)) < int(payload.get("size", 1))
                ):
                    return True
            except (httpx.HTTPError, TypeError, ValueError):
                pass
            await asyncio.sleep(0.15)
    return False


async def run_bidirectional(*directions: Awaitable[None]) -> None:
    tasks = [asyncio.create_task(direction) for direction in directions]
    done, pending = await asyncio.wait(
        tasks,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


async def llm_ready() -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.2) as client:
            response = await client.get(LLM_HEALTH_URL)
            return response.is_success and response.json().get("status") == "ok"
    except (httpx.HTTPError, ValueError):
        return False


def local_llm_command() -> list[str]:
    return [
        str(LOCAL_LLM_SERVER),
        "-m",
        str(LOCAL_LLM_MODEL),
        "--host",
        "127.0.0.1",
        "--port",
        str(LOCAL_LLM_PORT),
        "-ngl",
        "all",
        "-c",
        "8192",
        "-np",
        "1",
        "-fa",
        "on",
        "--temp",
        "0.7",
        "--top-p",
        "0.8",
        "--top-k",
        "20",
        "--repeat-penalty",
        "1.08",
        "--reasoning",
        "off",
        "--reasoning-format",
        "deepseek",
    ]


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def terminate_startup_llm_process(process_id: int) -> None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process_list = subprocess.run(
        [
            "tasklist",
            "/FI",
            f"PID eq {process_id}",
            "/FO",
            "CSV",
            "/NH",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        creationflags=flags,
        check=False,
    )
    if "llama-server.exe" not in process_list.stdout.lower():
        raise RuntimeError("无法确认本地 Qwen 进程归属，请使用一键启动重新启动。")
    stopped = subprocess.run(
        ["taskkill", "/PID", str(process_id), "/T", "/F"],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=flags,
        check=False,
    )
    if stopped.returncode != 0:
        raise RuntimeError("本地 Qwen 进程未能正常卸载。")


async def start_local_llm() -> None:
    global local_llm_process

    if await llm_ready():
        return
    if not LOCAL_LLM_SERVER.is_file():
        raise RuntimeError(f"缺少本地 LLM 服务：{LOCAL_LLM_SERVER}")
    if not LOCAL_LLM_MODEL.is_file():
        raise RuntimeError(f"缺少本地 Qwen 模型：{LOCAL_LLM_MODEL}")

    log_root = ROOT / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    stdout_log = (log_root / "ui-llama.stdout.log").open("ab")
    stderr_log = (log_root / "ui-llama.stderr.log").open("ab")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            local_llm_command(),
            cwd=ROOT,
            stdout=stdout_log,
            stderr=stderr_log,
            creationflags=flags,
        )
    except OSError as error:
        raise RuntimeError(f"本地 Qwen 启动失败：{error}") from error
    finally:
        stdout_log.close()
        stderr_log.close()
    local_llm_process = process

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            local_llm_process = None
            raise RuntimeError("本地 Qwen 启动失败，请查看 logs/ui-llama.stderr.log。")
        if await llm_ready():
            return
        await asyncio.sleep(0.5)

    await asyncio.to_thread(terminate_process, process)
    local_llm_process = None
    raise RuntimeError("等待本地 Qwen 启动超时。")


async def stop_local_llm() -> None:
    global local_llm_process, startup_local_llm_pid

    process = local_llm_process
    if process is not None:
        await asyncio.to_thread(terminate_process, process)
        local_llm_process = None
    elif startup_local_llm_pid:
        if not await llm_ready():
            startup_local_llm_pid = 0
            return
        await asyncio.to_thread(
            terminate_startup_llm_process,
            startup_local_llm_pid,
        )
        startup_local_llm_pid = 0
    elif await llm_ready():
        raise RuntimeError("无法确认本地 Qwen 进程归属，请使用一键启动重新启动。")
    else:
        return

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not await llm_ready():
            return
        await asyncio.sleep(0.25)
    raise RuntimeError("本地 Qwen 已退出，但模型端口仍未释放。")


async def apply_llm_provider_runtime(config: dict[str, Any]) -> None:
    """本地 Qwen 那一档已移除：模型一律走外部厂商，经 DSH bridge 转发。

    保留这个函数是为了兼容调用点；顺手确保本地 llama.cpp 不会残留占显存。
    """
    async with llm_runtime_lock:
        await stop_local_llm()


async def heygem_ready() -> bool:
    return HEYGEM_ENABLED and await tcp_ready("127.0.0.1", HEYGEM_PORT)


async def liveact_ready() -> bool:
    if not LIVEACT_ENABLED:
        return False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{LIVEACT_URL}/health")
        if response.status_code != 200:
            return False
        payload = response.json()
        return payload.get("status") == "ok"
    except (httpx.HTTPError, ValueError):
        return False


async def service_status() -> dict[str, Any]:
    local_llm, realtime, heygem_active, liveact_active = await asyncio.gather(
        llm_ready(),
        tcp_ready("127.0.0.1", REALTIME_PORT),
        heygem_ready(),
        liveact_ready(),
    )
    provider = active_llm_provider()
    driver = current_avatar_driver()
    llm = bool(provider and provider.get("base_url") and provider.get("api_key") and provider.get("model"))
    memory = {"status": "error", "message": "记忆服务未连接"}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(DSH_BRIDGE_URL.rstrip("/").removesuffix("/v1") + "/health")
            response.raise_for_status()
            bridge = response.json()
            memory = bridge.get("memory", {"status": "pending", "message": "等待检查记忆"})
            llm = llm and bool(bridge.get("ok"))
    except (httpx.HTTPError, ValueError):
        llm = False
    return {
        "llm": llm,
        "memory": memory,
        "local_llm": local_llm,
        "llm_provider": (provider or {}).get("label", ""),
        "realtime": realtime,
        "heygem": heygem_active,
        "heygem_active": heygem_active,
        "liveact": liveact_active,
        "liveact_active": liveact_active,
        "avatar_driver": driver,
        "avatar_driver_ready": liveact_active if driver == "image" else heygem_active,
    }


@app.on_event("startup")
async def startup() -> None:
    migrate_legacy_outputs()
    ensure_default_avatar()


@app.on_event("shutdown")
async def shutdown() -> None:
    global local_llm_process

    process = local_llm_process
    if process is not None:
        await asyncio.to_thread(terminate_process, process)
        local_llm_process = None


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    driver = current_avatar_driver()
    return {
        "character": {
            "id": CHARACTER,
            "name": "小满" if CHARACTER == "xiaoman" else "凛",
            "subtitle": "本地陪伴实验 / LOCAL PRESENCE",
        },
        "avatar": avatar_payload(driver),
        "idleVideo": idle_video_payload(),
        "systemPrompt": current_system_prompt(),
        "llmProvider": public_llm_provider_config(),
        "affinity": affinity.state(),
        "lipsync": {
            "driver": driver,
            "enabled": lipsync_enabled(),   # 前端以这个为准，不看自己的 localStorage
            "options": [{"id": "video", "label": "视频驱动"}],
        },
        "services": await service_status(),
        "audio": {
            "sampleRate": SAMPLE_RATE,
            "chunkMs": 320,
        },
    }


@app.put("/api/avatar-driver")
async def update_avatar_driver(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="请求内容不是有效的 JSON。") from None
    try:
        driver = normalize_avatar_driver(
            payload.get("driver") if isinstance(payload, dict) else None
        )
        persist_avatar_driver(driver)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"保存口型方案失败：{error}") from error
    return {
        "ok": True,
        "driver": driver,
        "avatar": avatar_payload(driver),
        "services": await service_status(),
    }


@app.put("/api/system-prompt")
async def update_system_prompt(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="请求内容不是有效的 JSON。") from None

    try:
        prompt = normalize_system_prompt(payload.get("prompt") if isinstance(payload, dict) else None)
        persist_system_prompt(prompt)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f"保存 System Prompt 失败：{error}") from error

    return {
        "ok": True,
        "prompt": prompt,
        "characters": len(prompt),
    }


@app.put("/api/llm-provider")
async def update_llm_provider(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="请求内容不是有效的 JSON。") from None

    previous = read_llm_provider_config()
    try:
        config = apply_provider_action(payload)
        await apply_llm_provider_runtime(config)
        persist_llm_provider_config(config)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except OSError as error:
        with suppress(RuntimeError):
            await apply_llm_provider_runtime(previous)
        raise HTTPException(status_code=500, detail=f"保存 LLM 配置失败：{error}") from error

    return {
        "ok": True,
        "llmProvider": public_llm_provider_config(),
        "localLlmLoaded": False,
    }


@app.post("/api/llm/v1/chat/completions")
async def proxy_llm_chat_completions(request: Request) -> Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="请求内容不是有效的 JSON。") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="LLM 请求必须是 JSON 对象。")

    warmup = pipeline_warmup_response(payload)
    if warmup is not None:
        return Response(
            content=json.dumps(warmup, ensure_ascii=False),
            media_type="application/json",
        )

    provider = active_llm_provider()
    if not provider or not provider.get("api_key"):
        raise HTTPException(status_code=503, detail="还没有配置语言模型：到设置里填一个提供方的 API Key。")
    prepared = prepare_llm_payload(payload, provider)
    target, headers = llm_completion_target(provider)
    timeout = httpx.Timeout(240.0, connect=15.0)

    if prepared.get("stream") is True:
        client = httpx.AsyncClient(timeout=timeout)
        try:
            upstream = await client.send(
                client.build_request("POST", target, headers=headers, json=prepared),
                stream=True,
            )
        except httpx.RequestError as error:
            await client.aclose()
            raise HTTPException(
                status_code=502,
                detail=f"LLM 服务连接失败：{type(error).__name__}",
            ) from error
        if not upstream.is_success:
            error_body = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            return Response(
                content=error_body,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )

        affinity.record_turn()

        async def stream_body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.post(target, headers=headers, json=prepared)
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=f"LLM 服务连接失败：{type(error).__name__}",
        ) from error
    # 只有真答上来了才记分：上游报错那次不算相处过
    if upstream.is_success:
        affinity.record_turn()
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@app.api_route("/api/memory", methods=["GET"])
@app.api_route("/api/memory/{memory_path:path}", methods=["GET", "POST", "DELETE"])
async def memory_proxy(request: Request, memory_path: str = "") -> Response:
    if memory_path not in ("", "search", "facts") and not re.fullmatch(r"facts/\d+", memory_path):
        raise HTTPException(404, "未知记忆操作")
    target = DSH_BRIDGE_URL.rstrip("/").removesuffix("/v1") + "/memory"
    if memory_path:
        target += "/" + memory_path
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(request.method, target, params=request.query_params,
                content=await request.body(), headers={"Content-Type": "application/json"})
        return Response(response.content, status_code=response.status_code, media_type="application/json")
    except httpx.HTTPError:
        raise HTTPException(503, "记忆服务暂不可用，请稍后重试。") from None


@app.get("/api/affinity")
async def get_affinity() -> dict[str, Any]:
    return affinity.state()



# ── 参考音色（我们加的）────────────────────────────────────────
# OmniVoice 是克隆式 TTS：没有内置音色表，声音完全来自一段参考音频 +
# 这段音频的逐字文本。所以"换个声音"对用户来说就是传一段自己的录音。
#
# 目录约定（和旁挂服务约好的）：
#   voices/<音色名>/ref_audio.wav   参考音频，16k 单声道
#   voices/<音色名>/ref_text.txt    逐字文本
#
# 换音色不重启管线：管线那边的 handler 不再固定 voice，由旁挂服务的当前
# 音色说了算，这里只负责写文件 + 通知它换（见 omnivoice/server.py 的说明）。

MAX_VOICE_AUDIO_BYTES = 30 * 1024 * 1024
MAX_VOICE_SECONDS = 20          # 参考音频超过这个长度会被截断
ALLOWED_VOICE_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".webm")
_VOICE_NAME_FORBIDDEN = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


def normalize_voice_name(value: Any) -> str:
    """音色名就是文件夹名，得挡住路径穿越和奇怪字符。"""
    name = str(value or "").strip()
    if not name:
        raise ValueError("音色需要一个名字。")
    if len(name) > 40:
        raise ValueError("音色名不能超过 40 个字。")
    if _VOICE_NAME_FORBIDDEN.search(name) or name in {".", ".."} or name.startswith("."):
        raise ValueError("音色名里不能有路径符号，也不能用点开头。")
    return name


def voice_entries() -> list[dict[str, Any]]:
    """扫 voices/ 下所有带参考音频的目录。没有音频的目录不算数。"""
    if not VOICES_ROOT.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for folder in sorted(VOICES_ROOT.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        audio = next(
            (f for f in sorted(folder.iterdir()) if f.suffix.lower() in ALLOWED_VOICE_SUFFIXES),
            None,
        )
        if audio is None:
            continue
        ref_text = ""
        text_file = folder / "ref_text.txt"
        if text_file.exists():
            try:
                ref_text = text_file.read_text(encoding="utf-8").strip()
            except OSError:
                ref_text = ""
        entries.append({"name": folder.name, "refText": ref_text, "bytes": audio.stat().st_size})
    return entries


async def omnivoice_active_voice() -> str:
    """问旁挂服务当前用的是哪个。它没起来就返回空，不影响列表显示。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0)) as client:
            response = await client.get(f"{OMNIVOICE_URL.rstrip('/')}/voices")
        if response.is_success:
            return str(response.json().get("active", "") or "")
    except (httpx.RequestError, json.JSONDecodeError, ValueError):
        pass
    return ""


async def omnivoice_set_voice(name: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
            response = await client.post(
                f"{OMNIVOICE_URL.rstrip('/')}/voices/active", json={"name": name}
            )
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=f"语音服务没连上，音色没切成：{type(error).__name__}",
        ) from error
    if not response.is_success:
        detail = "语音服务拒绝了这个音色。"
        try:
            detail = str(response.json().get("detail", detail))
        except (json.JSONDecodeError, ValueError):
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)


async def normalize_reference_audio(source: Path, target: Path) -> None:
    """统一成 16k 单声道 PCM。

    管线固定吃 16k，先转好省得每次合成再转；顺手砍掉视频轨和超长部分——
    参考音频越长，合成结果越容易把原音频里的内容串进来。
    """
    ffmpeg = resolve_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError(FFMPEG_MISSING)
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-v", "error",
        "-i", str(source),
        "-vn",
        "-t", str(MAX_VOICE_SECONDS),
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(target),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("参考音频转码超时。") from None
    if process.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"这段音频解不开：{message or 'ffmpeg 没有输出'}")


@app.get("/api/voices")
async def get_voices() -> dict[str, Any]:
    return {"voices": voice_entries(), "active": await omnivoice_active_voice()}


@app.post("/api/voice")
async def upload_voice(
    request: Request,
    name: str = Query(..., min_length=1, max_length=40),
    filename: str = Query(..., min_length=1, max_length=240),
    ref_text: str = Query(..., min_length=1, max_length=400),
) -> dict[str, Any]:
    try:
        voice_name = normalize_voice_name(name)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    transcript = ref_text.strip()
    if not transcript:
        raise HTTPException(status_code=422, detail="得填上这段录音里说的原话，一个字都不能差。")

    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_VOICE_SUFFIXES:
        raise HTTPException(status_code=415, detail="参考音频支持 WAV、MP3、FLAC、M4A、OGG、AAC。")

    VOICES_ROOT.mkdir(parents=True, exist_ok=True)
    staging = VOICES_ROOT / f".upload-{uuid.uuid4().hex[:8]}{suffix}"
    total = 0
    try:
        with staging.open("wb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_VOICE_AUDIO_BYTES:
                    raise HTTPException(status_code=413, detail="参考音频不能超过 30MB。")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传的音频是空的。")

        folder = VOICES_ROOT / voice_name
        folder.mkdir(parents=True, exist_ok=True)
        # 先转到临时文件，成功了再替换：转码失败不该把原来那个音色弄坏
        converted = folder / f".ref-{uuid.uuid4().hex[:8]}.wav"
        try:
            await normalize_reference_audio(staging, converted)
        except RuntimeError as error:
            converted.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=str(error)) from error

        # 清掉这个音色下的旧音频，免得挑音频时挑到上一份
        for old in folder.iterdir():
            if old.suffix.lower() in ALLOWED_VOICE_SUFFIXES and old != converted:
                old.unlink(missing_ok=True)
        os.replace(converted, folder / "ref_audio.wav")
        (folder / "ref_text.txt").write_text(transcript, encoding="utf-8")
    finally:
        staging.unlink(missing_ok=True)

    await omnivoice_set_voice(voice_name)
    return {"ok": True, "voices": voice_entries(), "active": voice_name}


@app.put("/api/voice")
async def activate_voice(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="请求内容不是有效的 JSON。") from None
    try:
        voice_name = normalize_voice_name((payload or {}).get("name"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not (VOICES_ROOT / voice_name).is_dir():
        raise HTTPException(status_code=404, detail=f"没有叫「{voice_name}」的音色。")
    await omnivoice_set_voice(voice_name)
    return {"ok": True, "voices": voice_entries(), "active": voice_name}


@app.delete("/api/voice")
async def delete_voice(name: str = Query(..., min_length=1, max_length=40)) -> dict[str, Any]:
    try:
        voice_name = normalize_voice_name(name)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    folder = VOICES_ROOT / voice_name
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"没有叫「{voice_name}」的音色。")
    remaining = [item for item in voice_entries() if item["name"] != voice_name]
    if not remaining:
        raise HTTPException(status_code=409, detail="这是最后一个音色了，删掉她就没声音了。")
    shutil.rmtree(folder, ignore_errors=True)
    active = await omnivoice_active_voice()
    if active == voice_name or not active:
        await omnivoice_set_voice(remaining[0]["name"])
        active = remaining[0]["name"]
    return {"ok": True, "voices": remaining, "active": active}


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    return await service_status()


@app.post("/api/llm/load")
async def report_resident_llm() -> dict[str, Any]:
    """Compatibility response for pages opened before Qwen became resident."""
    return {"ok": True, "managed": False, "status": "resident"}


@app.post("/api/avatar")
async def upload_avatar(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=240),
    driver: str | None = Query(None),
) -> dict[str, Any]:
    try:
        selected = normalize_avatar_driver(driver or current_avatar_driver())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    original = Path(filename).name
    suffix = Path(original).suffix.lower()
    allowed_suffixes = (
        ALLOWED_IMAGE_SUFFIXES if selected == "image" else ALLOWED_VIDEO_SUFFIXES
    )
    if suffix not in allowed_suffixes:
        detail = (
            "图片驱动仅支持 JPG、PNG 或 WebP 图片。"
            if selected == "image"
            else "视频驱动仅支持 MP4、MOV、M4V 或 WebM 视频。"
        )
        raise HTTPException(
            status_code=415,
            detail=detail,
        )

    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(original).stem).strip("-") or "avatar"
    target_suffix = ".png" if selected == "image" else ".mp4"
    target = INPUT_ROOT / (
        f"{selected}-{stem[:40]}-{uuid.uuid4().hex[:8]}{target_suffix}"
    )
    partial = target.with_suffix(target.suffix + ".upload")
    total = 0
    material_saved = False
    maximum_bytes = MAX_AVATAR_IMAGE_BYTES if selected == "image" else MAX_AVATAR_BYTES
    try:
        with partial.open("wb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > maximum_bytes:
                    maximum_label = "20MB" if selected == "image" else "2GB"
                    raise HTTPException(
                        status_code=413,
                        detail=f"素材不能超过 {maximum_label}。",
                    )
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传的素材为空。")
        try:
            if selected == "image":
                await asyncio.to_thread(normalize_liveact_image, partial, target)
            else:
                await normalize_heygem_video(partial, target)
            material_saved = True
        except RuntimeError as error:
            raise HTTPException(status_code=415, detail=str(error)) from error
    finally:
        if partial.exists():
            partial.unlink()
        if not material_saved and target.exists():
            target.unlink()

    state = read_state()
    avatars = state.setdefault("avatars", {})
    avatars[selected] = {"filename": target.name, "original_name": original}
    state["avatar_driver"] = selected
    if selected == "video":
        state["avatar"] = target.name
        state["original_name"] = original
    write_state(state)
    return {
        "ok": True,
        "driver": selected,
        "avatar": avatar_payload(selected),
    }


@app.post("/api/idle-video")
async def upload_idle_video(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=240),
) -> dict[str, Any]:
    original = Path(filename).name
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail="待机视频仅支持 MP4、MOV、M4V 或 WebM。",
        )

    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(original).stem).strip("-") or "idle"
    target = INPUT_ROOT / f"idle-{stem[:40]}-{uuid.uuid4().hex[:8]}.mp4"
    partial = target.with_suffix(".mp4.upload")
    total = 0
    material_saved = False
    try:
        with partial.open("wb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_AVATAR_BYTES:
                    raise HTTPException(status_code=413, detail="待机视频不能超过 2GB。")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传的待机视频为空。")
        try:
            await normalize_heygem_video(partial, target)
            material_saved = True
        except RuntimeError as error:
            raise HTTPException(status_code=415, detail=str(error)) from error
    finally:
        if partial.exists():
            partial.unlink()
        if not material_saved and target.exists():
            target.unlink()

    state = read_state()
    state["idle_video"] = {
        "filename": target.name,
        "original_name": original,
    }
    write_state(state)
    return {"ok": True, "idleVideo": idle_video_payload()}


@app.get("/media/avatar/{filename}")
async def avatar_media(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    path = INPUT_ROOT / safe_name
    if safe_name != filename or not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@app.get("/media/result/{task_code}")
async def result_media(task_code: str) -> FileResponse:
    path = rendered_jobs.get(task_code)
    if path is None or not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="video/mp4")


# ── 口型分段（我们加的）────────────────────────────────────────────────
# 原版是等整段回复说完，存成一个完整音频，再一次性渲染整段口型视频。
# 延迟 = 全部 TTS 时间 + 全部视频生成时间，串在一起，回复越长等得越久，
# 输入框在这期间一直是灰的。
#
# 【当前未启用】曾经用它做过"边说边切"：音频流攒够一段就切出来先渲染。
# 首句延迟确实降下来了，但段与段之间会断开，听起来像跳着说话，体验更差，
# 所以改回整段一次性渲染。函数留着，想再试分段时把 delta 分支接回来即可。
SEGMENT_MIN_SECONDS = float(os.getenv("CYBER_SEGMENT_SECONDS", "6"))
SEGMENT_SEARCH_SECONDS = 1.5      # 在目标长度前后这个范围里找最安静的点
SEGMENT_SILENCE_WINDOW = 0.06     # 判静音用的窗口长度


def find_silence_split(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> int:
    """在 PCM16 里找一个适合切段的静音点，返回字节偏移；不该切就返回 0。

    只在 [SEGMENT_MIN_SECONDS - 搜索范围, SEGMENT_MIN_SECONDS + 搜索范围] 里找，
    取能量最低的窗口中点。攒够的音频不足一段就返回 0，交给调用方继续攒。
    """
    samples = len(pcm) // 2
    target = int(SEGMENT_MIN_SECONDS * sample_rate)
    if samples < target:
        return 0

    span = int(SEGMENT_SEARCH_SECONDS * sample_rate)
    window = max(1, int(SEGMENT_SILENCE_WINDOW * sample_rate))
    lo = max(window, target - span)
    hi = min(samples - window, target + span)
    if hi <= lo:
        return target * 2      # 找不到搜索区间就按目标长度硬切

    data = array.array("h")
    data.frombytes(pcm[: hi * 2 + window * 2])

    best_pos, best_energy = lo, None
    step = max(1, window // 3)
    for pos in range(lo, hi, step):
        chunk = data[pos : pos + window]
        energy = sum(abs(v) for v in chunk) / len(chunk)
        if best_energy is None or energy < best_energy:
            best_energy, best_pos = energy, pos
    return (best_pos + window // 2) * 2


def save_response_audio(audio: bytes, turn_id: str) -> Path:
    OUTPUT_AUDIO_ROOT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_AUDIO_ROOT / f"reply-{turn_id}.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(audio)
    return path


def container_path(path: Path) -> str:
    resolved = path.resolve()
    for root, container_root in (
        (DATA_ROOT, "/code/data"),
        (OUTPUT_ROOT, "/code/output"),
    ):
        try:
            relative = resolved.relative_to(root.resolve())
        except ValueError:
            continue
        return f"{container_root}/{relative.as_posix()}"
    raise ValueError(f"媒体文件不在容器挂载目录中：{path}")


def archive_heygem_result(source: Path, task_code: str) -> Path:
    OUTPUT_VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_VIDEO_ROOT / f"reply-{task_code}.mp4"
    target.unlink(missing_ok=True)
    shutil.move(str(source), str(target))
    return target


def resolve_liveact_result(task_code: str, reported: str | None) -> Path | None:
    candidates = [LIVEACT_RESULT_ROOT / f"{task_code}.mp4"]
    if reported:
        candidates.insert(0, LIVEACT_RESULT_ROOT / Path(reported).name)
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(LIVEACT_RESULT_ROOT.resolve())
        except ValueError:
            continue
        if candidate.exists():
            return candidate
    return None


async def download_liveact_result(task_code: str) -> Path:
    LIVEACT_RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    target = LIVEACT_RESULT_ROOT / f"{task_code}.mp4"
    partial = target.with_suffix(".mp4.part")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(
                "GET",
                f"{LIVEACT_URL}/result/{task_code}",
            ) as response:
                response.raise_for_status()
                with partial.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        output.write(chunk)
        if not partial.exists() or partial.stat().st_size == 0:
            raise RuntimeError("云端图片驱动返回了空的视频文件。")
        partial.replace(target)
        return target
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def available_migration_target(target: Path) -> Path:
    if not target.exists():
        return target
    index = 1
    while True:
        candidate = target.with_name(f"{target.stem}-legacy-{index}{target.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def migrate_legacy_outputs() -> None:
    """Move conversation outputs created by older versions into output/."""
    OUTPUT_AUDIO_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_VIDEO_ROOT.mkdir(parents=True, exist_ok=True)

    for source in INPUT_ROOT.glob("reply-*.wav"):
        target = available_migration_target(OUTPUT_AUDIO_ROOT / source.name)
        shutil.move(str(source), str(target))

    task_video_pattern = re.compile(r"([0-9a-f]{32})-r\.mp4", re.IGNORECASE)
    for legacy_root in (TEMP_ROOT, RESULT_ROOT):
        if not legacy_root.exists():
            continue
        for source in legacy_root.glob("*-r.mp4"):
            match = task_video_pattern.fullmatch(source.name)
            if match is None:
                continue
            target = available_migration_target(
                OUTPUT_VIDEO_ROOT / f"reply-{match.group(1)}.mp4"
            )
            shutil.move(str(source), str(target))


def resolve_heygem_result(task_code: str, reported: str | None) -> Path | None:
    candidates: list[Path] = []
    if reported:
        normalized = reported.replace("\\", "/")
        if normalized.startswith("/code/data/"):
            normalized = normalized.removeprefix("/code/data/")
        candidates.append(DATA_ROOT / normalized.lstrip("/"))
    candidates.extend(
        [
            TEMP_ROOT / f"{task_code}-r.mp4",
            RESULT_ROOT / f"{task_code}-r.mp4",
            DATA_ROOT / f"{task_code}-r.mp4",
        ]
    )
    for path in candidates:
        try:
            path.resolve().relative_to(DATA_ROOT.resolve())
        except ValueError:
            continue
        if path.exists():
            return path
    return None


async def wait_for_stable_file(path: Path, timeout: float = 30.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    previous_size = -1
    stable_checks = 0
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            size = path.stat().st_size
            stable_checks = stable_checks + 1 if size == previous_size and size > 0 else 0
            previous_size = size
            if stable_checks >= 2:
                return True
        await asyncio.sleep(0.6)
    return False


async def render_heygem_lipsync(
    audio_path: Path,
    transcript: str,
    notify: Callable[[dict[str, Any]], Awaitable[None]],
    segment: int = 0,
    final: bool = True,
) -> None:
    avatar = current_avatar()
    if avatar is None:
        await notify(
            {
                "type": "lipsync.skipped",
                "segment": segment,
                "final": final,
                "engine": "heygem",
                "reason": "请先为视频驱动选择角色视频。",
            }
        )
        return

    if not HEYGEM_ENABLED:
        await notify(
            {
                "type": "lipsync.skipped",
                "segment": segment,
                "final": final,
                "engine": "heygem",
                "reason": "本次启动未启用视频驱动服务。",
            }
        )
        return

    task_code = uuid.uuid4().hex
    await notify(
        {
            "type": "lipsync.queued",
                "segment": segment,
                "final": final,
            "engine": "heygem",
            "task": task_code,
            "transcript": transcript,
        }
    )

    async with heygem_lock:
        output_path: Path | None = None
        failure: Exception | None = None
        payload = {
            "audio_url": container_path(audio_path),
            "video_url": container_path(avatar),
            "code": task_code,
            "chaofen": 1,
            "watermark_switch": 0,
            "pn": 0,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"{HEYGEM_URL}/easy/submit", json=payload)
                response.raise_for_status()
                submitted = response.json()
                if submitted.get("code") not in (None, 10000):
                    raise RuntimeError(submitted.get("msg") or f"提交失败：{submitted}")

            await notify(
                {
                    "type": "lipsync.started",
                "segment": segment,
                "final": final,
                    "engine": "heygem",
                    "task": task_code,
                }
            )
            deadline = asyncio.get_running_loop().time() + 30 * 60
            missing_checks = 0
            async with httpx.AsyncClient(timeout=10) as client:
                while asyncio.get_running_loop().time() < deadline:
                    query = await client.get(
                        f"{HEYGEM_URL}/easy/query",
                        params={"code": task_code},
                    )
                    query.raise_for_status()
                    result = query.json()
                    if result.get("code") != 10000:
                        if result.get("code") == 10004:
                            missing_checks += 1
                            if missing_checks >= 8:
                                raise RuntimeError("视频驱动任务未建立或已经丢失。")
                            await asyncio.sleep(1)
                            continue
                        if result.get("code") in (9999, 10002, 10003):
                            raise RuntimeError(result.get("msg") or "视频驱动渲染失败。")
                        await asyncio.sleep(1)
                        continue

                    missing_checks = 0
                    data = result.get("data") or {}
                    status = data.get("status")
                    await notify(
                        {
                            "type": "lipsync.progress",
                "segment": segment,
                "final": final,
                            "engine": "heygem",
                            "task": task_code,
                            "progress": data.get("progress"),
                            "message": data.get("msg") or "正在生成口型",
                        }
                    )
                    if status == 2:
                        output_path = resolve_heygem_result(task_code, data.get("result"))
                        if output_path is None:
                            raise RuntimeError("视频驱动已完成，但没有找到输出视频。")
                        if not await wait_for_stable_file(output_path):
                            raise RuntimeError("视频驱动输出视频尚未写入完成。")
                        break
                    if status == 3:
                        raise RuntimeError(data.get("msg") or "视频驱动渲染失败。")
                    await asyncio.sleep(1)

                if output_path is None:
                    raise TimeoutError("视频驱动渲染超过三十分钟。")
        except (
            httpx.HTTPError,
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
        ) as error:
            failure = error

        if failure is not None:
            await notify(
                {
                    "type": "lipsync.error",
                "segment": segment,
                "final": final,
                    "engine": "heygem",
                    "task": task_code,
                    "message": public_engine_text(failure),
                }
            )
            return

        if output_path is None:
            await notify(
                {
                    "type": "lipsync.error",
                "segment": segment,
                "final": final,
                    "engine": "heygem",
                    "task": task_code,
                    "message": "视频驱动没有返回成片。",
                }
            )
            return

        archived_path = archive_heygem_result(output_path, task_code)
        rendered_jobs[task_code] = archived_path
        await notify(
            {
                "type": "lipsync.done",
                "segment": segment,
                "final": final,
                "engine": "heygem",
                "task": task_code,
                "url": f"/media/result/{task_code}",
            }
        )


async def render_liveact_lipsync(
    audio_path: Path,
    transcript: str,
    notify: Callable[[dict[str, Any]], Awaitable[None]],
    segment: int = 0,
    final: bool = True,
) -> None:
    avatar = current_avatar(driver="image")
    if avatar is None:
        await notify(
            {
                "type": "lipsync.skipped",
                "segment": segment,
                "final": final,
                "engine": "image",
                "reason": "请先为图片驱动选择一张角色图片。",
            }
        )
        return
    if not LIVEACT_ENABLED:
        await notify(
            {
                "type": "lipsync.skipped",
                "segment": segment,
                "final": final,
                "engine": "image",
                "reason": "本次启动未启用图片驱动服务。",
            }
        )
        return
    if not await liveact_ready():
        await notify(
            {
                "type": "lipsync.skipped",
                "segment": segment,
                "final": final,
                "engine": "image",
                "reason": "图片驱动服务尚未就绪。",
            }
        )
        return

    task_code = uuid.uuid4().hex
    await notify(
        {
            "type": "lipsync.queued",
                "segment": segment,
                "final": final,
            "engine": "image",
            "task": task_code,
            "transcript": transcript,
        }
    )

    async with liveact_lock:
        output_path: Path | None = None
        failure: Exception | None = None
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                with avatar.open("rb") as image_file, audio_path.open("rb") as audio_file:
                    response = await client.post(
                        f"{LIVEACT_URL}/start_stream",
                        data={
                            "task_id": task_code,
                            "main_prompt": (
                                "A person speaks naturally to the camera with subtle "
                                "facial expressions and gentle head movement."
                            ),
                            "prompt_json": "[]",
                            "fps": "24",
                            "stream_with_audio": "false",
                        },
                        files={
                            "img_file": (avatar.name, image_file, "image/png"),
                            "audio_file": (audio_path.name, audio_file, "audio/wav"),
                        },
                    )
                response.raise_for_status()
                submitted = response.json()
                if submitted.get("status") != "success":
                    raise RuntimeError(
                        submitted.get("message") or f"提交失败：{submitted}"
                    )

            await notify(
                {
                    "type": "lipsync.started",
                "segment": segment,
                "final": final,
                    "engine": "image",
                    "task": task_code,
                }
            )
            deadline = asyncio.get_running_loop().time() + LIVEACT_TIMEOUT_SECONDS
            async with httpx.AsyncClient(timeout=20) as client:
                while asyncio.get_running_loop().time() < deadline:
                    response = await client.get(
                        f"{LIVEACT_URL}/task_status/{task_code}"
                    )
                    response.raise_for_status()
                    data = response.json()
                    status = str(data.get("status", "")).lower()
                    await notify(
                        {
                            "type": "lipsync.progress",
                "segment": segment,
                "final": final,
                            "engine": "image",
                            "task": task_code,
                            "progress": {
                                "done": data.get("generated_chunks"),
                                "total": data.get("total_chunks"),
                            },
                            "message": data.get("message") or "正在生成口型",
                        }
                    )
                    if status == "finished" or (
                        data.get("is_done") and not data.get("error")
                    ):
                        output_path = resolve_liveact_result(
                            task_code,
                            data.get("final_video_path"),
                        )
                        if output_path is None:
                            output_path = await download_liveact_result(task_code)
                        if not await wait_for_stable_file(output_path):
                            raise RuntimeError(
                                "图片驱动输出视频尚未写入完成。"
                            )
                        break
                    if status in {"failed", "error"} or data.get("error"):
                        raise RuntimeError(
                            data.get("error")
                            or data.get("message")
                            or "图片驱动渲染失败。"
                        )
                    await asyncio.sleep(1)
                if output_path is None:
                    raise TimeoutError("图片驱动渲染超时。")
        except (
            httpx.HTTPError,
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
        ) as error:
            failure = error

        if failure is not None:
            await notify(
                {
                    "type": "lipsync.error",
                "segment": segment,
                "final": final,
                    "engine": "image",
                    "task": task_code,
                    "message": str(failure),
                }
            )
            return
        if output_path is None:
            await notify(
                {
                    "type": "lipsync.error",
                "segment": segment,
                "final": final,
                    "engine": "image",
                    "task": task_code,
                    "message": "图片驱动没有返回成片。",
                }
            )
            return

        archived_path = archive_heygem_result(output_path, task_code)
        rendered_jobs[task_code] = archived_path
        await notify(
            {
                "type": "lipsync.done",
                "segment": segment,
                "final": final,
                "engine": "image",
                "task": task_code,
                "url": f"/media/result/{task_code}",
            }
        )


# 数字人开关。前端那个「数字人」按钮切的就是它——关掉之后不再提交渲染，
# 浏览器直接播语音管线推过来的 PCM，一轮从十几秒缩到两三秒。
#
# 存在 state.json 里而不是内存变量：内存变量一重启就回到"开"，
# 而前端那边记着"关"，于是界面显示关掉了、后台还在吭哧吭哧渲染。
# 现在以服务端为准，/api/config 会把它带给前端。
def lipsync_enabled() -> bool:
    value = read_state().get("lipsync_enabled")
    return True if value is None else bool(value)


def persist_lipsync_enabled(enabled: bool) -> None:
    state = read_state()
    state["lipsync_enabled"] = bool(enabled)
    write_state(state)


@app.put("/api/lipsync-enabled")
async def update_lipsync_enabled(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="请求内容不是有效的 JSON。") from None
    enabled = bool(payload.get("enabled", True))
    persist_lipsync_enabled(enabled)
    logger.info("数字人开关 -> %s", "开" if enabled else "关")
    return {"ok": True, "enabled": enabled}


async def render_selected_lipsync(
    audio_path: Path,
    transcript: str,
    notify: Callable[[dict[str, Any]], Awaitable[None]],
    segment: int = 0,
    final: bool = True,
) -> None:
    if not lipsync_enabled():
        # 纯对话模式：前端已经在播声音了，这里什么都不做，
        # 连 skipped 事件都不发——发了前端会以为"本该有口型但失败了"。
        return
    await render_heygem_lipsync(audio_path, transcript, notify, segment=segment, final=final)


@app.websocket("/ws/chat")
async def chat_socket(client: WebSocket) -> None:
    await client.accept()
    client_id = str(client.query_params.get("client_id", "")).strip()[:128]
    claimed, displaced = await claim_chat_client(client, client_id)
    if not claimed:
        await client.close(code=4001, reason="A newer browser tab owns the voice session")
        return
    if displaced is not None:
        with suppress(Exception):
            await displaced.close(
                code=4001,
                reason="Voice session moved to a newer browser tab",
            )

    # 新会话开始：把当前等级定格，并让下一次请求带上一句"你们处到哪一步了"。
    # 注意它走的是 prompt 前缀不是人设——人设一变 DSH 就重启，记忆会跟着没。
    affinity.begin_session()
    mark_affinity_opening()

    send_lock = asyncio.Lock()
    render_tasks: set[asyncio.Task[None]] = set()

    async def notify(payload: dict[str, Any]) -> None:
        async with send_lock:
            await client.send_json(payload)

    try:
        if not await wait_for_realtime_slot():
            await notify(
                {
                    "type": "gateway.error",
                    "message": "声音会话仍在释放，请稍后重试。",
                }
            )
            await client.close(code=1013, reason="Realtime session slot is busy")
            return

        async with websockets.connect(
            REALTIME_URL,
            additional_headers=[("Authorization", "Bearer local")],
            max_size=2**24,
        ) as upstream:
            first = json.loads(await asyncio.wait_for(upstream.recv(), timeout=15))
            await notify(first)

            async def browser_to_realtime() -> None:
                while True:
                    message = await client.receive_json()
                    message_type = message.get("type")
                    if message_type == "system.prompt.set":
                        try:
                            prompt = normalize_system_prompt(message.get("prompt"))
                        except ValueError as error:
                            await notify(
                                {
                                    "type": "gateway.error",
                                    "message": str(error),
                                }
                            )
                            continue
                        await upstream.send(
                            json.dumps(
                                system_prompt_update_event(prompt),
                                ensure_ascii=False,
                            )
                        )
                        await notify(
                            {
                                "type": "system.prompt.changed",
                                "characters": len(prompt),
                            }
                        )
                    elif message_type == "user.text":
                        text = str(message.get("text", "")).strip()
                        if not text:
                            continue
                        await upstream.send(
                            json.dumps(
                                {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "id": f"msg_{uuid.uuid4().hex}",
                                        "type": "message",
                                        "role": "user",
                                        "content": [{"type": "input_text", "text": text}],
                                    },
                                },
                                ensure_ascii=False,
                            )
                        )
                        await upstream.send(json.dumps({"type": "response.create"}))
                    elif message_type in {
                        "input_audio_buffer.append",
                        "input_audio_buffer.commit",
                        "input_audio_buffer.clear",
                        "response.cancel",
                    }:
                        if message_type == "response.cancel":
                            for task in tuple(render_tasks):
                                task.cancel()
                        await upstream.send(json.dumps(message))

            async def realtime_to_browser() -> None:
                response_audio = bytearray()
                transcript_parts: list[str] = []
                segment_index = 0

                def dispatch_segment(pcm: bytes, text: str, final: bool) -> None:
                    """把一段音频丢去渲染口型，不阻塞音频流继续往下收。"""
                    nonlocal segment_index
                    if not pcm:
                        return
                    turn_id = uuid.uuid4().hex
                    audio_path = save_response_audio(pcm, turn_id)
                    task = asyncio.create_task(
                        render_selected_lipsync(
                            audio_path, text, notify,
                            segment=segment_index, final=final,
                        )
                    )
                    render_tasks.add(task)
                    task.add_done_callback(render_tasks.discard)
                    segment_index += 1

                while True:
                    event = json.loads(await upstream.recv())
                    event_type = event.get("type", "")
                    if event_type == "response.created":
                        response_audio.clear()
                        transcript_parts.clear()
                        segment_index = 0
                    elif event_type in ("response.output_audio.delta", "response.audio.delta"):
                        delta = event.get("delta", "")
                        if delta:
                            response_audio.extend(base64.b64decode(delta))
                    elif event_type in (
                        "response.output_audio_transcript.done",
                        "response.audio_transcript.done",
                    ):
                        text = event.get("transcript", "")
                        if text:
                            transcript_parts.append(text)

                    await notify(event)

                    if event_type == "response.done" and response_audio:
                        # 整段一次性渲染。
                        # 之前试过按静音切段、生成一段播一段，首句确实快，但段与段
                        # 之间会断，听感是跳着说的，反而怪。宁可多等，也要一口气说完。
                        dispatch_segment(
                            bytes(response_audio),
                            "".join(transcript_parts).strip(),
                            final=True,
                        )
                        response_audio = bytearray()
                        transcript_parts = []
                    elif event_type == "response.done":
                        await notify(
                            {
                                "type": "lipsync.skipped",
                "segment": segment,
                "final": final,
                                "engine": current_avatar_driver(),
                                "reason": "本轮没有收到可用于口型的声音。",
                            }
                        )

            await run_bidirectional(browser_to_realtime(), realtime_to_browser())
    except WebSocketDisconnect:
        pass
    except (OSError, asyncio.TimeoutError, websockets.ConnectionClosed) as error:
        with suppress(Exception):
            await notify({"type": "gateway.error", "message": str(error)})
    finally:
        await release_chat_client(client)
        for task in render_tasks:
            task.cancel()
        if render_tasks:
            await asyncio.gather(*render_tasks, return_exceptions=True)
        with suppress(Exception):
            await client.close()


app.mount("/", StaticFiles(directory=STATIC_ROOT, html=True), name="static")
