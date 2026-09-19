# Third-party notices

这个仓库是多个组件的集成层。源码仓库不包含 Python runtime、模型权重、WSL rootfs、DSH `node_modules` 或用户媒体。

| 组件 | 用途 | 上游 / 许可说明 |
|---|---|---|
| Hugging Face speech-to-speech 0.2.11 | Realtime VAD/STT/LLM/TTS 管线 | [huggingface/speech-to-speech](https://github.com/huggingface/speech-to-speech)，Apache-2.0；许可证保留在 `app/speech-to-speech/LICENSE` |
| OmniVoice 0.2.1 | 本地声音克隆 | [k2-fsa/OmniVoice](https://github.com/k2-fsa/OmniVoice)；安装包和模型不在 Git 中，分发前须核对其当前许可证与模型卡 |
| DSH | 会话运行时与恢复 | DeepSeek DSH 本机安装；其 `node_modules`、会话和凭据不在 Git 中，使用与再分发遵循对应版本条款 |
| multilingual-e5-small | 本地语义嵌入 | [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)，MIT；模型权重不在 Git 中 |
| Silero VAD | 语音端点检测 | [snakers4/silero-vad](https://github.com/snakers4/silero-vad)；模型权重不在 Git 中 |
| Whisper large-v3-turbo | 语音识别 | Hugging Face 模型文件不在 Git 中；按模型卡许可使用 |
| Duix/HeyGem 256v1 | 数字人口型 | WSL rootfs 不在 Git 中；公开二进制整合包前必须单独确认镜像、模型与媒体素材的再分发许可 |
| OpenCV Haar cascade | UI 中的人脸检测资源 | 文件头保留 Intel License Agreement 与版权声明 |

浏览器 UI 与相关启动脚本的外部来源目前尚未确认。本仓库在确认其来源和公开授权前只能保存在私有仓库中；若它来自第三方项目，公开前必须补充项目链接、版权声明和许可证。

`docs/assets/demo-avatar.png` 是为本仓库公开演示生成的虚构人物图像，不对应真实人物，也不是运行包中的默认角色素材。

本说明不是法律意见。发布二进制整合包时，应同时提供所含依赖和模型的完整许可证清单。
