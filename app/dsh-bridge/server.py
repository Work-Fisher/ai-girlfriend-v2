"""把 DSH 包装成 OpenAI 兼容端点，给整合包当"有记忆的大脑"。

为什么是这个形状：
  整合包自己已经有一层 LLM 代理（ui/server.py 的 /api/llm/v1/chat/completions），
  它按 heygem-data/llm-provider.json 里的 base_url 转发。所以只要我们在本地
  提供一个 /v1/chat/completions，把 llm-provider 指过来，**他的前端和语音管线
  一行都不用改**，就能换成 DSH 驱动。

  上一版是走 DSH 插件让 DSH 当编排器，那样会和他的 UI 抢对话主循环。
  这一版 DSH 只当"模型"，主循环还在他手里，职责干净。

记忆：
  DSH 的会话是持久的，同一个 session_id 续着聊，上下文由 DSH 自己管理和压缩，
  不受这边 chat_size 的限制。session id 存在 dsh-bridge/session.json 里，
  完成一轮才更新检查点；重启从有效历史恢复，恢复失败暂停对话。POST /reset 明确重置。

密钥与上游：
  用户在整合包的设置面板里维护一张提供方列表（DeepSeek / Kimi / 智谱 …），
  每个存自己的 Base URL、API Key、模型。他的代理把当前启用的那个拆成：
      Authorization: Bearer <key>
      X-Upstream-Base-Url: <厂商地址>
      body.model: <模型名>
  我们照单全收去开 DSH。所以密钥只在整合包那边存一份，不必落到我们的
  config.json 里；config.json 只作为命令行/离线场景的兜底。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from memory import read_json, write_json, recovery_candidates, commit_session
from long_term_memory import MemoryStore, LocalEmbeddings
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dsh-bridge")

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
SESSION_PATH = Path(os.getenv("DSH_SESSION_PATH", str(HERE / "session.json")))
PERSONA_PATCH = Path(os.getenv("DSH_PERSONA_PATH", str(HERE / "persona.cordis.yml")))

app = FastAPI(title="DSH bridge")

_harness = None
_harness_key: tuple[str, str, str, str, str] | None = None   # (provider, base_url, api_key, model, persona指纹)
_harness_lock = threading.Lock()
_run_lock = threading.Lock()   # DSH 会话是有状态的，同一时刻只能有一个 run
_cfg: dict[str, Any] = {}
_active_session_id = ""
_memory_error = ""
_store = None
_index_lock = threading.Lock()
_memory_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-index")
_index_error = ""


@app.on_event("startup")
def warm_memory():
    def warm():
        global _index_error
        try:
            long_term_store().embeddings.encode(["记忆检索准备"])
            update_memory_index(None)
        except Exception:
            _index_error = "语义检索模型暂不可用；聊天与事实记忆仍可使用。"
    _memory_jobs.submit(warm)


def long_term_store():
    global _store
    if _store is None:
        home = Path(_cfg.get("dsh_home") or HERE / "dsh-home")
        model = HERE.parent.parent / "AI-Girlfriend-Models/memory/multilingual-e5-small"
        _store = MemoryStore(home / "long-term-memory.sqlite", LocalEmbeddings(model))
    return _store


def update_memory_index(provider):
    global _index_error
    if not _index_lock.acquire(blocking=False):
        return
    try:
        store = long_term_store()
        errors = []
        try:
            while store.index_pending():
                pass
        except Exception:
            errors.append("历史索引暂未完成")
        if provider:
            try:
                store.extract_pending(provider)
            except Exception:
                errors.append("事实整理暂未完成")
        _index_error = "；".join(errors)
    except Exception:
        _index_error = "长期记忆整理暂未完成，聊天记录已保留，下次会重试。"
        logger.warning("长期记忆整理失败", exc_info=False)
    finally:
        _index_lock.release()


@app.get("/memory")
def list_memories():
    store = long_term_store()
    return {"facts": store.facts(), "stats": store.stats(), "message": _index_error}


class FactInput(BaseModel):
    key: str
    value: str


@app.post("/memory/facts")
def save_memory(fact: FactInput):
    try:
        long_term_store().save_fact(fact.key, fact.value, manual=True, quote="用户在记忆面板确认")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


@app.delete("/memory/facts/{identifier}")
def delete_memory(identifier: int):
    long_term_store().delete_fact(identifier)
    return {"ok": True}


@app.get("/memory/search")
def search_memories(q: str):
    if not q.strip() or len(q) > 500:
        raise HTTPException(422, "搜索内容需为 1–500 字")
    return {"results": long_term_store().search(q)}


def load_config() -> dict[str, Any]:
    """配置优先级：环境变量 > config.json > 默认值。"""
    cfg: dict[str, Any] = {
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
        "base_url": "",
        "api_key": "",
        "dsh_home": str(HERE / "dsh-home"),
        "profile": "sdk",
        "cwd": str(HERE / "workspace"),
        "max_tokens": None,
        "request_timeout_seconds": 180.0,
    }
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("config.json 读不了（%s），用默认值", exc)
    for key, env in (
        ("provider", "DSH_PROVIDER"),
        ("model", "DSH_MODEL"),
        ("base_url", "DSH_BASE_URL"),
        ("api_key", "DSH_API_KEY"),
        ("dsh_home", "DSH_HOME"),
        ("profile", "DSH_PROFILE"),
    ):
        value = os.getenv(env, "").strip()
        if value:
            cfg[key] = value
    return cfg


def _read_session_state() -> dict[str, Any]:
    try:
        return read_json(SESSION_PATH)
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "记忆索引无法读取，已暂停对话以保护历史。") from exc


def seed_path() -> Path:
    return Path(_cfg.get("dsh_home") or (HERE / "dsh-home")) / "companion-seed.json"


def memory_status() -> dict:
    if _memory_error:
        return {"status": "error", "message": _memory_error}
    if not _active_session_id:
        return {"status": "pending", "message": "下次对话时检查并恢复历史"}
    try:
        status = read_json(seed_path())
        if status.get("target") != _active_session_id:
            raise ValueError("recovery acknowledgement mismatch")
        return status
    except (OSError, ValueError):
        return {"status": "error", "message": "无法确认记忆恢复状态，已暂停对话"}


def require_memory_ready() -> dict:
    status = memory_status()
    if status.get("status") not in ("ready", "restored", "fresh"):
        raise HTTPException(503, status.get("message") or "历史恢复失败，已暂停对话，原记录仍保留。")
    return status


def current_session_id() -> str:
    return _active_session_id or str(_read_session_state().get("session_id") or "")


def close_harness() -> None:
    global _harness, _harness_key
    harness, _harness = _harness, None
    _harness_key = None
    if harness is not None:
        harness.close()


def get_harness(api_key: str = "", model: str = "", persona: str = "", base_url: str = ""):
    """懒启动，并在凭据/模型变化时重建。

    api_key 和 model 来自整合包设置面板：他的 UI 代理把用户填的 key 放进
    Authorization 头、把模型名放进 body 再转给我们（见 ui/server.py 的
    llm_completion_target / prepare_llm_payload）。所以用户只要在他原有的
    「三方 API」面板里填一次，不需要我们再做一套界面，key 也不必落到我们的
    config.json 里。

    base_url 是上游厂商地址，走 X-Upstream-Base-Url 头传来；取不到才退回
    config.json。三样取值都参与签名。

    DSH 子进程一旦起来就复用；凭据、模型或人设变了才重启——人设是启动时
    定型的系统提示，不重启改不动。
    """
    global _harness, _harness_key, _active_session_id, _memory_error

    provider = _cfg["provider"]
    base_url = base_url or _cfg.get("base_url") or ""
    key = api_key or _cfg.get("api_key") or ""

    # 整合包的代理会用 llm-provider.json 里的 model 覆盖请求里的模型名
    # （见他 ui/server.py 的 prepare_llm_payload）。那个字段我们出厂填的是
    # 占位符，直接透传给 DSH 会变成"去问一个叫 dsh 的模型"，上游必然报错。
    # 所以占位符一律忽略，退回 config.json 里配的真实模型名。
    name = model if model.lower() not in ("", "dsh", "local", "default") else ""
    name = name or _cfg.get("model") or ""
    if not key:
        raise HTTPException(503, "还没有 API Key：在整合包设置里选「三方 API」并填写，或在 dsh-bridge/config.json 里配置。")

    # 人设进签名：它是启动时定型的系统提示，改了必须重启 DSH 才生效
    persona_mark = hashlib.sha1(persona.encode("utf-8")).hexdigest()[:12] if persona else ""
    signature = (provider, base_url, key, name, persona_mark)
    if _harness is not None and _harness_key == signature:
        return _harness

    with _harness_lock:
        if _harness is not None and _harness_key == signature:
            return _harness
        if _harness is not None:
            logger.info("模型/凭据/人设有变，重启 DSH")
            try:
                _harness.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("旧 DSH 关闭失败，继续新建")
            _harness = None

        from deepseek_harness import DeepSeekHarness

        Path(_cfg["cwd"]).mkdir(parents=True, exist_ok=True)
        Path(_cfg["dsh_home"]).mkdir(parents=True, exist_ok=True)

        kwargs: dict[str, Any] = {
            "provider": provider,
            "model": name,
            "api_key": key,
            "cwd": _cfg["cwd"],
            "dsh_home": _cfg["dsh_home"],
            "profile": _cfg["profile"],
        }
        if base_url:
            kwargs["base_url"] = base_url
        if _cfg.get("max_tokens"):
            kwargs["max_tokens"] = int(_cfg["max_tokens"])
        if _cfg.get("request_timeout_seconds"):
            kwargs["request_timeout_seconds"] = float(_cfg["request_timeout_seconds"])

        if persona:
            kwargs["patches"] = (str(write_persona_patch(persona)),)

        # A pending runtime ID never replaces the last completed checkpoint.
        _memory_error = ""
        _active_session_id = "companion-" + uuid.uuid4().hex[:12]
        candidates = recovery_candidates(_read_session_state())
        write_json(seed_path(), {
            "target": _active_session_id,
            "candidates": candidates,
            "status": "pending",
        })
        candidate_harness = DeepSeekHarness(**kwargs)
        try:
            candidate_harness.__enter__()
            require_memory_ready()  # Missing/failed plugin must not silently lose history.
        except Exception as exc:
            candidate_harness.close()
            _memory_error = "历史恢复未完成，已暂停对话，原记录仍保留。"
            raise HTTPException(503, _memory_error) from exc
        _harness = candidate_harness
        _harness_key = signature
        logger.info("DSH 就绪，记忆状态=%s", memory_status().get("status"))
    return _harness


def run_with_session(harness, prompt: str):
    global _memory_error
    session_id = current_session_id()
    try:
        require_memory_ready()
        result = harness.run(prompt, session_id=session_id)
        recovery = require_memory_ready()
        if not (result.final_response or "").strip():
            raise HTTPException(502, "模型没有返回内容，本轮未更新记忆检查点。")
        if recovery.get("status") == "ready":
            raise HTTPException(503, "历史尚未接入新会话，已暂停对话。")
        positions = [event["seq"] for event in result.events if type(event.get("seq")) is int]
        if not positions:
            raise HTTPException(503, "无法确认对话保存边界，本轮未更新记忆检查点。")
        commit_session(SESSION_PATH, session_id, recovery, max(positions) + 1)
        return session_id, result
    except Exception:
        # A failed/in-flight turn cannot become the next recovery checkpoint.
        _memory_error = "本轮对话未完成；下次对话将重新检查最近的有效历史。"
        close_harness()
        raise


# ---- OpenAI 兼容层 ----

class ChatMessage(BaseModel):
    role: str
    content: Any = ""


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = []
    companion_context: str = ""
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


def _text_of(content: Any) -> str:
    """OpenAI 的 content 可能是字符串，也可能是分段数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in (None, "text", "input_text"):
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content or "")


