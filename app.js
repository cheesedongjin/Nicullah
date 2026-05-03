const $ = (selector) => document.querySelector(selector);

let state = {
  health: null,
  projects: [],
  activeProjectId: null,
  selectedDecision: null,
  selectedChoiceId: null,
  activeTab: "logs",
  polling: null,
  filesPanelProjectId: null,
  fileTreeCollapsed: {},
  fileTreeScrollTop: 0,
  fileViewerScrollTop: 0,
  openFilePath: null,
  openFileContent: "",
  searchQuery: "",
  deferredDecisionIds: new Set(),
};

let searchTimer = null;
const DEFAULT_COLLAPSED_DIRS = new Set([
  ".git",
  ".next",
  ".venv",
  ".warroom",
  "__pycache__",
  "build",
  "coverage",
  "dist",
  "node_modules",
  "venv",
]);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
  return data;
}

// ─── Health & projects ───────────────────────────────────────────────────────

async function refreshHealth() {
  state.health = await api("/api/health");
  const ready = Boolean(state.health.gemini_ready);
  const footer = $(".sidebar-footer");
  footer.classList.toggle("is-ready", ready);
  footer.classList.remove("is-loading");
  $("#apiStatusLabel").textContent = ready ? "Gemini Ready" : "Gemini Required";
  $("#newProjectButton").disabled = !ready;
  $("#backendLock").hidden = ready;
  $("#backendLockReason").textContent = state.health.reason || "";
  return ready;
}

async function refreshProjects() {
  const data = await api("/api/projects");
  state.projects = data.projects || [];
  if (!state.activeProjectId || !state.projects.some((p) => p.id === state.activeProjectId)) {
    state.activeProjectId = state.projects[0]?.id || null;
  }
  render();
}

function activeProject() {
  return state.projects.find((p) => p.id === state.activeProjectId) || null;
}

function shortPath(path) {
  if (!path) return "";
  const normalized = path.replaceAll("\\", "/");
  return normalized.split("/").slice(-3).join("/");
}

