const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const socketClientId =
  globalThis.crypto?.randomUUID?.() ||
  `tab-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const RECORDING_TAIL_PADDING_MS = 500;
const SERVER_VAD_FLUSH_MS = 1600;
const VOICE_RESULT_TIMEOUT_MS = 12000;

const elements = {
  assistantReplies: $("#assistantReplies"),
  assistantText: $("#assistantText"),
  avatarImage: null,                    // 图片驱动已移除，保留字段避免各处判空
  avatarInput: $("#avatarInput"),
  avatarName: $("#avatarName"),
  avatarPlaceholder: $("#avatarPlaceholder"),
  avatarVideo: $("#avatarVideo"),
  avatarDriverServiceLabel: $("#avatarDriverServiceLabel"),
  characterName: $("#characterName"),
  composer: $("#composer"),
  composerHint: $("#composerHint"),
  history: $("#history"),
  historyCount: $("#historyCount"),
  historyList: $("#historyList"),
  historyToggle: $("#historyToggle"),
  idleUploadProgress: $("#idleUploadProgress"),
  idleVideoInput: $("#idleVideoInput"),
  idleVideoName: $("#idleVideoName"),
  llmApiKey: $("#llmApiKey"),
  llmBaseUrl: $("#llmBaseUrl"),
  llmModel: $("#llmModel"),
  llmModelPresets: $("#llmModelPresets"),
  llmProviderStatus: $("#llmProviderStatus"),
  llmServiceLabel: $("#llmServiceLabel"),
  messageInput: $("#messageInput"),
  placeholderButton: $("#placeholderButton"),
  placeholderCopy: $("#placeholderCopy"),
  placeholderTitle: $("#placeholderTitle"),
  presenceState: $("#presenceState"),
  refreshStatus: $("#refreshStatus"),
  replayButton: $("#replayButton"),
  resultVideo: $("#resultVideo"),
  sendButton: $(".send"),
  settingsButton: $("#settingsButton"),
  settingsClose: $("#settingsClose"),
  settingsPanel: $("#settingsPanel"),
  settingsScrim: $("#settingsScrim"),
  stageFrame: $(".stage-frame"),
  systemPromptCount: $("#systemPromptCount"),
  systemPromptInput: $("#systemPromptInput"),
  systemPromptStatus: $("#systemPromptStatus"),
  saveSystemPrompt: $("#saveSystemPrompt"),
  toast: $("#toast"),
  turnLabel: $("#turnLabel"),
  uploadProgress: $("#uploadProgress"),
  voiceButton: $("#voiceButton"),
  voiceLabel: $("#voiceLabel"),
  voiceOrbWrap: $(".mic-dock"),
  // 多提供方设置
  lipsyncToggle: $("#lipsyncToggle"),
  handsFreeToggle: $("#handsFreeToggle"),
  micDock: $("#micDock"),
  providerList: $("#providerList"),
  providerEditor: $("#providerEditor"),
  providerLabel: $("#providerLabel"),
  providerKind: $("#providerKind"),
  providerKindField: $("#providerKindField"),
  llmModelSelect: $("#llmModelSelect"),
  providerLabelField: $("#providerLabelField"),
  addProvider: $("#addProvider"),
  addCustomProvider: $("#addCustomProvider"),
  cancelProvider: $("#cancelProvider"),
  saveLlmProvider: $("#saveLlmProvider"),
  clearLlmApiKey: $("#clearLlmApiKey"),
  // 音色
  voiceStatus: $("#voiceStatus"),
  voiceList: $("#voiceList"),
  addVoice: $("#addVoice"),
  voiceEditor: $("#voiceEditor"),
  voiceName: $("#voiceName"),
  voiceFile: $("#voiceFile"),
  voiceFileLabel: $("#voiceFileLabel"),
  voiceRefText: $("#voiceRefText"),
  cancelVoice: $("#cancelVoice"),
  saveVoice: $("#saveVoice"),
  voiceProgress: $("#voiceProgress"),
  // 好感度
  bond: $("#bond"),
  bondLevel: $("#bondLevel"),
  bondTrack: $("#bondTrack"),
  bondFill: $("#bondFill"),
  bondNotches: $("#bondNotches"),
  bondMeta: $("#bondMeta"),
};

const state = {
  // 数字人开关。初值先用上次的本地记忆，拿到 /api/config 后以服务端为准——
  // 服务端那份是持久化的，它才决定后台到底渲不渲染。
  lipsyncEnabled: localStorage.getItem("lipsyncEnabled") !== "0",
  // 免提：麦克风一直开着，由服务端 VAD 断句，不用按住
  handsFree: localStorage.getItem("handsFree") === "1",
  avatar: null,
  avatarDriver: "video",
  busy: false,
  config: null,
  history: [],
  idleVideo: null,
  llmProvider: null,
  latestResultUrl: null,
  microphoneStream: null,
  playbackRequest: 0,
  inputContext: null,
  inputProcessor: null,
  inputSource: null,
  inputSink: null,
  activeResponseId: null,
  recordingRequested: false,
  // 本轮语音是否已经收到过服务端的任何回应（转写 / 回复 / 音频）。
  // 超时提示只在"说完了但服务端一点反应都没有"时才该出现。
  voiceTurnAnswered: false,
  affinity: null,
  voices: [],
  activeVoice: "",
  recordingStarting: false,
  stoppingRecording: false,
  recording: false,
  responseEventIds: new Set(),
  responseOpen: false,
  responseParts: [],
  systemPrompt: "",
  responseRawText: "",
  responseText: "",
  reconnectTimer: null,
  dialogueMotionTimer: null,
  socket: null,
  toastTimer: null,
  voiceResultTimer: null,
  voicePointerId: null,
};

class PcmStreamPlayer {
  constructor() {
    this.context = null;
    this.cursor = 0;
    this.sources = new Set();
  }

  async activate() {
    if (!this.context) {
      this.context = new AudioContext({ latencyHint: "interactive" });
    }
    if (this.context.state === "suspended") {
      await this.context.resume();
    }
  }

  async enqueue(base64Pcm) {
    await this.activate();
    const bytes = Uint8Array.from(atob(base64Pcm), (character) => character.charCodeAt(0));
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const sampleCount = Math.floor(bytes.byteLength / 2);
    const buffer = this.context.createBuffer(1, sampleCount, 16000);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < sampleCount; index += 1) {
      channel[index] = view.getInt16(index * 2, true) / 32768;
    }

    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const startAt = Math.max(this.context.currentTime + 0.025, this.cursor);
    source.start(startAt);
    this.cursor = startAt + buffer.duration;
    this.sources.add(source);
    source.addEventListener("ended", () => this.sources.delete(source), { once: true });
  }

  stop() {
    for (const source of this.sources) {
      try {
        source.stop();
      } catch {
        // A source may already have ended.
      }
    }
    this.sources.clear();
    this.cursor = this.context?.currentTime || 0;
  }
}

const audioPlayer = new PcmStreamPlayer();




function showToast(message, duration = 3200) {
  clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.add("show");
  state.toastTimer = setTimeout(() => elements.toast.classList.remove("show"), duration);
}

function setPresence(label) {
  if (elements.presenceState) elements.presenceState.textContent = label;
}

function clearVoiceResultTimeout() {
  if (state.voiceResultTimer === null) return;
  clearTimeout(state.voiceResultTimer);
  state.voiceResultTimer = null;
}

/**
 * 标记本轮"服务端已经有反应了"。
 *
 * 之所以要这个标记，而不是只靠 clearVoiceResultTimeout()：服务端 VAD 可能在你
 * 松手之前就断好句、转写完、甚至开始回复了，那时候定时器还没装上，清了个寂寞；
 * 等松手时再 arm 一个 12 秒的定时器，回复要是刚好在 12 秒内播完（responseOpen
 * 又变回 false），定时器就会在她说完话之后弹一句"没有听清"。
 */
function markVoiceTurnAnswered() {
  state.voiceTurnAnswered = true;
  clearVoiceResultTimeout();
}

function armVoiceResultTimeout() {
  clearVoiceResultTimeout();
  if (state.voiceTurnAnswered) return;
  state.voiceResultTimer = setTimeout(() => {
    state.voiceResultTimer = null;
    if (
      state.voiceTurnAnswered ||
      state.responseOpen ||
      state.recording ||
      state.recordingRequested
    ) {
      return;
    }
    setPresence(state.socket?.readyState === WebSocket.OPEN ? "在线" : "连接中断");
    showToast(notHeardMessage(), 4200);
  }, VOICE_RESULT_TIMEOUT_MS);
}

/** 免提模式没有"按住"这个动作，提示语得跟着模式走。 */
function notHeardMessage() {
  return state.handsFree ? "没有听清，再说一次。" : "没有听清，再按住说一次。";
}

function setTurnLabel(text, userTurn = false) {
  // 元素默认带 hidden（开局没有内容时不占位），有文字才显示
  elements.turnLabel.textContent = text || "";
  elements.turnLabel.hidden = !text;
  elements.turnLabel.classList.toggle("user-turn", userTurn);
}

function expandDialogue() {
  // 旧版有个"对话区收拢"的动画，新版左栏本来就是常驻布局，这里只需清掉定时器
  clearTimeout(state.dialogueMotionTimer);
  state.dialogueMotionTimer = null;
}

function settleDialogue(delay = 680) {
  clearTimeout(state.dialogueMotionTimer);
  state.dialogueMotionTimer = setTimeout(() => {
    state.dialogueMotionTimer = null;
    if (
      state.responseOpen ||
      state.recording ||
      state.recordingRequested ||
      elements.messageInput.value.trim()
    ) {
      return;
    }
    if (elements.assistantReplies.children.length) {
    }
  }, delay);
}

function cleanAssistantText(text) {
  let clean = String(text || "");
  clean = clean.replace(
    /<(think|analysis|reasoning)\b[^>]*>[\s\S]*?<\/\1>/gi,
    "",
  );
  const unfinishedThought = clean.search(/<(think|analysis|reasoning)\b/i);
  if (unfinishedThought >= 0) clean = clean.slice(0, unfinishedThought);
  const finalAnswer = clean.match(
    /(?:最终回答|最终答复|最终台词)\s*[:：]\s*([\s\S]+)/i,
  );
  if (finalAnswer) clean = finalAnswer[1];
  clean = clean
    .replace(/\r\n?/g, "\n")
    .replace(/<\/?(?:think|analysis|reasoning)\b[^>]*>/gi, "")
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/^[ \t]{0,3}(?:#{1,6}|>|[-+*][ \t])/gm, "")
    .replace(/[*_`~]/g, "")
    .replace(/[^\S\r\n]+/g, " ")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .join("\n");
  return clean;
}

