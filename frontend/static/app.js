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
  actionTimeline: [],
  actionTimelineSessionId: "",
};

const $ = (id) => document.getElementById(id);

function messageActionId(sessionId, messageIndex) {
  return `${sessionId}:${messageIndex}`;
}

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
    if (event?.event === "response") {
      finalResponse = event.data;
      handlers.onResponse?.(event.data);
    }
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
  return agentActionTextForTrace(step);
}

function agentReplyContent(response) {
  return response?.agent_message?.content || response?.answer || "";
}

function shouldTrackJobResponse(response) {
  if (!response?.job?.id) return false;
  if (response?.trace?.some((step) => step?.stage === "answer" && step?.status === "recent_crawler_audit")) return false;
  if (response?.delegation) return true;
  return response.job.status === "queued" || response.job.status === "running";
}

function shouldRenderJobReadable(response) {
  if (!response?.job?.readable) return false;
  if (response?.trace?.some((step) => step?.stage === "answer" && step?.status === "recent_crawler_audit")) return false;
  return true;
}

function progressTextForTrace(step) {
  return agentActionTextForTrace(step);
}

function detailText(detail, keys = []) {
  if (!detail || typeof detail !== "object") return "";
  for (const key of keys) {
    const value = detail[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function actionActorForTrace(step, detail = {}) {
  const explicit = detail.actor || detail.agent || detail.from_agent || detail.to_agent || "";
  if (explicit) return String(explicit);
  if (step?.stage === "delegate" || detail.tool === "delegate_crawler") return "CrawlerAgent";
  return state.agents.find((item) => item.id === state.activeAgent)?.name || (state.activeAgent === "crawler_agent" ? "CrawlerAgent" : "MCagent");
}

function agentActionTextForTrace(step) {
  const detail = step?.detail || {};
  const stage = `${step?.stage || ""}:${step?.status || ""}`;
  const agentReport = detailText(detail, ["agent_report", "narration", "progress_report", "self_report", "agent_observation"]);
  if (agentReport) return agentReport;
  const activeName = actionActorForTrace(step, detail);
  if (stage === "message:agent_message_preparing") {
    return `我准备通过 AgentMessage 发给 ${detail.to_agent || "Agent"}：${String(detail.content || "").slice(0, 160)}`;
  }
  if (stage === "message:agent_message_relayed") {
    return `我已发出 AgentMessage：${detail.from_agent || activeName} -> ${detail.to_agent || "Agent"}`;
  }
  if (stage === "message:agent_message_waiting_for_reply") {
    return `我在等 ${detail.to_agent || "对方 Agent"} 回答。`;
  }
  if (stage === "message:agent_message_reply_received") {
    return `我收到 ${detail.from_agent || "对方 Agent"} 的回复，准备转达给你。`;
  }
  if (stage === "message:agent_message_summary_ready") {
    return "我已整理好这轮跨 Agent 沟通结果。";
  }
  if (stage === "message:received") {
    const tuple = detail.tuple || [detail.from_agent || "User", detail.content || "", detail.to_agent || activeName];
    return `我收到 AgentMessage：${tuple[0]} -> ${tuple[2]}：${String(tuple[1] || "").slice(0, 160)}`;
  }
  if (stage === "observe:received") return `我收到你的问题：${String(detail.content || detail.question || "").slice(0, 160) || "开始处理这轮请求。"}`;
  if (stage === "observe:contextualized") return `我把当前问题和会话上下文合并后继续处理。${detail.rewritten ? `改写焦点：${detail.rewritten}` : ""}`;
  if (stage === "decide:tool_selected") {
    const reason = detailText(detail.decision || detail, ["reason", "goal", "collection_target", "rag_focus"]);
    return `我选择下一步：${detail.tool || "local_rag_search"}${reason ? `。理由/目标：${reason}` : ""}`;
  }
  if (stage === "plan:created") return `我列出了行动计划：${(detail.steps || []).map((item) => item.goal || item.tool).filter(Boolean).join("；")}`;
  if (stage === "plan:rag_focus") return `我把检索焦点定为：${detail.question || detail.focus || ""}`;
  if (stage === "decide:side_effect_boundary_corrected") return `我根据副作用边界修正了下一步：${detail.reason || "这一步不应产生持久化副作用。"}`;
  if (stage === "decide:inter_agent_workflow_corrected") return `我把这轮改成跨 Agent 工作流：${detail.reason || detail.goal || ""}`;
  if (stage === "decide:mcagent_context_selected") return `我先读取 MCagent/RAG 的本地上下文：${detail.rag_focus || detail.goal || ""}`;
  if (stage === "retrieve:planning") return `我在规划本地资料检索。${detail.question ? `焦点：${detail.question}` : ""}`;
  if (stage === "retrieve:planned") return `我已确定检索方向。${detail.query ? `查询：${detail.query}` : ""}`;
  if (stage === "retrieve:searching") return `我开始检索本地资料库。${detail.query ? `查询：${detail.query}` : ""}`;
  if (stage === "retrieve:inventory_scanning") return `我开始扫描本地入库文档。${detail.db_path ? `数据库：${detail.db_path}` : ""}`;
  if (stage === "retrieve:inventory_done") return `我完成了本地资料盘点。${detail.documents ? `扫描 ${detail.documents} 篇文档。` : ""}`;
  if (stage === "retrieve:done") {
    const count = Number(detail.results || 0);
    return count ? `我找到 ${count} 条候选资料，继续筛选证据。` : `我这轮没有找到候选资料，继续判断下一步。`;
  }
  if (stage === "decide:next_step_confirmed") return `我确认下一步：${detail.tool || detail.suggested_tool || detail.goal || detail.reason || step.status}`;
  if (stage === "decide:selecting_evidence") return `我开始判断哪些证据能回答这轮问题。`;
  if (stage === "decide:evidence_step_started") {
    return `我检查证据步骤：${detail.step || "evidence_step"}`;
  }
  if (stage === "decide:evidence_step_done") {
    const count = Number(detail.results || 0);
    return `我完成一个证据步骤，得到 ${count} 条候选。`;
  }
  if (stage === "decide:evidence_step_failed") return `证据步骤失败：${detail.error || "未知错误"}`;
  if (stage === "decide:evidence_selected") {
    if (detail.verdict && detail.verdict !== "ok") return `我判断现有证据还不足：${detail.verdict}`;
    return `我筛好了证据，准备组织回答。`;
  }
  if (stage === "extract:next_step_confirmed") return `我确认临时读取步骤：${detail.url || detail.goal || detail.reason || ""}`;
  if (stage === "extract:temporary_url_extracted") return `我读完网页，准备总结：${detail.url || ""}`;
  if (stage === "extract:temporary_url_failed") return `我读取网页失败：${detail.error || detail.url || ""}`;
  if (stage === "delegate:handoff_brief") return `我整理了给 CrawlerAgent 的原始说明：${detail.brief || detail.collection_target || detail.task || ""}`;
  if (stage === "delegate:next_step_confirmed") return `我确认 CrawlerAgent 的下一步：${detail.collection_target || detail.goal || detail.reason || ""}`;
  if (stage === "delegate:planned_workflow") return `CrawlerAgent 已创建后续采集工作：${detail.task || detail.job_id || ""}`;
  if (stage === "answer:generating" && String(detail.mode || "").startsWith("direct")) return `我开始组织直接回复。`;
  if (stage === "answer:generating") return `我开始基于当前证据组织回答。`;
  if (stage === "answer:thinking") {
    const count = Number(detail.reasoning_events || 0);
    const dots = ".".repeat((Math.floor(count / 8) % 3) + 1);
    return `我还在整理答案${dots}`;
  }
  if (stage === "answer:local_fact_answer") return `我用本地事实证据生成回答。`;
  if (stage === "delegate:answer_marked_missing") return `我发现回答仍有缺口：${detail.reason || ""}`;
  if (stage === "done:response_ready") return `我完成了这轮处理。${detail.sources != null ? `来源数：${detail.sources}` : ""}`;
  if (stage === "done:insufficient_evidence") return `我判断证据不足：${detail.reason || ""}`;
  if (stage === "done:router_error") return `我无法执行这一步：${detail.error || ""}`;
  return `我记录到事件 ${stage || "update"}。${detailText(detail, ["reason", "goal", "summary", "error"])}`;
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

function renderAgentActions() {
  const container = $("agentActionTimeline");
  if (!container) return;
  const items = (state.actionTimeline || []).slice(-80);
  if ($("actionCount")) $("actionCount").textContent = String(items.length);
  if (!items.length) {
    container.innerHTML = `<div class="empty-state source-meta">还没有动作记录。发出问题后，我会把 MCagent 和 CrawlerAgent 的动作按时间累积在这里。</div>`;
    return;
  }
  container.innerHTML = items.map((item, index) => `
    <div class="agent-action-row ${escapeHtml(item.kind || "")}">
      <div class="agent-action-index">${index + 1}</div>
      <div class="agent-action-body">
        <div class="agent-action-head">
          <strong>${escapeHtml(item.actor || "Agent")}</strong>
          <span>${fmtTime(item.time)}</span>
        </div>
        <div class="agent-action-text">${escapeHtml(item.text || "")}</div>
        ${item.meta ? `<div class="agent-action-meta">${escapeHtml(item.meta)}</div>` : ""}
      </div>
    </div>
  `).join("");
  container.scrollTop = container.scrollHeight;
}

function recordAgentAction({ actor = "", text = "", kind = "", meta = "", messageKey = "" } = {}) {
  const value = String(text || "").trim();
  if (!value) return;
  const current = state.actionTimeline || [];
  const last = current[current.length - 1];
  if (last && last.text === value && last.actor === actor && last.messageKey === messageKey) return;
  state.actionTimeline = [...current, {
    actor: actor || "Agent",
    text: value,
    kind,
    meta,
    messageKey,
    time: Date.now(),
  }].slice(-120);
  renderAgentActions();
}

function resetAgentActionsForSession(force = false) {
  const session = activeSession();
  const sessionId = session?.id || "";
  if (!force && state.actionTimelineSessionId === sessionId) {
    renderAgentActions();
    return;
  }
  state.actionTimeline = [];
  state.actionTimelineSessionId = sessionId;
  for (const [index, message] of (session?.messages || []).entries()) {
    if (message.role !== "assistant") continue;
    const key = messageActionId(session.id, index);
    for (const text of message.processLog || []) {
      recordAgentAction({
        actor: message.agent || "MCagent",
        text,
        kind: "history",
        messageKey: key,
      });
    }
  }
  renderAgentActions();
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
      <span>${escapeHtml(agentHelpText(agent))}</span>
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

function agentHelpText(agent) {
  if (agent.id === "mcagent_rag") return "问 MC 资料、整合包、模组和玩法";
  if (agent.id === "crawler_agent") return "去公开网页采集、保存或补库";
  return agent.description || "";
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
    <article class="message ${message.role}" data-role="${escapeHtml(message.role)}" data-message-index="${index}" data-final-answer="${message.hasFinalAnswer ? "true" : "false"}">
      <div class="message-header">
        <span>${message.role === "user" ? "\u4f60" : escapeHtml(message.agent || "MCagent")}</span>
        <span>${fmtTime(message.time)}</span>
      </div>
      ${renderAssistantContent(message, session.id, index)}
    </article>
  `).join("");
  bindExpandableSections();
  if (shouldFollow) box.scrollTop = box.scrollHeight;
  resetAgentActionsForSession();
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
    ${renderCollaboration(mergedCollaboration(message), sectionKey(sessionId, index, "collaboration"))}
  `;
}

function mergedCollaboration(message) {
  const items = Array.isArray(message.collaboration) ? [...message.collaboration] : [];
  for (const item of agentMessagesFromTrace(message.trace || [])) {
    const exists = items.some((existing) => existing.text === item.text && existing.speaker === item.speaker && existing.state === item.state);
    if (!exists) items.push(item);
  }
  return items;
}

function agentMessagesFromTrace(trace) {
  const rows = [];
  for (const step of trace || []) {
    const detail = step?.detail || {};
    if (step?.stage !== "message" || !detail || typeof detail !== "object") continue;
    const content = String(detail.content || "");
    if (!content.trim()) continue;
    rows.push({
      speaker: detail.from_agent || "Agent",
      state: `${detail.from_agent || "Agent"} -> ${detail.to_agent || "Agent"}`,
      text: content,
    });
  }
  return rows;
}

function shouldOpenProcessLog(message) {
  const steps = Array.isArray(message.processLog) ? message.processLog.filter(Boolean) : [];
  if (!steps.length) return false;
  return true;
}

function renderProcessLog(items, key = "", defaultOpen = false) {
  const steps = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!steps.length) return "";
  return `
    <details class="message-section trace-panel process-panel" ${detailsAttrs(key || "process", defaultOpen)}>
      <summary>
        <span>\u6267\u884c\u8fc7\u7a0b</span>
        <span>${steps.length} \u6b65</span>
      </summary>
      <div class="trace-list">
        ${steps.map((text, index) => `
          <div class="trace-row">
            <strong>${index + 1}</strong>
            <span>${escapeHtml(text)}</span>
          </div>
        `).join("")}
      </div>
    </details>
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
    warning: "注意",
    retry: "重试",
  };
  return labels[status] || status || "未知";
}

function renderUsefulOutputs(readable) {
  const items = readable?.useful_outputs || [];
  if (!items.length) return `<div class="compact-empty">本轮还没有形成新的可用资料。</div>`;
  return `
    <div class="compact-list">
      ${items.map((item) => `
        <div class="compact-row">
          <strong>${escapeHtml(item.source || "资料")}</strong>
          <span>${escapeHtml(item.status_label || observationLabel(item.status))}${item.records && item.records !== "0" ? ` · ${escapeHtml(item.records)} 条` : ""}</span>
          ${item.query ? `<small>${escapeHtml(item.query)}</small>` : ""}
        </div>
      `).join("")}
    </div>
  `;
}

function renderBlockedOutputs(readable) {
  const items = readable?.blocked_outputs || [];
  if (!items.length) return "";
  return `
    <details class="compact-details">
      <summary>未补到/受限来源 ${items.length} 项</summary>
      <div class="compact-list muted-list">
        ${items.map((item) => `
          <div class="compact-row">
            <strong>${escapeHtml(item.source || "来源")}</strong>
            <span>${escapeHtml(observationLabel(item.status))}</span>
            ${item.query ? `<small>${escapeHtml(item.query)}</small>` : ""}
          </div>
        `).join("")}
      </div>
    </details>
  `;
}

function auditEvidenceText(item) {
  const evidence = item?.objective_evidence || {};
  const parts = [];
  if (evidence.url) parts.push(`URL ${evidence.url}`);
  if (evidence.status_code) parts.push(`HTTP ${evidence.status_code}`);
  if (evidence.content_type) parts.push(evidence.content_type);
  if (evidence.archive_path) parts.push(`archive ${evidence.archive_path}`);
  if (evidence.download_url) parts.push(`download ${evidence.download_url}`);
  if (evidence.download_status) parts.push(`download ${evidence.download_status}`);
  if (evidence.returncode !== undefined) parts.push(`return ${evidence.returncode}`);
  if (evidence.records !== undefined) parts.push(`records ${evidence.records}`);
  if (evidence.usable_records !== undefined) parts.push(`usable ${evidence.usable_records}`);
  if (evidence.record_bytes) parts.push(`${evidence.record_bytes} bytes`);
  return parts.join(" · ");
}

function auditExampleText(item) {
  const accepted = item?.accepted_examples || [];
  const rejected = item?.rejected_examples || [];
  const examples = accepted.length ? accepted : rejected;
  if (!examples.length) return "";
  return examples.slice(0, 2).map((example) => {
    if (typeof example === "string") return example;
    return [example.title, example.url, example.reason || example.hits].filter(Boolean).join(" · ");
  }).filter(Boolean).join("；");
}

function renderSelfAudit(readable, key = "") {
  const audit = readable?.self_audit || {};
  const accepted = audit.accepted_sources || [];
  const rejected = audit.rejected_sources || [];
  const pending = audit.pending_review_sources || [];
  const counts = audit.counts || {};
  const total = accepted.length + rejected.length + pending.length;
  if (!total && !readable?.self_audit_summary) return "";
  const renderRows = (items, emptyText) => items.length ? `
    <div class="compact-list">
      ${items.map((item) => {
        const reason = item.review_note || item.accepted_reason || item.rejected_reason || item.summary || "";
        const evidence = auditEvidenceText(item);
        const examples = auditExampleText(item);
        return `
        <div class="compact-row audit-row">
          <div class="audit-row-head">
            <strong>${escapeHtml(item.source || "来源")}</strong>
            <span>${escapeHtml(observationLabel(item.status))}${item.records ? ` · ${escapeHtml(String(item.records))} 条` : ""}${item.usable_records || item.empty_records ? ` · 可用 ${escapeHtml(String(item.usable_records || 0))} / 空 ${escapeHtml(String(item.empty_records || 0))}` : ""}</span>
          </div>
          ${item.query ? `<small>${escapeHtml(item.query)}</small>` : ""}
          ${reason ? `<small class="audit-note">${escapeHtml(reason)}</small>` : ""}
          ${evidence ? `<small class="audit-evidence">客观证据：${escapeHtml(evidence)}</small>` : ""}
          ${item.ingest_decision ? `<small>入库：${escapeHtml(item.ingest_decision)}</small>` : ""}
          ${item.next_action ? `<small>下一步：${escapeHtml(item.next_action)}</small>` : ""}
          ${examples ? `<small>样例：${escapeHtml(examples)}</small>` : ""}
        </div>
      `;
      }).join("")}
    </div>
  ` : `<div class="compact-empty">${escapeHtml(emptyText)}</div>`;
  return `
    <details class="message-section compact-details" ${detailsAttrs(key || "self-audit", true)}>
      <summary>
        <span>Crawler 自审</span>
        <span>${escapeHtml(`accepted ${counts.accepted || accepted.length} / rejected ${counts.rejected || rejected.length} / pending ${counts.pending_review || pending.length} / ingest ${audit.ingest_status || "skipped"}`)}</span>
      </summary>
      ${readable.self_audit_summary ? `<div class="compact-summary">${escapeHtml(readable.self_audit_summary)}</div>` : ""}
      ${audit.review_summary ? `<div class="source-meta">自审摘要：${escapeHtml(audit.review_summary)}</div>` : ""}
      ${audit.ingest_note ? `<div class="source-meta">入库判断：${escapeHtml(audit.ingest_note)}</div>` : ""}
      <div class="compact-columns">
        <div>
          <div class="compact-title">接受的来源</div>
          ${renderRows(accepted, "暂无接受来源。")}
        </div>
        <div>
          <div class="compact-title">拒绝/受限的来源</div>
          ${renderRows(rejected, "暂无拒绝来源。")}
        </div>
      </div>
      ${pending.length ? `
        <div class="compact-title">待复核来源</div>
        ${renderRows(pending, "暂无待复核来源。")}
      ` : ""}
      ${audit.principle ? `<div class="source-meta">${escapeHtml(audit.principle)}</div>` : ""}
    </details>
  `;
}

function renderInterAgentMessages(readable, key = "") {
  const messages = readable?.inter_agent_messages || [];
  if (!messages.length) return "";
  return `
    <details class="message-section collab-panel compact-details" ${detailsAttrs(key || "inter-agent")}>
      <summary>
        <span>Agent 间通信</span>
        <span>${messages.length} 条</span>
      </summary>
      <div class="collab-log">
        ${messages.map((item) => `
          <div class="collab-row ${escapeHtml((item.from_agent || "").toLowerCase())}">
            <div class="collab-speaker">${escapeHtml(item.from_agent || "Agent")} → ${escapeHtml(item.to_agent || "Agent")}</div>
            <div class="collab-bubble">
              <div class="collab-state">${escapeHtml(item.intent || "message")}</div>
              <div class="collab-text">${escapeHtml(item.content || "")}</div>
            </div>
          </div>
        `).join("")}
      </div>
    </details>
  `;
}

function renderJobTimeline(readable, key = "") {
  const timeline = readable?.timeline || [];
  if (!timeline.length) return "";
  return `
    <details class="message-section trace-panel compact-details" ${detailsAttrs(key || "timeline", true)}>
      <summary>
        <span>采集过程详情</span>
        <span>${timeline.length} 条</span>
      </summary>
      <div class="job-timeline">
        ${timeline.map((item) => `
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
    </details>
  `;
}

function renderObservationStatus(readable) {
  const statuses = readable?.observation_statuses || {};
  const entries = Object.entries(statuses)
    .filter(([, count]) => Number(count || 0) > 0)
    .map(([status, count]) => `${observationLabel(status)} ${count}`);
  if (!entries.length) return "";
  return `<div class="source-meta">结果概况：${escapeHtml(entries.join(" · "))}</div>`;
}

function renderJobActorPanel(readable, headline, displayStatus, progressText) {
  const useful = readable?.useful_outputs || [];
  const blocked = readable?.blocked_outputs || [];
  const usefulText = useful.length
    ? `已补到/复用 ${useful.length} 类资料：${useful.map((item) => item.source || "资料").slice(0, 4).join("、")}`
    : (readable.status === "running" || readable.status === "queued" ? "还在采集中，暂未形成新的可用资料。" : "本轮没有形成新的可用资料。");
  const blockedText = blocked.length
    ? `${blocked.length} 条空结果、跑偏或受限来源已放到详情里。`
    : "暂无明显限制。";
  const currentText = readable.current_source || readable.current_query
    ? `${readable.current_source || "当前来源"}${readable.current_query ? `：${readable.current_query}` : ""}`
    : (readable.next_action || "等待下一步动作。");
  const auditText = readable.self_audit_summary || "";
  return `
    <section class="message-section job-actor-panel">
      <div class="collab-log">
        <div class="collab-row crawleragent">
          <div class="collab-speaker">CrawlerAgent</div>
          <div class="collab-bubble">
            <div class="collab-state">${escapeHtml(displayStatus)}</div>
            <div class="collab-text">${escapeHtml(headline)}</div>
          </div>
        </div>
        <div class="collab-row crawleragent">
          <div class="collab-speaker">CrawlerAgent</div>
          <div class="collab-bubble">
            <div class="collab-state">进度</div>
            <div class="collab-text">${escapeHtml(progressText)}</div>
          </div>
        </div>
        <div class="collab-row crawleragent">
          <div class="collab-speaker">CrawlerAgent</div>
          <div class="collab-bubble">
            <div class="collab-state">当前动作</div>
            <div class="collab-text">${escapeHtml(currentText)}</div>
          </div>
        </div>
        <div class="collab-row crawleragent">
          <div class="collab-speaker">CrawlerAgent</div>
          <div class="collab-bubble">
            <div class="collab-state">成果</div>
            <div class="collab-text">${escapeHtml(usefulText)}</div>
          </div>
        </div>
        <div class="collab-row crawleragent">
          <div class="collab-speaker">CrawlerAgent</div>
          <div class="collab-bubble">
            <div class="collab-state">限制</div>
            <div class="collab-text">${escapeHtml(blockedText)}</div>
          </div>
        </div>
        ${auditText ? `
          <div class="collab-row crawleragent">
            <div class="collab-speaker">CrawlerAgent</div>
            <div class="collab-bubble">
              <div class="collab-state">自审</div>
              <div class="collab-text">${escapeHtml(auditText)}</div>
            </div>
          </div>
        ` : ""}
      </div>
    </section>
  `;
}

function renderJobReadable(readable, key = "") {
  if (!readable) return "";
  const total = Number(readable.total_tasks || 0);
  const current = Number(readable.current_index || 0);
  const percent = readable.progress_percent != null
    ? Math.max(0, Math.min(100, Number(readable.progress_percent || 0)))
    : total ? Math.max(0, Math.min(100, (current / total) * 100)) : 0;
  const statusText = readable.status === "running" ? "运行中"
    : readable.status === "queued" ? "排队中"
    : readable.status === "succeeded" ? "已完成"
    : readable.status === "failed" ? "失败"
    : readable.status === "stopped" ? "已停止"
    : readable.status || "";
  const displayStatus = readable.status_label || statusText;
  const headline = readable.headline || readable.target || readable.title || "Crawler 采集任务";
  const progressText = readable.progress_text || `第 ${current || 0} / ${total} 个采集动作`;
  const detailCount = Number(readable.blocked_outputs?.length || 0) + Number(readable.timeline?.length || 0);
  return `
    ${renderJobActorPanel(readable, headline, displayStatus, progressText)}
    <details class="message-section job-readable compact-job" ${detailsAttrs(key || "job", true)}>
      <summary>
        <span>查看采集详情</span>
        <span>${escapeHtml(displayStatus)}${detailCount ? ` · ${detailCount} 条细节` : ""}</span>
      </summary>
      <div class="compact-job-body">
        <div class="job-readable-head">
          <strong>${escapeHtml(headline)}</strong>
          <span>${escapeHtml(displayStatus)}</span>
        </div>
        ${total ? progressBar("进度", percent, progressText) : `<div class="source-meta">${escapeHtml(progressText)}</div>`}
        ${readable.plain_summary ? `<div class="compact-summary">${escapeHtml(readable.plain_summary)}</div>` : ""}
        ${readable.planner_warning ? `<div class="source-meta warning-text">规划警告：${escapeHtml(readable.planner_warning)}</div>` : ""}
        <div class="compact-columns">
          <div>
            <div class="compact-title">补到/复用的资料</div>
            ${renderUsefulOutputs(readable)}
          </div>
          <div>
            <div class="compact-title">限制与缺口</div>
            ${renderBlockedOutputs(readable) || `<div class="compact-empty">暂无明显限制。</div>`}
          </div>
        </div>
        <details class="compact-details" ${detailsAttrs(`${key || "job"}:meta`, true)}>
          <summary>任务元信息</summary>
          <div class="job-readable-grid compact-grid">
            ${readable.delivery_target ? statRow("交付对象", readable.delivery_target) : ""}
            ${readable.current_source ? statRow("最后来源", readable.current_source) : ""}
            ${readable.current_query ? statRow("最后搜索", readable.current_query) : ""}
            ${statRow("成功候选", String(readable.success_count || 0))}
            ${statRow("空结果", String(readable.empty_count || 0))}
            ${statRow("跑偏", String(readable.off_topic_count || 0))}
          </div>
          ${readable.health_text ? `<div class="source-meta">状态判断：${escapeHtml(readable.health_text)}</div>` : ""}
          ${readable.next_action ? `<div class="source-meta">下一步：${escapeHtml(readable.next_action)}</div>` : ""}
        </details>
        ${renderInterAgentMessages(readable, `${key || "job"}:inter-agent`)}
        ${renderSelfAudit(readable, `${key || "job"}:self-audit`)}
        ${renderJobTimeline(readable, `${key || "job"}:timeline`)}
      </div>
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
    lastReadableKey: "",
  };
}

function relinkTrackedJobs(jobs) {
  const jobIds = new Set((jobs || []).filter((job) => job?.id).map((job) => job.id));
  if (!jobIds.size) return;
  for (const [agentId, sessions] of Object.entries(state.sessionsByAgent || {})) {
    void agentId;
    for (const session of sessions || []) {
      for (const [index, message] of (session.messages || []).entries()) {
        if (message?.jobId && jobIds.has(message.jobId) && !state.trackedJobs[message.jobId]) {
          rememberJobMessage({ id: message.jobId, status: message.jobStatus || "" }, session.id, index);
        }
      }
    }
  }
}

function applyJobUpdatesToMessages(jobs) {
  relinkTrackedJobs(jobs);
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
      message.jobId = job.id;
      message.jobStatus = job.status || "";
      const readableKey = jobReadableActionKey(job);
      if (readableKey && readableKey !== link.lastReadableKey) {
        appendProcessStep(message, crawlerProgressText(job), {
          actor: "CrawlerAgent",
          kind: "crawler",
          meta: job.id,
          messageKey: messageActionId(session.id, link.messageIndex),
        });
        link.lastReadableKey = readableKey;
        if (!message.hasFinalAnswer && !message.finalAnswerText && !message.isStreamingAnswer && !result.mcagent_recheck) {
          updateAssistantDisplayText(message, "", "progress");
        }
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

function jobReadableActionKey(job) {
  const readable = job?.readable || {};
  return [
    job?.status || "",
    readable.status || "",
    readable.current_index || "",
    readable.total_tasks || "",
    readable.progress_text || "",
    readable.current_source || "",
    readable.current_query || "",
    readable.next_action || "",
    readable.plain_summary || "",
    readable.latest_observation?.status || "",
    readable.latest_observation?.summary || "",
    readable.agent_reflection?.reason || "",
  ].join("|");
}

function crawlerProgressText(job) {
  const readable = job.readable || {};
  const status = readable.status || job.status || "";
  const statusText = status === "running" ? "正在采集"
    : status === "queued" ? "正在排队"
    : status === "succeeded" ? "完成采集"
    : status === "failed" ? "采集失败"
    : status === "stopped" ? "停止采集"
    : "更新采集任务";
  const total = Number(readable.total_tasks || 0);
  const current = Number(readable.current_index || 0);
  const progress = total ? `第 ${current || 0}/${total} 步` : "正在规划";
  const summary = readable.plain_summary || readable.health_text || readable.summary || "";
  const target = readable.headline || readable.target || job.title || "采集任务";
  const lines = [`我${statusText}：${target}`];
  lines.push(summary || `当前进度：${progress}。`);
  if ((status === "running" || status === "queued") && (readable.current_source || readable.current_query)) {
    lines.push(`当前动作：${[readable.current_source, readable.current_query].filter(Boolean).join("：")}`);
  }
  if (readable.latest_observation?.summary) {
    lines.push(`最近工具结果：${readable.latest_observation.summary}`);
  }
  if (readable.agent_reflection?.reason) {
    lines.push(`我的判断：${readable.agent_reflection.reason}`);
  }
  const useful = readable.useful_outputs || [];
  const blocked = readable.blocked_outputs || [];
  if (status === "succeeded" && useful.length) {
    const sources = useful.map((item) => item.source).filter(Boolean).slice(0, 3).join("、");
    lines.push(`我这轮补到或复用了 ${useful.length} 类资料${sources ? `：${sources}` : ""}。`);
  }
  if (blocked.length) {
    lines.push(`我把 ${blocked.length} 条受限或低价值结果放进采集详情。`);
  }
  return lines.join("\n");
}

function appendProcessStep(message, text, options = {}) {
  const value = String(text || "").trim();
  if (!value) return;
  const current = Array.isArray(message.processLog) ? message.processLog : [];
  if (current.includes(value)) return;
  message.processLog = [...current, value];
  recordAgentAction({
    actor: options.actor || message.agent || "Agent",
    text: value,
    kind: options.kind || "process",
    meta: options.meta || "",
    messageKey: options.messageKey || "",
  });
}

function setInitialProcessStep(message, text, options = {}) {
  appendProcessStep(message, text, options);
  message.hasFinalAnswer = false;
  message.isStreamingAnswer = false;
  updateAssistantDisplayText(message);
}

function processBlockText(message) {
  const steps = Array.isArray(message.processLog) ? message.processLog.filter(Boolean) : [];
  if (!steps.length) return "";
  return `\u6267\u884c\u8fc7\u7a0b\uff1a\n${steps.map((text, index) => `${index + 1}. ${text}`).join("\n")}`;
}

function composeAssistantDisplayText(message, answerText = "", mode = "final") {
  const answer = String(answerText || "").trim();
  if (answer) return answer;
  return mode === "progress" ? "处理中..." : (message.text || "");
}

function updateAssistantDisplayText(message, answerText = "", mode = "progress") {
  if (mode === "progress") {
    message.text = composeAssistantDisplayText(message, message.finalAnswerText || "");
    return;
  }
  if (mode === "final") {
    message.finalAnswerText = String(answerText || "").trim();
    message.text = composeAssistantDisplayText(message, message.finalAnswerText, mode);
    return;
  }
  message.text = composeAssistantDisplayText(message, answerText, mode);
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
    $("sources").innerHTML = `<div class="source-item empty-state"><strong>还没有引用来源</strong><div class="source-meta">本轮可能是闲聊、状态回复，或 Crawler 正在后台采集。</div></div>`;
    return;
  }
  $("sources").innerHTML = state.lastSources.map((source) => `
    <div class="source-item">
      <strong>[S${source.rank}] ${escapeHtml(source.title)}</strong>
      <div class="source-meta">匹配度 ${Number(source.score).toFixed(3)} · ${escapeHtml(source.url || source.source_path)}</div>
      <div class="source-text">${escapeHtml(source.text)}</div>
    </div>
  `).join("");
}

function statRow(label, value) {
  return `<div class="stat-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function jobStatusLabel(status) {
  return {
    queued: "排队中",
    running: "运行中",
    succeeded: "已完成",
    failed: "失败",
    stopped: "已停止",
    finished: "已结束",
  }[status] || status || "未知";
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
    return `<div class="source-meta">全面补库：当前没有运行。</div>`;
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
    <details class="compact-details crawler-progress-details">
      <summary>全面补库：${escapeHtml(jobStatusLabel(status))}</summary>
      <div class="crawler-progress">
      <div class="progress-status">
        <strong>批量脚本</strong>
        <span>${progress.active ? escapeHtml(processes || "运行中") : "未运行"}</span>
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
    </details>
  `;
}

function renderActiveCrawlerOverview(jobs) {
  const items = (jobs || []).filter((job) => job.kind === "crawler");
  const active = items.find((job) => job.status === "queued" || job.status === "running");
  const latest = active || items[0];
  if (!latest) {
    return `<div class="crawler-progress empty-state"><div class="progress-status"><strong>当前没有 Crawler 任务</strong><span>需要补资料时再启动</span></div></div>`;
  }
  const readable = latest.readable || {};
  const target = readable.headline || readable.target || latest.title || latest.kind;
  const total = Number(readable.total_tasks || 0);
  const currentIndex = Number(readable.current_index || 0);
  const progress = total ? `第 ${currentIndex || 0}/${total} 步` : "等待规划";
  const counts = [
    `可用 ${readable.success_count || 0}`,
    `受限/空 ${Number(readable.empty_count || 0) + Number(readable.off_topic_count || 0)}`,
  ].join(" · ");
  return `
    <div class="crawler-progress active-job-overview">
      <div class="progress-status">
        <strong>Crawler：${escapeHtml(jobStatusLabel(latest.status))}</strong>
        <span>${escapeHtml(fmtDateTime(latest.started_at || latest.created_at))}</span>
      </div>
      <div class="source-meta">目标：${escapeHtml(target)}</div>
      <div class="source-meta">${escapeHtml(progress)} · ${escapeHtml(counts)}</div>
      ${readable.plain_summary ? `<div class="source-meta">${escapeHtml(readable.plain_summary)}</div>` : ""}
      <details class="compact-details">
        <summary>展开技术细节</summary>
        ${readable.current_query ? `<div class="source-meta">当前查询：${escapeHtml(readable.current_query)}</div>` : ""}
        ${readable.progress_text ? `<div class="source-meta">${escapeHtml(readable.progress_text)}</div>` : ""}
        ${readable.health_text ? `<div class="source-meta">${escapeHtml(readable.health_text)}</div>` : ""}
        ${renderObservationStatus(readable)}
          ${readable.latest_observation?.status ? `<div class="source-meta">最近工具结果：${escapeHtml(observationLabel(readable.latest_observation.status))}${readable.latest_observation.summary ? ` · ${escapeHtml(readable.latest_observation.summary)}` : ""}</div>` : ""}
          ${readable.agent_reflection?.reason ? `<div class="source-meta">Agent 判断：${escapeHtml(readable.agent_reflection.reason).slice(0, 220)}</div>` : ""}
          ${renderSelfAudit(readable, `active-crawler:${latest.id || latest.started_at || "latest"}`)}
        </details>
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
        setActivity("已请求提前结束：CrawlerAgent 会完成当前动作后停止。", "crawler");
      } else {
        setActivity(crawlerProgressText(activeCrawler).split("\n")[0], "crawler");
      }
    } else if (activeIngest) {
      setActivity("正在把采集资料清洗并写入本地向量库。", "ingest");
    } else {
      setActivity(idleActivityText(), "idle");
    }
  }
  $("runIngest").disabled = state.jobs.some((job) => job.kind === "ingest" && (job.status === "queued" || job.status === "running"));
  $("runCrawler").disabled = state.jobs.some((job) => job.kind === "crawler" && (job.status === "queued" || job.status === "running"));
  $("runIngest").textContent = $("runIngest").disabled ? "正在导入" : "导入新资料";
  $("runCrawler").textContent = $("runCrawler").disabled ? "采集中" : `启动采集（${$("crawlerRounds")?.value || 1}轮）`;
  if (!state.jobs.length) {
    $("jobList").innerHTML = `<div class="empty-state source-meta">暂无后台任务。</div>`;
    return active;
  }
  $("jobList").innerHTML = state.jobs.slice(0, 6).map((job) => {
    const isActiveCrawler = job.kind === "crawler" && (job.status === "queued" || job.status === "running");
    const canStop = isActiveCrawler && !job.stop_requested;
    const readable = job.readable || {};
    const readableLine = isActiveCrawler && (readable.headline || readable.target)
      ? `<div class="source-meta">目标：${escapeHtml(readable.headline || readable.target)}</div>`
      : "";
    const roundInfo = job.result && job.result.rounds_total
      ? `<div class="source-meta">轮次：${escapeHtml(job.result.rounds_completed || (job.result.rounds || []).length || 0)} / ${escapeHtml(job.result.rounds_total)}</div>`
      : "";
    const foldedDetails = readable.current_query || readable.latest_observation?.status || readable.agent_reflection?.reason || readable.health_text
      ? `
        <details class="compact-details">
          <summary>展开技术细节</summary>
          ${!isActiveCrawler && (readable.headline || readable.target) ? `<div class="source-meta">目标：${escapeHtml(readable.headline || readable.target)}</div>` : ""}
          ${readable.progress_text ? `<div class="source-meta">${escapeHtml(readable.progress_text)}</div>` : ""}
          ${readable.plain_summary ? `<div class="source-meta">${escapeHtml(readable.plain_summary)}</div>` : ""}
          ${readable.current_query ? `<div class="source-meta">当前查询：${escapeHtml(readable.current_query)}</div>` : ""}
          ${readable.latest_observation?.status ? `<div class="source-meta">最近结果：${escapeHtml(observationLabel(readable.latest_observation.status))}${readable.latest_observation.summary ? ` · ${escapeHtml(readable.latest_observation.summary).slice(0, 180)}` : ""}</div>` : ""}
          ${readable.agent_reflection?.reason ? `<div class="source-meta">Agent 判断：${escapeHtml(readable.agent_reflection.reason).slice(0, 180)}</div>` : ""}
          ${readable.health_text ? `<div class="source-meta">${escapeHtml(readable.health_text)}</div>` : ""}
          ${renderSelfAudit(readable, `job-list:${job.id || "job"}`)}
          ${job.summary ? `<div class="source-meta">${escapeHtml(job.summary).slice(0, 220)}</div>` : ""}
        </details>
      `
      : "";
    return `
      <div class="job-item">
        <strong>${escapeHtml(job.title || job.kind)}</strong>
        <span class="job-status ${escapeHtml(job.status)}">${escapeHtml(jobStatusLabel(job.status))}</span>
        <div class="source-meta">${fmtDateTime(job.started_at || job.created_at)}${job.ended_at ? ` - ${fmtDateTime(job.ended_at)}` : ""}</div>
        ${readableLine}
        ${isActiveCrawler && readable.progress_text ? `<div class="source-meta">${escapeHtml(readable.progress_text)}</div>` : ""}
        ${isActiveCrawler && readable.plain_summary ? `<div class="source-meta">${escapeHtml(readable.plain_summary)}</div>` : ""}
        ${foldedDetails}
        ${roundInfo}
        ${job.stop_requested && isActiveCrawler ? `<div class="source-meta stop-note">已收到提前结束请求，当前轮完成后停止。</div>` : ""}
        ${isActiveCrawler && job.summary ? `<div class="source-meta">${escapeHtml(job.summary).slice(0, 220)}</div>` : ""}
        ${canStop ? `<div class="job-actions"><button class="mini-button danger-outline" type="button" data-stop-job="${escapeHtml(job.id)}">停止采集</button></div>` : ""}
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
    statRow("资料文档", db.documents),
    statRow("可检索片段", db.chunks),
    statRow("向量索引", db.index_exists ? `${Math.round(db.index_size / 1024)} KB` : "未建立"),
    statRow("采集文件", status.sources.files),
  ].join("");

  const ledger = status.ledger || {};
  $("ledgerStats").innerHTML = [
    statRow("去重后资料", ledger.unique || 0),
    statRow("账本记录", ledger.entries || 0),
    statRow("新增", (ledger.by_status && ledger.by_status.new) || 0),
    statRow("跳过重复", (ledger.by_status && ledger.by_status.skipped_unchanged) || 0),
    ledger.latest && ledger.latest.length
      ? `<div class="source-meta">最近：${escapeHtml(ledger.latest[0].title || ledger.latest[0].key || "")}</div>`
      : `<div class="source-meta">账本还没有记录。</div>`,
  ].join("");

  const progress = status.crawler_progress || {};
  const jobs = status.jobs || [];
  const activeCrawler = jobs.find((job) => job.kind === "crawler" && (job.status === "queued" || job.status === "running"));
  $("crawlerState").textContent = activeCrawler
    ? `CrawlerAgent：${jobStatusLabel(activeCrawler.status)}`
    : `无运行中动作 · ${status.sources.files} 个采集文件`;
  const latest = status.sources.latest_files || [];
  const runs = status.agenttest_runs || [];
  $("crawlerInfo").innerHTML = [
    renderActiveCrawlerOverview(jobs),
    renderCrawlerProgress(progress),
    latest.length ? `<div class="source-meta">最近文件：${escapeHtml(latest[0].path)}</div>` : `<div class="source-meta">没有可用采集文件。</div>`,
    `<details class="compact-details"><summary>展开存储细节</summary>
      ${statRow("采集目录", status.sources.source_dir)}
      ${statRow("manifest", status.sources.manifests)}
      ${statRow("报告", status.sources.reports)}
      ${statRow("测试记录", runs.length)}
      ${status.knowledge_map && status.knowledge_map.exists ? statRow("知识图谱", `${status.knowledge_map.documents || 0} docs`) : statRow("知识图谱", "未建立")}
      ${status.toolsets ? statRow("工具集", status.toolsets.length) : ""}
      ${status.memory ? statRow("Agent 记忆", `${status.memory.events || 0} events`) : ""}
    </details>`,
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
  return session.messages.length - 1;
}

function requestHistoryForAgent(session, pendingIndex, limit = 24) {
  const messages = (session.messages || [])
    .slice(0, Math.max(0, pendingIndex))
    .filter((message) => message.role === "user" || message.role === "assistant")
    .filter((message) => String(message.text || "").trim() && String(message.text || "").trim() !== "处理中...");
  return messages.slice(-limit).map((message) => ({
    role: message.role,
    text: message.finalAnswerText || message.text,
    time: message.time,
    agent: message.agent || "",
    sources: message.sources || [],
  }));
}

function sourcePreview(sources) {
  const items = (sources || []).slice(0, 4);
  if (!items.length) return "本地资料库暂时没有命中，我正在判断是否需要通过 AgentMessage 询问 CrawlerAgent。";
  const lines = items.map((source) => `- [S${source.rank}] ${source.title} (${Number(source.score).toFixed(3)})`);
  return `我已命中本地资料 ${sources.length} 条，正在组织回答。\n\n优先来源：\n${lines.join("\n")}`;
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
    const pendingMessage = session.messages[pendingIndex];
    const messageKey = messageActionId(session.id, pendingIndex);
    if (payload.agent === "mcagent_rag") {
      const initialText = `我收到你的问题：${question}`;
      setActivity(initialText, "thinking");
      setInitialProcessStep(pendingMessage, initialText, { actor: "MCagent", kind: "start", messageKey });
      saveSessions();
      renderMessages();
    } else if (payload.agent === "retriever_only") {
      const initialText = `我收到你的检索请求：${question}`;
      setActivity(initialText, "working");
      setInitialProcessStep(pendingMessage, initialText, { actor: "MCagent", kind: "start", messageKey });
    } else if (payload.agent === "crawler_agent") {
      const initialText = `我收到你的 Crawler 请求：${question}`;
      setActivity(initialText, "crawler");
      setInitialProcessStep(pendingMessage, initialText, { actor: "CrawlerAgent", kind: "start", messageKey });
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
        appendProcessStep(message, progressText, {
          actor: actionActorForTrace(step, step?.detail || {}),
          kind: step.stage || "trace",
          meta: step.status || "",
          messageKey,
        });
        if (!message.hasFinalAnswer && !message.isStreamingAnswer) updateAssistantDisplayText(message);
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
        updateAssistantDisplayText(message, streamedAnswer, "streaming");
        message.isStreamingAnswer = true;
        setActivity("我正在组织回答，内容会实时出现在这条消息里。", "thinking");
        saveSessions();
        renderMessages();
      },
      onResponse(partial) {
        if (state.currentChat !== currentChat || !partial) return;
        const replyText = agentReplyContent(partial);
        const message = session.messages[pendingIndex];
        if (replyText) message.finalAnswerText = replyText;
        message.hasFinalAnswer = Boolean(replyText);
        message.isStreamingAnswer = false;
        message.agentMessage = partial.agent_message || null;
        message.trace = partial.trace || message.trace || [];
        for (const step of message.trace || []) appendProcessStep(message, progressTextForTrace(step), {
          actor: actionActorForTrace(step, step?.detail || {}),
          kind: step.stage || "trace",
          meta: step.status || "",
          messageKey,
        });
        updateAssistantDisplayText(message, replyText || message.finalAnswerText || streamedAnswer || "", "final");
        message.collaboration = partial.collaboration || [];
        message.sources = partial.sources || [];
        message.jobReadable = shouldRenderJobReadable(partial) ? partial.job.readable : message.jobReadable;
        if (shouldTrackJobResponse(partial)) {
          message.jobId = partial.job.id;
          message.jobStatus = partial.job.status || "";
          rememberJobMessage(partial.job, session.id, pendingIndex);
        }
        saveSessions();
        renderMessages();
      },
    });
    if (state.currentChat !== currentChat) return;
    const message = session.messages[pendingIndex];
    const finalText = agentReplyContent(data) || streamedAnswer || message.finalAnswerText || "";
    message.finalAnswerText = finalText;
    message.hasFinalAnswer = true;
    message.isStreamingAnswer = false;
    message.agent = state.agents.find((item) => item.id === state.activeAgent)?.name || state.activeAgent;
    message.agentMessage = data.agent_message || null;
    message.trace = data.trace || [];
    for (const step of message.trace || []) appendProcessStep(message, progressTextForTrace(step), {
      actor: actionActorForTrace(step, step?.detail || {}),
      kind: step.stage || "trace",
      meta: step.status || "",
      messageKey,
    });
    updateAssistantDisplayText(message, finalText, "final");
    message.collaboration = data.collaboration || [];
    message.sources = data.sources || [];
    message.jobReadable = shouldRenderJobReadable(data) ? data.job.readable : message.jobReadable;
    if (shouldTrackJobResponse(data)) {
      message.jobId = data.job.id;
      message.jobStatus = data.job.status || "";
    }
    renderSources(data.sources || []);
    if (shouldTrackJobResponse(data) && data.job && (data.job.status === "queued" || data.job.status === "running")) {
      rememberJobMessage(data.job, session.id, pendingIndex);
      const crawlerText = crawlerProgressText(data.job);
      appendProcessStep(message, crawlerText, { actor: "CrawlerAgent", kind: "crawler", meta: data.job.id, messageKey });
      setActivity(crawlerText.split("\n")[0], "crawler");
    } else {
      setActivity("我完成了这轮处理。", "done");
      setTimeout(() => {
        if (!state.currentChat) setActivity(idleActivityText(), "idle");
      }, 2500);
    }
  } catch (error) {
    if (state.currentChat !== currentChat) return;
    const message = session.messages[pendingIndex];
    const hasUsableAnswer = Boolean(message.hasFinalAnswer || streamedAnswer || (message.text && message.text.length > 80 && message.isStreamingAnswer));
    if (error.name === "AbortError") {
      message.text = "已暂停本次回复。";
      message.hasFinalAnswer = false;
      message.isStreamingAnswer = false;
      setActivity("已暂停本次回复。", "paused");
    } else if (hasUsableAnswer) {
      message.finalAnswerText = streamedAnswer || message.finalAnswerText || "";
      updateAssistantDisplayText(message, message.finalAnswerText, "final");
      message.hasFinalAnswer = Boolean(message.text);
      message.isStreamingAnswer = false;
      setActivity("连接中断，已保留已收到的回答。", "done");
    } else {
      message.text = `请求失败：${error.message}`;
      message.hasFinalAnswer = false;
      message.isStreamingAnswer = false;
      renderSources([]);
      setActivity("请求失败：请查看消息或后台任务。", "error");
    }
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
    const question = $("crawlerQuery") ? $("crawlerQuery").value.trim() : "";
    if (!question) {
      addMessage("assistant", "请先写清楚希望 CrawlerAgent 采集什么。", "系统");
      return;
    }
    addMessage("user", question);
    const pendingIndex = addMessage("assistant", "处理中...", "CrawlerAgent");
    const session = activeSession();
    const message = session.messages[pendingIndex];
    const messageKey = messageActionId(session.id, pendingIndex);
    setInitialProcessStep(message, `我收到你的采集请求：${question}`, {
      actor: "CrawlerAgent",
      kind: "start",
      messageKey,
    });
    setActivity("CrawlerAgent 正在理解这条采集消息。", "crawler");
    const payload = {
      from_agent: "User",
      to_agent: "CrawlerAgent",
      content: question,
      intent: "collection_request",
      session_id: activeSession().id,
      agent: "crawler_agent",
      source: $("crawlerSource").value,
      query: question,
      question,
      rounds: Number($("crawlerRounds").value || 1),
      interval_seconds: Number($("crawlerInterval").value || 0),
      metadata: {
        source: $("crawlerSource").value,
        requested_by: "user",
        ui_entry: "crawler_panel",
        rounds: Number($("crawlerRounds").value || 1),
        interval_seconds: Number($("crawlerInterval").value || 0),
      },
    };
    const data = await api("/api/agent-message", { method: "POST", body: JSON.stringify(payload) });
    const replyText = agentReplyContent(data) || "我是 CrawlerAgent。我已收到这条 AgentMessage。";
    message.finalAnswerText = replyText;
    message.hasFinalAnswer = true;
    message.isStreamingAnswer = false;
    message.agentMessage = data.agent_message || null;
    message.trace = data.trace || [];
    message.collaboration = data.collaboration || [];
    message.sources = data.sources || [];
    for (const step of message.trace || []) appendProcessStep(message, progressTextForTrace(step), {
      actor: actionActorForTrace(step, step?.detail || {}),
      kind: step.stage || "trace",
      meta: step.status || "",
      messageKey,
    });
    if (data.job) {
      message.jobId = data.job.id;
      message.jobStatus = data.job.status || "";
      message.jobReadable = data.job.readable || null;
      rememberJobMessage(data.job, session.id, pendingIndex);
      appendProcessStep(message, crawlerProgressText(data.job), {
        actor: "CrawlerAgent",
        kind: "crawler",
        meta: data.job.id,
        messageKey,
      });
      renderJobs([data.job, ...state.jobs.filter((job) => job.id !== data.job.id)]);
    }
    updateAssistantDisplayText(message, replyText, "final");
    saveSessions();
    renderMessages(true);
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
