"""Warm the resident HeyGem workers before the UI accepts the first turn."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
import wave
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "heygem-data"
INPUT_ROOT = DATA_ROOT / "input"
DEFAULT_AVATAR = INPUT_ROOT / "avatar-demo.mp4"
DEFAULT_AVATAR_SOURCE = Path(
    os.getenv("CYBER_DEFAULT_AVATAR", str(DEFAULT_AVATAR))
)
WARMUP_AUDIO = INPUT_ROOT / "heygem-warmup.wav"
BASE_URL = os.getenv("CYBER_HEYGEM_URL", "http://127.0.0.1:8383").rstrip("/")


def ensure_inputs() -> None:
    INPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_AVATAR.exists():
        if not DEFAULT_AVATAR_SOURCE.exists() or DEFAULT_AVATAR_SOURCE == DEFAULT_AVATAR:
            raise FileNotFoundError(f"缺少 HeyGem 预热视频：{DEFAULT_AVATAR_SOURCE}")
        shutil.copy2(DEFAULT_AVATAR_SOURCE, DEFAULT_AVATAR)

    with wave.open(str(WARMUP_AUDIO), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 12_800)


def request_json(path: str, payload: dict[str, object] | None = None) -> dict:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{BASE_URL}{path}", data=body, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    ensure_inputs()
    task_code = f"warmup{uuid.uuid4().hex[:24]}"
    started = time.perf_counter()
    submitted = request_json(
        "/easy/submit",
        {
            "audio_url": "/code/data/input/heygem-warmup.wav",
            "video_url": "/code/data/input/avatar-demo.mp4",
            "code": task_code,
            "chaofen": 1,
            "watermark_switch": 0,
            "pn": 0,
        },
    )
    if submitted.get("code") != 10000:
        raise RuntimeError(submitted.get("msg") or f"HeyGem 预热提交失败：{submitted}")

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        query = request_json(f"/easy/query?{urlencode({'code': task_code})}")
        if query.get("code") == 10000:
            data = query.get("data") or {}
            if data.get("status") == 2:
                elapsed = time.perf_counter() - started
                print(f"HeyGem GPU 预热完成：{elapsed:.1f} 秒", flush=True)
                return
            if data.get("status") == 3:
                raise RuntimeError(data.get("msg") or "HeyGem 预热任务失败。")
        time.sleep(1)

    raise TimeoutError("HeyGem GPU 预热超过三分钟。")


if __name__ == "__main__":
    main()
