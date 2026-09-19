"""Report whether Windows exposes a usable default microphone."""

from __future__ import annotations

import sys

import sounddevice as sd


def main() -> None:
    input_device, _ = sd.default.device
    if input_device is None or int(input_device) < 0:
        print(
            "没有检测到 Windows 默认麦克风。请在“设置 > 系统 > 声音 > 输入”中启用并选择录音设备。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    try:
        stream = sd.InputStream(
            device=int(input_device),
            samplerate=16000,
            dtype="int16",
            channels=1,
            blocksize=512,
        )
        stream.start()
        stream.stop()
        stream.close()
    except Exception as error:
        print(f"默认麦克风无法打开：{error}", file=sys.stderr)
        raise SystemExit(3) from error

    print(f"麦克风预检通过：{sd.query_devices(int(input_device))['name']}")


if __name__ == "__main__":
    main()
