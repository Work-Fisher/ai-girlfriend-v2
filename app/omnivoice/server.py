"""OmniVoice 声音克隆合成服务（旁挂进程）。

为什么单独起一个进程，而不是装进整合包自带的环境：
  OmniVoice 0.2.1 要 transformers 5.x（它用的是 Mimi 音频分词器那套新 API），
  而整合包自带的环境是 transformers 4.57.3，Whisper 和 faster-qwen3-tts 都
  钉在这个大版本上。硬升会把作者调通的那几样一起弄坏。
  两边各跑各的解释器，用 HTTP 传 PCM，谁也不碰谁的依赖。

接口：
  GET  /health               -> {"ok": true, "loaded": bool}
  GET  /voices               -> 列出音色，标出当前生效的那个
  POST /voices/active {name} -> 换音色，立刻生效，不用重启
  POST /tts  {text, voice?, speed?}
       -> audio/wav（16kHz 单声道 int16，和语音管线的采样率一致）

为什么"当前音色"存在这边而不是管线里：
  管线的 TTS handler 是启动时把 voice 定死的（从环境变量读一次），要换得重启
  整条管线，模型全部重载，十几秒起步。所以我们让 handler 不传 voice，由这里的
  默认值说了算——换音色就是改这个进程里的一个变量，下一句话就是新声音。
  另外写一份到 voices/.active，进程重启后还认得。

音色目录约定（沿用我们一直在用的那套）：
  voices/<音色名>/ref_audio.wav   参考音频
  voices/<音色名>/ref_text.txt    这段音频的逐字文本（必须逐字对齐，
                                  错一个字都会让克隆音色飘）
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import threading
import time
import wave
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logger = logging.getLogger("omnivoice")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PIPELINE_SR = 16000          # speech-to-speech 管线固定吃 16k
AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a")

app = FastAPI(title="OmniVoice sidecar")

_model = None
_model_lock = threading.Lock()
_voices_dir: Path
_model_dir: Path
_default_voice: str
_default_speed: float
_active_file: Path


def _load_model():
    """懒加载，进程内只加载一次。首次约 10-20 秒。"""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        import torch
        from omnivoice import OmniVoice

        if not _model_dir.is_dir():
            raise RuntimeError(f"OmniVoice 模型目录不存在：{_model_dir}")
        logger.info("loading OmniVoice from %s", _model_dir)
        t0 = time.time()
        _model = OmniVoice.from_pretrained(
            str(_model_dir),
            device_map="cuda:0" if torch.cuda.is_available() else "cpu",
            dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        )
        logger.info("OmniVoice loaded in %.1fs", time.time() - t0)
    return _model


def _voice_names() -> list[str]:
    """带参考音频的目录才算一个可用音色。"""
    if not _voices_dir.is_dir():
        return []
    return [
        d.name
        for d in sorted(_voices_dir.iterdir())
        if d.is_dir() and any(f.suffix.lower() in AUDIO_SUFFIXES for f in d.iterdir())
    ]


def _persist_active(name: str) -> None:
    """把当前音色写进 voices/.active，进程重启后还认得。"""
    try:
        _active_file.parent.mkdir(parents=True, exist_ok=True)
        _active_file.write_text(name, encoding="utf-8")
    except OSError:
        logger.warning("写不了 %s，这次换的音色重启后会丢", _active_file)


def _resolve_voice(name: str | None) -> tuple[Path, str]:
    """返回 (参考音频路径, 逐字文本)。"""
    voice = (name or _default_voice or "").strip()
    candidate = _voices_dir / voice if voice else None
    if candidate is None or not candidate.is_dir():
        # 没指定或指定的不存在：取第一个带音频的目录，保证服务不至于直接不可用
        dirs = sorted(d for d in _voices_dir.iterdir() if d.is_dir()) if _voices_dir.is_dir() else []
        candidate = next(
            (d for d in dirs if any(f.suffix.lower() in AUDIO_SUFFIXES for f in d.iterdir())),
            None,
        )
    if candidate is None:
        raise HTTPException(500, f"没有可用音色，请在 {_voices_dir} 下建 <音色名>/ref_audio.wav")

    audio = next(
        (f for f in sorted(candidate.iterdir()) if f.suffix.lower() in AUDIO_SUFFIXES), None
    )
    if audio is None:
        raise HTTPException(500, f"音色 {candidate.name} 缺参考音频")
    text_file = candidate / "ref_text.txt"
    ref_text = text_file.read_text(encoding="utf-8").strip() if text_file.exists() else ""
    return audio, ref_text


def _to_wav_bytes(pcm: np.ndarray, sr: int = PIPELINE_SR) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float | None = None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "loaded": _model is not None, "voices_dir": str(_voices_dir)}


@app.get("/voices")
def voices() -> dict:
    names = _voice_names()
    items = []
    for name in names:
        text_file = _voices_dir / name / "ref_text.txt"
        ref_text = text_file.read_text(encoding="utf-8").strip() if text_file.exists() else ""
        items.append({"name": name, "refText": ref_text, "hasRefText": bool(ref_text)})
    # 当前生效的那个：默认值指不到时，_resolve_voice 会退到第一个，这里跟它保持一致
    active = _default_voice if _default_voice in names else (names[0] if names else "")
    return {"voices": names, "items": items, "active": active, "default": _default_voice}


class ActiveVoiceRequest(BaseModel):
    name: str


@app.post("/voices/active")
def set_active_voice(req: ActiveVoiceRequest) -> dict:
    global _default_voice
    name = (req.name or "").strip()
    names = _voice_names()
    if name not in names:
        raise HTTPException(404, f"没有叫「{name}」的音色（现有：{'、'.join(names) or '无'}）")
    _default_voice = name
    _persist_active(name)
    logger.info("音色切到 %s", name)
    return {"ok": True, "active": name}


@app.post("/tts")
def tts(req: TTSRequest) -> Response:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空")

    ref_audio, ref_text = _resolve_voice(req.voice)
    model = _load_model()

    # speed：1.0 是模型默认语速，OmniVoice 默认偏快，陪聊场景 0.75~0.9 更自然
    speed = req.speed if req.speed is not None else _default_speed
    gen_kwargs = {"text": text, "ref_audio": str(ref_audio), "ref_text": ref_text}
    if speed is not None:
        gen_kwargs["speed"] = float(speed)

    t0 = time.time()
    audio = np.asarray(model.generate(**gen_kwargs)[0], dtype=np.float32).flatten()

    native_sr = int(getattr(model, "sampling_rate", 24000) or 24000)
    if native_sr != PIPELINE_SR:
        from scipy.signal import resample_poly

        g = int(np.gcd(native_sr, PIPELINE_SR))
        audio = resample_poly(audio, up=PIPELINE_SR // g, down=native_sr // g)

    pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    logger.info("tts %d chars -> %.2fs audio in %.2fs", len(text), len(pcm) / PIPELINE_SR, time.time() - t0)
    return Response(content=_to_wav_bytes(pcm), media_type="audio/wav")


def main() -> None:
    global _voices_dir, _model_dir, _default_voice, _default_speed
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--model-dir", default=os.getenv("OMNIVOICE_MODEL_DIR", ""))
    parser.add_argument("--voices-dir", default=os.getenv("OMNIVOICE_VOICES_DIR", ""))
    parser.add_argument("--voice", default=os.getenv("OMNIVOICE_VOICE", ""))
    parser.add_argument("--speed", type=float, default=float(os.getenv("OMNIVOICE_SPEED", "0.85")))
    parser.add_argument("--preload", action="store_true", help="启动时就把模型载进显存")
    args = parser.parse_args()

    _model_dir = Path(args.model_dir) if args.model_dir else here.parent.parent / "AI-Girlfriend-Models" / "tts" / "omnivoice"
    _voices_dir = Path(args.voices_dir) if args.voices_dir else here.parent / "voices"
    _default_speed = args.speed

    # 上次在界面里选的音色优先于命令行 --voice：命令行那个是出厂默认，
    # 用户后来选过的才是他真正想要的。
    global _active_file
    _active_file = _voices_dir / ".active"
    remembered = ""
    if _active_file.exists():
        try:
            remembered = _active_file.read_text(encoding="utf-8").strip()
        except OSError:
            remembered = ""
    _default_voice = remembered or args.voice

    logger.info("model_dir=%s voices_dir=%s voice=%s speed=%s",
                _model_dir, _voices_dir, _default_voice or "(自动)", _default_speed)
    if args.preload:
        _load_model()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
