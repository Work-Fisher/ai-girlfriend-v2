"""Keyboard client for the local OpenAI Realtime speech-to-speech server."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import queue
import sys
import threading
import uuid

import sounddevice as sd
import websockets

SAMPLE_RATE = 16000
BLOCK_SIZE = 512


class StreamingAudioPlayer:
    """Play PCM chunks on a worker thread as soon as the server emits them."""

    def __init__(self) -> None:
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.error: Exception | None = None

    def write(self, pcm: bytes) -> None:
        if not pcm or self.error is not None:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._play, daemon=True)
            self._thread.start()
        self._chunks.put(pcm)

    def finish(self) -> None:
        if self._thread is None:
            return
        self._chunks.put(None)
        self._thread.join()

    def _play(self) -> None:
        try:
            with sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_SIZE,
            ) as stream:
                while True:
                    chunk = self._chunks.get()
                    if chunk is None:
                        return
                    stream.write(chunk)
        except Exception as error:
            self.error = error


async def receive_response(socket: websockets.ClientConnection) -> str:
    transcript_parts: list[str] = []
    transcript_deltas: list[str] = []
    player = StreamingAudioPlayer()

    try:
        while True:
            raw_event = await asyncio.wait_for(socket.recv(), timeout=180)
            event = json.loads(raw_event)
            event_type = event.get("type", "")

            if event_type in ("response.output_audio.delta", "response.audio.delta"):
                delta = event.get("delta", "")
                if delta:
                    player.write(base64.b64decode(delta))
            elif event_type in (
                "response.output_audio_transcript.delta",
                "response.audio_transcript.delta",
                "response.output_text.delta",
            ):
                transcript_deltas.append(event.get("delta", ""))
            elif event_type in (
                "response.output_audio_transcript.done",
                "response.audio_transcript.done",
            ):
                text = event.get("transcript", "")
                if text:
                    transcript_parts.append(text)
            elif event_type == "response.output_text.done":
                text = event.get("text", "")
                if text:
                    transcript_parts.append(text)
            elif event_type == "error":
                error = event.get("error", event)
                message = error.get("message", error) if isinstance(error, dict) else error
                raise RuntimeError(str(message))
            elif event_type == "response.done":
                response = event.get("response", {})
                if response.get("status") not in (None, "completed"):
                    details = response.get("status_details") or response
                    raise RuntimeError(f"回答未完成：{details}")
                break
    finally:
        player.finish()

    transcript = "".join(transcript_parts) or "".join(transcript_deltas)
    if player.error is not None:
        print(f"提示：语音播放失败，已保留文字回答：{player.error}", file=sys.stderr)
    return transcript.strip()


async def chat(url: str) -> None:
    async with websockets.connect(
        url,
        additional_headers=[("Authorization", "Bearer local")],
        max_size=2**24,
    ) as socket:
        first = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
        if first.get("type") != "session.created":
            raise RuntimeError(f"Realtime 服务返回了异常事件：{first}")

        print("键盘模式已就绪。输入内容后按回车，输入“退出”结束。")
        while True:
            try:
                text = await asyncio.to_thread(input, "你：")
            except EOFError:
                print()
                return

            text = text.strip()
            if not text:
                continue
            if text.lower() in {"/quit", "/exit"} or text in {"退出", "结束"}:
                return

            await socket.send(
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
            await socket.send(json.dumps({"type": "response.create"}))

            transcript = await receive_response(socket)
            print(f"她：{transcript or '（没有返回文字）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local keyboard-to-voice chat client")
    parser.add_argument(
        "--url",
        default="ws://127.0.0.1:8766/v1/realtime",
        help="Realtime WebSocket URL",
    )
    args = parser.parse_args()

    try:
        asyncio.run(chat(args.url))
    except KeyboardInterrupt:
        print("\n已结束。")
    except Exception as error:
        print(f"键盘模式运行失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
