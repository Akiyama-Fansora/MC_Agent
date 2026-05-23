const state = {
  agents: [],
  activeAgent: "mcagent_rag",
  models: [],
  sessionsByAgent: {},
  activeSessionByAgent: {},
  lastSources: [],
  jobs: [],
  currentChat: null,
  lastDatabase: null,
  trackedJobs: {},
  expandedSections: {},
  llmProfiles: [],
  llmAssignments: {},
  editingProfileId: "",
};

const $ = (id) => document.getElementById(id);

function makeSessionName() {
  const now = new Date();
  return `会话 ${now.getHours().toString().padStart(2, "0")}:${now.getMinutes().toString().padStart(2, "0")}`;
}

function loadSessions() {
  const scopedRaw = localStorage.getItem("mcagent.sessionsByAgent");
  if (scopedRaw) {
    state.sessionsByAgent = JSON.parse(scopedRaw);
    state.activeSessionByAgent = JSON.parse(localStorage.getItem("mcagent.activeSessionByAgent") || "{}");
  } else {
    const legacyRaw = localStorage.getItem("mcagent.sessions");
    const legacySessions = legacyRaw ? JSON.parse(legacyRaw) : [];
    state.sessionsByAgent = { mcagent_rag: legacySessions.length ? legacySessions : [] };
    const legacyActive = localStorage.getItem("mcagent.activeSession") || "";
    if (legacyActive) state.activeSessionByAgent = { mcagent_rag: legacyActive };
  }
  ensureOneSession();
}

function saveSessions() {
  localStorage.setItem("mcagent.sessionsByAgent", JSON.stringify(state.sessionsByAgent));
  localStorage.setItem("mcagent.activeSessionByAgent", JSON.stringify(state.activeSessionByAgent));
}

function currentAgentId() {
  return state.activeAgent === "retriever_only" ? "mcagent_rag" : state.activeAgent;
}

function agentSessions(agentId = currentAgentId()) {
  if (!Array.isArray(state.sessionsByAgent[agentId])) state.sessionsByAgent[agentId] = [];
  return state.sessionsByAgent[agentId];
}

function activeSession() {
  const agentId = currentAgentId();
  const sessions = agentSessions(agentId);
  const activeId = state.activeSessionByAgent[agentId];
  return sessions.find((item) => item.id === activeId) || sessions[0];
}

function ensureOneSession(agentId = currentAgentId()) {
  const sessions = agentSessions(agentId);
  if (!sessions.length) {
    const session = { id: crypto.randomUUID(), name: makeSessionName(), agent: agentId, messages: [] };
    sessions.push(session);
    state.activeSessionByAgent[agentId] = session.id;
    return;
  }
  if (!sessions.some((item) => item.id === state.activeSessionByAgent[agentId])) {
    state.activeSessionByAgent[agentId] = sessions[0].id;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

async function streamChat(payload, controller, handlers = {}) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: controller.signal,
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const data = await response.json();
      message = data.error || message;
    } catch {
      // Keep the HTTP status text.
    }
    throw new Error(message);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalResponse = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const event = parseSseEvent(part);
      if (!event) continue;
      if (event.event === "trace") handlers.onTrace?.(event.data);
      if (event.event === "delta") handlers.onDelta?.(event.data);
      if (event.event === "response") {
        finalResponse = event.data;
        handlers.onResponse?.(event.data);
      }
      if (event.event === "error") throw new Error(event.data?.error || "stream error");
    }
  }
  if (buffer.trim()) {
    const event = parseSseEvent(buffer);
    if (event?.event === "response") finalResponse = event.data;
    if (event?.event === "delta") handlers.onDelta?.(event.data);
  }
  return finalResponse;
}

function parseSseEvent(chunk) {
  let event = "message";
  const dataLines = [];
  for (const line of chunk.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return { event, data: dataLines.join("\n") };
  }
}

function activityTextForTrace(step) {
  return progressTextForTrace(step);
}

function progressTextForTrace(step) {
  const detail = step?.detail || {};
  const stage = `${step?.stage || ""}:${step?.status || ""}`;
  const activeName = state.agents.find((item) => item.id === state.activeAgent)?.name || (state.activeAgent === "crawler_agent" ? "CrawlerAgent" : "MCagent");
  if (stage === "message:received") {
    const tuple = detail.tuple || [detail.from_agent || "User", detail.content || "", detail.to_agent || activeName];
    return `消息通道：${tuple[0]} -> ${tuple[2]}。${tuple[2]} 正在理解消息。`;
  }
  if (stage === "observe:received") return `${activeName} 正在读取你的目标和当前上下文。`;
  if (stage === "observe:contextualized") return `${activeName} 正在把这轮问题和最近会话上下文合并理解。`;
  if (stage === "decide:tool_selected") {
    if (detail.tool === "direct_answer") return `${activeName} 认为这轮可以直接回答。`;
    if (detail.tool === "status") return `${activeName} 正在读取运行状态。`;
    if (detail.tool === "temporary_extract") return `${activeName} 会临时读取公开网页并直接总结，不写入本地。`;
    if (detail.tool === "delegate_crawler") return `${activeName} 已决定启动采集任务，并生成明确交接。`;
    if (detail.tool === "planned_workflow") return `${activeName} 已列出多步工作流。`;
    return `${activeName} 已选择下一步工具：${detail.tool || "local_rag_search"}。`;
  }
  if (stage === "decide:side_effect_boundary_corrected") return `已按副作用边界调整：这轮改为临时读取，不启动后台保存任务。`;
  if (stage === "decide:inter_agent_workflow_corrected") return `已切换为跨 Agent 工作流：先查 MCagent/RAG 上下文，再交给 Crawler 补资料。`;
  if (stage === "decide:mcagent_context_selected") return `Crawler 正在读取 MCagent/RAG 本地上下文。`;
  if (stage === "retrieve:planning") return `${activeName} 正在规划本地资料检索问题。`;
  if (stage === "retrieve:planned") return `检索方向已确定，开始查找可用证据。`;
  if (stage === "retrieve:searching") return `正在检索本地资料库、全文索引和 raw HTML 线索。`;
  if (stage === "retrieve:done") {
    const count = Number(detail.results || 0);
    return count ? `找到 ${count} 条候选资料，正在筛选可用证据。` : `本地暂时没有找到候选资料，正在确认下一步。`;
  }
  if (stage === "decide:selecting_evidence") return `正在判断哪些证据真正能回答这轮问题。`;
  if (stage === "decide:evidence_selected") {
    if (detail.verdict && detail.verdict !== "ok") return `现有资料仍不足，正在确认是否需要补充采集。`;
    return `证据已经筛好，准备组织回答。`;
  }
  if (stage === "extract:next_step_confirmed") return `已确认临时网页读取步骤。`;
  if (stage === "extract:temporary_url_extracted") return `网页已读取完成，正在总结内容。`;
  if (stage === "extract:temporary_url_failed") return `临时读取失败，正在整理失败原因。`;
  if (stage === "delegate:handoff_brief") return `采集任务交接说明已生成。`;
  if (stage === "delegate:next_step_confirmed") return `已确认采集委托步骤。`;
  if (stage === "answer:generating" && String(detail.mode || "").startsWith("direct")) return `模型正在直接组织回复。`;
  if (stage === "answer:generating") return `模型正在基于当前证据组织回答。`;
  if (stage === "answer:thinking") {
    const count = Number(detail.reasoning_events || 0);
    const dots = ".".repeat((Math.floor(count / 8) % 3) + 1);
    return `模型还在整理答案${dots}`;
  }
  if (stage === "delegate:answer_marked_missing") return `回答中仍有缺口，已进入补充采集流程。`;
  if (stage === "done:response_ready") return `完成。`;
  return `${activeName} 正在处理这轮请求。`;
}