function setAssistantText(text, updating = false) {
  const clean = cleanAssistantText(text);
  const length = Array.from(clean).length;
  elements.assistantText.textContent = clean || (updating ? "……" : "");
  elements.assistantText.title = clean;
  elements.assistantText.classList.toggle("updating", updating);
  elements.assistantText.classList.toggle("compact-response", length > 48);
  elements.assistantText.classList.toggle("dense-response", length > 96);
  fitAssistantLine(elements.assistantText);
}

function fitAssistantLine(line) {
  if (!line) return;
  line.style.removeProperty("font-size");
  requestAnimationFrame(() => {
    if (!line.isConnected || line.scrollWidth <= line.clientWidth) return;
    const currentSize = Number.parseFloat(getComputedStyle(line).fontSize);
    const fittedSize = Math.max(
      12,
      Math.floor((currentSize * line.clientWidth) / line.scrollWidth),
    );
    line.style.fontSize = `${fittedSize}px`;
  });
}

function beginAssistantTurn() {
  expandDialogue();
  // 只保留当前这一句。旧版会叠最多四条，在这版的排版里会并成四列；
  // 而且左栏本来就是"此刻她在说什么"，往回翻看对话记录就够了。
  const line = document.createElement("p");
  line.className = "assistant-reply current";
  line.setAttribute("aria-label", "本轮回复");
  elements.assistantReplies.replaceChildren(line);
  elements.assistantText = line;
  setAssistantText("", true);   // 不放省略号占位，等真的有字再显示
}

function addHistory(role, text) {
  const clean = String(text || "").trim();
  if (!clean) return;
  state.history.push({ role, text: clean });
  if (state.history.length > 12) state.history.shift();
  elements.historyCount.textContent = `${state.history.length} 条`;
  elements.historyList.replaceChildren(
    ...state.history.map((item) => {
      const row = document.createElement("div");
      row.className = "history-message";
      const label = document.createElement("b");
      label.textContent = item.role === "user" ? "你" : "她";
      const copy = document.createElement("span");
      copy.textContent = item.text;
      row.append(label, copy);
      return row;
    }),
  );
  elements.historyList.scrollTop = elements.historyList.scrollHeight;
}

function updateServices(services) {
  // 三项：语言模型（走 DSH bridge 到外部厂商）、语音管线、口型引擎
  ["llm", "realtime", "avatar_driver"].forEach((name) => {
    const online =
      name === "avatar_driver"
        ? Boolean(services?.avatar_driver_ready)
        : Boolean(services?.[name]);
    const detail = document.querySelector(`[data-service-text="${name}"]`);
    if (!detail) return;
    detail.textContent = online ? "已连接" : "未连接";
    detail.classList.toggle("online", online);
  });

  const memory = services?.memory;
  const modelDetail = document.querySelector('[data-service-text="llm"]');
  if (memory && modelDetail) {
    const labels = {
      error: "记忆恢复异常", pending: "记忆待恢复", ready: "历史已读取",
      restored: memory.fallback ? "记忆已回退恢复" : "记忆已恢复", fresh: "新会话",
    };
    modelDetail.textContent = labels[memory.status] || "记忆状态未知";
    modelDetail.title = memory.message || "";
    modelDetail.classList.toggle("online", Boolean(services.llm) && memory.status !== "error");
  }

  // 语言模型那一行顺带显示当前用的是哪家
  if (elements.llmServiceLabel) {
    const label = services?.llm_provider;
    elements.llmServiceLabel.textContent = label ? `语言模型 · ${label}` : "语言模型";
  }
  if (elements.avatarDriverServiceLabel) {
    elements.avatarDriverServiceLabel.textContent = "口型";
  }
}

function freezeAvatarOnFirstFrame(expectedSource) {
  const freeze = () => {
    if (
      expectedSource &&
      !elements.avatarVideo.src.includes(expectedSource) &&
      !elements.avatarVideo.currentSrc.includes(expectedSource)
    ) {
      return;
    }
    elements.avatarVideo.pause();
    elements.avatarVideo.currentTime = 0;
    elements.avatarVideo.classList.add("ready");
    elements.avatarPlaceholder.classList.add("hidden");
  };

  elements.avatarVideo.loop = false;
  elements.avatarVideo.muted = true;
  elements.avatarVideo.pause();
  if (elements.avatarVideo.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
    freeze();
  } else {
    elements.avatarVideo.addEventListener("loadeddata", freeze, { once: true });
  }
}

function playIdleVideo(expectedSource) {
  const reveal = () => {
    if (
      expectedSource &&
      !elements.avatarVideo.src.includes(expectedSource) &&
      !elements.avatarVideo.currentSrc.includes(expectedSource)
    ) {
      return;
    }
    elements.avatarVideo.loop = true;
    elements.avatarVideo.muted = true;
    elements.avatarVideo.classList.add("ready");
    elements.avatarPlaceholder.classList.add("hidden");
    elements.avatarVideo.play().catch(() => {});
  };

  if (elements.avatarVideo.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
    reveal();
  } else {
    elements.avatarVideo.addEventListener("loadeddata", reveal, { once: true });
  }
}

function currentAvatar() {
  return state.avatar;
}

function setAvatar(avatar) {
  state.avatar = avatar?.url ? avatar : null;
  if (avatar?.driver) state.avatarDriver = avatar.driver;
  restoreIdleAvatar();
}

// ── 画面焦点 ─────────────────────────────────────────────
// 舞台是竖高的，素材是横幅，object-fit: cover 默认居中裁，人物偏在一侧就会
// 被切到边上。服务端用人脸检测算出了焦点（media_focus），这里换算成
// object-position，让脸落在舞台中间。
//
// 待机层和成片层用同一个焦点，两段素材分辨率不同也不会在切换时"跳一下"。
function applyStageFocus(focus) {
  const x = Number.isFinite(focus?.x) ? focus.x : 0.5;
  const y = Number.isFinite(focus?.y) ? focus.y : 0.5;
  // 脸略微偏上更好看：把纵向焦点往上提一点，留出下巴和肩的空间
  const position = `${(x * 100).toFixed(1)}% ${(Math.max(0, y - 0.06) * 100).toFixed(1)}%`;
  for (const video of [elements.avatarVideo, elements.resultVideo]) {
    if (video) video.style.objectPosition = position;
  }
}