function formatSize(bytes) {
  if (bytes == null || bytes === 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatRelTime(mtime) {
  const secs = Math.floor(Date.now() / 1000 - mtime);
  if (secs < 60) return "방금";
  if (secs < 3600) return `${Math.floor(secs / 60)}분 전`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}시간 전`;
  return `${Math.floor(secs / 86400)}일 전`;
}

function flattenTree(nodes, prefix = "") {
  const result = [];
  for (const node of nodes || []) {
    const path = prefix ? `${prefix}/${node.name}` : node.name;
    if (node.type === "dir" && isDefaultCollapsedDir(node.name)) continue;
    if (node.type === "file") {
      result.push({ path, size: node.size || 0, mtime: node.mtime || 0 });
    }
    if (node.children) {
      result.push(...flattenTree(node.children, path));
    }
  }
  return result;
}

function isDefaultCollapsedDir(name) {
  return DEFAULT_COLLAPSED_DIRS.has(String(name || "").toLowerCase());
}

function collapsedDirsForProject(projectId) {
  if (!state.fileTreeCollapsed[projectId]) state.fileTreeCollapsed[projectId] = {};
  return state.fileTreeCollapsed[projectId];
}

// ─── Render ──────────────────────────────────────────────────────────────────

function render() {
  renderProjectList();
  renderWarRoom();
}

function renderProjectList() {
  const list = $("#projectList");
  const template = $("#projectItemTemplate");
  list.innerHTML = "";

  if (!state.projects.length) {
    list.innerHTML = `<div class="empty-sidebar">프로젝트가 없습니다.</div>`;
    return;
  }

  state.projects.forEach((project) => {
    const item = template.content.firstElementChild.cloneNode(true);
    item.dataset.projectId = project.id;
    item.classList.toggle("is-active", project.id === state.activeProjectId);
    item.querySelector(".project-glyph").textContent = project.name.slice(0, 2).toUpperCase();
    item.querySelector("strong").textContent = project.name;
    item.querySelector("small").textContent = statusLabel(project.status);
    item.addEventListener("click", () => {
      state.activeProjectId = project.id;
      render();
    });
    list.appendChild(item);
  });
}

function statusLabel(status) {
  const map = {
    created: "생성됨",
    running: "실행 중",
    stopping: "작업 중단 중",
    stopped: "중단됨",
    waiting_for_handoff: "대기 중",
    waiting_for_approval: "승인 대기",
    complete: "완료",
    error: "오류",
  };
  return map[status] || status || "대기";
}

function renderWarRoom() {
  const project = activeProject();
  const pending = project?.pending_decisions?.[0] || null;

  $("#projectTitle").textContent = project?.name || "Nicullah";
  $("#projectStatusLabel").textContent = project?.status?.toUpperCase() || "STANDBY";
  $("#rootGoalLabel").textContent = project?.vision || "프로젝트를 생성하면 워룸이 시작됩니다.";
  $("#projectDirectoryLabel").textContent = project ? shortPath(project.directory) : "no project directory";
  $("#openDecisionButton").hidden = !pending;
  $("#openDecisionButton").disabled = !pending;
  renderStopResumeButton(project);
  renderPoMessageForm(project);

  const agents = project?.agents || {};
  setAgent("pm", agents.pm);
  setAgent("designer", agents.designer);
  setAgent("backend", agents.backend);
  setAgent("frontend", agents.frontend);
  setAgent("qa", agents.qa);

  renderActivity(project);
  renderPmChat(project);
  renderProgress(project);

  if (state.activeTab === "files") renderFiles(project);

  drawGraph(Boolean(project), project?.iteration || 0);
  maybeAutoOpenDecision(pending);
}

function isStoppedProject(project) {
  return ["stopped", "waiting_for_handoff", "complete"].includes(project?.status);
}

function renderStopResumeButton(project) {
  const btn = $("#stopResumeButton");
  if (!btn) return;
  btn.hidden = !project;
  btn.disabled = !project || project.status === "stopping" || project.stop_requested;

  if (!project) {
    btn.textContent = "작업 중단";
  } else if (project.status === "stopping" || project.stop_requested) {
    btn.textContent = "작업 중단 중...";
  } else if (isStoppedProject(project)) {
    btn.textContent = "재개";
  } else {
    btn.textContent = "작업 중단";
  }
}

function renderPoMessageForm(project) {
  const input = $("#poMessageInput");
  const button = $("#poMessageButton");
  if (!input || !button) return;
  const enabled = Boolean(project);
  input.disabled = !enabled;
  button.disabled = !enabled || !input.value.trim();
  input.placeholder = enabled ? "PM에게 질문, 지시, 피드백 보내기" : "프로젝트를 먼저 선택하세요";
}

function maybeAutoOpenDecision(decision) {
  const dialog = $("#decisionModal");
  if (!decision || !dialog || dialog.open) return;
  if (state.deferredDecisionIds.has(decision.id)) return;
  if (state.selectedDecision?.id === decision.id) return;
  window.setTimeout(() => {
    const latest = activeProject()?.pending_decisions?.[0];
    if (!latest || latest.id !== decision.id || dialog.open) return;
    openDecision();
  }, 0);
}

function ticketStatusKey(status) {
  const key = String(status || "ready").toLowerCase();
  return {
    todo: "ready",
    in_progress: "progress",
    working: "progress",
    needs_review: "review",
  }[key] || key;
}

function ticketDetail(ticket) {
  const notes = Array.isArray(ticket.notes) ? ticket.notes.filter(Boolean) : [];
  const detail = notes.at(-1) || ticket.description || "";
  return detail
    ? `<span class="ticket-detail">${escapeHtml(detail)}</span>`
    : "";
}

function setAgent(id, data) {
  const el = document.querySelector(`[data-agent="${id}"]`);
  const label = document.querySelector(`#${id}State`);
  if (!el || !label) return;
  const status = data?.status || "idle";
  el.dataset.status = status;
  label.textContent = data?.message || "대기 중";
}

// ─── Activity feed ───────────────────────────────────────────────────────────

function renderActivity(project) {
  const feed = $("#activityFeed");
  const logs = project?.logs?.length
    ? project.logs
    : [{ agent: "System", message: "새 프로젝트를 생성하면 에이전트가 실제 폴더에서 작업을 시작합니다." }];

  feed.innerHTML = [...logs]
    .reverse()
    .map(
      (log) => `
      <li data-level="${log.level || "info"}">
        <strong>${log.agent}</strong>
        <span>${escapeHtml(log.message)}</span>
        ${log.time ? `<time>${log.time.slice(11, 16)}</time>` : ""}
      </li>`,
    )
    .join("");
}

function renderPmChat(project) {
  const panel = $("#pmChatPanel");
  if (!panel) return;
  const messages = project?.pm_chat || [];
  if (!project) {
    panel.innerHTML = `<p class="empty-state">프로젝트를 선택하세요.</p>`;
    return;
  }
  if (!messages.length) {
    panel.innerHTML = `<p class="empty-state">PO가 질문을 보내면 PM이 여기에서 이어서 답합니다.</p>`;
    return;
  }

  panel.innerHTML = messages
    .map((item) => {
      const from = item.from || "PM";
      const speaker = from.toLowerCase() === "po" ? "po" : "pm";
      return `
        <article class="chat-message" data-speaker="${speaker}">
          <div class="chat-meta">
            <strong>${escapeHtml(from)}</strong>
            ${item.created_at ? `<time>${item.created_at.slice(11, 16)}</time>` : ""}
          </div>
          <p>${escapeHtml(item.message || "")}</p>
        </article>`;
    })
    .join("");
  panel.scrollTop = panel.scrollHeight;
}

// ─── Progress panel ──────────────────────────────────────────────────────────

function renderProgress(project) {
  const panel = $("#progressPanel");
  if (!project) {
    panel.innerHTML = `<p class="empty-state">프로젝트를 선택하거나 생성하세요.</p>`;
    return;
  }

  const iteration = project.iteration || 0;
  const maxIter = project.max_agent_steps || 18;
  const displayedIteration = Math.min(iteration, maxIter);
  const pct = Math.min(100, Math.round((displayedIteration / maxIter) * 100));
  const tickets = project.tickets || [];
  const handoffs = (project.handoffs || []).filter((h) => h.status === "open").slice(0, 5);

  const count = (status) => tickets.filter((t) => ticketStatusKey(t.status) === status).length;
  const done = count("done");
  const inProg = count("progress");
  const review = count("review");
  const blocked = count("blocked");
  const ready = count("ready");

  panel.innerHTML = `
    <div class="progress-section">
      <div class="section-title">에이전트 루프</div>
      <div class="iteration-track">
        <div class="iteration-fill" style="width:${pct}%"></div>
      </div>
      <div class="iteration-label">
        <span>Step ${displayedIteration} / ${maxIter}</span>
        <span>${pct}%</span>
      </div>
    </div>

    <div class="progress-section">
      <div class="section-title">티켓 현황</div>
      ${
        tickets.length
          ? `<div class="ticket-stats">
          ${done ? `<span class="badge badge-done">✓ ${done} 완료</span>` : ""}
          ${inProg ? `<span class="badge badge-progress">↻ ${inProg} 진행중</span>` : ""}
          ${review ? `<span class="badge badge-review">◌ ${review} 리뷰</span>` : ""}
          ${blocked ? `<span class="badge badge-blocked">✗ ${blocked} 블록됨</span>` : ""}
          ${ready ? `<span class="badge badge-todo">… ${ready} 대기</span>` : ""}
        </div>
        <div class="ticket-list">
          ${tickets
            .map(
              (t) => `
            <div class="ticket-item" data-status="${ticketStatusKey(t.status)}">
              <span class="ticket-dot"></span>
              <span class="ticket-copy">
                <span class="ticket-name">${escapeHtml(t.title)}</span>
                ${ticketDetail(t)}
              </span>
              <span class="ticket-owner-tag">${t.owner || ""}</span>
            </div>`,
            )
            .join("")}
        </div>`
          : `<p class="empty-state">아직 티켓이 없습니다.</p>`
      }
    </div>

    ${
      handoffs.length
        ? `<div class="progress-section">
        <div class="section-title">진행 중인 핸드오프</div>
        <div class="handoff-list">
          ${handoffs
            .map(
              (h) => `
            <div class="handoff-item">
              <span class="handoff-arrow">${h.from} → ${h.to}</span>
              <span class="handoff-msg">${escapeHtml(h.message || "")}</span>
            </div>`,
            )
            .join("")}
        </div>
      </div>`
        : ""
    }

    ${
      project.pending_decisions?.length
        ? `<div class="progress-section">
        <div class="section-title">승인 대기 중</div>
        ${project.pending_decisions
          .map(
            (d) => `
          <div class="ticket-item" data-status="blocked">
            <span class="ticket-dot"></span>
            <span class="ticket-copy">
              <span class="ticket-name">${escapeHtml(d.question || "승인 필요")}</span>
            </span>
            <span class="ticket-owner-tag">${d.agent || ""}</span>
          </div>`,
          )
          .join("")}
      </div>`
        : ""
    }
  `;
}

// ─── Files panel ─────────────────────────────────────────────────────────────

async function renderFiles(project) {
  const panel = $("#filesPanel");
  if (!project) {
    panel.innerHTML = `<p class="empty-state">프로젝트를 선택하세요.</p>`;
    state.filesPanelProjectId = null;
    return;
  }

  if (state.filesPanelProjectId === project.id && panel.dataset.ready === "true") return;

  if (state.filesPanelProjectId !== project.id) {
    state.searchQuery = "";
    state.openFilePath = null;
    state.openFileContent = "";
    state.fileTreeScrollTop = 0;
    state.fileViewerScrollTop = 0;
  }

  state.filesPanelProjectId = project.id;
  panel.dataset.projectId = project.id;
  panel.dataset.ready = "true";
  const currentQuery = state.searchQuery;

  panel.innerHTML = `
    <div class="file-search-bar">
      <input class="file-search-input" type="search" placeholder="파일 내 텍스트 검색 (2자 이상)..." value="${escapeHtml(currentQuery)}" />
    </div>
    <div class="file-content-area"></div>
    <div class="file-viewer" id="fileViewer" hidden>
      <div class="file-viewer-header">
        <span class="file-viewer-path"></span>
        <button class="file-viewer-close" type="button" aria-label="닫기">✕</button>
      </div>
      <pre class="file-viewer-content"></pre>
    </div>
  `;

  const searchInput = panel.querySelector(".file-search-input");
  const contentArea = panel.querySelector(".file-content-area");
  const viewer = panel.querySelector("#fileViewer");
  const tabPanel = $("#tab-files");

  if (tabPanel) {
    tabPanel.onscroll = () => {
      state.fileTreeScrollTop = tabPanel.scrollTop;
    };
  }

  searchInput.addEventListener("input", () => {
    state.searchQuery = searchInput.value;
    clearTimeout(searchTimer);
    if (!state.searchQuery.trim() || state.searchQuery.trim().length < 2) {
      loadFileTree(project, contentArea, viewer);
    } else {
      contentArea.innerHTML = `<p class="loading-text">검색 중...</p>`;
      searchTimer = setTimeout(() => doSearch(project.id, state.searchQuery, contentArea, viewer), 400);
    }
  });

  panel.querySelector(".file-viewer-close").addEventListener("click", () => {
    viewer.hidden = true;
    state.openFilePath = null;
    state.openFileContent = "";
    state.fileViewerScrollTop = 0;
  });

  viewer.querySelector(".file-viewer-content").addEventListener("scroll", (event) => {
    state.fileViewerScrollTop = event.currentTarget.scrollTop;
  }, { passive: true });

  if (currentQuery && currentQuery.trim().length >= 2) {
    contentArea.innerHTML = `<p class="loading-text">검색 중...</p>`;
    await doSearch(project.id, currentQuery, contentArea, viewer);
  } else {
    await loadFileTree(project, contentArea, viewer);
  }
}

async function loadFileTree(project, contentArea, viewer, showLoading = true) {
  const tabPanel = $("#tab-files");
  const previousTreeScrollTop = tabPanel?.scrollTop || state.fileTreeScrollTop || 0;
  const viewerContent = viewer.querySelector(".file-viewer-content");
  const previousViewerScrollTop = viewerContent?.scrollTop || state.fileViewerScrollTop || 0;

  if (showLoading) contentArea.innerHTML = `<p class="loading-text">파일 목록 로딩 중...</p>`;
  try {
    const data = await api(`/api/projects/${project.id}/files`);
    const root = data.root || "";
    const files = data.files || [];
    const allFiles = flattenTree(files);
    const recentItems = [...allFiles].sort((a, b) => b.mtime - a.mtime).slice(0, 5);

    const recentHtml = recentItems.length ? `
      <div class="section-title" style="margin-bottom:8px">최근 수정</div>
      <div class="recent-files-list">
        ${recentItems.map(f => `
          <div class="recent-file-item" data-path="${escapeHtml(f.path)}">
            <span class="recent-file-name">${escapeHtml(f.path.split("/").pop())}</span>
            <span class="recent-file-time">${formatRelTime(f.mtime)}</span>
          </div>
        `).join("")}
      </div>
      <div class="section-title" style="margin:14px 0 8px">파일 트리</div>
    ` : "";

    contentArea.innerHTML = `
      <div class="file-root-path">${escapeHtml(root)}</div>
      ${recentHtml}
      <div class="file-tree">${renderCollapsibleTree(files, 0, "", project.id)}</div>
    `;

    contentArea.querySelectorAll("[data-dir-path]").forEach(el => {
      el.addEventListener("click", () => {
        const collapsed = collapsedDirsForProject(project.id);
        const path = el.dataset.dirPath;
        collapsed[path] = !(collapsed[path] ?? isDefaultCollapsedDir(path.split("/").pop()));
        loadFileTree(project, contentArea, viewer, false);
      });
    });

    contentArea.querySelectorAll("[data-path]").forEach(el => {
      el.addEventListener("click", () => viewFile(project.id, el.dataset.path, viewer));
    });

    if (tabPanel) tabPanel.scrollTop = previousTreeScrollTop;
  } catch {
    contentArea.innerHTML = `<p class="empty-state">파일 목록을 가져올 수 없습니다.</p>`;
  }

  if (state.openFilePath) {
    viewer.hidden = false;
    viewer.querySelector(".file-viewer-path").textContent = state.openFilePath;
    viewer.querySelector(".file-viewer-content").textContent = state.openFileContent;
    viewer.querySelector(".file-viewer-content").scrollTop = previousViewerScrollTop;
  }
}

async function doSearch(projectId, query, contentArea, viewer) {
  try {
    const data = await api(`/api/projects/${projectId}/search?q=${encodeURIComponent(query)}`);
    renderSearchResults(contentArea, data.results || [], query, projectId, viewer);
  } catch (err) {
    contentArea.innerHTML = `<p class="empty-state">검색 오류: ${escapeHtml(err.message)}</p>`;
  }
}

function renderSearchResults(contentArea, results, query, projectId, viewer) {
  if (!results.length) {
    contentArea.innerHTML = `<p class="empty-state">"${escapeHtml(query)}" 검색 결과 없음</p>`;
    return;
  }
  contentArea.innerHTML = `
    <div class="search-result-header">${results.length}개 파일에서 발견</div>
    ${results.map(r => `
      <div class="search-result">
        <button class="search-result-file" data-path="${escapeHtml(r.file)}">${escapeHtml(r.file)}</button>
        ${r.matches.map(m => `
          <div class="search-result-match">
            <span class="match-line">${m.line}</span>
            <span class="match-text">${escapeHtml(m.text)}</span>
          </div>
        `).join("")}
      </div>
    `).join("")}
  `;
  contentArea.querySelectorAll(".search-result-file").forEach(el => {
    el.addEventListener("click", () => viewFile(projectId, el.dataset.path, viewer));
  });
}

async function viewFile(projectId, path, viewer) {
  state.openFilePath = path;
  state.openFileContent = "로딩 중...";
  state.fileViewerScrollTop = 0;
  viewer.hidden = false;
  viewer.querySelector(".file-viewer-path").textContent = path;
  viewer.querySelector(".file-viewer-content").textContent = "로딩 중...";
  viewer.querySelector(".file-viewer-content").scrollTop = 0;
  viewer.scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const data = await api(`/api/projects/${projectId}/file?path=${encodeURIComponent(path)}`);
    state.openFileContent = data.content || "(빈 파일)";
    viewer.querySelector(".file-viewer-content").textContent = state.openFileContent;
  } catch (err) {
    state.openFileContent = `오류: ${err.message}`;
    viewer.querySelector(".file-viewer-content").textContent = state.openFileContent;
  }
}