function updateComposerState() {
  const button = $("sendButton");
  const hasText = $("question").value.trim().length > 0;
  if (!button) return;
  if (state.currentChat && !hasText) {
    button.textContent = "暂停";
    button.classList.add("danger-button");
    button.title = "停止等待本次回复";
  } else {
    button.textContent = "发送";
    button.classList.remove("danger-button");
    button.title = hasText && state.currentChat ? "取消上一轮等待并发送新问题" : "发送问题";
  }
}

function setActivity(text, kind = "idle") {
  const activity = $("agentActivity");
  if (!activity) return;
  activity.textContent = text;
  activity.className = `agent-activity ${kind}`;
}

function idleActivityText() {
  const db = state.lastDatabase;
  if (!db) return "就绪：等待问题";
  return `就绪：本地库 ${db.documents} documents / ${db.chunks} chunks`;
}

function pauseCurrentChat(replacement = "已暂停本次回复。") {
  const current = state.currentChat;
  if (!current) return;
  current.controller.abort();
  const session = findSessionById(current.sessionId);
  if (session && session.messages[current.pendingIndex]) {
    session.messages[current.pendingIndex].text = replacement;
    session.messages[current.pendingIndex].agent = state.agents.find((item) => item.id === state.activeAgent)?.name || state.activeAgent;
  }
  state.currentChat = null;
  saveSessions();
  renderMessages();
  updateComposerState();
  setActivity("已暂停本次回复，可以继续输入新问题。", "paused");
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtTime(ts) {
  return new Date(ts || Date.now()).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function fmtDateTime(ts) {
  if (!ts) return "未知";
  return new Date(ts * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderAgents() {
  if (state.activeAgent === "retriever_only") state.activeAgent = "mcagent_rag";
  $("noLlm").disabled = state.activeAgent === "crawler_agent";
  $("agentList").innerHTML = state.agents.map((agent) => `
    <button type="button" class="agent-option ${agent.id === state.activeAgent ? "active" : ""}" data-agent="${agent.id}">
      <strong>${escapeHtml(agent.name)}</strong>
      <span>${escapeHtml(agent.description || "")}</span>
    </button>
  `).join("");
  document.querySelectorAll("[data-agent]").forEach((button) => {
    button.addEventListener("click", () => {
      if (state.currentChat) pauseCurrentChat("已切换 Agent，本次回复已停止。");
      state.activeAgent = button.dataset.agent;
      if (state.activeAgent === "crawler_agent") $("noLlm").checked = false;
      $("noLlm").disabled = state.activeAgent === "crawler_agent";
      ensureOneSession();
      renderAgents();
      renderSessions();
      renderMessages();
      renderSources([]);
      renderLlmSettings();
    });
  });
}

function activeProfileAgentId() {
  return state.activeAgent === "crawler_agent" ? "crawler_agent" : "mcagent_rag";
}

function profileLabel(profile) {
  const keyText = profile.key_configured ? "已保存 key" : (profile.provider === "ollama" ? "本地" : "无 key");
  return `${profile.name || profile.model || profile.id} · ${profile.model || "未填模型"} · ${keyText}`;
}

function profileById(profileId) {
  return state.llmProfiles.find((profile) => profile.id === profileId) || null;
}

function selectedProfileId(agentId = activeProfileAgentId()) {
  return state.llmAssignments[agentId] || state.llmProfiles[0]?.id || "";
}

function profileOptions(selectedId = "") {
  return state.llmProfiles.map((profile) => `
    <option value="${escapeHtml(profile.id)}" ${profile.id === selectedId ? "selected" : ""}>${escapeHtml(profileLabel(profile))}</option>
  `).join("");
}

function renderLlmSettings() {
  const modelSelect = $("modelSelect");
  if (!modelSelect) return;
  const activeId = selectedProfileId();
  modelSelect.innerHTML = profileOptions(activeId);
  modelSelect.value = activeId;
  if ($("mcagentProfileSelect")) $("mcagentProfileSelect").innerHTML = profileOptions(selectedProfileId("mcagent_rag"));
  if ($("crawlerProfileSelect")) $("crawlerProfileSelect").innerHTML = profileOptions(selectedProfileId("crawler_agent"));
  if ($("profileEditorSelect")) {
    if (!state.editingProfileId || !profileById(state.editingProfileId)) state.editingProfileId = activeId || state.llmProfiles[0]?.id || "";
    $("profileEditorSelect").innerHTML = profileOptions(state.editingProfileId);
    $("profileEditorSelect").value = state.editingProfileId;
    fillProfileForm(profileById(state.editingProfileId));
  }
}

function fillProfileForm(profile) {
  if (!$("profileName")) return;
  $("profileName").value = profile?.name || "";
  $("profileModel").value = profile?.model || "";
  $("profileBaseUrl").value = profile?.base_url || "";
  $("profileApiKey").value = "";
  $("profileApiKey").placeholder = profile?.key_configured ? "已保存 key；留空不修改" : "输入 API Key；本地 Ollama 可留空";
  $("profileProvider").value = profile?.provider || "openai-compatible";
  $("profileTimeout").value = profile?.timeout_seconds || 180;
}

function syncProfileFormToState() {
  const id = state.editingProfileId;
  if (!id) return null;
  let profile = profileById(id);
  if (!profile) {
    profile = { id };
    state.llmProfiles.push(profile);
  }
  profile.name = $("profileName").value.trim() || $("profileModel").value.trim() || id;
  profile.model = $("profileModel").value.trim();
  profile.base_url = $("profileBaseUrl").value.trim().replace(/\/+$/, "");
  profile.provider = $("profileProvider").value || "openai-compatible";
  profile.timeout_seconds = Number($("profileTimeout").value || 180);
  const apiKey = $("profileApiKey").value.trim();
  if (apiKey) profile.api_key = apiKey;
  return profile;
}

async function saveLlmSettings(statusText = "模型设置已保存。") {
  if ($("profileName")) syncProfileFormToState();
  const payload = {
    profiles: state.llmProfiles,
    assignments: state.llmAssignments,
  };
  const data = await api("/api/llm-profiles", { method: "POST", body: JSON.stringify(payload) });
  state.llmProfiles = data.profiles || [];
  state.llmAssignments = data.assignments || {};
  $("modelStatus").textContent = statusText;
  renderLlmSettings();
  await loadModels();
}

async function testProfile(profile) {
  if (!profile) return;
  const testing = { ...profile };
  const apiKey = $("profileApiKey")?.value.trim();
  if (apiKey && testing.id === state.editingProfileId) testing.api_key = apiKey;
  $("modelStatus").textContent = `正在测试 ${testing.name || testing.model}...`;
  const data = await api("/api/llm-profiles/test", { method: "POST", body: JSON.stringify({ id: testing.id, profile: testing }) });
  if (data.ok) {
    $("modelStatus").textContent = `连接成功：${data.label}，${data.elapsed_ms} ms，返回：${data.sample || "OK"}`;
  } else {
    $("modelStatus").textContent = `连接失败：${data.error || "未知错误"}`;
  }
}

function renderSessions() {
  const agentId = currentAgentId();
  const sessions = agentSessions(agentId);
  const activeId = state.activeSessionByAgent[agentId];
  $("sessionTabs").innerHTML = sessions.map((session) => `
    <div class="session-tab-wrap ${session.id === activeId ? "active" : ""}">
      <button type="button" class="session-tab ${session.id === activeId ? "active" : ""}" data-session="${session.id}" title="${escapeHtml(session.name)}">
        ${escapeHtml(session.name)}
      </button>
      <button type="button" class="session-delete" data-delete-session="${session.id}" title="删除会话" aria-label="删除会话 ${escapeHtml(session.name)}">×</button>
    </div>
  `).join("");
  document.querySelectorAll("[data-session]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeSessionByAgent[currentAgentId()] = button.dataset.session;
      saveSessions();
      renderSessions();
      renderMessages();
      renderSources([]);
    });
  });
  document.querySelectorAll("[data-delete-session]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteSession(button.dataset.deleteSession);
    });
  });
}