# 语音管线会在真正的用户消息后面追加一条语言指令
# （base_openai_compatible_language_model.py:568
#  `Please reply to my message in {lang}.`）。
# 如果按"取最后一条 user"来读，读到的就是这句指令，用户真正说的话被丢掉，
# 于是她会回"回中文没问题……倒是你，到现在还什么都没说"。
# 这类附加指令要识别出来并跳过。
_APPENDED_INSTRUCTIONS = (
    "please reply to my message in ",
    "please respond in ",
)


def _is_appended_instruction(text: str) -> bool:
    low = text.strip().lower()
    return any(low.startswith(prefix) for prefix in _APPENDED_INSTRUCTIONS)


def split_messages(messages: list[ChatMessage]) -> tuple[str, str]:
    """拆出 (人设, 这一轮用户真正说的话)。

    历史不用重传：DSH 自己按 session_id 记着上下文，重传反而会和它的记忆打架、
    白烧 token。

    "最后一条 user" 不一定是用户说的话——语音路会在后面追加语言指令，
    所以从后往前找第一条**不是附加指令**的 user 消息。
    """
    system = "\n\n".join(
        p for p in (_text_of(m.content).strip() for m in messages if m.role == "system") if p
    )

    last_user = ""
    skipped: list[str] = []
    for m in reversed(messages):
        if m.role != "user":
            continue
        text = _text_of(m.content).strip()
        if not text:
            continue
        if _is_appended_instruction(text):
            skipped.append(text)
            continue
        last_user = text
        break

    if not last_user:
        raise HTTPException(400, "messages 里没有用户消息（只有附加指令）")

    # 语言指令本身是有用的（决定用哪种语言回），附回到句尾而不是丢掉
    # 附加语言提示不是用户事实，也不能成为新一轮问题。
    return system, last_user