function renderCollapsibleTree(nodes, depth, pathPrefix, projectId) {
  if (!nodes?.length) return "";
  const indent = depth * 14;
  const collapsed = collapsedDirsForProject(projectId);
  return nodes
    .map((node) => {
      const isDir = node.type === "dir";
      const fullPath = pathPrefix ? `${pathPrefix}/${node.name}` : node.name;
      const isCollapsed = isDir ? (collapsed[fullPath] ?? isDefaultCollapsedDir(node.name)) : false;
      const icon = isDir ? (isCollapsed ? "▸" : "▾") : "•";
      const nameClass = isDir ? "tree-name is-dir" : "tree-name";
      const sizeHtml = !isDir && node.size ? `<span class="tree-size">${formatSize(node.size)}</span>` : "";
      const pathAttr = isDir
        ? `data-dir-path="${escapeHtml(fullPath)}" aria-expanded="${String(!isCollapsed)}"`
        : `data-path="${escapeHtml(fullPath)}"`;
      const children = isDir && !isCollapsed && node.children
        ? renderCollapsibleTree(node.children, depth + 1, fullPath, projectId)
        : "";

      return `<div class="tree-node is-clickable" style="padding-left:${indent}px" ${pathAttr}>
        <span class="tree-icon">${icon}</span>
        <span class="${nameClass}">${escapeHtml(node.name)}</span>
        ${sizeHtml}
      </div>${children}`;
    })
    .join("");
}

