"""OmniVoice 声音克隆 TTS handler。

它不在本进程里加载模型，而是调用旁挂的 OmniVoice 服务（app/omnivoice/server.py）。

为什么这样拆：
  OmniVoice 0.2.1 需要 transformers 5.x，整合包自带环境是 4.57.3，Whisper 和
  faster-qwen3-tts 都钉在 4.x 上。硬升级会把作者已经调通的部分弄坏。
  旁挂进程各用各的解释器，这边只做一次 HTTP 调用，依赖零冲突。

相比整合包原本的 Qwen3-TTS（预设音色 + 文字描述风格），OmniVoice 是直接拿一段
参考音频克隆，能复刻指定的人声；代价是不支持流式，必须整句合成完才出声。
所以这里按句子切分、逐句请求，让首字延迟接近流式的体验。
"""

from __future__ import annotations

import io
import logging
import re
import wave
from threading import Event
from typing import Any, Iterator, Optional

import numpy as np
import requests

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.cancel_scope import CancelScope
from speech_to_speech.pipeline.handler_types import TTSIn, TTSOut
from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, EndOfResponse
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker

logger = logging.getLogger(__name__)

PIPELINE_SR = 16000

# 断句：中英文句末标点都算，保留标点让合成带上语气
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；!?;])|(?<=\.\s)")
# 太短的片段单独合成会丢韵律，并回前一句
_MIN_SEGMENT_CHARS = 8


class OmniVoiceTTSHandler(BaseHandler[TTSIn, TTSOut]):
    """把文本交给旁挂 OmniVoice 服务，产出 16kHz int16 音频块。"""

    def setup(
        self,
        should_listen: Event,
        base_url: str = "http://127.0.0.1:8791",
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        blocksize: int = 1024,
        timeout: float = 120.0,
        gen_kwargs: dict[str, Any] | None = None,
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
    ) -> None:
        self.should_listen = should_listen
        self.base_url = base_url.rstrip("/")
        self.voice = voice or None
        self.speed = speed
        self.blocksize = int(blocksize)
        self.timeout = float(timeout)
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self._session = requests.Session()

        logger.info("OmniVoice TTS -> %s (voice=%s speed=%s)", self.base_url, self.voice or "默认", self.speed)
        self.warmup()

    def warmup(self) -> None:
        """只探活，不强制加载模型——让首次真实请求去触发加载，启动更快。"""
        try:
            resp = self._session.get(f"{self.base_url}/health", timeout=5)
            resp.raise_for_status()
            logger.info("OmniVoice sidecar ready: %s", resp.json())
        except Exception as exc:  # noqa: BLE001
            logger.warning("OmniVoice sidecar 暂时不可达（%s）：%s，首次合成时会重试", self.base_url, exc)

    # ---- 合成 ----

    def _synthesize(self, text: str) -> np.ndarray:
        payload: dict[str, Any] = {"text": text}
        if self.voice:
            payload["voice"] = self.voice
        if self.speed is not None:
            payload["speed"] = self.speed

        resp = self._session.post(f"{self.base_url}/tts", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        with wave.open(io.BytesIO(resp.content), "rb") as w:
            if w.getframerate() != PIPELINE_SR or w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise RuntimeError(
                    f"OmniVoice 返回的格式不对：{w.getnchannels()}ch {w.getsampwidth()*8}bit {w.getframerate()}Hz"
                )
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    @staticmethod
    def _segments(text: str) -> list[str]:
        """按句切开，过短的并进前一句。"""
        parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip()]
        if not parts:
            return []
        merged: list[str] = []
        for part in parts:
            if merged and len(part) < _MIN_SEGMENT_CHARS:
                merged[-1] = f"{merged[-1]}{part}"
            else:
                merged.append(part)
        return merged

    def process(self, tts_input: TTSIn) -> Iterator[TTSOut]:
        speculative_turns = getattr(self, "speculative_turns", None)

        if isinstance(tts_input, EndOfResponse):
            if speculative_turns and not speculative_turns.is_latest_after_reopen_grace(
                tts_input.turn_id, tts_input.turn_revision
            ):
                return
            yield AUDIO_RESPONSE_DONE
            return

        if speculative_turns and not speculative_turns.is_latest_after_reopen_grace(
            tts_input.turn_id, tts_input.turn_revision
        ):
            logger.debug("丢弃过期的 TTS 输入 turn=%s rev=%s", tts_input.turn_id, tts_input.turn_revision)
            return
        if speculative_turns:
            speculative_turns.commit(tts_input.turn_id, tts_input.turn_revision)

        # 前端/会话里指定的音色优先于启动参数
        response = tts_input.response
        runtime_config = tts_input.runtime_config
        voice: Optional[str] = None
        if response and response.audio and response.audio.output and response.audio.output.voice:
            voice = str(response.audio.output.voice)
        if not voice and runtime_config:
            audio_cfg = runtime_config.session.audio
            audio_output = audio_cfg.output if audio_cfg is not None else None
            voice = str(audio_output.voice) if audio_output is not None and audio_output.voice else None
        if voice:
            self.voice = voice

        text = (tts_input.text or "").strip()
        if not text:
            return

        for segment in self._segments(text):
            if self.stop_event.is_set():
                return
            try:
                pcm = self._synthesize(segment)
            except Exception as exc:  # noqa: BLE001
                logger.error("OmniVoice 合成失败（%s）：%s", segment[:20], exc)
                continue
            for start in range(0, len(pcm), self.blocksize):
                if self.stop_event.is_set():
                    return
                chunk = pcm[start : start + self.blocksize]
                if chunk.size:
                    yield chunk

    def on_session_end(self) -> None:
        pass
