const $ = (selector) => document.querySelector(selector);

let state = {
  health: null,
  projects: [],
  activeProjectId: null,
  selectedDecision: null,
  selectedChoiceId: null,
  activeTab: "logs",
  polling: null,
};

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
    waiting_for_approval: "승인 대기",
    complete: "완료",
    error: "오류",
  };
  return map[status] || status || "대기";
}

function renderWarRoom() {
  const project = activeProject();
  const pending = project?.pending_decisions?.[0] || null;

  $("#projectTitle").textContent = project?.name || "AI Agent War Room";
  $("#projectStatusLabel").textContent = project?.status?.toUpperCase() || "STANDBY";
  $("#rootGoalLabel").textContent = project?.vision || "프로젝트를 생성하면 워룸이 시작됩니다.";
  $("#projectDirectoryLabel").textContent = project ? shortPath(project.directory) : "no project directory";
  $("#openDecisionButton").hidden = !pending;
  $("#openDecisionButton").disabled = !pending;

  const agents = project?.agents || {};
  setAgent("pm", agents.pm);
  setAgent("designer", agents.designer);
  setAgent("backend", agents.backend);
  setAgent("frontend", agents.frontend);
  setAgent("qa", agents.qa);

  renderActivity(project);
  renderProgress(project);

  if (state.activeTab === "files") renderFiles(project);

  drawGraph(Boolean(project), project?.iteration || 0);
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

// ─── Progress panel ──────────────────────────────────────────────────────────

function renderProgress(project) {
  const panel = $("#progressPanel");
  if (!project) {
    panel.innerHTML = `<p class="empty-state">프로젝트를 선택하거나 생성하세요.</p>`;
    return;
  }

  const iteration = project.iteration || 0;
  const maxIter = 18;
  const pct = Math.round((iteration / maxIter) * 100);
  const tickets = project.tickets || [];
  const handoffs = (project.handoffs || []).filter((h) => h.status === "open").slice(0, 5);

  const done = tickets.filter((t) => t.status === "done").length;
  const inProg = tickets.filter((t) => t.status === "in_progress").length;
  const blocked = tickets.filter((t) => t.status === "blocked").length;
  const todo = tickets.filter((t) => t.status === "todo").length;

  panel.innerHTML = `
    <div class="progress-section">
      <div class="section-title">에이전트 루프</div>
      <div class="iteration-track">
        <div class="iteration-fill" style="width:${pct}%"></div>
      </div>
      <div class="iteration-label">
        <span>Step ${iteration} / ${maxIter}</span>
        <span>${pct}%</span>
      </div>
    </div>

    <div class="progress-section">
      <div class="section-title">티켓 현황</div>
      ${
        tickets.length
          ? `<div class="ticket-stats">
          ${done ? `<span class="badge badge-done">✓ ${done} 완료</span>` : ""}
          ${inProg ? `<span class="badge badge-progress">▶ ${inProg} 진행중</span>` : ""}
          ${blocked ? `<span class="badge badge-blocked">✗ ${blocked} 블록됨</span>` : ""}
          ${todo ? `<span class="badge badge-todo">◦ ${todo} 대기</span>` : ""}
        </div>
        <div class="ticket-list">
          ${tickets
            .map(
              (t) => `
            <div class="ticket-item" data-status="${t.status}">
              <span class="ticket-dot"></span>
              <span class="ticket-name">${escapeHtml(t.title)}</span>
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
            <span class="ticket-name">${escapeHtml(d.question || "승인 필요")}</span>
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
    return;
  }

  panel.innerHTML = `<p class="loading-text">파일 목록 로딩 중...</p>`;

  try {
    const data = await api(`/api/projects/${project.id}/files`);
    const root = data.root || "";
    const files = data.files || [];

    panel.innerHTML = `
      <div class="file-root-path">${escapeHtml(root)}</div>
      <div class="file-tree">${renderTree(files, 0)}</div>
    `;
  } catch (err) {
    panel.innerHTML = `<p class="empty-state">파일 목록을 가져올 수 없습니다.</p>`;
  }
}

function renderTree(nodes, depth) {
  if (!nodes?.length) return "";
  const indent = depth * 14;
  return nodes
    .map((node) => {
      const isDir = node.type === "dir";
      const icon = isDir ? "▸" : "·";
      const nameClass = isDir ? "tree-name is-dir" : "tree-name";
      const children = isDir && node.children ? renderTree(node.children, depth + 1) : "";
      return `<div class="tree-node" style="padding-left:${indent}px">
        <span class="tree-icon">${icon}</span>
        <span class="${nameClass}">${escapeHtml(node.name)}</span>
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

function renderChoices(decision) {
  const template = $("#choiceTemplate");
  const grid = $("#choiceGrid");
  const options = [...(decision?.options || [])];
  if (!options.some((o) => o.id === "custom")) {
    options.push({
      id: "custom",
      title: "직접 입력",
      description: "PO가 조건을 직접 지정합니다.",
      recommendation: "Manual",
      colors: ["#050706", "#13dced", "#d9ff8b"],
    });
  }

  state.selectedChoiceId = options[0]?.id || "custom";
  $("#decisionQuestion").textContent = decision?.question || "PO 승인이 필요합니다.";
  $("#customPanel").hidden = state.selectedChoiceId !== "custom";
  grid.innerHTML = "";

  options.forEach((choice) => {
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.choice = choice.id;
    node.setAttribute("aria-pressed", String(choice.id === state.selectedChoiceId));
    node.querySelector(".recommendation").textContent = choice.recommendation || "";
    node.querySelector("strong").textContent = choice.title || choice.id;
    node.querySelector("small").textContent = choice.description || "";
    node.querySelector(".swatches").innerHTML = (choice.colors || ["#050706", "#13dced", "#d9ff8b"])
      .slice(0, 3)
      .map((color) => `<i style="background:${color}"></i>`)
      .join("");
    node.addEventListener("click", () => {
      state.selectedChoiceId = choice.id;
      $("#customPanel").hidden = choice.id !== "custom";
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
  const btn = $("#confirmDecisionButton");
  btn.disabled = true;
  try {
    await api(`/api/projects/${project.id}/approve`, {
      method: "POST",
      body: JSON.stringify({ decision_id: decision.id, choice_id: state.selectedChoiceId, note }),
    });
    closeDialog($("#decisionModal"));
    state.selectedDecision = null;
    await refreshProjects();
  } finally {
    btn.disabled = false;
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
  $("#deferDecisionButton").addEventListener("click", () => closeDialog($("#decisionModal")));
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