function renderTree(nodes, depth, pathPrefix) {
  if (!nodes?.length) return "";
  const indent = depth * 14;
  return nodes
    .map((node) => {
      const isDir = node.type === "dir";
      const fullPath = pathPrefix ? `${pathPrefix}/${node.name}` : node.name;
      const icon = isDir ? "▸" : "·";
      const nameClass = isDir ? "tree-name is-dir" : "tree-name";
      const sizeHtml = !isDir && node.size ? `<span class="tree-size">${formatSize(node.size)}</span>` : "";
      const pathAttr = !isDir ? `data-path="${escapeHtml(fullPath)}"` : "";
      const clickClass = !isDir ? " is-clickable" : "";
      const children = isDir && node.children ? renderTree(node.children, depth + 1, fullPath) : "";
      return `<div class="tree-node${clickClass}" style="padding-left:${indent}px" ${pathAttr}>
        <span class="tree-icon">${icon}</span>
        <span class="${nameClass}">${escapeHtml(node.name)}</span>
        ${sizeHtml}
      </div>${children}`;
    })
    .join("");
}

// ─── Tab switching ────────────────────────────────────────────────────────────

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.tab === tab);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== `tab-${tab}`;
  });
  if (tab === "files") renderFiles(activeProject());
}

// ─── Modals ───────────────────────────────────────────────────────────────────

