const $ = (selector) => document.querySelector(selector);

let state = {
  health: null,
  projects: [],
  activeProjectId: null,
  selectedDecision: null,
  selectedChoiceId: null,
  polling: null,
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Request failed: ${response.status}`);
  }
  return data;
}

async function refreshHealth() {
  state.health = await api("/api/health");
  const ready = Boolean(state.health.gemini_ready);
  $("#apiStatusLabel").textContent = ready ? "Gemini Ready" : "Gemini Required";
  $("#newProjectButton").disabled = !ready;
  $("#backendLock").hidden = ready;
  $("#backendLockReason").textContent = state.health.reason || "";
  return ready;
}

async function refreshProjects() {
  const data = await api("/api/projects");
  state.projects = data.projects || [];
  if (!state.activeProjectId || !state.projects.some((project) => project.id === state.activeProjectId)) {
    state.activeProjectId = state.projects[0]?.id || null;
  }
  render();
}

function activeProject() {
  return state.projects.find((project) => project.id === state.activeProjectId) || null;
}

function shortPath(path) {
  if (!path) return "";
  const normalized = path.replaceAll("\\", "/");
  const parts = normalized.split("/");
  return parts.slice(-2).join("/");
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
    item.querySelector("small").textContent = `${project.status} · ${project.slug}`;
    item.addEventListener("click", () => {
      state.activeProjectId = project.id;
      render();
    });
    list.appendChild(item);
  });
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

function renderActivity(project) {
  const feed = $("#activityFeed");
  const logs = project?.logs?.length
    ? project.logs
    : [{ agent: "System", message: "새 프로젝트를 생성하면 에이전트가 실제 폴더에서 작업을 시작합니다." }];
  feed.innerHTML = logs
    .slice(-6)
    .reverse()
    .map((log) => `<li data-level="${log.level || "info"}"><strong>${log.agent}</strong><span>${log.message}</span></li>`)
    .join("");
}

function renderChoices(decision) {
  const template = $("#choiceTemplate");
  const grid = $("#choiceGrid");
  const options = [...(decision?.options || [])];
  if (!options.some((option) => option.id === "custom")) {
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

  $("#projectSubmitButton").disabled = true;
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
    $("#projectSubmitButton").disabled = false;
  }
}

async function handleApproveDecision(event) {
  event.preventDefault();
  const project = activeProject();
  const decision = state.selectedDecision;
  if (!project || !decision) return;

  const note = $("#customPrompt").value.trim();
  $("#confirmDecisionButton").disabled = true;
  try {
    await api(`/api/projects/${project.id}/approve`, {
      method: "POST",
      body: JSON.stringify({
        decision_id: decision.id,
        choice_id: state.selectedChoiceId,
        note,
      }),
    });
    closeDialog($("#decisionModal"));
    state.selectedDecision = null;
    await refreshProjects();
  } finally {
    $("#confirmDecisionButton").disabled = false;
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

function render() {
  renderProjectList();
  renderWarRoom();
}

function drawGraph(active, iteration) {
  const canvas = $("#thinkGraph");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(720, Math.floor(rect.width * dpr));
  canvas.height = Math.max(440, Math.floor(rect.height * dpr));

  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const width = rect.width;
  const height = rect.height;
  const styles = getComputedStyle(document.documentElement);
  const accent = styles.getPropertyValue("--accent").trim();
  const signal = styles.getPropertyValue("--signal").trim();

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#050706";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "rgba(217,255,139,0.045)";
  for (let x = 0; x < width; x += 42) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }
  for (let y = 0; y < height; y += 42) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }

  const nodes = makeNodes(width, height, active ? 11 + iteration : 3);
  nodes.forEach((node, index) => {
    for (let next = index + 1; next < nodes.length; next += 1) {
      const other = nodes[next];
      const distance = Math.hypot(node.x - other.x, node.y - other.y);
      if (distance < 170) {
        ctx.strokeStyle = `rgba(19,220,237,${active ? 0.16 - distance / 1300 : 0.05})`;
        ctx.beginPath();
        ctx.moveTo(node.x, node.y);
        ctx.lineTo(other.x, other.y);
        ctx.stroke();
      }
    }
  });

  nodes.forEach((node) => {
    ctx.beginPath();
    ctx.fillStyle = node.root ? accent : signal;
    ctx.globalAlpha = active ? (node.root ? 0.96 : 0.62) : 0.18;
    ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.globalAlpha = 1;

  drawLabel(ctx, active ? "PM" : "Standby", width * 0.22, height * 0.36);
  drawLabel(ctx, active ? "Designer" : "No Project", width * 0.55, height * 0.2);
  drawLabel(ctx, active ? "QA Loop" : "Await Input", width * 0.68, height * 0.68);
}

function makeNodes(width, height, seed) {
  const nodes = [];
  for (let i = 0; i < 78; i += 1) {
    const angle = (i * 2.399 + seed) % (Math.PI * 2);
    const spread = 0.18 + ((i * 29 + seed) % 100) / 100 * 0.45;
    nodes.push({
      x: Math.max(28, Math.min(width - 28, width * 0.5 + Math.cos(angle) * width * spread)),
      y: Math.max(28, Math.min(height - 28, height * 0.5 + Math.sin(angle) * height * spread * 0.7)),
      r: i % 17 === 0 ? 9 : i % 9 === 0 ? 6 : 3,
      root: i % 17 === 0,
    });
  }
  return nodes;
}

function drawLabel(ctx, text, x, y) {
  ctx.fillStyle = "rgba(5,7,6,0.86)";
  ctx.strokeStyle = "rgba(217,255,139,0.24)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(x - 8, y - 20, 118, 38, 5);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = "rgba(244,246,241,0.82)";
  ctx.font = "12px Cascadia Mono, Consolas, monospace";
  ctx.fillText(text, x + 4, y + 4);
}

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

  const ready = await refreshHealth();
  await refreshProjects();
  if (ready && !state.projects.length) {
    openDialog($("#projectModal"));
  }
  state.polling = setInterval(async () => {
    try {
      await refreshHealth();
      await refreshProjects();
    } catch {
      // Keep the last visible state if the server is briefly busy.
    }
  }, 2500);
}

init();