_LENGTH_POLICY = re.compile(
    r"\n?【说话长度 · 硬性要求】.*?(?=\n【输出格式】)",
    re.DOTALL,
)
_VOICE_BRIEF_RULE = (
    "- Keep replies brief by default: usually one spoken sentence, two if needed. "
    "Go longer only when asked."
)


def persona_for_output_mode(persona: str, mode: str) -> str:
    """移除固定篇幅规则，再把当前播放通道的规则放在人格末尾。"""
    stable = _LENGTH_POLICY.sub("\n", persona).replace(_VOICE_BRIEF_RULE, "")
    if mode == "voice-only":
        policy = (
            "【当前通道：纯语音，硬性规则】\n"
            "可以把意思完整讲完，通常说四到八句、约一百二十到四百八十个中文字符；"
            "内容简单时可以更短，用户要求详细时可以适当更长。"
            "每句话都用句号、问号、感叹号或分号自然收尾，方便逐句合成和连续播放。"
            "不要因为旧的人格篇幅建议把回答压成两三句，也不要为了凑长度重复。"
        )
    else:
        policy = (
            "【当前通道：数字人，硬性规则】\n"
            "每次回复最多九十六个字符，标点也计入；控制在两三句话内。"
            "即使用户要求详细说明，也先在九十六字内回答重点。"
        )
    return stable.rstrip() + "\n\n" + policy