function decisionAllowsColors(decision) {
  if (!decision) return false;
  if (decision.allow_colors || decision.requires_colors || decision.color_required) return true;
  return (decision.options || []).some((option) => normalizeColors(option.colors).length > 0);
}

function normalizeColors(colors) {
  if (!Array.isArray(colors)) return [];
  return colors
    .map((color) => String(color || "").trim())
    .filter((color) => /^#[0-9a-f]{6}$/i.test(color));
}

function setCustomPanelVisibility(choiceId, allowsColors) {
  const isCustom = choiceId === "custom";
  $("#customPanel").hidden = !isCustom;
  $("#customColorPanel").hidden = !isCustom || !allowsColors;
}

function selectedCustomColors(decision) {
  if (state.selectedChoiceId !== "custom" || !decisionAllowsColors(decision)) return [];
  return ["#customColorOne", "#customColorTwo", "#customColorThree"]
    .map((selector) => $(selector)?.value)
    .filter(Boolean);
}

function renderChoices(decision) {
  const template = $("#choiceTemplate");
  const grid = $("#choiceGrid");
  const allowsColors = decisionAllowsColors(decision);
  const options = [...(decision?.options || [])];
  if (!options.some((o) => o.id === "custom")) {
    options.push({
      id: "custom",
      title: "직접 입력",
      description: "PO가 조건을 직접 지정합니다.",
      recommendation: "Manual",
    });
  }

  state.selectedChoiceId = options[0]?.id || "custom";
  $("#decisionQuestion").textContent = decision?.question || "PO 승인이 필요합니다.";
  setCustomPanelVisibility(state.selectedChoiceId, allowsColors);
  grid.innerHTML = "";

  options.forEach((choice) => {
    const node = template.content.firstElementChild.cloneNode(true);
    const colors = normalizeColors(choice.colors);
    const swatches = node.querySelector(".swatches");
    node.dataset.choice = choice.id;
    node.setAttribute("aria-pressed", String(choice.id === state.selectedChoiceId));
    node.querySelector(".recommendation").textContent = choice.recommendation || "";
    node.querySelector("strong").textContent = choice.title || choice.id;
    node.querySelector("small").textContent = choice.description || "";
    swatches.hidden = colors.length === 0;
    swatches.innerHTML = colors
      .slice(0, 3)
      .map((color) => `<i style="background:${color}"></i>`)
      .join("");
    node.addEventListener("click", () => {
      state.selectedChoiceId = choice.id;
      setCustomPanelVisibility(choice.id, allowsColors);
      document.querySelectorAll(".choice-card").forEach((card) => {
        card.setAttribute("aria-pressed", String(card.dataset.choice === choice.id));
      });
    });
    grid.appendChild(node);
  });
}

async function handleCreateProject(event) {
  event.preventDefault();
  const name = $("#projectNameInput").value.trim();
  const vision = $("#projectVisionInput").value.trim();
  if (!name || !vision) return;

  const btn = $("#projectSubmitButton");
  btn.disabled = true;
  $("#projectModalHint").textContent = "Gemini 팀을 시작하는 중입니다...";
  try {
    const project = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, vision }),
    });
    state.activeProjectId = project.id;
    closeDialog($("#projectModal"));
    await refreshProjects();
  } catch (error) {
    $("#projectModalHint").textContent = error.message;
  } finally {
    btn.disabled = false;
  }
}