function restoreIdleAvatar() {
  state.playbackRequest += 1;
  elements.resultVideo.pause();
  elements.resultVideo.classList.remove("ready");
  const avatar = currentAvatar();
  const idleVideo = state.idleVideo;
  elements.avatarVideo.classList.remove("rendering");
  // 待机用待机素材的焦点；没有待机素材就用角色素材的
  applyStageFocus(idleVideo?.focus || avatar?.focus);
  elements.avatarName.textContent = avatar?.name || "未选择";
  elements.idleVideoName.textContent = idleVideo?.name || "未选择";

  if (idleVideo?.url) {
    if (!elements.avatarVideo.src.includes(idleVideo.url)) {
      elements.avatarVideo.src = `${idleVideo.url}?v=${Date.now()}`;
    }
    playIdleVideo(idleVideo.url);
    return;
  }

  if (!avatar?.url) {
    elements.avatarVideo.pause();
    elements.avatarVideo.classList.remove("ready");
    elements.avatarPlaceholder.classList.remove("hidden");
    return;
  }

  if (avatar.kind === "image") {
    elements.avatarVideo.pause();
    elements.avatarVideo.classList.remove("ready");
    elements.avatarVideo.removeAttribute("src");
    elements.avatarPlaceholder.classList.add("hidden");
    return;
  }

  if (!elements.avatarVideo.src.includes(avatar.url)) {
    elements.avatarVideo.src = `${avatar.url}?v=${Date.now()}`;
  }
  freezeAvatarOnFirstFrame(avatar.url);
}

function updateAvatarDriverMode() {
  // 只保留视频驱动（图片驱动那套云端 LiveAct 我们不用）
  state.avatarDriver = "video";
  if (elements.avatarDriverServiceLabel) {
    elements.avatarDriverServiceLabel.textContent = "口型";
  }
}


function openSettings() {
  elements.settingsPanel.classList.add("open");
  elements.settingsScrim.classList.add("open");
  elements.settingsPanel.setAttribute("aria-hidden", "false");
}

function closeSettings() {
  elements.settingsPanel.classList.remove("open");
  elements.settingsScrim.classList.remove("open");
  elements.settingsPanel.setAttribute("aria-hidden", "true");
}

function setBusy(busy) {
  state.busy = Boolean(busy);
  elements.messageInput.disabled = state.busy;
  elements.sendButton.disabled = state.busy;
  elements.voiceButton.disabled = state.busy || state.socket?.readyState !== WebSocket.OPEN;
  elements.composer.classList.toggle("busy", state.busy);
  elements.composerHint.textContent = state.busy
    ? "正在生成声画同步成片，请稍等……"
    : "Enter 发送 · Shift + Enter 换行";
}

function socketUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/ws/chat?client_id=${encodeURIComponent(socketClientId)}`;
}

function sendSocket(payload) {
  if (state.socket?.readyState !== WebSocket.OPEN) {
    showToast("声音服务尚未连接，请稍等。");
    return false;
  }
  state.socket.send(JSON.stringify(payload));
  return true;
}

function updateSystemPromptCount() {
  const count = elements.systemPromptInput.value.length;
  elements.systemPromptCount.textContent = `${count} / 16000`;
}

function loadSystemPrompt(prompt) {
  state.systemPrompt = prompt || "";
  elements.systemPromptInput.value = state.systemPrompt;
  elements.systemPromptStatus.textContent = state.systemPrompt ? "已同步" : "未配置";
  updateSystemPromptCount();
}

// ── 语言模型：多提供方 ────────────────────────────────────
// 参考 DSH 的做法：列出若干提供方，每个存自己的 Base URL / Key / 模型，
// 点一个就切过去。密钥只在本机保存，界面上永远只显示"已保存"。
// 三家提供方的地址和可选模型，抄自本机 DSH 的 settings.yaml（llm-pi-ai.providers），
// 保证和你在 DSH 桌面端用的是同一套 id，不会出现"模型名写错上游报错"。
const PROVIDER_PRESETS = [
  {
    id: "deepseek",
    label: "DeepSeek",
    baseUrl: "https://api.deepseek.com/v1",
    models: [
      // 这两个是 /models 实际返回的规范名。deepseek-v4-flash / deepseek-chat
      // 只是能用的别名，不写进预设，免得哪天别名下线。
      { id: "deepseek-flash",  name: "Flash（快）" },
      { id: "deepseek-v4-pro", name: "V4 Pro（强）" },
    ],
  },
  {
    id: "kimi",
    label: "Kimi",
    baseUrl: "https://api.moonshot.cn/v1",
    models: [
      { id: "kimi-k3",   name: "Kimi K3" },
      { id: "kimi-k2.5", name: "Kimi K2.5" },
    ],
  },
  {
    id: "glm",
    label: "智谱 GLM",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    models: [
      { id: "GLM-5.3-Flash", name: "GLM-5.3 Flash（快）" },
      { id: "glm-5.3",       name: "GLM-5.3（强）" },
    ],
  },
];

let editingProviderId = null;   // null = 新建
let editingIsCustom = false;

function loadLlmProvider(config) {
  const payload = config || {};
  state.llmProvider = {
    active: payload.active || "",
    providers: Array.isArray(payload.providers) ? payload.providers : [],
    presets: Array.isArray(payload.presets) ? payload.presets : [],
  };
  renderProviderList();
  elements.llmModelPresets.replaceChildren(
    ...state.llmProvider.presets.map((model) => {
      const option = document.createElement("option");
      option.value = model;
      return option;
    }),
  );
}

function renderProviderList() {
  const { providers = [], active } = state.llmProvider || {};
  elements.providerList.replaceChildren(
    ...providers.map((provider) => {
      const row = document.createElement("div");
      row.className = "provider-row" + (provider.id === active ? " active" : "");

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = provider.label;
      row.append(name);

      if (provider.custom) {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = "自定义";
        row.append(tag);
      }

      const dot = document.createElement("span");
      dot.className = "dot" + (provider.hasApiKey ? " on" : "");
      dot.title = provider.hasApiKey ? "已填写密钥" : "还没有密钥";
      row.append(dot);

      const grow = document.createElement("span");
      grow.className = "grow";
      row.append(grow);

      if (provider.id !== active) {
        const use = document.createElement("button");
        use.type = "button";
        use.textContent = "启用";
        use.addEventListener("click", () => activateProvider(provider.id));
        row.append(use);
      }

      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "编辑";
      edit.addEventListener("click", () => openProviderEditor(provider, provider.custom));
      row.append(edit);

      if (providers.length > 1) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "remove";
        remove.textContent = "删除";
        remove.addEventListener("click", () => removeProvider(provider.id));
        row.append(remove);
      }

      return row;
    }),
  );

  const current = providers.find((item) => item.id === active);
  elements.llmProviderStatus.textContent = current
    ? (current.hasApiKey ? `${current.label} · ${current.model}` : `${current.label} · 等待密钥`)
    : "未配置";
}

/** 换提供方：自动填地址、刷新可选模型。自定义的则放开名称和地址让用户自己写。 */
function selectPreset(presetId, keepModel) {
  const preset = PROVIDER_PRESETS.find((item) => item.id === presetId);
  editingProviderId = presetId;
  editingIsCustom = !preset;

  elements.providerKindField.hidden = false;
  elements.providerKind.value = preset ? preset.id : "custom";
  elements.providerLabelField.hidden = Boolean(preset);

  if (preset) {
    elements.providerLabel.value = preset.label;
    elements.llmBaseUrl.value = preset.baseUrl;
    elements.llmBaseUrl.readOnly = true;
    // 模型变成下拉：只列这家真实存在的几个，避免手写出错
    elements.llmModelSelect.hidden = false;
    elements.llmModel.hidden = true;
    elements.llmModelSelect.replaceChildren(
      ...preset.models.map((model) => {
        const option = document.createElement("option");
        option.value = model.id;
        option.textContent = `${model.name} · ${model.id}`;
        return option;
      }),
    );
    if (keepModel && preset.models.some((m) => m.id === keepModel)) {
      elements.llmModelSelect.value = keepModel;
    }
  } else {
    elements.llmBaseUrl.readOnly = false;
    elements.llmModelSelect.hidden = true;
    elements.llmModel.hidden = false;
  }
}

function openProviderEditor(provider, custom) {
  const preset = provider && PROVIDER_PRESETS.find((item) => item.id === provider.id);

  if (!provider && !custom) {
    // 「添加提供方」：默认落在第一个还没添加过的预设上
    const used = new Set((state.llmProvider?.providers || []).map((item) => item.id));
    const next = PROVIDER_PRESETS.find((item) => !used.has(item.id)) || PROVIDER_PRESETS[0];
    selectPreset(next.id);
  } else if (preset) {
    selectPreset(preset.id, provider.model);
    elements.llmBaseUrl.value = provider.baseUrl || preset.baseUrl;
  } else {
    // 自定义：名称、地址、模型都手填
    selectPreset("custom");
    editingProviderId = provider?.id || null;
    elements.providerLabel.value = provider?.label || "";
    elements.llmBaseUrl.value = provider?.baseUrl || "";
    elements.llmModel.value = provider?.model || "";
  }

  elements.llmApiKey.value = "";
  elements.llmApiKey.placeholder = provider?.hasApiKey ? "已保存，留空则保持不变" : "在这里填密钥";
  elements.clearLlmApiKey.disabled = !provider?.hasApiKey;
  elements.providerEditor.hidden = false;
  elements.llmApiKey.focus();
}

function closeProviderEditor() {
  elements.providerEditor.hidden = true;
  editingProviderId = null;
  editingIsCustom = false;
}

async function putProviders(body, successMessage) {
  const response = await fetch("/api/llm-provider", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  loadLlmProvider(payload.llmProvider);
  if (state.config) state.config.llmProvider = payload.llmProvider;
  await refreshStatus();
  if (successMessage) showToast(successMessage);
}

async function activateProvider(id) {
  try {
    await putProviders({ action: "activate", id }, "已切换，下一轮生效。");
  } catch (error) {
    showToast(`切换失败：${error.message}`);
  }
}

async function removeProvider(id) {
  try {
    await putProviders({ action: "remove", id }, "已删除。");
  } catch (error) {
    showToast(`删除失败：${error.message}`);
  }
}

async function saveLlmProvider() {
  const label = elements.providerLabel.value.trim();
  const baseUrl = elements.llmBaseUrl.value.trim();
  const model = elements.llmModelSelect.hidden
    ? elements.llmModel.value.trim()
    : elements.llmModelSelect.value;
  const apiKey = elements.llmApiKey.value.trim();

  if (editingIsCustom && !label) {
    showToast("自定义提供方需要一个名称。");
    elements.providerLabel.focus();
    return;
  }
  if (!baseUrl || !model) {
    showToast("Base URL 和模型都要填。");
    return;
  }
  const existing = (state.llmProvider?.providers || []).find((item) => item.id === editingProviderId);
  if (!apiKey && !existing?.hasApiKey) {
    showToast("请先填写 API Key。");
    elements.llmApiKey.focus();
    return;
  }

  elements.saveLlmProvider.disabled = true;
  elements.saveLlmProvider.textContent = "保存中";
  try {
    await putProviders(
      {
        action: "upsert",
        id: editingProviderId,
        label: label || undefined,
        baseUrl,
        model,
        apiKey: apiKey || undefined,
        custom: editingIsCustom,
        activate: true,
      },
      "已保存并启用，下一轮生效。",
    );
    closeProviderEditor();
  } catch (error) {
    showToast(`保存失败：${error.message}`);
  } finally {
    elements.saveLlmProvider.disabled = false;
    elements.saveLlmProvider.textContent = "保存并启用";
  }
}

async function clearLlmApiKey() {
  if (!editingProviderId) return;
  try {
    await putProviders({ action: "clear-key", id: editingProviderId }, "密钥已清除。");
    closeProviderEditor();
  } catch (error) {
    showToast(`清除失败：${error.message}`);
  }
}

async function saveSystemPrompt() {
  const prompt = elements.systemPromptInput.value.trim();
  if (prompt.length < 20) {
    showToast("System Prompt 至少需要二十个字符。");
    elements.systemPromptInput.focus();
    return;
  }

  elements.saveSystemPrompt.disabled = true;
  elements.saveSystemPrompt.textContent = "保存中";
  elements.systemPromptStatus.textContent = "正在保存";

  try {
    const response = await fetch("/api/system-prompt", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || `HTTP ${response.status}`);
    }

    loadSystemPrompt(payload.prompt);
    if (state.config) state.config.systemPrompt = payload.prompt;
    const sent = sendSocket({
      type: "system.prompt.set",
      prompt: payload.prompt,
    });
    elements.systemPromptStatus.textContent = sent ? "正在热更新" : "已保存";
    if (!sent) {
      showToast("人设已保存，下次连接时生效。");
    }
  } catch (error) {
    elements.systemPromptStatus.textContent = "保存失败";
    showToast(`人设保存失败：${error.message}`);
  } finally {
    elements.saveSystemPrompt.disabled = false;
    elements.saveSystemPrompt.textContent = "保存并热更新";
  }
}

function scheduleSocketReconnect() {
  if (state.reconnectTimer !== null) return;
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    connectSocket();
  }, 2500);
}

function connectSocket() {
  if (
    state.socket?.readyState === WebSocket.CONNECTING ||
    state.socket?.readyState === WebSocket.OPEN
  ) {
    return;
  }
  if (state.reconnectTimer !== null) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }

  const socket = new WebSocket(socketUrl());
  state.socket = socket;
  setPresence("正在连接", "listen");

  socket.addEventListener("open", () => {
    if (state.socket !== socket) return;
    setPresence("在线", "listen");
    setBusy(state.busy);
  });

  socket.addEventListener("close", (event) => {
    if (state.socket !== socket) return;
    clearVoiceResultTimeout();
    state.socket = null;
    setPresence("连接中断", "listen");
    elements.voiceButton.disabled = true;
    if (event.code === 4001) {
      setPresence("已在新窗口连接", "listen");
      return;
    }
    scheduleSocketReconnect();
  });

  socket.addEventListener("error", () => {
    if (state.socket !== socket) return;
    showToast("Realtime 服务连接失败。");
  });

  socket.addEventListener("message", async (message) => {
    if (state.socket !== socket) return;
    const event = JSON.parse(message.data);
    await handleRealtimeEvent(event);
  });
}

function responseIdFromEvent(event) {
  return String(event.response_id || event.response?.id || "").trim();
}

function isCurrentResponseEvent(event) {
  if (!state.responseOpen) return false;
  const responseId = responseIdFromEvent(event);
  return !state.activeResponseId || !responseId || responseId === state.activeResponseId;
}

function ensureResponseOpen(event) {
  const responseId = responseIdFromEvent(event);
  if (
    state.responseOpen &&
    (!state.activeResponseId || !responseId || responseId === state.activeResponseId)
  ) {
    return;
  }

  setBusy(true);
  state.activeResponseId = responseId || null;
  state.responseEventIds.clear();
  state.responseOpen = true;
  state.responseParts = [];
  state.responseRawText = "";
  state.responseText = "";
  state.latestResultUrl = null;
  audioPlayer.stop();
  elements.replayButton.classList.add("hidden");
  beginAssistantTurn();
}

async function handleRealtimeEvent(event) {
  const type = event.type || "";

  if (type === "session.created") {
    setPresence("在线", "listen");
    return;
  }

  if (type === "system.prompt.changed") {
    elements.systemPromptStatus.textContent = "已热更新";
    showToast("人设已保存，下一轮对话立即生效。");
    return;
  }

  if (type === "conversation.item.input_audio_transcription.completed") {
    const transcript = event.transcript?.trim();
    if (transcript) {
      markVoiceTurnAnswered();
      expandDialogue();
      addHistory("user", transcript);
      setTurnLabel(`你刚刚说：“${transcript}”`, true);
      setPresence("正在回应", "think");
    } else {
      clearVoiceResultTimeout();
      setPresence("在线", "listen");
      // 免提模式下 VAD 会被咳嗽、键盘声这类动静触发，转写出来是空的。
      // 那不是"你说了我没听清"，是根本没人说话，弹提示只会显得神经质。
      if (!state.handsFree) showToast(notHeardMessage(), 4200);
    }
    return;
  }

  if (type === "response.created") {
    markVoiceTurnAnswered();
    ensureResponseOpen(event);
    setPresence("正在回应", "think");
    return;
  }

  if (type === "response.output_audio.delta" || type === "response.audio.delta") {
    markVoiceTurnAnswered();
    ensureResponseOpen(event);
    setPresence("正在说话", "voice");
    // 开着数字人时声音跟着成片走，这里不放，否则会和视频里的声音重叠；
    // 关掉数字人（纯对话）就得靠这条流直接发声。
    if (!state.lipsyncEnabled && event.delta) {
      audioPlayer.enqueue(event.delta).catch(() => {});
    }
    return;
  }

  if (
    type === "response.output_audio_transcript.done" ||
    type === "response.audio_transcript.done"
  ) {
    ensureResponseOpen(event);
    if (!isCurrentResponseEvent(event)) return;
    const eventId = String(event.event_id || "").trim();
    if (eventId && state.responseEventIds.has(eventId)) return;
    if (eventId) state.responseEventIds.add(eventId);
    const part = cleanAssistantText(event.transcript || "");
    if (!part) return;
    state.responseParts.push(part);
    state.responseRawText = state.responseParts.join(" ");
    state.responseText = cleanAssistantText(state.responseRawText);
    setAssistantText(state.responseText, false);
    return;
  }

  if (type === "response.done") {
    if (!isCurrentResponseEvent(event)) return;
    refreshAffinity();
    if (state.responseText) addHistory("assistant", state.responseText);
    state.responseOpen = false;
    if (!state.lipsyncEnabled) {
      // 纯对话：声音已经在播，这一轮到此为止
      setPresence("在线", "listen");
      setBusy(false);
      maybeResumeHandsFree();
      return;
    }
    setPresence("等待口型", "lipsync");
    settleDialogue();
    return;
  }

  if (type === "response.created") {
    // 新一轮开始：上一轮残留的片段作废，否则会串台
    clearSegmentQueue();
  }

  if (type === "lipsync.queued" || type === "heygem.queued") {
    setPresence("口型排队", "lipsync");
    elements.stageFrame.classList.add("is-rendering");
    elements.avatarVideo.classList.add("rendering");
    return;
  }

  if (
    type === "lipsync.started" ||
    type === "lipsync.progress" ||
    type === "heygem.started" ||
    type === "heygem.progress"
  ) {
    setPresence("正在生成口型", "lipsync");
    return;
  }

  if (type === "lipsync.done" || type === "heygem.done") {
    // 服务端现在是边说边切段渲染的：一轮回复会依次到来多个片段。
    // 每段直接排进队列，第一段到了就开口，后面的边播边等。
    // 只有最后一段（final）到齐才算这一轮结束，busy 状态才解除。
    const url = `${event.url}?v=${Date.now()}`;
    state.latestResultUrl = url;
    enqueueSegment(url);

    elements.stageFrame.classList.remove("is-rendering");
    elements.avatarVideo.classList.remove("rendering");
    elements.replayButton.classList.remove("hidden");

    // 服务端现在整段一次性渲染，正常只会来一段（final=true）。
    // 队列逻辑保留：万一以后再开分段，或者一轮里补发，顺序播放仍然正确。
    if (event.final !== false) {
      setPresence("回放就绪", "listen");
      setBusy(false);
      refreshStatus();
      maybeResumeHandsFree();
    }
    return;
  }

  if (type === "lipsync.skipped" || type === "heygem.skipped") {
    clearSegmentQueue();
    elements.stageFrame.classList.remove("is-rendering");
    elements.avatarVideo.classList.remove("rendering");
    setPresence("在线", "listen");
    setBusy(false);
    restoreIdleAvatar();
    showToast(event.reason || "本轮没有生成口型。");
    return;
  }

  if (type === "lipsync.error" || type === "heygem.error") {
    clearSegmentQueue();
    elements.stageFrame.classList.remove("is-rendering");
    elements.avatarVideo.classList.remove("rendering");
    setPresence("口型失败", "listen");
    setBusy(false);
    showToast(`口型生成失败：${event.message}`, 5200);
    return;
  }

  if (type === "gateway.error" || type === "error") {
    clearVoiceResultTimeout();
    const error = event.error?.message || event.message || "服务发生错误。";
    showToast(error, 5200);
  }
}

// ── 分段播放队列（我们加的）──────────────────────────────────────────
// 服务端把一轮回复切成若干段依次渲染，这里保证按到达顺序连续播完，
// 不会出现后一段把正在播的前一段顶掉。
const segmentQueue = [];
let segmentPlaying = false;

function enqueueSegment(url) {
  segmentQueue.push(url);
  if (!segmentPlaying) {
    void drainSegmentQueue();
  }
}

function clearSegmentQueue() {
  segmentQueue.length = 0;
}

async function drainSegmentQueue() {
  segmentPlaying = true;
  try {
    while (segmentQueue.length) {
      const url = segmentQueue.shift();
      state.latestResultUrl = url;
      await playRenderedVideo();
      await waitForSegmentEnd();
    }
  } finally {
    segmentPlaying = false;
    // 全部播完再回到待机画面，中途不要闪回，否则段与段之间会黑一下
    if (!segmentQueue.length) restoreIdleAvatar();
  }
}

function waitForSegmentEnd() {
  const video = elements.resultVideo;
  return new Promise((resolve) => {
    if (!video || video.ended || video.paused) {
      resolve();
      return;
    }
    const done = () => {
      video.removeEventListener("ended", done);
      video.removeEventListener("error", done);
      resolve();
    };
    video.addEventListener("ended", done);
    video.addEventListener("error", done);
  });
}

async function playRenderedVideo() {
  if (!state.latestResultUrl) return;
  // 成片是用角色素材生成的，焦点跟着它走
  applyStageFocus(currentAvatar()?.focus || state.idleVideo?.focus);
  const request = ++state.playbackRequest;
  const video = elements.resultVideo;
  audioPlayer.stop();
  video.pause();
  video.classList.remove("ready");
  if (video.getAttribute("src") !== state.latestResultUrl) {
    video.src = state.latestResultUrl;
    video.load();
  } else {
    video.currentTime = 0;
  }
  try {
    if (video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) {
      await new Promise((resolve, reject) => {
        const timeout = window.setTimeout(() => {
          cleanup();
          reject(new Error("视频预加载超时"));
        }, 15000);
        const cleanup = () => {
          window.clearTimeout(timeout);
          video.removeEventListener("canplay", ready);
          video.removeEventListener("error", failed);
        };
        const ready = () => {
          cleanup();
          resolve();
        };
        const failed = () => {
          cleanup();
          reject(video.error || new Error("视频加载失败"));
        };
        video.addEventListener("canplay", ready, { once: true });
        video.addEventListener("error", failed, { once: true });
        if (video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) ready();
      });
    }
    if (request !== state.playbackRequest) return;
    await video.play();
    if (typeof video.requestVideoFrameCallback === "function") {
      await new Promise((resolve) => video.requestVideoFrameCallback(resolve));
    } else {
      await new Promise((resolve) => requestAnimationFrame(resolve));
    }
    if (request !== state.playbackRequest) return;
    video.classList.add("ready");
  } catch {
    if (request !== state.playbackRequest) return;
    elements.replayButton.classList.remove("hidden");
    showToast("点击“播放口型成片”开始有声回放。");
  }
}

function textToPcm16(floatSamples, inputRate, outputRate = 16000) {
  if (inputRate === outputRate) {
    return Int16Array.from(floatSamples, (sample) =>
      Math.max(-32768, Math.min(32767, Math.round(sample * 32767))),
    );
  }
  const ratio = inputRate / outputRate;
  const outputLength = Math.round(floatSamples.length / ratio);
  const output = new Int16Array(outputLength);
  let outputIndex = 0;
  let inputIndex = 0;

  while (outputIndex < outputLength) {
    const nextInputIndex = Math.round((outputIndex + 1) * ratio);
    let sum = 0;
    let count = 0;
    for (let index = inputIndex; index < nextInputIndex && index < floatSamples.length; index += 1) {
      sum += floatSamples[index];
      count += 1;
    }
    const sample = count ? sum / count : 0;
    output[outputIndex] = Math.max(-32768, Math.min(32767, Math.round(sample * 32767)));
    outputIndex += 1;
    inputIndex = nextInputIndex;
  }
  return output;
}

function pcmToBase64(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

// ── 两个开关：数字人 / 免提 ───────────────────────────────
// 都存在 localStorage，刷新后保持上次的选择。

function applyLipsyncToggle() {
  const on = state.lipsyncEnabled;
  elements.lipsyncToggle.setAttribute("aria-pressed", String(on));
  elements.lipsyncToggle.title = on
    ? "数字人开启：会生成口型画面，一轮十几秒"
    : "纯对话：只出声不生成画面，快很多";
  // 关掉数字人时让待机画面继续循环，不要停在某一帧
  if (!on) {
    clearSegmentQueue();
    restoreIdleAvatar();
  }
}

function toggleLipsync() {
  state.lipsyncEnabled = !state.lipsyncEnabled;
  localStorage.setItem("lipsyncEnabled", state.lipsyncEnabled ? "1" : "0");
  applyLipsyncToggle();
  // 告诉服务端这一轮之后要不要渲染口型
  fetch("/api/lipsync-enabled", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: state.lipsyncEnabled }),
  }).catch(() => {});
  showToast(state.lipsyncEnabled ? "数字人已开启。" : "已切到纯对话，响应会快很多。");
}

// ── 音色 ────────────────────────────────────────────────
// OmniVoice 是克隆式 TTS：没有内置音色表，声音完全来自一段参考录音和
// 这段录音的逐字文本。所以"换个嗓音"对用户来说就是传一段音频。
// 切换不用重启：后端只是告诉旁挂服务换一个，下一句话就是新声音。
let pendingVoiceFile = null;

function renderVoices(payload) {
  const voices = payload?.voices || [];
  const active = payload?.active || "";
  state.voices = voices;
  state.activeVoice = active;

  elements.voiceStatus.textContent = active || (voices.length ? "未选择" : "未设置");

  if (!voices.length) {
    const empty = document.createElement("p");
    empty.className = "block-note";
    empty.textContent = "还没有音色。传一段录音，她就用那个嗓音说话。";
    elements.voiceList.replaceChildren(empty);
    return;
  }

  elements.voiceList.replaceChildren(
    ...voices.map((voice) => {
      const row = document.createElement("div");
      row.className = "voice-row" + (voice.name === active ? " active" : "");

      const dot = document.createElement("span");
      dot.className = "dot";

      const grow = document.createElement("div");
      grow.className = "grow";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = voice.name;
      const said = document.createElement("span");
      said.className = "said";
      said.textContent = voice.refText || "（没有逐字文本，克隆会不准）";
      grow.append(name, said);

      row.append(dot, grow);

      if (voice.name !== active) {
        const use = document.createElement("button");
        use.type = "button";
        use.textContent = "启用";
        use.addEventListener("click", () => activateVoice(voice.name));
        row.append(use);
      }

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove";
      remove.textContent = "删除";
      remove.addEventListener("click", () => removeVoice(voice.name));
      row.append(remove);

      return row;
    }),
  );
}

async function loadVoices() {
  try {
    const response = await fetch("/api/voices");
    if (response.ok) renderVoices(await response.json());
  } catch {
    /* 语音服务没起来时列表空着就行，别打扰 */
  }
}

async function activateVoice(name) {
  try {
    const response = await fetch("/api/voice", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || response.statusText);
    renderVoices(payload);
    showToast(`已换成「${name}」，下一句就是新声音。`);
  } catch (error) {
    showToast(`换音色失败：${error.message}`);
  }
}

async function removeVoice(name) {
  if (!window.confirm(`删掉音色「${name}」？参考录音会一起删除。`)) return;
  try {
    const response = await fetch(`/api/voice?name=${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || response.statusText);
    renderVoices(payload);
    showToast(`「${name}」已删除。`);
  } catch (error) {
    showToast(`删除失败：${error.message}`);
  }
}