def spoken_text_for_mode(text: str, mode: str) -> str:
    """数字人最终保险：模型偶尔越界时也不提交超长口型任务。"""
    cleaned = text.strip()
    if mode != "digital-human" or len(cleaned) <= 96:
        return cleaned
    clipped = cleaned[:96]
    cut = max(clipped.rfind(mark) for mark in "。！？；!?;")
    return clipped[:cut + 1] if cut >= 24 else clipped


def write_persona_patch(persona: str) -> Path:
    """把人设写成 DSH 的配置补丁。

    为什么不能只在每轮 prompt 里塞一段 <persona>：
      DSH 不是纯聊天模型，它是个带工具和工作区的 agent，有自己的系统提示和身份。
      实测它会用那个身份回答——问"听得到我说话吗"，它答"刚才在改一段对话，
      删了又加回来"，那是它把工作区里的动静当成自己刚才在做的事。
      prompt 里的一段文本压不住系统提示层面的身份。

    正确的入口是覆盖 system-prompt 那一行的配置。它出厂长这样：
      personaPrefix: You are a coding agent powered by the {{model}} model.
      personaSuffix: Your working directory is {{cwd}}.
    —— "编码 agent" 和 "你的工作目录是" 就是从这儿来的。

    四个开关一起用才干净：
      personaPrefix          换成用户的人设
      personaSuffix          清空，去掉"工作目录"那句
      includeHarnessIdentity 关掉写死的 DeepSeek Harness 身份
      includeRuntimeContext  关掉运行时上下文快照，"刚才在改一段对话"就是它漏的

    （注意不能用 insert 新加一行 @deepseek-ai/dsh-persona：profile 里已经注册过
     deployment:persona-prefix 这个段落，重复注册会直接启动失败。）

    人设来自整合包设置面板里那个 System Prompt 框（他已经有 PUT /api/system-prompt），
    每轮请求都会随 messages 带过来，变了就重写补丁并重启 DSH。
    """
    # 模板里的 {{variable}} 是严格解析的，用户人设里出现 {{ 会让整个配置加载失败。
    # 人设是自然语言，没有理由带模板语法，直接转义掉最安全。
    safe = persona.replace("{{", "｛｛").replace("}}", "｝｝")

    body = {
        "id": "system-prompt",
        "config": {
            "personaPrefix": safe,
            "personaSuffix": "",
            "includeHarnessIdentity": False,
            "includeRuntimeContext": False,
        },
    }
    # JSON 是 YAML 的子集，用 json.dumps 省掉多行文本的转义烦恼
    PERSONA_PATCH.write_text(
        "# 由 dsh-bridge 自动生成，内容来自整合包设置面板里的人设，请勿手工编辑。\n"
        + json.dumps([body], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return PERSONA_PATCH


def _completion_payload(text: str, model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:16],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _spoken_segments(text: str, target: int = 90) -> list[str]:
    """把相邻句子合并成约 target 字的语音段，并始终优先在标点处结束。"""
    sentences: list[str] = []
    buffer = ""
    for character in text:
        buffer += character
        if character in "。！？；!?;\n":
            if buffer.strip():
                sentences.append(buffer)
            buffer = ""
    if buffer:
        sentences.append(buffer)

    segments: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = current + sentence
        # 60–120 字是 90 字附近的自然浮动；单句超过 120 时保留完整句子。
        if current and len(current) >= 60 and len(candidate) > 120:
            segments.append(current)
            current = sentence
        elif current and len(candidate) > target and abs(len(current) - target) <= abs(len(candidate) - target):
            segments.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        segments.append(current)
    return segments


def _sse_chunks(text: str, model: str) -> Iterator[str]:
    """将完整回复按约 90 字的标点段转换为兼容 SSE。"""
    cid = "chatcmpl-" + uuid.uuid4().hex[:16]
    created = int(time.time())

    def frame(delta: dict[str, Any], finish: str | None = None) -> str:
        body = {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"

    yield frame({"role": "assistant", "content": ""})
    for segment in _spoken_segments(text):
        yield frame({"content": segment})
    yield frame({}, finish="stop")
    yield "data: [DONE]\n\n"


@app.get("/health")
def health() -> dict:
    return {
        "ok": memory_status().get("status") != "error",
        "memory": memory_status(),
        "started": _harness is not None,
        "provider": _cfg.get("provider"),
        "model": _harness_key[3] if _harness_key else _cfg.get("model"),
        "session_id": current_session_id(),
        # key 平时由整合包设置面板随请求带过来，这里只报告当前是否已经握到一个
        "has_api_key": bool((_harness_key[2] if _harness_key else "") or _cfg.get("api_key")),
        "key_source": "设置面板" if (_harness_key and _harness_key[2] and _harness_key[2] != _cfg.get("api_key")) else ("config.json" if _cfg.get("api_key") else "未配置"),
    }


@app.get("/v1/models")
def models() -> dict:
    name = _cfg.get("model") or "dsh"
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "owned_by": "dsh"},
            {"id": "dsh", "object": "model", "owned_by": "dsh"},
        ],
    }