async function handleApproveDecision(event) {
  event.preventDefault();
  const project = activeProject();
  const decision = state.selectedDecision;
  if (!project || !decision) return;

  const note = $("#customPrompt").value.trim();
  const customColors = selectedCustomColors(decision);
  const btn = $("#confirmDecisionButton");
  btn.disabled = true;
  try {
    await api(`/api/projects/${project.id}/approve`, {
      method: "POST",
      body: JSON.stringify({
        decision_id: decision.id,
        choice_id: state.selectedChoiceId,
        note,
        custom_colors: customColors,
      }),
    });
    closeDialog($("#decisionModal"));
    state.deferredDecisionIds.delete(decision.id);
    state.selectedDecision = null;
    await refreshProjects();
  } finally {
    btn.disabled = false;
  }
}

async function handleStopResume() {
  const project = activeProject();
  if (!project) return;
  const btn = $("#stopResumeButton");
  btn.disabled = true;
  try {
    if (isStoppedProject(project)) {
      await api(`/api/projects/${project.id}/resume`, { method: "POST" });
    } else {
      btn.textContent = "작업 중단 중...";
      await api(`/api/projects/${project.id}/stop`, { method: "POST" });
    }
    await refreshProjects();
  } finally {
    renderStopResumeButton(activeProject());
  }
}