function openVoiceEditor() {
  pendingVoiceFile = null;
  elements.voiceName.value = "";
  elements.voiceRefText.value = "";
  elements.voiceFile.value = "";
  elements.voiceFileLabel.textContent = "选择参考音频";
  elements.voiceProgress.style.width = "0%";
  elements.voiceEditor.hidden = false;
  elements.voiceName.focus();
}

function closeVoiceEditor() {
  elements.voiceEditor.hidden = true;
  pendingVoiceFile = null;
}

function submitVoice() {
  const name = elements.voiceName.value.trim();
  const refText = elements.voiceRefText.value.trim();
  if (!name) return showToast("先给这个音色起个名字。");
  if (!pendingVoiceFile) return showToast("还没有选参考音频。");
  if (!refText) return showToast("得填上这段录音里说的原话，一个字都不能差。");

  const query =
    `/api/voice?name=${encodeURIComponent(name)}` +
    `&filename=${encodeURIComponent(pendingVoiceFile.name)}` +
    `&ref_text=${encodeURIComponent(refText)}`;

  elements.saveVoice.disabled = true;
  elements.voiceProgress.style.width = "2%";

  const request = new XMLHttpRequest();
  request.open("POST", query);
  request.upload.addEventListener("progress", (event) => {
    if (event.lengthComputable) {
      elements.voiceProgress.style.width = `${Math.round((event.loaded / event.total) * 90)}%`;
    }
  });
  request.addEventListener("load", () => {
    elements.saveVoice.disabled = false;
    elements.voiceProgress.style.width = "100%";
    setTimeout(() => (elements.voiceProgress.style.width = "0%"), 600);
    if (request.status >= 200 && request.status < 300) {
      renderVoices(JSON.parse(request.responseText));
      closeVoiceEditor();
      showToast(`音色「${name}」已启用，下一句就是这个嗓音。`);
    } else {
      let detail = request.statusText;
      try {
        detail = JSON.parse(request.responseText).detail || detail;
      } catch {
        /* 不是 JSON 就用状态码文字 */
      }
      showToast(`上传失败：${detail}`, 5200);
    }
  });
  request.addEventListener("error", () => {
    elements.saveVoice.disabled = false;
    elements.voiceProgress.style.width = "0%";
    showToast("音频上传失败，检查一下本地服务。");
  });
  request.send(pendingVoiceFile);
}