@app.post("/reset")
def reset() -> dict:
    """Explicit reset starts a new lineage without deleting old session files."""
    global _active_session_id, _memory_error
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "正在对话，请结束后再重置记忆。")
    try:
        if not _index_lock.acquire(blocking=False):
            raise HTTPException(409, "正在整理记忆，请稍后重置。")
        try:
            close_harness()
            write_json(SESSION_PATH, {"history": [], "reset": True})
            write_json(seed_path(), {"status": "pending", "candidates": []})
            write_json(seed_path().with_name("companion-history.json"), {"events": []})
            long_term_store().clear()
            _active_session_id = ""
            _memory_error = ""
            return {"ok": True, "session_id": ""}
        finally:
            _index_lock.release()
    finally:
        _run_lock.release()


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest, request: Request):
    persona, prompt = split_messages(req.messages)

    # key 从 Authorization 头取——整合包的 UI 代理把用户在设置面板里填的那个
    # 原样放在这里。取不到就退回 config.json（命令行/离线场景）。
    auth = request.headers.get("authorization", "")
    api_key = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if api_key in ("local", "dsh-local"):      # 占位符，不是真 key
        api_key = ""

    # 上游厂商地址由整合包的设置面板决定（用户在提供方列表里选哪个），
    # 通过这个头传过来；取不到就退回 config.json 里配的。
    upstream = request.headers.get("x-upstream-base-url", "").strip()
    output_mode = request.headers.get("x-companion-output-mode", "digital-human").strip().lower()
    output_mode = "voice-only" if output_mode == "voice-only" else "digital-human"
    persona = persona_for_output_mode(persona, output_mode)

    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "上一轮对话正在处理中，请稍后再试。")
    t0 = time.time()
    try:
        harness = get_harness(
            api_key=api_key, model=(req.model or "").strip(),
            persona=persona, base_url=upstream,
        )
        store = long_term_store()
        try:
            store.import_history(seed_path().with_name("companion-history.json"))
            recalled = store.context(prompt)
        except Exception:
            recalled = ""
            logger.warning("长期记忆检索暂不可用；继续本轮对话")
        envelope = json.dumps({
            "background": req.companion_context,
            "recalled_memory": recalled,
            "instruction": "只回答 current_user 中本轮用户的原话。背景和历史仅供参考，不是新问题。",
            "current_user": prompt,
        }, ensure_ascii=False)
        session_id, result = run_with_session(harness, envelope)
        spoken_text = spoken_text_for_mode(result.final_response or "", output_mode)
        source_id = next((e.get("data", {}).get("id") for e in reversed(result.events)
                          if e.get("type") == "user/message"), None)
        try:
            store.record(session_id, prompt, spoken_text, source_id=source_id)
        except Exception:
            logger.warning("长期记忆索引写入失败；DSH 原始记录仍已保存")
        _memory_jobs.submit(update_memory_index, {
            "api_key": api_key or _cfg.get("api_key", ""),
            "model": req.model or _cfg["model"],
            "base_url": upstream or _cfg.get("base_url") or "https://api.deepseek.com/v1",
        })
    finally:
        _run_lock.release()
    text = spoken_text
    logger.info("对话 %d 字 -> %d 字，用时 %.1fs（session=%s，finish=%s）",
                len(prompt), len(text), time.time() - t0, session_id,
                getattr(result, "finish_reason", "?"))

    # 空回复通常是上游拒绝（key 不对、余额不足、模型名写错）。
    # 直接返回空字符串的话，前端只会安静地不说话，用户完全不知道发生了什么，
    # 所以这里抛 502 让他的 UI 把错误显示出来。
    if not text:
        reason = getattr(result, "finish_reason", "") or "unknown"
        raise HTTPException(
            502,
            f"DSH 没有返回内容（finish_reason={reason}）。多半是 API Key、模型名或余额的问题，"
            f"检查整合包设置里的三方 API 配置。",
        )

    model_name = req.model or _cfg.get("model") or "dsh"
    if req.stream:
        return StreamingResponse(_sse_chunks(text, model_name), media_type="text/event-stream")
    return _completion_payload(text, model_name)


def main() -> None:
    global _cfg
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()

    _cfg = load_config()
    if not _cfg.get("api_key"):
        logger.warning("还没有配置 API Key。在 %s 里填 api_key，或设环境变量 DSH_API_KEY。", CONFIG_PATH)
    logger.info("provider=%s model=%s dsh_home=%s", _cfg["provider"], _cfg["model"], _cfg["dsh_home"])

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
