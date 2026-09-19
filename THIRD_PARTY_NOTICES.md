# Third-party notices

这个仓库是多个组件的集成层。源码仓库不包含 Python runtime、模型权重、WSL rootfs、DSH `node_modules` 或用户媒体。

| 组件 | 用途 | 上游 / 许可说明 |
|---|---|---|
| penposs“开源赛博女友”方案 | 浏览器 UI、原始启动脚本、Windows 整合和数字人接入方案的参考与修改来源 | [原作者飞书资料页](https://e5fklqa5fj.feishu.cn/wiki/ZPFIwAWzfiDilAkcHk0czuiRnhf)、[GitHub](https://github.com/penposs)；感谢原作者公开分享。项目维护者确认引用部分来自作者公开发布的开源版本；来源页未附单独的统一许可证文件，因此本仓库不擅自为其指定 MIT、Apache-2.0 等许可证，授权范围以原作者说明为准 |
| Hugging Face speech-to-speech 0.2.11 | Realtime VAD/STT/LLM/TTS 管线 | [huggingface/speech-to-speech](https://github.com/huggingface/speech-to-speech)，Apache-2.0；许可证保留在 `app/speech-to-speech/LICENSE` |
| OmniVoice 0.2.1 | 本地声音克隆 | [代码](https://github.com/k2-fsa/OmniVoice)为 Apache-2.0；[预训练模型](https://huggingface.co/k2-fsa/OmniVoice)为 CC-BY-NC，仅限非商业用途；安装包和权重不在 Git 中 |
| DSH | 会话运行时与恢复 | DeepSeek DSH 本机安装；其 `node_modules`、会话和凭据不在 Git 中，使用与再分发遵循对应版本条款 |
| multilingual-e5-small | 本地语义嵌入 | [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)，MIT；模型权重不在 Git 中 |
| Silero VAD 6.2.1 | 语音端点检测 | [snakers4/silero-vad](https://github.com/snakers4/silero-vad/tree/v6.2.1)，MIT；模型权重不在 Git 中 |
| Whisper large-v3-turbo | 语音识别 | [openai/whisper-large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo)，MIT；模型文件不在 Git 中 |
| Duix/HeyGem 256v1 | 数字人口型 | 上游源码为 [duixcom/Duix-Avatar](https://github.com/duixcom/Duix-Avatar)，原作者保留 [HeyGem.ai 分支](https://github.com/penposs/HeyGem.ai)；WSL rootfs 来自原作者公开整合资料且不在 Git 中，其镜像、模型和媒体素材的再分发范围以来源页及各组件条款为准 |
| OpenCV Haar cascade | UI 中的人脸检测资源 | 文件头保留 Intel License Agreement 与版权声明 |

`docs/assets/demo-avatar.png` 是为本仓库公开演示生成的虚构人物图像，不对应真实人物，也不是运行包中的默认角色素材。

本说明不是法律意见。发布二进制整合包时，应同时提供所含依赖和模型的完整许可证清单。