elements.addVoice.addEventListener("click", openVoiceEditor);
elements.cancelVoice.addEventListener("click", closeVoiceEditor);
elements.saveVoice.addEventListener("click", submitVoice);
elements.voiceFile.addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  pendingVoiceFile = file;
  elements.voiceFileLabel.textContent = file.name;
  // 名字留空就拿文件名垫上，省一步输入
  if (!elements.voiceName.value.trim()) {
    elements.voiceName.value = file.name.replace(/\.[^.]+$/, "").slice(0, 40);
  }
});

// ── 好感度 ──────────────────────────────────────────────
// 进度条画当前阶段到下一阶段的进度。阶段门槛差距很大，如果把六个阶段均分
// 到一条轨道里，初识期即使已经 55/60 也只会显示很短，看起来像没有增长。
let bondLevelShown = null;

function renderAffinity(data) {
  if (!data || typeof data.level !== "number") return;
  state.affinity = data;

  const isMax = data.nextAt === null || data.nextAt === undefined;
  const progress = isMax ? 1 : Math.max(0, Math.min(1, data.progress || 0));

  elements.bond.hidden = false;
  elements.bondFill.style.width = `${(progress * 100).toFixed(2)}%`;
  elements.bondTrack.setAttribute("aria-valuenow", Math.round(progress * 100));
  elements.bondTrack.setAttribute(
    "aria-valuetext",
    isMax ? `${data.levelName}，已满` : `${data.levelName}，距${data.nextName}还有${Math.ceil(data.nextAt - data.points)}分`,
  );
  elements.bondLevel.textContent = data.levelName || "";

  elements.bondNotches.replaceChildren();

  const bits = [`${data.turns} 轮`, `${data.days} 天`];
  if (isMax) {
    bits.push("已是最亲近的一档");
  } else {
    bits.push(`距「${data.nextName}」还差 ${Math.max(0, Math.ceil(data.nextAt - data.points))}`);
    if (data.dailyCapReached) bits.push("今天的聊天成长已满，明天继续");
  }
  elements.bondMeta.innerHTML = bits.map((t) => `<em>${t}</em>`).join(" · ");

  // 人设是会话开始时定格的，升级要下次打开才生效——说清楚，免得以为没用
  if (typeof data.personaLevel === "number" && data.personaLevel < data.level) {
    const hint = document.createElement("span");
    hint.className = "pending";
    hint.textContent = ` · 她还停在「${data.personaName}」，下次打开生效`;
    elements.bondMeta.append(hint);
  }

  if (bondLevelShown !== null && data.level > bondLevelShown) {
    elements.bond.classList.remove("levelled");
    void elements.bond.offsetWidth;   // 重启动画
    elements.bond.classList.add("levelled");
    showToast(`好感度升到「${data.levelName}」了。`, 5000);
  }
  bondLevelShown = data.level;
}

async function refreshAffinity() {
  try {
    const response = await fetch("/api/affinity");
    if (response.ok) renderAffinity(await response.json());
  } catch {
    /* 好感度拿不到就先不动，不值得打扰用户 */
  }
}

function applyHandsFreeToggle() {
  const on = state.handsFree;
  elements.handsFreeToggle.setAttribute("aria-pressed", String(on));
  elements.voiceLabel.textContent = on
    ? (state.recording ? "正在聆听" : "点击开始聆听")
    : "按住说话";
  elements.micDock.classList.toggle("listening", Boolean(on && state.recording));
  elements.voiceButton.setAttribute("aria-label", on ? "点击停止聆听" : "按住说话");
}

