# 模型与数字人资源下载

GitHub 仓库只保存源码和文档，不包含约 10.6 GiB 的模型与数字人 rootfs。下载后请保持下面的目录名不变；启动器会按这些固定路径检查文件。

## 先区分源码和整合包

- **Windows 整合包**已经带有固定版本的 Python、FFmpeg、Python 依赖层和模型，适合直接双击使用。
- **GitHub 源码**用于查看和继续开发，不是一个可以单独从零安装的二进制发行包。仅下载下表四套模型还不能启动，它还需要 `runtime/python311/`、`runtime/ffmpeg/`、`app/.venv/`、`app/.venv-omni-overlay/` 和 DSH 运行组件。

这些运行资源与当前 CUDA / PyTorch 版本强绑定，仓库暂未提供可复现的从零构建脚本。拿到源码的普通用户应使用同版本、已经验证过的 Windows 整合包；开发者不要把通用 `pip install` 当作一键包的等价替代。

## 原项目参考资料

本项目的 Windows 整合方式部分参考了 `penposs` 作者公开分享的“开源赛博女友”方案。原作者的资料入口：

- [AI 学习资料库 / 开源赛博女友](https://e5fklqa5fj.feishu.cn/wiki/ZPFIwAWzfiDilAkcHk0czuiRnhf)
- [夸克网盘下载](https://pan.quark.cn/s/0add2d447f6c?pwd=56W8)，提取码 `56W8`
- [penposs 的 GitHub](https://github.com/penposs)

网盘内容由原作者维护，是本项目的上游参考资料，不是当前 GitHub 版本的二进制发行包。其版本和文件结构可能变化，不能直接假定与本仓库完全一致。

## 分别下载模型

| 组件 | 官方地址 | 本项目使用版本 | 放置目录 | 本机占用 | 许可要点 |
|---|---|---|---|---:|---|
| OmniVoice | [固定 revision](https://huggingface.co/k2-fsa/OmniVoice/tree/c5fdb5ccb189668d56333f77ba2629f4cd7535f4) | revision `c5fdb5c` | `AI-Girlfriend-Models/tts/omnivoice/` | 约 3.04 GiB | 代码 Apache-2.0；**预训练权重 CC-BY-NC，仅限非商业用途** |
| Whisper large-v3-turbo | [固定 revision](https://huggingface.co/openai/whisper-large-v3-turbo/tree/41f01f3fe87f28c78e2fbf8b568835947dd65ed9) | revision `41f01f3` | `AI-Girlfriend-Models/stt/whisper-large-v3-turbo/` | 约 1.51 GiB | MIT |
| multilingual-e5-small | [固定 revision](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3) | revision `614241f` | `AI-Girlfriend-Models/memory/multilingual-e5-small/` | 约 470 MiB（筛选后的运行文件） | MIT |
| Silero VAD | [v6.2.1](https://github.com/snakers4/silero-vad/tree/v6.2.1) | tag `v6.2.1` | `AI-Girlfriend-Models/vad/silero-vad/` | 约 34 MiB | MIT |

这些版本号对应当前已验证的本地整合包。官方仓库将来更新时，新版本不一定与现有运行环境兼容。

安装 [Hugging Face Hub CLI](https://huggingface.co/docs/huggingface_hub/guides/cli) 后，可在项目根目录执行：

```powershell
hf download k2-fsa/OmniVoice `
  --revision c5fdb5ccb189668d56333f77ba2629f4cd7535f4 `
  --local-dir AI-Girlfriend-Models/tts/omnivoice

hf download openai/whisper-large-v3-turbo `
  --revision 41f01f3fe87f28c78e2fbf8b568835947dd65ed9 `
  --local-dir AI-Girlfriend-Models/stt/whisper-large-v3-turbo

hf download intfloat/multilingual-e5-small `
  --revision 614241f622f53c4eeff9890bdc4f31cfecc418b3 `
  --include config.json model.safetensors sentencepiece.bpe.model special_tokens_map.json tokenizer_config.json tokenizer.json `
  --local-dir AI-Girlfriend-Models/memory/multilingual-e5-small

git clone --depth 1 --branch v6.2.1 `
  https://github.com/snakers4/silero-vad.git `
  AI-Girlfriend-Models/vad/silero-vad
```

国内访问 Hugging Face 较慢时，可以打开表格里的“固定 revision”链接手动下载。整个仓库的文件层级要原样保留，不能只下载最大的权重文件。E5 只需下载命令中 `--include` 列出的六个运行文件；如果下载整个 E5 仓库，会连同 ONNX、OpenVINO 等重复格式占用约 2.12 GiB。

## 数字人 rootfs

数字人口型还需要一个独立的 WSL2 镜像：

```text
engine/duix-rootfs.tar.gz    约 5.57 GiB（5.98 GB）
```

它不是 Hugging Face 模型。数字人上游源码是 [duixcom/Duix-Avatar](https://github.com/duixcom/Duix-Avatar)，`penposs` 作者保留了 [HeyGem.ai 分支](https://github.com/penposs/HeyGem.ai)。当前本机已验证整合包中的 rootfs 大小约 5.57 GiB；原始数字人整合思路来源于上面的原作者公开资料。

GitHub 仓库不附带该镜像，也不主张上游网盘中的文件一定与当前版本相同或可以被任意二次分发。取得与本项目匹配的镜像后放到 `engine/`，再双击 `首次安装.cmd`；安装器会把它导入为 WSL2 发行版。

只使用纯语音时，不需要下载这个 rootfs，也不需要安装 WSL2。

## 不需要下载的语言模型

对话 LLM 使用 GLM、Kimi、DeepSeek 或其他 OpenAI-compatible API，不在本机加载大语言模型。第一次启动后，在“设置 → 语言模型”中填写自己的 API Key 即可。

## 下载完成后的检查

目录至少应包含：

```text
AI-Girlfriend-Models/
├── memory/multilingual-e5-small/config.json
├── stt/whisper-large-v3-turbo/config.json
├── tts/omnivoice/model.safetensors
└── vad/silero-vad/hubconf.py

engine/
└── duix-rootfs.tar.gz       # 仅数字人需要
```

如果下载的是源码仓库而非完整整合包，上面的目录检查只覆盖模型，不代表运行环境已经齐全。面向新手发布时，应直接提供已经验证过的整合包；模型权重和数字人镜像必须继续遵守各自许可证与来源页面的条款。