async function handlePoMessage(event) {
  event.preventDefault();
  const project = activeProject();
  const input = $("#poMessageInput");
  const message = input.value.trim();
  if (!project || !message) return;
  const button = $("#poMessageButton");
  button.disabled = true;
  try {
    const updated = await api(`/api/projects/${project.id}/messages`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    const lastChat = (updated.pm_chat || []).at(-1);
    input.value = "";
    await refreshProjects();
    if (lastChat?.from === "PM") switchTab("chat");
  } finally {
    renderPoMessageForm(activeProject());
  }
}

function openDecision() {
  const project = activeProject();
  const decision = project?.pending_decisions?.[0];
  if (!decision) return;
  state.selectedDecision = decision;
  $("#customPrompt").value = "";
  renderChoices(decision);
  openDialog($("#decisionModal"));
}

function openProjectModal() {
  if (!state.health?.gemini_ready) return;
  $("#projectModalHint").textContent = "Gemini가 없으면 프로젝트를 시작할 수 없습니다.";
  openDialog($("#projectModal"));
}

function openDialog(dialog) {
  if (!dialog.open) dialog.showModal();
}

function closeDialog(dialog) {
  if (dialog.open) dialog.close();
}

// ─── Canvas ───────────────────────────────────────────────────────────────────

function drawGraph(active, iteration) {
  const canvas = $("#thinkGraph");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(400, Math.floor(rect.width * dpr));
  canvas.height = Math.max(200, Math.floor(rect.height * dpr));

  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const w = rect.width;
  const h = rect.height;

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#050706";
  ctx.fillRect(0, 0, w, h);

  ctx.strokeStyle = "rgba(217,255,139,0.035)";
  ctx.lineWidth = 1;
  for (let x = 0; x < w; x += 40) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
  }
  for (let y = 0; y < h; y += 40) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }

  const nodes = makeNodes(w, h, active ? 11 + iteration : 3);
  nodes.forEach((node, i) => {
    for (let j = i + 1; j < nodes.length; j++) {
      const other = nodes[j];
      const dist = Math.hypot(node.x - other.x, node.y - other.y);
      if (dist < 160) {
        ctx.strokeStyle = `rgba(19,220,237,${active ? 0.14 - dist / 1400 : 0.04})`;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(node.x, node.y); ctx.lineTo(other.x, other.y); ctx.stroke();
      }
    }
  });

  nodes.forEach((node) => {
    ctx.beginPath();
    ctx.fillStyle = node.root ? "#d9ff8b" : "#13dced";
    ctx.globalAlpha = active ? (node.root ? 0.9 : 0.55) : 0.15;
    ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.globalAlpha = 1;

  drawLabel(ctx, active ? "PM" : "Standby",     w * 0.22, h * 0.36);
  drawLabel(ctx, active ? "Designer" : "—",     w * 0.55, h * 0.22);
  drawLabel(ctx, active ? "QA Loop" : "—",      w * 0.68, h * 0.66);
}

function makeNodes(width, height, seed) {
  const nodes = [];
  for (let i = 0; i < 78; i++) {
    const angle = (i * 2.399 + seed) % (Math.PI * 2);
    const spread = 0.18 + ((i * 29 + seed) % 100) / 100 * 0.45;
    nodes.push({
      x: Math.max(20, Math.min(width - 20, width * 0.5 + Math.cos(angle) * width * spread)),
      y: Math.max(20, Math.min(height - 20, height * 0.5 + Math.sin(angle) * height * spread * 0.7)),
      r: i % 17 === 0 ? 8 : i % 9 === 0 ? 5 : 3,
      root: i % 17 === 0,
    });
  }
  return nodes;
}

function drawLabel(ctx, text, x, y) {
  if (text === "—") return;
  const textW = ctx.measureText(text).width + 20;
  ctx.fillStyle = "rgba(4,6,5,0.88)";
  ctx.strokeStyle = "rgba(217,255,139,0.2)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(x - 4, y - 18, textW, 30, 4);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = "rgba(240,242,237,0.78)";
  ctx.font = "11px Cascadia Mono, Consolas, monospace";
  ctx.fillText(text, x + 6, y + 4);
}

// ─── Utilities ────────────────────────────────────────────────────────────────

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ─── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  $("#newProjectButton").addEventListener("click", openProjectModal);
  $("#projectForm").addEventListener("submit", handleCreateProject);
  $("#cancelProjectButton").addEventListener("click", () => closeDialog($("#projectModal")));
  $("#closeProjectModal").addEventListener("click", () => closeDialog($("#projectModal")));
  $("#openDecisionButton").addEventListener("click", openDecision);
  $("#decisionForm").addEventListener("submit", handleApproveDecision);
  $("#closeDecisionModal").addEventListener("click", () => closeDialog($("#decisionModal")));
  $("#deferDecisionButton").addEventListener("click", () => {
    if (state.selectedDecision?.id) state.deferredDecisionIds.add(state.selectedDecision.id);
    closeDialog($("#decisionModal"));
  });
  $("#stopResumeButton").addEventListener("click", handleStopResume);
  $("#poMessageForm").addEventListener("submit", handlePoMessage);
  $("#poMessageInput").addEventListener("input", () => renderPoMessageForm(activeProject()));
  window.addEventListener("resize", () => drawGraph(Boolean(activeProject()), activeProject()?.iteration || 0));

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  const ready = await refreshHealth();
  await refreshProjects();
  if (ready && !state.projects.length) openDialog($("#projectModal"));

  state.polling = setInterval(async () => {
    try {
      await refreshHealth();
      await refreshProjects();
    } catch {
      // Keep last visible state if server is briefly busy.
    }
  }, 2500);
}

init();