function toggleHandsFree() {
  state.handsFree = !state.handsFree;
  localStorage.setItem("handsFree", state.handsFree ? "1" : "0");
  applyHandsFreeToggle();
  if (state.handsFree) {
    showToast("免提已开启：直接说就行，说完自动断句。");
    startHandsFree();
  } else {
    showToast("已关闭免提，恢复按住说话。");
    state.recordingRequested = false;
    stopRecording({ expectReply: false });
  }
}

function startHandsFree() {
  if (!state.handsFree || state.recording || state.recordingStarting) return;
  state.recordingRequested = true;
  // 高亮等录音真的起来了再点亮（见 startRecording 里 state.recording = true 之后），
  // 否则麦克风权限被拒时会一直亮着，看起来在听其实没有。
  startRecording();
}

/** 一轮结束后，免提模式要自动把麦克风重新打开继续听。 */
function maybeResumeHandsFree() {
  if (!state.handsFree) return;
  // 留一点间隔，避免把她自己的尾音当成新的一句
  setTimeout(startHandsFree, 400);
}

async function startRecording() {
  if (
    !state.recordingRequested ||
    state.recording ||
    state.recordingStarting ||
    state.stoppingRecording
  ) {
    return;
  }
  clearVoiceResultTimeout();
  state.voiceTurnAnswered = false;
  expandDialogue();
  state.recordingStarting = true;
  try {
    await audioPlayer.activate();
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (!state.recordingRequested) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    const context = new AudioContext({ latencyHint: "interactive" });
    await context.resume();
    if (!state.recordingRequested) {
      stream.getTracks().forEach((track) => track.stop());
      await context.close();
      return;
    }
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(1024, 1, 1);
    const sink = context.createGain();
    sink.gain.value = 0;

    processor.onaudioprocess = (audioEvent) => {
      if (!state.recording || state.socket?.readyState !== WebSocket.OPEN) return;
      const input = audioEvent.inputBuffer.getChannelData(0);
      const pcm = textToPcm16(input, context.sampleRate);
      sendSocket({
        type: "input_audio_buffer.append",
        audio: pcmToBase64(pcm),
      });
    };

    state.microphoneStream = stream;
    state.inputContext = context;
    state.inputProcessor = processor;
    state.inputSource = source;
    state.inputSink = sink;
    state.recording = true;
    state.stoppingRecording = false;
    if (state.handsFree) {
      elements.micDock.classList.add("listening");
      elements.voiceLabel.textContent = "正在聆听";
    }

    source.connect(processor);
    processor.connect(sink);
    sink.connect(context.destination);
    elements.voiceButton.classList.add("recording");
    elements.voiceOrbWrap.classList.add("recording");
    elements.voiceLabel.textContent = "松开结束录音";
    elements.voiceButton.setAttribute("aria-label", "正在录音，松开结束");
    elements.voiceButton.setAttribute("aria-pressed", "true");
    elements.voiceButton.title = "松开结束";
    setPresence("正在倾听", "listen");
    setTurnLabel("我在听，慢慢说。");
  } catch (error) {
    state.recordingRequested = false;
    elements.voiceOrbWrap.classList.remove("holding");
    elements.composer.classList.add("attention");
    showToast("没有可用麦克风，已保留键盘输入。");
    elements.messageInput.focus();
  } finally {
    state.recordingStarting = false;
  }
}

/**
 * @param {{expectReply?: boolean}} [options] expectReply=false 表示这是"主动停止聆听"
 *   （关免提、点暂停），不是说完一句等回复，所以不该装超时提示。
 */
async function stopRecording({ expectReply = true } = {}) {
  state.recordingRequested = false;
  elements.voiceOrbWrap.classList.remove("holding");
  if (!state.recording || state.stoppingRecording) return;
  state.stoppingRecording = true;
  elements.voiceLabel.textContent = "正在收尾……";

  // Keep the graph alive for a few processing quanta so the samples spoken
  // immediately before release are delivered before disconnecting.
  await new Promise((resolve) => setTimeout(resolve, 90));
  state.recording = false;
  elements.micDock.classList.remove("listening");
  elements.voiceButton.classList.remove("recording");
  elements.voiceOrbWrap.classList.remove("recording");
  elements.voiceLabel.textContent = state.handsFree ? "已暂停聆听" : "按住说话";
  elements.voiceButton.setAttribute("aria-label", state.handsFree ? "点击继续聆听" : "按住说话");
  elements.voiceButton.setAttribute("aria-pressed", "false");
  elements.voiceButton.title = "按住说话";

  state.inputProcessor?.disconnect();
  state.inputSource?.disconnect();
  state.inputSink?.disconnect();
  state.microphoneStream?.getTracks().forEach((track) => track.stop());
  await state.inputContext?.close();

  state.inputProcessor = null;
  state.inputSource = null;
  state.inputSink = null;
  state.microphoneStream = null;
  state.inputContext = null;
  state.stoppingRecording = false;

  setPresence("正在听懂你", "think");
  // Keep a deliberate 0.5s tail after the user's last sample so the recognizer
  // does not clip the final syllable. The remaining silence flushes server VAD.
  const totalSilenceMs = RECORDING_TAIL_PADDING_MS + SERVER_VAD_FLUSH_MS;
  const silence = pcmToBase64(new Int16Array(Math.round((16000 * totalSilenceMs) / 1000)));
  const appended = sendSocket({ type: "input_audio_buffer.append", audio: silence });
  const committed = appended && sendSocket({ type: "input_audio_buffer.commit" });
  if (committed && expectReply) {
    armVoiceResultTimeout();
  }
}

function submitText() {
  const text = elements.messageInput.value.trim();
  if (!text) return;
  clearVoiceResultTimeout();
  expandDialogue();
  audioPlayer.activate().catch(() => {});
  if (!sendSocket({ type: "user.text", text })) return;
  addHistory("user", text);
  setTurnLabel(`你说：“${text}”`, true);
  elements.messageInput.value = "";
  elements.messageInput.style.height = "auto";
  setPresence("正在回应", "think");
}

function uploadAvatar(file) {
  const imageMode = state.avatarDriver === "image";
  const validMime = imageMode
    ? ["image/jpeg", "image/png", "image/webp"].includes(file.type)
    : ["video/mp4", "video/quicktime", "video/webm"].includes(file.type);
  const validSuffix = imageMode
    ? /\.(jpe?g|png|webp)$/i.test(file.name)
    : /\.(mp4|mov|m4v|webm)$/i.test(file.name);
  if (!validMime && !validSuffix) {
    showToast(
      imageMode
        ? "图片驱动请选择 JPG、PNG 或 WebP 图片。"
        : "视频驱动请选择 MP4、MOV、M4V 或 WebM 视频。",
    );
    return;
  }
  if (imageMode && file.size > 20 * 1024 * 1024) {
    showToast("角色图片不能超过 20MB。");
    return;
  }

  const previewUrl = URL.createObjectURL(file);
  if (imageMode) {
    elements.avatarVideo.pause();
    elements.avatarVideo.classList.remove("ready");
    elements.avatarPlaceholder.classList.add("hidden");
  } else {
    elements.avatarVideo.src = previewUrl;
    freezeAvatarOnFirstFrame(previewUrl);
  }
  elements.avatarName.textContent = file.name;
  elements.uploadProgress.style.width = "2%";

  const request = new XMLHttpRequest();
  request.open(
    "POST",
    `/api/avatar?driver=${encodeURIComponent(state.avatarDriver)}&filename=${encodeURIComponent(file.name)}`,
  );
  request.upload.addEventListener("progress", (event) => {
    if (event.lengthComputable) {
      elements.uploadProgress.style.width = `${Math.round((event.loaded / event.total) * 100)}%`;
    }
  });
  request.addEventListener("load", () => {
    URL.revokeObjectURL(previewUrl);
    elements.uploadProgress.style.width = "100%";
    setTimeout(() => (elements.uploadProgress.style.width = "0%"), 600);
    if (request.status >= 200 && request.status < 300) {
      const payload = JSON.parse(request.responseText);
      state.avatarDriver = payload.driver;
      updateAvatarDriverMode();
      setAvatar(payload.avatar);
      closeSettings();
      showToast(
        payload.driver === "image"
          ? "图片驱动的角色图片已更新，下一轮会使用它生成口型。"
          : "视频驱动的角色视频已更新，下一轮会使用它生成口型。",
      );
    } else {
      let detail = request.statusText;
      try {
        detail = JSON.parse(request.responseText).detail || detail;
      } catch {
        // Keep the HTTP status text when the response is not JSON.
      }
      showToast(`上传失败：${detail}`);
    }
  });
  request.addEventListener("error", () => showToast("素材上传失败，请检查本地服务。"));
  request.send(file);
}

