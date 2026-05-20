const state = {
  profiles: [],
  assignments: {},
  editingProfileId: "",
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setStatus(text) {
  $("settingsStatus").textContent = text;
}

function profileLabel(profile) {
  const keyText = profile.key_configured ? "已保存 key" : (profile.provider === "ollama" ? "本地" : "无 key");
  return `${profile.name || profile.model || profile.id} · ${profile.model || "未填模型"} · ${keyText}`;
}

function profileById(profileId) {
  return state.profiles.find((profile) => profile.id === profileId) || null;
}

function profileOptions(selectedId = "") {
  return state.profiles.map((profile) => `
    <option value="${escapeHtml(profile.id)}" ${profile.id === selectedId ? "selected" : ""}>${escapeHtml(profileLabel(profile))}</option>
  `).join("");
}

function fillProfileForm(profile) {
  $("profileName").value = profile?.name || "";
  $("profileModel").value = profile?.model || "";
  $("profileBaseUrl").value = profile?.base_url || "";
  $("profileApiKey").value = "";
  $("profileApiKey").placeholder = profile?.key_configured ? "已保存 key；留空不修改" : "输入 API Key；本地 Ollama 可留空";
  $("profileProvider").value = profile?.provider || "openai-compatible";
  $("profileTimeout").value = profile?.timeout_seconds || 180;
}

function render() {
  if (!state.editingProfileId || !profileById(state.editingProfileId)) {
    state.editingProfileId = state.assignments.mcagent_rag || state.profiles[0]?.id || "";
  }
  $("mcagentProfileSelect").innerHTML = profileOptions(state.assignments.mcagent_rag || "");
  $("crawlerProfileSelect").innerHTML = profileOptions(state.assignments.crawler_agent || "");
  $("profileEditorSelect").innerHTML = profileOptions(state.editingProfileId);
  $("profileEditorSelect").value = state.editingProfileId;
  fillProfileForm(profileById(state.editingProfileId));
}

function syncProfileFormToState() {
  const id = state.editingProfileId;
  if (!id) return null;
  let profile = profileById(id);
  if (!profile) {
    profile = { id };
    state.profiles.push(profile);
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

async function loadProfiles() {
  const data = await api("/api/llm-profiles");
  state.profiles = data.profiles || [];
  state.assignments = data.assignments || {};
  render();
}

async function saveProfiles(message = "模型设置已保存。") {
  syncProfileFormToState();
  const data = await api("/api/llm-profiles", {
    method: "POST",
    body: JSON.stringify({ profiles: state.profiles, assignments: state.assignments }),
  });
  state.profiles = data.profiles || [];
  state.assignments = data.assignments || {};
  setStatus(message);
  render();
}

async function testCurrentProfile() {
  const profile = syncProfileFormToState();
  if (!profile) return;
  setStatus(`正在测试 ${profile.name || profile.model}...`);
  const data = await api("/api/llm-profiles/test", {
    method: "POST",
    body: JSON.stringify({ id: profile.id, profile }),
  });
  if (data.ok) {
    setStatus(`连接成功：${data.label}，${data.elapsed_ms} ms，返回：${data.sample || "OK"}`);
  } else {
    setStatus(`连接失败：${data.error || "未知错误"}`);
  }
}

function bindEvents() {
  $("mcagentProfileSelect").addEventListener("change", async () => {
    state.assignments.mcagent_rag = $("mcagentProfileSelect").value;
    await saveProfiles("MCagent 使用的模型已保存。");
  });
  $("crawlerProfileSelect").addEventListener("change", async () => {
    state.assignments.crawler_agent = $("crawlerProfileSelect").value;
    await saveProfiles("CrawlerAgent 使用的模型已保存。");
  });
  $("profileEditorSelect").addEventListener("change", () => {
    state.editingProfileId = $("profileEditorSelect").value;
    render();
  });
  $("newProfile").addEventListener("click", () => {
    const id = `custom-${Date.now()}`;
    state.profiles.push({
      id,
      name: "新模型",
      provider: "openai-compatible",
      base_url: "",
      model: "",
      timeout_seconds: 180,
      key_configured: false,
    });
    state.editingProfileId = id;
    render();
  });
  $("saveProfiles").addEventListener("click", () => saveProfiles().catch((error) => setStatus(`保存失败：${error.message}`)));
  $("testProfile").addEventListener("click", () => testCurrentProfile().catch((error) => setStatus(`测试失败：${error.message}`)));
  $("deleteProfile").addEventListener("click", async () => {
    const profile = profileById(state.editingProfileId);
    if (!profile) return;
    if (!window.confirm(`删除模型配置“${profile.name || profile.id}”？`)) return;
    state.profiles = state.profiles.filter((item) => item.id !== profile.id);
    for (const agentId of ["mcagent_rag", "crawler_agent"]) {
      if (state.assignments[agentId] === profile.id) state.assignments[agentId] = state.profiles[0]?.id || "";
    }
    state.editingProfileId = state.profiles[0]?.id || "";
    await saveProfiles("模型配置已删除。");
  });
}

bindEvents();
loadProfiles().catch((error) => {
  document.body.innerHTML = `<pre style="padding:20px">设置页启动失败：${escapeHtml(error.message)}</pre>`;
});
