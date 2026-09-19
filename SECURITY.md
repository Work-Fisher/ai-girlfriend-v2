# Security and privacy

## 本地保存的数据

运行后可能包含敏感信息的目录：

- `app/heygem-data/llm-provider.json`：LLM API Key。
- `app/dsh-bridge/dsh-home/`：原始 DSH 会话与长期记忆数据库。
- `app/heygem-data/input/`：角色与待机视频。
- `app/voices/`：参考声音及逐字文本。
- `app/logs/`、`app/output/`：运行日志和生成音视频。

这些路径已加入 `.gitignore`。公开 issue、日志或压缩包前仍应人工检查，因为 `.gitignore` 不能保护已经复制到其他目录的文件。

## 网络边界

本地服务默认只监听 `127.0.0.1`。用户文本会发送给所配置的外部 LLM 提供方；语音识别、声音克隆、语义嵌入和数字人口型默认在本机处理。

## 报告问题

请不要在公开 issue 中粘贴 API Key、完整日志、真实聊天记录、声音样本或角色视频。安全问题可以通过 GitHub Security Advisory 私下报告。