function uploadIdleVideo(file) {
  const validMime = ["video/mp4", "video/quicktime", "video/webm"].includes(file.type);
  const validSuffix = /\.(mp4|mov|m4v|webm)$/i.test(file.name);
  if (!validMime && !validSuffix) {
    showToast("待机视频请选择 MP4、MOV、M4V 或 WebM 文件。");
    return;
  }
  if (file.size > 2 * 1024 * 1024 * 1024) {
    showToast("待机视频不能超过 2GB。");
    return;
  }

  const previous = state.idleVideo;
  const previewUrl = URL.createObjectURL(file);
  elements.idleVideoName.textContent = file.name;
  elements.avatarVideo.src = previewUrl;
  playIdleVideo(previewUrl);
  elements.idleUploadProgress.style.width = "2%";

  const request = new XMLHttpRequest();
  request.open(
    "POST",
    `/api/idle-video?filename=${encodeURIComponent(file.name)}`,
  );
  request.upload.addEventListener("progress", (event) => {
    if (event.lengthComputable) {
      elements.idleUploadProgress.style.width =
        `${Math.round((event.loaded / event.total) * 100)}%`;
    }
  });
  request.addEventListener("load", () => {
    elements.idleUploadProgress.style.width = "100%";
    setTimeout(() => (elements.idleUploadProgress.style.width = "0%"), 600);
    if (request.status >= 200 && request.status < 300) {
      const payload = JSON.parse(request.responseText);
      state.idleVideo = payload.idleVideo;
      restoreIdleAvatar();
      closeSettings();
      showToast("待机视频已更新，空闲时会自动循环播放。");
    } else {
      state.idleVideo = previous;
      restoreIdleAvatar();
      let detail = request.statusText;
      try {
        detail = JSON.parse(request.responseText).detail || detail;
      } catch {
        // Keep the HTTP status text when the response is not JSON.
      }
      showToast(`待机视频上传失败：${detail}`);
    }
    URL.revokeObjectURL(previewUrl);
  });
  request.addEventListener("error", () => {
    state.idleVideo = previous;
    restoreIdleAvatar();
    elements.idleUploadProgress.style.width = "0%";
    URL.revokeObjectURL(previewUrl);
    showToast("待机视频上传失败，请检查本地服务。");
  });
  request.send(file);
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status");
    const services = await response.json();
    if (state.config) state.config.services = services;
    updateServices(services);
  } catch {
    showToast("无法读取本地服务状态。");
  }
}

async function initialize() {
  elements.voiceButton.disabled = true;
  // 好感度单独先拉一次：/api/config 要顺带体检各路服务，慢的时候要好几秒，
  // 不该让这条进度条陪着一起空在那儿。
  refreshAffinity();
  loadVoices();
  try {
    const response = await fetch("/api/config");
    const config = await response.json();
    state.config = config;
    state.avatarDriver = config.lipsync?.driver === "image" ? "image" : "video";
    updateAvatarDriverMode();
    if (typeof config.lipsync?.enabled === "boolean") {
      state.lipsyncEnabled = config.lipsync.enabled;
      localStorage.setItem("lipsyncEnabled", state.lipsyncEnabled ? "1" : "0");
    }
    state.avatar = config.avatar || null;
    state.idleVideo = config.idleVideo || null;
    loadLlmProvider(config.llmProvider || null);
    loadSystemPrompt(config.systemPrompt || "");
    renderAffinity(config.affinity);
    restoreIdleAvatar();
  } catch (error) {
    showToast(`界面配置加载失败：${error.message}`);
    restoreIdleAvatar();
  }
  // 套用上次记住的两个开关
  applyLipsyncToggle();
  applyHandsFreeToggle();

  connectSocket();
  setInterval(refreshStatus, 15000);
}

elements.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  submitText();
});

elements.messageInput.addEventListener("input", () => {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 110)}px`;
  if (elements.messageInput.value.trim()) {
    expandDialogue();
  } else if (!state.responseOpen) {
    settleDialogue(420);
  }
});

elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submitText();
  }
});

elements.voiceButton.addEventListener("pointerdown", (event) => {
  if (elements.voiceButton.disabled || (event.pointerType === "mouse" && event.button !== 0)) {
    return;
  }
  event.preventDefault();
  // 免提模式下按钮是"开/停聆听"的开关，不是按住说话
  if (state.handsFree) {
    if (state.recording) {
      state.recordingRequested = false;
      elements.micDock.classList.remove("listening");
      stopRecording({ expectReply: false });
    } else {
      startHandsFree();
    }
    return;
  }
  state.recordingRequested = true;
  state.voicePointerId = event.pointerId;
  elements.voiceOrbWrap.classList.add("holding");
  try {
    elements.voiceButton.setPointerCapture(event.pointerId);
  } catch {
    // Pointer capture is optional; pointerup on the button still stops recording.
  }
  startRecording();
});

function endVoicePointer(event) {
  if (state.handsFree) return;     // 免提模式不靠松手断句，交给服务端 VAD
  if (state.voicePointerId !== event.pointerId) return;
  event.preventDefault();
  state.voicePointerId = null;
  stopRecording();
}

elements.voiceButton.addEventListener("pointerup", endVoicePointer);
elements.voiceButton.addEventListener("pointercancel", endVoicePointer);
elements.voiceButton.addEventListener("lostpointercapture", endVoicePointer);
elements.voiceButton.addEventListener("contextmenu", (event) => event.preventDefault());
elements.voiceButton.addEventListener("keydown", (event) => {
  if (
    !event.repeat &&
    (event.key === " " || event.key === "Enter") &&
    !elements.voiceButton.disabled
  ) {
    event.preventDefault();
    state.recordingRequested = true;
    elements.voiceOrbWrap.classList.add("holding");
    startRecording();
  }
});
elements.voiceButton.addEventListener("keyup", (event) => {
  if (event.key === " " || event.key === "Enter") {
    event.preventDefault();
    stopRecording();
  }
});
window.addEventListener("blur", () => {
  if (state.handsFree) return;
  if (state.recordingRequested) {
    state.voicePointerId = null;
    stopRecording();
  }
});

elements.historyToggle.addEventListener("click", () => elements.history.classList.toggle("open"));
elements.lipsyncToggle.addEventListener("click", toggleLipsync);
elements.handsFreeToggle.addEventListener("click", toggleHandsFree);
elements.settingsButton.addEventListener("click", openSettings);
elements.settingsClose.addEventListener("click", closeSettings);
elements.settingsScrim.addEventListener("click", closeSettings);
elements.refreshStatus.addEventListener("click", refreshStatus);
elements.addProvider.addEventListener("click", () => openProviderEditor(null, false));
elements.addCustomProvider.addEventListener("click", () => openProviderEditor(null, true));
elements.cancelProvider.addEventListener("click", closeProviderEditor);
elements.providerKind.addEventListener("change", () => selectPreset(elements.providerKind.value));
elements.saveLlmProvider.addEventListener("click", saveLlmProvider);
elements.clearLlmApiKey.addEventListener("click", clearLlmApiKey);
elements.replayButton.addEventListener("click", playRenderedVideo);
elements.avatarInput.addEventListener("change", () => {
  const [file] = elements.avatarInput.files;
  if (file) uploadAvatar(file);
  elements.avatarInput.value = "";
});
elements.idleVideoInput.addEventListener("change", () => {
  const [file] = elements.idleVideoInput.files;
  if (file) uploadIdleVideo(file);
  elements.idleVideoInput.value = "";
});
elements.systemPromptInput.addEventListener("input", () => {
  updateSystemPromptCount();
  elements.systemPromptStatus.textContent =
    elements.systemPromptInput.value.trim() === state.systemPrompt ? "已同步" : "有未保存修改";
});
elements.saveSystemPrompt.addEventListener("click", saveSystemPrompt);

$$("[data-open-upload]").forEach((button) => button.addEventListener("click", openSettings));

elements.resultVideo.addEventListener("ended", () => {
  elements.resultVideo.classList.remove("ready");
  setPresence("在线", "listen");
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSettings();
});

window.addEventListener("resize", () => {
  $$(".assistant-reply", elements.assistantReplies).forEach(fitAssistantLine);
});

fitAssistantLine(elements.assistantText);

initialize();
