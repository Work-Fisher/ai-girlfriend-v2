"""Send the generated Chinese WAV through the local Realtime pipeline once."""

from __future__ import annotations

import asyncio
import base64
import json
import wave
from pathlib import Path

import numpy as np
import soundfile as sf
import websockets
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
INPUT_WAV = ROOT / "artifacts" / "tts-smoke.wav"
OUTPUT_WAV = ROOT / "artifacts" / "e2e-response.wav"
URL = "ws://127.0.0.1:8766/v1/realtime"
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 320


def load_input() -> bytes:
    audio, source_rate = sf.read(INPUT_WAV, always_2d=True)
    mono = audio.mean(axis=1)
    if source_rate != SAMPLE_RATE:
        mono = resample_poly(mono, SAMPLE_RATE, source_rate)
    return (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def save_output(audio: bytes) -> None:
    with wave.open(str(OUTPUT_WAV), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(audio)


async def main() -> None:
    input_audio = load_input()
    response_audio = bytearray()
    input_transcript = ""
    output_transcript = ""

    async with websockets.connect(
        URL,
        additional_headers=[("Authorization", "Bearer local")],
        max_size=2**24,
    ) as socket:
        first = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        if first.get("type") != "session.created":
            raise RuntimeError(f"Unexpected first event: {first}")

        chunk_bytes = CHUNK_SAMPLES * 2
        for offset in range(0, len(input_audio), chunk_bytes):
            chunk = input_audio[offset : offset + chunk_bytes].ljust(chunk_bytes, b"\0")
            await socket.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }
                )
            )
            await asyncio.sleep(CHUNK_SAMPLES / SAMPLE_RATE)

        silence = base64.b64encode(b"\0" * chunk_bytes).decode("ascii")
        for _ in range(75):
            await socket.send(json.dumps({"type": "input_audio_buffer.append", "audio": silence}))
            await asyncio.sleep(CHUNK_SAMPLES / SAMPLE_RATE)

        while True:
            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=120))
            event_type = event.get("type", "")
            if event_type == "conversation.item.input_audio_transcription.completed":
                input_transcript = event.get("transcript", "")
            elif event_type in ("response.output_audio.delta", "response.audio.delta"):
                response_audio.extend(base64.b64decode(event.get("delta", "")))
            elif event_type in (
                "response.output_audio_transcript.delta",
                "response.audio_transcript.delta",
            ):
                output_transcript += event.get("delta", "")
            elif event_type in (
                "response.output_audio_transcript.done",
                "response.audio_transcript.done",
            ):
                output_transcript = event.get("transcript") or output_transcript
            elif event_type == "error":
                raise RuntimeError(event.get("error", event))
            elif event_type == "response.done":
                break

    if not response_audio:
        raise RuntimeError("Pipeline returned no audio")
    save_output(bytes(response_audio))
    print(f"input_transcript={input_transcript}")
    print(f"output_transcript={output_transcript}")
    print(f"response_bytes={len(response_audio)}")
    print(f"output={OUTPUT_WAV}")


if __name__ == "__main__":
    asyncio.run(main())