async function deleteSession(sessionId) {
  const agentId = currentAgentId();
  const sessions = agentSessions(agentId);
  const session = sessions.find((item) => item.id === sessionId);
  if (!session) return;
  const label = session.name || "当前会话";
  if (!window.confirm(`删除会话“${label}”？`)) return;
  if (state.currentChat?.sessionId === sessionId) {
    pauseCurrentChat("该会话已删除，本次回复已停止。");
  }
  state.sessionsByAgent[agentId] = sessions.filter((item) => item.id !== sessionId);
  ensureOneSession(agentId);
  if (state.activeSessionByAgent[agentId] === sessionId || !agentSessions(agentId).some((item) => item.id === state.activeSessionByAgent[agentId])) {
    state.activeSessionByAgent[agentId] = agentSessions(agentId)[0].id;
  }
  for (const [jobId, link] of Object.entries(state.trackedJobs)) {
    if (link.sessionId === sessionId) delete state.trackedJobs[jobId];
  }
  saveSessions();
  try {
    await api("/api/session/delete", { method: "POST", body: JSON.stringify({ session_id: sessionId }) });
  } catch (error) {
    console.warn("Failed to delete server session memory", error);
  }
  renderSessions();
  renderMessages();
  renderSources([]);
}

function isNearBottom(element, threshold = 120) {
  if (!element) return true;
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

function renderMessages(forceScroll = false) {
  ensureOneSession();
  const session = activeSession();
  const box = $("messages");
  const shouldFollow = forceScroll || isNearBottom(box);
  box.innerHTML = session.messages.map((message, index) => `
    <article class="message ${message.role}">
      <div class="message-header">
        <span>${message.role === "user" ? "\u4f60" : escapeHtml(message.agent || "MCagent")}</span>
        <span>${fmtTime(message.time)}</span>
      </div>
      ${renderAssistantContent(message, session.id, index)}
      ${renderTrace(message.trace, sectionKey(session.id, index, "trace"))}
    </article>
  `).join("");
  bindExpandableSections();
  if (shouldFollow) box.scrollTop = box.scrollHeight;
}

function sectionKey(sessionId, index, panel) {
  return `${sessionId}:${index}:${panel}`;
}

function detailsAttrs(key, defaultOpen = false) {
  const known = Object.prototype.hasOwnProperty.call(state.expandedSections, key);
  const open = known ? state.expandedSections[key] : defaultOpen;
  return `data-section-key="${escapeHtml(key)}"${open ? " open" : ""}`;
}

function bindExpandableSections() {
  document.querySelectorAll("details[data-section-key]").forEach((detail) => {
    detail.addEventListener("toggle", () => {
      if (!detail.isConnected) return;
      state.expandedSections[detail.dataset.sectionKey] = detail.open;
    });
  });
}

function renderAssistantContent(message, sessionId, index) {
  if (message.role === "user") {
    return `<div class="message-body">${escapeHtml(message.text)}</div>`;
  }
  const parts = splitAssistantText(message.text || "");
  return `
    <div class="message-body">${escapeHtml(parts.answer)}</div>
    ${renderJobReadable(message.jobReadable, sectionKey(sessionId, index, "job"))}
    ${renderEvidencePanel(message.sources, parts.evidenceText, sectionKey(sessionId, index, "evidence"))}
    ${renderCollaboration(message.collaboration, sectionKey(sessionId, index, "collaboration"))}
  `;
}

function observationLabel(status) {
  const labels = {
    ok: "拿到可用资料",
    empty: "空结果",
    off_topic: "跑偏",
    duplicate_reused: "复用已有资料",
    auth_required: "需要授权",
    quota_limited: "额度不足",
    captcha_required: "需要验证",
    login_required: "需要登录",
    network_error: "网络错误",
    timeout: "超时",
    parse_error: "解析失败",
    execution_error: "执行失败",
    uncertain: "相关性待确认",
    blocked: "被工具层拦截",
    stopped: "已停止",
  };
  return labels[status] || status || "未知";
}

function renderObservationStatus(readable) {
  const counts = readable?.observation_statuses || {};
  const entries = Object.entries(counts).filter(([, count]) => Number(count || 0) > 0);
  if (!entries.length) return "";
  return `
    <div class="job-readable-statuses">
      ${entries.map(([status, count]) => `<span class="observation-pill ${escapeHtml(status)}">${escapeHtml(observationLabel(status))} ${escapeHtml(count)}</span>`).join("")}
    </div>
  `;
}

function renderLatestObservation(readable) {
  const observation = readable?.latest_observation || {};
  if (!observation.status) return "";
  const retryText = observation.retryable ? "可换策略重试" : "不建议原路重试";
  const next = observation.suggested_next ? `；建议：${observation.suggested_next}` : "";
  return `<div class="source-meta">最近工具结果：${escapeHtml(observationLabel(observation.status))}，${escapeHtml(observation.summary || retryText)}${escapeHtml(next)}</div>`;
}

function renderJobTimeline(readable) {
  const timeline = readable?.timeline || [];
  if (!timeline.length) return "";
  return `
    <div class="job-timeline">
      ${timeline.slice(-10).map((item) => `
        <div class="job-timeline-row ${escapeHtml(item.type || "")}">
          <span class="job-timeline-label">${escapeHtml(item.label || item.type || "")}</span>
          <div>
            <strong>${escapeHtml(item.title || "")}</strong>
            ${item.status ? `<span class="source-meta"> · ${escapeHtml(observationLabel(item.status))}</span>` : ""}
            ${item.text ? `<div class="source-meta">${escapeHtml(item.text)}</div>` : ""}
          </div>
        </div>
      `).join("")}
    </div>
  `;
}

function renderJobReadable(readable, key = "") {
  if (!readable) return "";
  const total = Number(readable.total_tasks || 0);
  const current = Number(readable.current_index || 0);
  const percent = readable.progress_percent != null
    ? Math.max(0, Math.min(100, Number(readable.progress_percent || 0)))
    : total ? Math.max(0, Math.min(100, (current / total) * 100)) : 0;
  const goals = readable.coverage_goals || [];
  const reflection = readable.agent_reflection || {};
  const statusText = readable.status === "running" ? "运行中"
    : readable.status === "queued" ? "排队中"
    : readable.status === "succeeded" ? "已完成"
    : readable.status === "failed" ? "失败"
    : readable.status === "stopped" ? "已停止"
    : readable.status || "";
  const displayStatus = readable.status_label || statusText;
  const headline = readable.headline || readable.target || readable.title || "Crawler 采集任务";
  const progressText = readable.progress_text || `第 ${current || 0} / ${total} 个采集动作`;
  return `
    <details class="message-section job-readable" ${detailsAttrs(key || "job", true)}>
      <summary>
        <span>${escapeHtml(headline)}</span>
        <span>${escapeHtml(displayStatus)}</span>
      </summary>
      <div class="job-readable-head">
        <strong>${escapeHtml(headline)}</strong>
        <span>${escapeHtml(displayStatus)}</span>
      </div>
      ${total ? progressBar("当前任务", percent, progressText) : `<div class="source-meta">${escapeHtml(progressText)}</div>`}
      <div class="job-readable-grid">
        ${readable.delivery_target ? statRow("交付对象", readable.delivery_target) : ""}
        ${readable.current_source ? statRow("当前来源", readable.current_source) : ""}
        ${readable.current_query ? statRow("当前搜索", readable.current_query) : ""}
        ${statRow("成功入库候选", String(readable.success_count || 0))}
        ${statRow("空结果", String(readable.empty_count || 0))}
        ${statRow("跑偏", String(readable.off_topic_count || 0))}
      </div>
      ${renderObservationStatus(readable)}
      ${renderLatestObservation(readable)}
      ${readable.health_text ? `<div class="source-meta">状态判断：${escapeHtml(readable.health_text)}</div>` : ""}
      ${readable.current_reason ? `<div class="source-meta">当前动作理由：${escapeHtml(readable.current_reason)}</div>` : ""}
      ${reflection.reason ? `<div class="source-meta">CrawlerAgent 判断：${escapeHtml(reflection.reason)}</div>` : ""}
      ${goals.length ? `<div class="job-readable-goals">${goals.map((goal) => `<span>${escapeHtml(goal)}</span>`).join("")}</div>` : ""}
      <div class="source-meta">${escapeHtml(readable.next_action || readable.summary || "")}</div>
      ${renderJobTimeline(readable)}
    </details>
  `;
}

function splitAssistantText(text) {
  const markers = [
    "\n\n\u6765\u6e90\uff1a",
    "\n\u6765\u6e90\uff1a",
    "\n\n\u8865\u5e93\u52a8\u4f5c\uff1a",
    "\n\u8865\u5e93\u52a8\u4f5c\uff1a",
    "\n\n\u6a21\u578b\uff1a",
    "\n\u6a21\u578b\uff1a",
    "\n\n\u8bc1\u636e\u6838\u5bf9\u8865\u5145\uff1a",
    "\n\u8bc1\u636e\u6838\u5bf9\u8865\u5145\uff1a",
    "\n\n\u6765\u6e90",
    "\n\u6765\u6e90",
    "\n\n\u8865\u5e93\u52a8\u4f5c",
    "\n\u8865\u5e93\u52a8\u4f5c",
    "\n\n\u6a21\u578b",
    "\n\u6a21\u578b",
  ];
  let cut = -1;
  for (const marker of markers) {
    const index = text.indexOf(marker);
    if (index >= 0 && (cut < 0 || index < cut)) cut = index;
  }
  if (cut < 0) return { answer: text, evidenceText: "" };
  return {
    answer: text.slice(0, cut).trimEnd(),
    evidenceText: text.slice(cut).trim(),
  };
}

function renderEvidencePanel(sources, evidenceText = "", key = "") {
  const items = sources || [];
  if (!items.length && !evidenceText) return "";
  return `
    <details class="message-section evidence-panel" ${detailsAttrs(key || "evidence")}>
      <summary>
        <span>\u8bc1\u636e\u4e0e\u6765\u6e90</span>
        <span>${items.length || "\u6587\u672c"} \u6761</span>
      </summary>
      ${evidenceText ? `<pre class="section-text">${escapeHtml(evidenceText)}</pre>` : ""}
      ${items.length ? `
        <div class="evidence-list">
          ${items.map((source, index) => `
            <div class="evidence-item">
              <strong>[S${index + 1}] ${escapeHtml(source.title || "")}</strong>
              <div class="source-meta">score ${Number(source.score || 0).toFixed(4)} · ${escapeHtml(source.url || source.source_path || "")}</div>
              ${source.metadata?.raw_html_path ? `<div class="source-meta">raw HTML: ${escapeHtml(source.metadata.raw_html_path)}</div>` : ""}
              <div class="source-text">${escapeHtml(source.text || "")}</div>
            </div>
          `).join("")}
        </div>
      ` : ""}
    </details>
  `;
}

function rememberJobMessage(job, sessionId, messageIndex) {
  if (!job || !job.id) return;
  state.trackedJobs[job.id] = {
    sessionId,
    messageIndex,
    lastStatus: job.status || "",
  };
}

function applyJobUpdatesToMessages(jobs) {
  let changed = false;
  for (const job of jobs || []) {
    if (!job || !job.id || !state.trackedJobs[job.id]) continue;
    const link = state.trackedJobs[job.id];
    const session = findSessionById(link.sessionId);
    const message = session && session.messages[link.messageIndex];
    if (!message) continue;
    const result = job.result || {};
    if (Array.isArray(result.collaboration) && result.collaboration.length) {
      message.collaboration = result.collaboration;
      changed = true;
    }
    if (job.readable) {
      message.jobReadable = job.readable;
      if (!message.isStreamingAnswer && !result.mcagent_recheck) {
        message.text = crawlerProgressText(job);
      }
      changed = true;
    }
    if (result.mcagent_recheck && job.status !== link.lastStatus) {
      const recheck = result.mcagent_recheck;
      if (recheck.status === "evidence_ok" && recheck.answer) {
        message.text = `Crawler 补库完成，MCagent 已回查并得到可回答证据。\n\n${recheck.answer}`;
        message.sources = recheck.sources || message.sources || [];
      } else if (recheck.answer) {
        message.text = recheck.answer;
      }
      changed = true;
    }
    link.lastStatus = job.status || link.lastStatus;
  }
  if (changed) {
    saveSessions();
    renderMessages();
  }
}

function crawlerProgressText(job) {
  const readable = job.readable || {};
  const status = readable.status || job.status || "";
  const statusText = status === "running" ? "正在采集"
    : status === "queued" ? "正在排队"
    : status === "succeeded" ? "采集完成"
    : status === "failed" ? "这轮采集没拿到可用新增资料"
    : status === "stopped" ? "采集已停止"
    : "采集任务更新";
  const total = Number(readable.total_tasks || 0);
  const current = Number(readable.current_index || 0);
  const progress = total ? `第 ${current || 0}/${total} 个动作` : "等待规划动作";
  const lines = [
    `CrawlerAgent ${statusText}：${readable.target || job.title || "采集任务"}`,
    `进度：${progress}`,
  ];
  if (readable.current_source || readable.current_query) {
    lines.push(`当前：${[readable.current_source, readable.current_query].filter(Boolean).join(" · ")}`);
  }
  if (readable.latest_observation?.status) {
    lines.push(`最近结果：${observationLabel(readable.latest_observation.status)}${readable.latest_observation.summary ? `，${readable.latest_observation.summary}` : ""}`);
  }
  if (readable.agent_reflection?.reason) {
    lines.push(`判断：${readable.agent_reflection.reason}`);
  }
  if (readable.next_action) {
    lines.push(`下一步：${readable.next_action}`);
  }
  return lines.join("\n");
}

function renderCollaboration(dialog, key = "") {
  const items = dialog || [];
  if (!items.length) return "";
  return `
    <details class="message-section collab-panel" ${detailsAttrs(key || "collaboration")}>
      <summary>
        <span>MCagent \u2194 Crawler</span>
        <span>${items.length} \u6761</span>
      </summary>
      <div class="collab-log">
        ${items.map((item) => `
          <div class="collab-row ${escapeHtml((item.speaker || "").toLowerCase())}">
            <div class="collab-speaker">${escapeHtml(item.speaker || "Agent")}</div>
            <div class="collab-bubble">
              <div class="collab-state">${escapeHtml(item.state || "")}</div>
              <div class="collab-text">${escapeHtml(item.text || "")}</div>
            </div>
          </div>
        `).join("")}
      </div>
    </details>
  `;
}

function renderTrace(trace, key = "") {
  const steps = (trace || []).slice(-8);
  if (!steps.length) return "";
  return `
    <details class="message-section trace-panel" ${detailsAttrs(key || "trace")}>
      <summary>
        <span>过程详情</span>
        <span>${steps.length} 步</span>
      </summary>
      <div class="trace-list">
        ${steps.map((step) => {
          const detail = typeof step.detail === "string" ? step.detail : JSON.stringify(step.detail || {}, null, 2);
          return `
            <div class="trace-row">
              <strong>${escapeHtml(step.stage)} · ${escapeHtml(step.status)}</strong>
              <pre>${escapeHtml(detail)}</pre>
            </div>
          `;
        }).join("")}
      </div>
    </details>
  `;
}

function renderSources(sources) {
  state.lastSources = sources || [];
  $("sourceCount").textContent = String(state.lastSources.length);
  if (!state.lastSources.length) {
    $("sources").innerHTML = `<div class="source-item"><div class="source-meta">暂无来源。当前资料库可能为空，或本次回答来自爬虫agent/监控。</div></div>`;
    return;
  }
  $("sources").innerHTML = state.lastSources.map((source) => `
    <div class="source-item">
      <strong>[S${source.rank}] ${escapeHtml(source.title)}</strong>
      <div class="source-meta">score ${Number(source.score).toFixed(4)} · ${escapeHtml(source.url || source.source_path)}</div>
      <div class="source-text">${escapeHtml(source.text)}</div>
    </div>
  `).join("");
}

function statRow(label, value) {
  return `<div class="stat-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function findSessionById(sessionId) {
  for (const sessions of Object.values(state.sessionsByAgent)) {
    const found = (sessions || []).find((item) => item.id === sessionId);
    if (found) return found;
  }
  return null;
}

function formatMb(value) {
  const number = Number(value || 0);
  return `${number.toFixed(number >= 100 ? 1 : 2)} MB`;
}

function shortCommand(command) {
  const text = Array.isArray(command) ? command.join(" ") : String(command || "");
  if (!text) return "";
  const script = text.match(/scripts\\([^\\\s]+\.py)|scripts\/([^\/\s]+\.py)/);
  const query = text.match(/--query\s+(.+?)(?:\s+--|$)/);
  const head = script ? (script[1] || script[2]) : text.split(/\s+/).slice(0, 4).join(" ");
  return query ? `${head} · ${query[1].replaceAll('"', "")}` : head;
}

function progressBar(label, percent, meta = "") {
  const safePercent = Math.max(0, Math.min(100, Number(percent || 0)));
  return `
    <div class="progress-card">
      <div class="progress-head">
        <span>${escapeHtml(label)}</span>
        <strong>${safePercent.toFixed(1)}%</strong>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${safePercent}%"></div></div>
      ${meta ? `<div class="source-meta">${escapeHtml(meta)}</div>` : ""}
    </div>
  `;
}

function renderCrawlerProgress(progress) {
  if (!progress || (!progress.active && !progress.status)) {
    return `<div class="source-meta">批量采集脚本：当前没有运行。</div>`;
  }
  const status = progress.active ? (progress.status || "running") : (progress.status || "idle");
  const cycle = Number(progress.cycle || 0);
  const cyclesTotal = Number(progress.cycles_total || 0);
  const commandDone = Number(progress.commands_completed || 0);
  const commandTotal = Number(progress.commands_total || 0);
  const added = progress.added_bytes ? `${formatMb(Number(progress.added_bytes) / 1024 / 1024)} this cycle` : "";
  const topic = progress.current_topic ? `当前主题：${progress.current_topic}` : "";
  const command = shortCommand(progress.current_command);
  const processes = (progress.processes || []).map((item) => `PID ${item.pid}`).join(" / ");
  return `
    <div class="crawler-progress">
      <div class="progress-status">
        <strong>批量采集脚本：${escapeHtml(status)}</strong>
        <span>${progress.active ? escapeHtml(processes || "running") : "not running"}</span>
      </div>
      ${progress.target_percent ? progressBar("容量目标", progress.target_percent, `${formatMb(progress.current_mb)} / ${formatMb(progress.target_mb)}`) : ""}
      ${progress.cycle_percent ? progressBar("任务进度", progress.cycle_percent, cyclesTotal ? `第 ${cycle || 0} / ${cyclesTotal} 轮，当前轮 ${commandDone} / ${commandTotal || "?"} 个任务` : "") : ""}
      ${topic ? `<div class="source-meta">${escapeHtml(topic)}</div>` : ""}
      ${command ? `<div class="source-meta">当前命令：${escapeHtml(command)}</div>` : ""}
      ${added ? `<div class="source-meta">最近新增：${escapeHtml(added)}</div>` : ""}
      ${progress.low_yield_streak ? `<div class="source-meta">低收益连续轮数：${escapeHtml(progress.low_yield_streak)}</div>` : ""}
      ${progress.stopped_reason ? `<div class="source-meta">停止原因：${escapeHtml(progress.stopped_reason)}</div>` : ""}
      ${progress.updated_at ? `<div class="source-meta">更新时间：${escapeHtml(progress.updated_at)}</div>` : ""}
    </div>
  `;
}

function renderActiveCrawlerOverview(jobs) {
  const items = (jobs || []).filter((job) => job.kind === "crawler");
  const active = items.find((job) => job.status === "queued" || job.status === "running");
  const latest = active || items[0];
  if (!latest) {
    return `<div class="crawler-progress"><div class="progress-status"><strong>当前 Crawler 任务：无</strong><span>可从会话或右侧按钮启动</span></div></div>`;
  }
  const readable = latest.readable || {};
  const current = readable.current_query ? `当前：${readable.current_query}` : (latest.status === "running" ? "正在等待 CrawlerAgent 规划可执行查询" : "");
  const target = readable.headline || readable.target || latest.title || latest.kind;
  const reflection = readable.agent_reflection?.reason ? `<div class="source-meta">CrawlerAgent 判断：${escapeHtml(readable.agent_reflection.reason).slice(0, 220)}</div>` : "";
  const latestObservation = readable.latest_observation?.status
    ? `<div class="source-meta">最近工具结果：${escapeHtml(observationLabel(readable.latest_observation.status))}${readable.latest_observation.summary ? ` · ${escapeHtml(readable.latest_observation.summary)}` : ""}</div>`
    : "";
  const counts = [
    `成功 ${readable.success_count || 0}`,
    `空结果 ${readable.empty_count || 0}`,
    `跑偏 ${readable.off_topic_count || 0}`,
  ].join(" · ");
  return `
    <div class="crawler-progress active-job-overview">
      <div class="progress-status">
        <strong>当前 Crawler 任务：${escapeHtml(latest.status || "unknown")}</strong>
        <span>${escapeHtml(fmtDateTime(latest.started_at || latest.created_at))}</span>
      </div>
      <div class="source-meta">目标：${escapeHtml(target)}</div>
      ${current ? `<div class="source-meta">${escapeHtml(current)}</div>` : ""}
      <div class="source-meta">${escapeHtml(counts)}</div>
      ${readable.progress_text ? `<div class="source-meta">${escapeHtml(readable.progress_text)}</div>` : ""}
      ${readable.health_text ? `<div class="source-meta">${escapeHtml(readable.health_text)}</div>` : ""}
      ${renderObservationStatus(readable)}
      ${latestObservation}
      ${reflection}
      ${latest.stop_requested && latest.status === "running" ? `<div class="source-meta stop-note">已请求提前结束，等待当前动作退出。</div>` : ""}
    </div>
  `;
}

function renderJobs(jobs) {
  state.jobs = jobs || [];
  applyJobUpdatesToMessages(state.jobs);
  const active = state.jobs.some((job) => job.status === "queued" || job.status === "running");
  const activeCrawler = state.jobs.find((job) => job.kind === "crawler" && (job.status === "queued" || job.status === "running"));
  const activeIngest = state.jobs.find((job) => job.kind === "ingest" && (job.status === "queued" || job.status === "running"));
  if (!state.currentChat) {
    if (activeCrawler) {
      if (activeCrawler.stop_requested) {
        setActivity("已请求提前结束：Crawler 正在完成当前轮，完成后就会停止。", "crawler");
      } else {
        setActivity("Crawler 正在联网检索，整理 Markdown，完成后会自动清洗入库。", "crawler");
      }
    } else if (activeIngest) {
      setActivity("正在把采集资料清洗并写入本地向量库。", "ingest");
    } else {
      setActivity(idleActivityText(), "idle");
    }
  }
  $("runIngest").disabled = state.jobs.some((job) => job.kind === "ingest" && (job.status === "queued" || job.status === "running"));
  $("runCrawler").disabled = state.jobs.some((job) => job.kind === "crawler" && (job.status === "queued" || job.status === "running"));
  $("runIngest").textContent = $("runIngest").disabled ? "导入运行中" : "后台导入";
  $("runCrawler").textContent = $("runCrawler").disabled ? "补库中" : `启动补库（${$("crawlerRounds")?.value || 1}轮）`;
  if (!state.jobs.length) {
    $("jobList").innerHTML = `<div class="source-meta">暂无后台任务。</div>`;
    return active;
  }
  $("jobList").innerHTML = state.jobs.slice(0, 6).map((job) => {
    const isActiveCrawler = job.kind === "crawler" && (job.status === "queued" || job.status === "running");
    const canStop = isActiveCrawler && !job.stop_requested;
    const readable = job.readable || {};
    const readableLine = readable.headline || readable.target
      ? `<div class="source-meta">目标：${escapeHtml(readable.headline || readable.target)}${readable.current_query ? ` · 当前：${escapeHtml(readable.current_query)}` : ""}</div>`
      : "";
    const reflectionLine = readable.agent_reflection?.reason
      ? `<div class="source-meta">Agent 判断：${escapeHtml(readable.agent_reflection.reason).slice(0, 180)}</div>`
      : "";
    const observationLine = readable.latest_observation?.status
      ? `<div class="source-meta">最近结果：${escapeHtml(observationLabel(readable.latest_observation.status))}${readable.latest_observation.summary ? ` · ${escapeHtml(readable.latest_observation.summary).slice(0, 180)}` : ""}</div>`
      : "";
    const roundInfo = job.result && job.result.rounds_total
      ? `<div class="source-meta">轮次：${escapeHtml(job.result.rounds_completed || (job.result.rounds || []).length || 0)} / ${escapeHtml(job.result.rounds_total)}</div>`
      : "";
    return `
      <div class="job-item">
        <strong>${escapeHtml(job.title || job.kind)}</strong>
        <span class="job-status ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span>
        <div class="source-meta">${fmtDateTime(job.started_at || job.created_at)}${job.ended_at ? ` - ${fmtDateTime(job.ended_at)}` : ""}</div>
        ${readableLine}
        ${observationLine}
        ${reflectionLine}
        ${readable.progress_text ? `<div class="source-meta">${escapeHtml(readable.progress_text)}</div>` : ""}
        ${readable.health_text ? `<div class="source-meta">${escapeHtml(readable.health_text)}</div>` : ""}
        ${roundInfo}
        ${job.stop_requested && isActiveCrawler ? `<div class="source-meta stop-note">已收到提前结束请求，当前轮完成后停止。</div>` : ""}
        ${job.summary ? `<div class="source-meta">${escapeHtml(job.summary).slice(0, 220)}</div>` : ""}
        ${canStop ? `<div class="job-actions"><button class="mini-button danger-outline" type="button" data-stop-job="${escapeHtml(job.id)}">提前结束</button></div>` : ""}
      </div>
    `;
  }).join("");
  document.querySelectorAll("[data-stop-job]").forEach((button) => {
    button.addEventListener("click", () => stopJob(button.dataset.stopJob));
  });
  return active;
}

function renderAgenttestRuns(runs) {
  const items = runs || [];
  $("runCount").textContent = String(items.length);
  if (!$("agenttestRuns")) return;
  if (!items.length) {
    $("agenttestRuns").innerHTML = `<div class="source-meta">未发现 AgentTest run。</div>`;
    return;
  }
  $("agenttestRuns").innerHTML = items.slice(0, 8).map((run) => `
    <div class="run-item">
      <strong>${escapeHtml(run.display_name || run.name)}</strong>
      <div class="source-meta">状态：${escapeHtml(run.status_label || (run.has_final ? "已结束" : "未完成"))} · ${escapeHtml(run.import_label || "未知")}</div>
      <div class="source-meta">${escapeHtml(run.summary || "")}</div>
      <div class="source-meta">最近：${fmtDateTime(run.latest_time || run.mtime)} · 原始ID：${escapeHtml(run.name)}</div>
    </div>
  `).join("");
}

function renderStatus(status) {
  const db = status.database;
  state.lastDatabase = db;
  $("indexStats").innerHTML = [
    statRow("documents", db.documents),
    statRow("chunks", db.chunks),
    statRow("index", db.index_exists ? `${Math.round(db.index_size / 1024)} KB` : "missing"),
    statRow("exports", status.sources.files),
  ].join("");

  const ledger = status.ledger || {};
  $("ledgerStats").innerHTML = [
    statRow("unique", ledger.unique || 0),
    statRow("entries", ledger.entries || 0),
    statRow("new", (ledger.by_status && ledger.by_status.new) || 0),
    statRow("skipped", (ledger.by_status && ledger.by_status.skipped_unchanged) || 0),
    ledger.latest && ledger.latest.length
      ? `<div class="source-meta">最近：${escapeHtml(ledger.latest[0].title || ledger.latest[0].key || "")}</div>`
      : `<div class="source-meta">账本还没有记录。</div>`,
  ].join("");

  const progress = status.crawler_progress || {};
  const jobs = status.jobs || [];
  const activeCrawler = jobs.find((job) => job.kind === "crawler" && (job.status === "queued" || job.status === "running"));
  $("crawlerState").textContent = activeCrawler
    ? `当前任务：${activeCrawler.status}`
    : `当前无任务 · ${status.sources.files} files`;
  const latest = status.sources.latest_files || [];
  const runs = status.agenttest_runs || [];
  $("crawlerInfo").innerHTML = [
    renderActiveCrawlerOverview(jobs),
    renderCrawlerProgress(progress),
    statRow("source", status.sources.source_dir),
    statRow("manifest", status.sources.manifests),
    statRow("reports", status.sources.reports),
    statRow("recent runs", runs.length),
    status.knowledge_map && status.knowledge_map.exists ? statRow("map", `${status.knowledge_map.documents || 0} docs`) : statRow("map", "not built"),
    status.toolsets ? statRow("toolsets", status.toolsets.length) : "",
    status.memory ? statRow("memory", `${status.memory.events || 0} events`) : "",
    latest.length ? `<div class="source-meta">最近文件：${escapeHtml(latest[0].path)}</div>` : `<div class="source-meta">没有可用采集文件。</div>`,
  ].join("");
  renderAgenttestRuns(runs);
  renderJobs(status.jobs || []);
}

async function loadAgents() {
  const data = await api("/api/agents");
  state.agents = data.agents || [];
  renderAgents();
}

async function loadModels() {
  const data = await api("/api/llm-profiles");
  state.llmProfiles = data.profiles || [];
  state.llmAssignments = data.assignments || {};
  state.models = state.llmProfiles.map((profile) => ({ id: profile.id, label: profileLabel(profile) }));
  renderLlmSettings();
}

async function loadStatus() {
  const data = await api("/api/status");
  renderStatus(data);
}

async function loadJobs() {
  const data = await api("/api/jobs");
  renderJobs(data.jobs || []);
}

async function stopJob(jobId) {
  if (!jobId) return;
  try {
    const data = await api("/api/jobs/stop", { method: "POST", body: JSON.stringify({ id: jobId }) });
    renderJobs([data.job, ...state.jobs.filter((job) => job.id !== data.job.id)]);
    addMessage("assistant", "已请求提前结束：Crawler 会把当前轮跑完，然后停止。", "系统");
    setActivity("已请求提前结束：等待 Crawler 完成当前轮。", "crawler");
  } catch (error) {
    addMessage("assistant", `提前结束请求失败：${error.message}`, "系统");
  } finally {
    loadStatus();
  }
}

function addMessage(role, text, agent = "") {
  const session = activeSession();
  session.messages.push({ role, text, agent, time: Date.now() });
  session.messages = session.messages.slice(-120);
  if (role === "user" && session.messages.filter((item) => item.role === "user").length === 1) {
    session.name = text.slice(0, 18) || session.name;
  }
  saveSessions();
  renderSessions();
  renderMessages(true);
}

function requestHistoryForAgent(session, pendingIndex, limit = 24) {
  const messages = (session.messages || [])
    .slice(0, Math.max(0, pendingIndex - 1))
    .filter((message) => message.role === "user" || message.role === "assistant")
    .filter((message) => String(message.text || "").trim() && String(message.text || "").trim() !== "处理中...");
  return messages.slice(-limit).map((message) => ({
    role: message.role,
    text: message.text,
    time: message.time,
    agent: message.agent || "",
    sources: message.sources || [],
  }));
}

function sourcePreview(sources) {
  const items = (sources || []).slice(0, 4);
  if (!items.length) return "本地资料库暂时没有命中，正在判断是否需要交给爬虫agent补库...";
  const lines = items.map((source) => `- [S${source.rank}] ${source.title} (${Number(source.score).toFixed(3)})`);
  return `已命中本地资料 ${sources.length} 条，正在调用模型生成回答...\n\n优先来源：\n${lines.join("\n")}`;
}

function delegationPreviewDialog(question) {
  return [
    {
      speaker: "MCagent",
      state: "检索",
      text: `本地库暂时没有找到可用资料：${question}`,
    },
    {
      speaker: "MCagent",
      state: "准备派单",
      text: "正在判断数据源与查询词，准备把补库任务交给 Crawler。",
    },
    {
      speaker: "Crawler",
      state: "等待接单",
      text: "等待 MCagent 给出 source、query、保存格式和成功标准。",
    },
  ];
}

async function sendQuestion(event) {
  event.preventDefault();
  const question = $("question").value.trim();
  if (state.currentChat && !question) {
    pauseCurrentChat();
    return;
  }
  if (state.currentChat && question) {
    pauseCurrentChat("已取消上一轮等待，改为发送新问题。");
  }
  if (!question) return;
  $("question").value = "";
  updateComposerState();
  addMessage("user", question);
  addMessage("assistant", "处理中...", state.activeAgent);
  const session = activeSession();
  const pendingIndex = session.messages.length - 1;
  const controller = new AbortController();
  const currentChat = { controller, sessionId: session.id, pendingIndex };
  state.currentChat = currentChat;
  updateComposerState();
  renderMessages();

  const payload = {
    session_id: session.id,
    agent: $("noLlm").checked && state.activeAgent === "mcagent_rag" ? "retriever_only" : state.activeAgent,
    question,
    agent_message: {
      from_agent: "User",
      to_agent: state.activeAgent === "crawler_agent" ? "CrawlerAgent" : "MCagent",
      content: question,
      intent: "user_chat",
      conversation_id: session.id,
      requires_reply: true,
      metadata: { ui_agent: state.activeAgent },
    },
    history: requestHistoryForAgent(session, pendingIndex),
    model_profile_id: $("modelSelect").value,
    model: `profile:${$("modelSelect").value}`,
    temperature: Number($("temperature").value || 0.2),
    max_tokens: "auto",
    no_llm: $("noLlm").checked,
    show_context: $("showContext").checked,
  };

  try {
    if (payload.agent === "mcagent_rag") {
      const initialText = "MCagent 正在读取你的问题。";
      setActivity(initialText, "thinking");
      session.messages[pendingIndex].text = initialText;
      saveSessions();
      renderMessages();
    } else if (payload.agent === "retriever_only") {
      setActivity("仅检索：正在读取本地向量库...", "working");
    } else if (payload.agent === "crawler_agent") {
      const initialText = "CrawlerAgent 正在读取你的任务。";
      setActivity(initialText, "crawler");
      session.messages[pendingIndex].text = initialText;
      saveSessions();
      renderMessages();
    }
    let streamedAnswer = "";
    const data = await streamChat(payload, controller, {
      onTrace(step) {
        if (state.currentChat !== currentChat) return;
        const message = session.messages[pendingIndex];
        message.trace = [...(message.trace || []), step].slice(-20);
        const progressText = progressTextForTrace(step);
        if (!message.hasFinalAnswer && !message.isStreamingAnswer) message.text = progressText;
        setActivity(progressText, step.stage === "answer" ? "thinking" : "working");
        saveSessions();
        renderMessages();
      },
      onDelta(partial) {
        if (state.currentChat !== currentChat || !partial) return;
        const chunk = typeof partial === "string" ? partial : partial.text || "";
        if (!chunk) return;
        streamedAnswer += chunk;
        const message = session.messages[pendingIndex];
        message.text = streamedAnswer;
        message.isStreamingAnswer = true;
        setActivity("模型正在组织回答，内容会实时出现在这条消息里。", "thinking");
        saveSessions();
        renderMessages();
      },
      onResponse(partial) {
        if (state.currentChat !== currentChat || !partial) return;
        session.messages[pendingIndex].text = partial.answer || session.messages[pendingIndex].text || "";
        session.messages[pendingIndex].hasFinalAnswer = Boolean(partial.answer);
        session.messages[pendingIndex].isStreamingAnswer = false;
        session.messages[pendingIndex].trace = partial.trace || session.messages[pendingIndex].trace || [];
        session.messages[pendingIndex].collaboration = partial.collaboration || [];
        session.messages[pendingIndex].sources = partial.sources || [];
        session.messages[pendingIndex].jobReadable = partial.job?.readable || session.messages[pendingIndex].jobReadable;
        saveSessions();
        renderMessages();
      },
    });
    if (state.currentChat !== currentChat) return;
    session.messages[pendingIndex].text = data.answer || "";
    session.messages[pendingIndex].hasFinalAnswer = true;
    session.messages[pendingIndex].isStreamingAnswer = false;
    session.messages[pendingIndex].agent = state.agents.find((item) => item.id === state.activeAgent)?.name || state.activeAgent;
    session.messages[pendingIndex].trace = data.trace || [];
    session.messages[pendingIndex].collaboration = data.collaboration || [];
    session.messages[pendingIndex].sources = data.sources || [];
    session.messages[pendingIndex].jobReadable = data.job?.readable || session.messages[pendingIndex].jobReadable;
    renderSources(data.sources || []);
    if (data.job && (data.job.status === "queued" || data.job.status === "running")) {
      rememberJobMessage(data.job, session.id, pendingIndex);
      setActivity("Crawler 已接单，正在联网检索、生成 Markdown 并准备入库。", "crawler");
    } else {
      setActivity("完成：已根据当前本地资料返回结果。", "done");
      setTimeout(() => {
        if (!state.currentChat) setActivity(idleActivityText(), "idle");
      }, 2500);
    }
  } catch (error) {
    if (state.currentChat !== currentChat) return;
    session.messages[pendingIndex].text = error.name === "AbortError" ? "已暂停本次回复。" : `请求失败：${error.message}`;
    if (error.name !== "AbortError") renderSources([]);
    setActivity(error.name === "AbortError" ? "已暂停本次回复。": "请求失败：请查看消息或后台任务。", error.name === "AbortError" ? "paused" : "error");
  } finally {
    if (state.currentChat === currentChat) {
      state.currentChat = null;
      updateComposerState();
    }
  }
  saveSessions();
  renderMessages();
  loadStatus();
}

async function runIngest() {
  $("runIngest").disabled = true;
  $("runIngest").textContent = "启动中";
  try {
    const data = await api("/api/jobs/start-ingest", { method: "POST", body: "{}" });
    renderJobs([data.job, ...state.jobs.filter((job) => job.id !== data.job.id)]);
    addMessage("assistant", `后台导入已启动：${data.job.id}`, "系统");
  } catch (error) {
    addMessage("assistant", `启动导入失败：${error.message}`, "系统");
  } finally {
    loadStatus();
  }
}

async function runCrawler() {
  $("runCrawler").disabled = true;
  $("runCrawler").textContent = "启动中";
  try {
    const payload = {
      source: $("crawlerSource").value,
      query: $("crawlerQuery") ? $("crawlerQuery").value.trim() : "",
      question: $("crawlerQuery") ? $("crawlerQuery").value.trim() : "",
      rounds: Number($("crawlerRounds").value || 1),
      interval_seconds: Number($("crawlerInterval").value || 0),
    };
    const data = await api("/api/jobs/start-crawler", { method: "POST", body: JSON.stringify(payload) });
    renderJobs([data.job, ...state.jobs.filter((job) => job.id !== data.job.id)]);
    addMessage("assistant", `爬虫agent补库任务已启动：${data.job.id}（${payload.rounds}轮）`, "系统");
  } catch (error) {
    addMessage("assistant", `启动采集失败：${error.message}`, "系统");
  } finally {
    loadStatus();
  }
}

function initEvents() {
  $("chatForm").addEventListener("submit", sendQuestion);
  $("question").addEventListener("input", updateComposerState);
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("chatForm").requestSubmit();
    }
  });
  $("newSession").addEventListener("click", () => {
    const agentId = currentAgentId();
    const session = { id: crypto.randomUUID(), name: makeSessionName(), agent: agentId, messages: [] };
    agentSessions(agentId).unshift(session);
    state.activeSessionByAgent[agentId] = session.id;
    saveSessions();
    renderSessions();
    renderMessages();
    renderSources([]);
  });
  $("reloadStatus").addEventListener("click", loadStatus);
  $("refreshModels")?.addEventListener("click", loadModels);
  $("testActiveModel").addEventListener("click", () => testProfile(profileById($("modelSelect").value)));
  $("modelSelect").addEventListener("change", async () => {
    state.llmAssignments[activeProfileAgentId()] = $("modelSelect").value;
    try {
      await saveLlmSettings("当前 Agent 的模型已切换。");
    } catch (error) {
      $("modelStatus").textContent = `保存失败：${error.message}`;
    }
  });
  $("runIngest").addEventListener("click", runIngest);
  $("runCrawler").addEventListener("click", runCrawler);
  $("crawlerRounds").addEventListener("input", () => renderJobs(state.jobs));
}

async function boot() {
  loadSessions();
  initEvents();
  renderSessions();
  renderMessages();
  renderSources([]);
  await Promise.all([loadAgents(), loadModels(), loadStatus()]);
  updateComposerState();
  setInterval(loadStatus, 5000);
}

boot().catch((error) => {
  document.body.innerHTML = `<pre style="padding:20px">启动失败：${escapeHtml(error.message)}</pre>`;
});
