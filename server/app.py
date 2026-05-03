from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from gemini_retry_handler import GeminiRetryHandler


ROOT = Path(__file__).resolve().parents[1]
PROJECTS_DIR = ROOT / "projects"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "4173"))
MAX_AGENT_STEPS = int(os.environ.get("WARROOM_MAX_AGENT_STEPS", "18"))
COMMAND_TIMEOUT = int(os.environ.get("WARROOM_COMMAND_TIMEOUT", "90"))
RUNNER_STALE_SECONDS = int(os.environ.get("WARROOM_RUNNER_STALE_SECONDS", str(max(180, COMMAND_TIMEOUT + 60))))
PREVIEW_START_TIMEOUT = int(os.environ.get("WARROOM_PREVIEW_START_TIMEOUT", "25"))
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
IGNORED_PROJECT_DIRS = {".warroom", ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "coverage", ".next"}
LOCAL_PREVIEW_HOSTS = {"127.0.0.1", "localhost", "::1"}
FUZZY_EDIT_MIN_SCORE = 0.86
FUZZY_EDIT_MIN_MARGIN = 0.04
FUZZY_EDIT_MIN_EXACT_ANCHOR_LEN = 12
TICKET_STATUS_ALIASES = {
    "todo": "ready",
    "in_progress": "progress",
    "working": "progress",
    "needs_review": "review",
}
TICKET_STATUSES = {"ready", "progress", "review", "blocked", "done"}
STOPPING_STATUSES = {"stopping", "stopped"}
PAUSED_STATUSES = {"waiting_for_approval", "waiting_for_handoff", "complete", "error", "stopped"}
AGENT_SET_PROJECT_STATUSES = {"running", "waiting_for_approval", "complete"}
AGENT_LEVEL_WAIT_STATUSES = {"idle", "ready", "waiting", "waiting_for_agent", "in_progress", "progress"}
RESUME_NOTES_PATH = "docs/RESUME.md"

AGENT_ORDER = ["pm", "designer", "backend", "frontend", "qa"]
AGENTS = {
    "pm": {
        "label": "PM",
        "role": "Product Manager",
        "mission": "Turn PO vision into PRD, backlog, acceptance criteria, and sequencing.",
        "responsibilities": [
            "Write and maintain docs/PRD.md with goals, personas, scope, non-goals, risks, and success metrics.",
            "Split work into tickets with owner, priority, dependencies, and acceptance criteria.",
            "Keep docs/backlog.md and project_context.md aligned with decisions and implementation reality.",
            "Clarify product ambiguity with PO approval only when it changes product direction.",
        ],
        "artifacts": ["docs/PRD.md", "docs/backlog.md", "project_context.md"],
        "collaboration": [
            "Hand off scoped tickets to Designer, Backend, Frontend, and QA.",
            "Read QA/design/backend/frontend feedback and resequence the backlog when needed.",
        ],
    },
    "designer": {
        "label": "Designer",
        "role": "Product Designer",
        "mission": "Define UX, screens, design decisions, and visual review feedback.",
        "responsibilities": [
            "Write docs/design_brief.md with information architecture, flows, interaction states, and visual rules.",
            "Produce code-based wireframes or UI sketches in design/ or the app source when useful.",
            "Review rendered screenshots with capture_preview and record visual consistency findings.",
            "Convert product requirements into concrete UI guidance Frontend can implement without guessing.",
        ],
        "artifacts": ["docs/design_brief.md", "docs/wireframes.md", "design/wireframe.html"],
        "collaboration": [
            "Hand off implementation-ready UI guidance to Frontend.",
            "Send visual defects back to Frontend and product tradeoffs back to PM.",
        ],
    },
    "backend": {
        "label": "Backend",
        "role": "Backend Engineer",
        "mission": "Design data/API contracts and implement server-side logic where needed.",
        "responsibilities": [
            "Write docs/api.md or openapi.yaml with endpoints, request/response contracts, and errors.",
            "Write docs/database.md with schema, persistence strategy, migrations, and data constraints.",
            "Implement business logic, server architecture, validation, security boundaries, and integration seams.",
            "Run backend tests or smoke commands after meaningful changes.",
        ],
        "artifacts": ["docs/api.md", "openapi.yaml", "docs/database.md"],
        "collaboration": [
            "Hand off API contracts to Frontend and testable behavior to QA.",
            "Respond to QA defects with fixes or precise technical blockers.",
        ],
    },
    "frontend": {
        "label": "Frontend",
        "role": "Frontend Engineer",
        "mission": "Implement user-facing UI, connect APIs, and produce runnable previews.",
        "responsibilities": [
            "Implement the UI using the appropriate local stack, such as React, Next.js, Vite, Tailwind, or vanilla code when simpler.",
            "Translate Designer wireframes and design rules into working, responsive components.",
            "Connect Backend API contracts and handle loading, error, empty, and success states.",
            "Run build/lint/tests and provide a preview URL for Designer and QA visual review.",
        ],
        "artifacts": ["package.json", "src", "app", "pages", "components"],
        "collaboration": [
            "Request design review after visible UI changes and QA review after functional changes.",
            "Hand API mismatches to Backend with exact contract details.",
        ],
    },
    "qa": {
        "label": "QA",
        "role": "QA / SET",
        "mission": "Write and run tests, inspect screenshots, report defects, and request fixes.",
        "responsibilities": [
            "Write docs/test_plan.md with test scenarios mapped to PRD acceptance criteria.",
            "Create and run automated tests or smoke scripts using run_command.",
            "Use capture_preview for UI projects and analyze screenshots multimodally for layout and UX defects.",
            "Record defects as tickets and hand them to the responsible role with reproduction steps.",
        ],
        "artifacts": ["docs/test_plan.md", "tests", "e2e", "playwright.config.*"],
        "collaboration": [
            "Review PM acceptance criteria, Designer visual intent, Backend contracts, and Frontend behavior.",
            "Close the loop by re-running tests after fixes and recording pass/fail reviews.",
        ],
    },
}

TEAM_PROTOCOL = [
    "Treat tickets, handoffs, reviews, command results, previews, and project_context.md as shared team memory.",
    "When you produce an artifact another role needs, create a handoff to that role with file paths and the expected next action.",
    "When you find a defect, create or update a ticket, record a review, and hand it to the accountable role.",
    "Before changing another role's contract, read their artifact first and either adapt or hand off a precise mismatch.",
    "If one role is blocked by PO approval, keep all non-blocked work moving.",
    "Do not ask PO for routine engineering choices; make conservative professional decisions and document them.",
    "Ask PO for a choice when a product, UX, brand, or design-direction decision has multiple reasonable outcomes.",
]

state_lock = threading.RLock()
preview_lock = threading.RLock()
runner_threads: dict[str, threading.Thread] = {}
preview_processes: dict[str, subprocess.Popen] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def seconds_since(value: str | None) -> float | None:
    parsed = parse_timestamp(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def resumable_project_status(status: str | None) -> bool:
    return str(status or "") not in PAUSED_STATUSES and str(status or "") not in STOPPING_STATUSES


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return slug[:60] or f"project-{int(time.time())}"


def require_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required. Local fallback is disabled.")
    try:
        from google import genai
    except Exception as exc:
        raise RuntimeError(f"google-genai is required: {exc}") from exc
    return genai.Client(api_key=api_key)


def gemini_status() -> dict:
    try:
        require_gemini_client()
        return {"ready": True, "reason": ""}
    except Exception as exc:
        return {"ready": False, "reason": str(exc)}


def project_root(project_id: str) -> Path:
    for path in PROJECTS_DIR.glob("*"):
        state_path = path / ".warroom" / "state.json"
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("id") == project_id:
                return path
    raise KeyError(f"Project not found: {project_id}")


def state_path(root: Path) -> Path:
    return root / ".warroom" / "state.json"


def load_state(root: Path) -> dict:
    with state_lock:
        return json.loads(state_path(root).read_text(encoding="utf-8"))


def save_state(root: Path, state: dict) -> None:
    with state_lock:
        (root / ".warroom").mkdir(parents=True, exist_ok=True)
        state["updated_at"] = now()
        state_path(root).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(state: dict, agent: str, message: str, level: str = "info") -> None:
    state.setdefault("logs", []).append({
        "time": now(),
        "agent": agent,
        "message": message[:2000],
        "level": level,
    })
    state["logs"] = state["logs"][-300:]


def create_pending_decision(
    state: dict,
    agent_id: str,
    approval: dict,
    *,
    source: str = "agent",
) -> dict:
    agent_label = AGENTS.get(agent_id, {}).get("label", agent_id)
    decision = {
        "id": make_id("decision"),
        "agent": agent_id,
        "question": str(approval.get("question") or "PO approval is required."),
        "options": normalize_decision_options(approval.get("options")),
        "created_at": now(),
        "source": source,
    }
    if approval.get("allow_colors") or approval.get("requires_colors") or approval.get("color_required"):
        decision["allow_colors"] = True
    state.setdefault("pending_decisions", []).append(decision)

    if approval.get("blocks_agent", True) and agent_id in state.get("agents", {}):
        state["agents"][agent_id]["blocked"] = True
        state["agents"][agent_id]["status"] = "blocked"
        state["agents"][agent_id]["message"] = "Waiting for PO approval"
        state["agents"][agent_id]["updated_at"] = now()

    append_log(state, agent_label, f"requested PO approval: {decision['question']}", "approval")
    return decision


def make_id(prefix: str) -> str:
    return f"{prefix}-{time.time_ns()}"


def bullet_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def normalize_string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(value)]


def normalize_ticket_status(value) -> str:
    status = str(value or "ready").strip().lower()
    status = TICKET_STATUS_ALIASES.get(status, status)
    return status if status in TICKET_STATUSES else "ready"


def normalize_decision_options(value) -> list[dict]:
    options = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict):
                option = dict(item)
            else:
                option = {"title": str(item)}
            option["id"] = str(option.get("id") or f"option-{index + 1}")
            option["title"] = str(option.get("title") or option["id"])
            option["description"] = str(option.get("description") or "")
            option["colors"] = normalize_colors(option.get("colors"))
            if option.get("recommendation") is not None:
                option["recommendation"] = str(option.get("recommendation"))
            options.append(option)
    return options


def normalize_colors(value) -> list[str]:
    if not isinstance(value, list):
        return []
    colors = []
    for item in value:
        color = str(item or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            colors.append(color)
    return colors[:6]


def unresolved_handoff(handoff: dict) -> bool:
    return handoff.get("status") not in {"resolved", "closed"}


def handoff_visible_to(handoff: dict, agent_id: str) -> bool:
    return handoff.get("to") in {agent_id, "all"} or handoff.get("from") == agent_id


def project_completion_blockers(state: dict) -> list[str]:
    open_tickets = [
        ticket for ticket in state.get("tickets", [])
        if normalize_ticket_status(ticket.get("status")) != "done"
    ]
    unresolved_handoffs = [
        handoff for handoff in state.get("handoffs", [])
        if unresolved_handoff(handoff)
    ]
    pending_decisions = state.get("pending_decisions", [])
    open_po_messages = [
        message for message in state.get("po_messages", [])
        if message.get("status") in {"open", "received"}
    ]

    blockers = []
    if open_tickets:
        blockers.append(f"{len(open_tickets)} open ticket(s)")
    if unresolved_handoffs:
        blockers.append(f"{len(unresolved_handoffs)} unresolved handoff(s)")
    if pending_decisions:
        blockers.append(f"{len(pending_decisions)} pending PO decision(s)")
    if open_po_messages:
        blockers.append(f"{len(open_po_messages)} open PO message(s)")
    return blockers


def acknowledge_handoffs(state: dict, agent_id: str) -> int:
    count = 0
    for handoff in state.get("handoffs", []):
        if handoff.get("to") in {agent_id, "all"} and handoff.get("status") == "open":
            handoff["status"] = "received"
            handoff["received_at"] = now()
            count += 1
    if agent_id == "pm":
        for message in state.get("po_messages", []):
            if message.get("status") == "open":
                message["status"] = "received"
                message["received_at"] = now()
    return count


def public_state(state: dict) -> dict:
    public = dict(state)
    public["directory"] = str(Path(state["directory"]))
    public["max_agent_steps"] = MAX_AGENT_STEPS
    public.setdefault("pm_chat", [])
    return public


def _list_dir(directory: str, depth: int = 0) -> list[dict]:
    if not directory or not os.path.isdir(directory) or depth > 4:
        return []
    result = []
    try:
        entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(), e.name.lower()))
        for entry in entries:
            if entry.name.startswith(".") and entry.name not in {".warroom"}:
                continue
            try:
                stat = entry.stat()
                mtime = int(stat.st_mtime)
                size = stat.st_size if entry.is_file() else 0
            except OSError:
                mtime = 0
                size = 0
            node: dict = {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": size,
                "mtime": mtime,
            }
            if entry.is_dir():
                if entry.name.lower() in IGNORED_PROJECT_DIRS:
                    node["children"] = []
                else:
                    node["children"] = _list_dir(entry.path, depth + 1)
            result.append(node)
    except PermissionError:
        pass
    return result


def iter_project_paths(root: Path, include_dirs: bool = False):
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(
            name for name in dirs
            if name not in IGNORED_PROJECT_DIRS and not (name.startswith(".") and name != ".warroom")
        )
        current_path = Path(current)
        if include_dirs:
            for dirname in dirs:
                yield current_path / dirname
        for filename in sorted(files):
            yield current_path / filename


def recent_files(root: Path, limit: int = 10) -> list[dict]:
    files = []
    for path in iter_project_paths(root):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        try:
            files.append({"path": str(rel).replace("\\", "/"), "mtime": int(path.stat().st_mtime)})
        except OSError:
            continue
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files[:limit]


def project_stats(root: Path) -> dict:
    ext_counts: dict[str, int] = {}
    total_lines = 0
    total_files = 0
    total_size = 0
    for path in iter_project_paths(root):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        total_files += 1
        try:
            stat = path.stat()
            size = stat.st_size
            total_size += size
            ext = path.suffix.lower() or "(no ext)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            if size < 500_000:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    total_lines += text.count("\n") + (1 if text and not text.endswith("\n") else 0)
                except Exception:
                    pass
        except OSError:
            continue
    return {
        "total_files": total_files,
        "total_lines": total_lines,
        "total_size": total_size,
        "extensions": ext_counts,
    }


def search_project(root: Path, query: str, max_results: int = 30) -> list[dict]:
    if not query or len(query.strip()) < 2:
        return []
    try:
        pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)
    except re.error:
        return []
    results = []
    for path in iter_project_paths(root):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        try:
            if path.stat().st_size > 500_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        matches = []
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                matches.append({"line": i, "text": line.strip()[:200]})
                if len(matches) >= 5:
                    break
        if matches:
            results.append({"file": str(rel).replace("\\", "/"), "matches": matches})
            if len(results) >= max_results:
                break
    return results


def list_projects() -> list[dict]:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    projects = []
    for path in sorted(PROJECTS_DIR.glob("*")):
        sp = path / ".warroom" / "state.json"
        if not sp.exists():
            continue
        try:
            state = json.loads(sp.read_text(encoding="utf-8"))
            maybe_recover_or_resume_runner(path, state)
            projects.append(public_state(load_state(path)))
        except Exception:
            continue
    projects.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return projects


def safe_path(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise ValueError(f"Path escapes project directory: {relative}")
    if ".warroom" in target.relative_to(root.resolve()).parts:
        raise ValueError("Agents may not edit .warroom internal files.")
    return target


def project_tree(root: Path, limit: int = 80) -> str:
    lines = []
    for path in iter_project_paths(root, include_dirs=True):
        rel = path.relative_to(root)
        lines.append(("/" if path.is_dir() else "") + str(rel).replace("\\", "/"))
        if len(lines) >= limit:
            lines.append("... truncated ...")
            break
    return "\n".join(lines) or "(empty project directory)"


def po_message_needs_design_selection(message: str) -> bool:
    lowered = message.lower()
    design_terms = [
        "ui",
        "ux",
        "design",
        "modern",
        "visual",
        "style",
        "look",
        "feel",
        "모던",
        "현대",
        "디자인",
        "스타일",
        "화면",
        "브랜드",
        "시각",
    ]
    exact_terms = ["dark mode", "다크 모드", "라이트 모드", "버튼", "색상 #", "font"]
    return any(term in lowered for term in design_terms) and not any(term in lowered for term in exact_terms)


def fallback_design_selection(message: str) -> dict:
    return {
        "question": f"'{message}' 요청을 어떤 디자인 방향으로 진행할까요?",
        "options": [
            {
                "id": "modern-ops",
                "title": "정돈된 운영형 UI",
                "description": "정보 밀도와 가독성을 높이고, 카드보다 표면/구획 중심으로 차분하게 현대화합니다.",
                "recommendation": "Recommended",
                "colors": ["#101413", "#4cc9f0", "#e8f971"],
            },
            {
                "id": "bold-product",
                "title": "강한 제품형 UI",
                "description": "대비, 타이포그래피, 주요 액션을 더 선명하게 만들어 제품 데모 느낌을 강화합니다.",
                "recommendation": "Expressive",
                "colors": ["#090b10", "#ff6b6b", "#ffd166"],
            },
            {
                "id": "clean-saas",
                "title": "깨끗한 SaaS형 UI",
                "description": "여백, 밝은 표면, 절제된 포인트 컬러로 더 범용적이고 친숙한 화면을 만듭니다.",
                "recommendation": "Neutral",
                "colors": ["#f8fafc", "#2563eb", "#10b981"],
            },
        ],
        "blocks_agent": True,
    }


def po_message_chat_candidate(message: str) -> bool:
    lowered = message.lower().strip()
    if not lowered:
        return False

    conversational_terms = [
        "고마워",
        "감사",
        "좋아",
        "오케이",
        "ㅇㅋ",
        "ok",
        "okay",
        "thanks",
        "thank you",
    ]
    information_terms = [
        "상태",
        "현황",
        "진행",
        "요약",
        "설명",
        "알려줘",
        "어떻게",
        "무엇",
        "뭐",
        "왜",
        "언제",
        "어디",
        "누가",
        "가능",
        "괜찮",
        "맞아",
        "맞나요",
        "되나요",
        "돼?",
        "습니까",
        "나요",
    ]
    question_markers = ["?", "？"]
    return any(term in lowered for term in conversational_terms + information_terms + question_markers)


def append_chat_message(
    state: dict,
    sender: str,
    message: str,
    *,
    source_id: str | None = None,
    reply_to: str | None = None,
) -> dict:
    entry = {
        "id": make_id("chat"),
        "from": sender,
        "message": str(message or "").strip()[:4000],
        "created_at": now(),
    }
    if source_id:
        entry["source_id"] = source_id
    if reply_to:
        entry["reply_to"] = reply_to
    state.setdefault("pm_chat", []).append(entry)
    return entry


def ensure_po_message_in_chat(state: dict, po_message: dict | None) -> dict | None:
    if not po_message:
        return None
    chat_entry_id = po_message.get("chat_entry_id")
    if chat_entry_id:
        return next((item for item in state.get("pm_chat", []) if item.get("id") == chat_entry_id), None)
    source_id = po_message.get("id")
    existing = next(
        (item for item in state.get("pm_chat", []) if source_id and item.get("source_id") == source_id),
        None,
    )
    if existing:
        po_message["chat_entry_id"] = existing["id"]
        return existing
    entry = append_chat_message(state, "PO", po_message.get("message", ""), source_id=source_id)
    po_message["chat_entry_id"] = entry["id"]
    return entry


def record_pm_chat_reply(state: dict, po_message: dict | None, reply: str) -> dict:
    po_chat = ensure_po_message_in_chat(state, po_message)
    reply_entry = append_chat_message(
        state,
        "PM",
        reply,
        reply_to=po_chat.get("id") if po_chat else None,
    )
    if po_message is not None:
        po_message["status"] = "chat_replied"
        po_message["handled_at"] = now()
    if "pm" in state.get("agents", {}):
        state["agents"]["pm"]["status"] = "idle"
        state["agents"]["pm"]["message"] = "Answered PO chat"
        state["agents"]["pm"]["updated_at"] = now()
    append_log(state, "PM", reply, "po_chat")
    return reply_entry


def latest_unanswered_po_message(state: dict, message_id: str | None = None) -> dict | None:
    messages = state.setdefault("po_messages", [])
    if message_id:
        return next((item for item in messages if item.get("id") == message_id), None)
    return next(
        (
            item for item in reversed(messages)
            if item.get("status") in {"open", "received"}
        ),
        None,
    )


def markdown_list(items: list[str], fallback: str = "- None") -> str:
    return "\n".join(f"- {item}" for item in items) if items else fallback


def write_resume_notes(root: Path, state: dict, reason: str = "Graceful stop requested") -> Path:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    target = root / RESUME_NOTES_PATH

    tickets = state.get("tickets", [])
    open_tickets = [
        f"{ticket.get('priority', 'P2')} {ticket.get('id', '')} [{ticket.get('status', 'ready')}] "
        f"{ticket.get('title', 'Untitled')} ({ticket.get('owner', 'unassigned')})"
        for ticket in tickets
        if ticket.get("status") != "done"
    ][:25]
    decisions = [
        f"{decision.get('id')}: {decision.get('question', '')} ({decision.get('agent', 'pm')})"
        for decision in state.get("pending_decisions", [])
    ][:10]
    handoffs = [
        f"{handoff.get('from')} -> {handoff.get('to')}: {handoff.get('message', '')[:180]}"
        for handoff in state.get("handoffs", [])
        if unresolved_handoff(handoff)
    ][-15:]
    po_messages = [
        f"{message.get('status', 'open')}: {message.get('message', '')[:180]}"
        for message in state.get("po_messages", [])
    ][-10:]
    pm_chat = [
        f"{item.get('from', 'PM')}: {item.get('message', '')[:180]}"
        for item in state.get("pm_chat", [])
    ][-10:]
    recent_logs = [
        f"{log.get('agent')}: {log.get('message', '')[:180]}"
        for log in state.get("logs", [])
    ][-12:]

    content = f"""# Resume Notes

Generated: {now()}
Reason: {reason}
Project: {state.get('name', '')}
Status at stop: {state.get('status', '')}
Iteration: {state.get('iteration', 0)}

## How to Resume
- Press Resume in Nicullah, or call the project resume API.
- PM should read this file first, then update docs/backlog.md and hand off the next smallest runnable slice.
- If pending PO decisions exist, resolve them before asking blocked agents to continue.

## Open Tickets
{markdown_list(open_tickets)}

## Pending PO Decisions
{markdown_list(decisions)}

## Open Handoffs
{markdown_list(handoffs)}

## Recent PO Messages
{markdown_list(po_messages)}

## Recent PM Chat
{markdown_list(pm_chat)}

## Recent Activity
{markdown_list(recent_logs)}
"""
    target.write_text(content, encoding="utf-8")
    return target


def read_excerpt(path: Path, max_chars: int = 10000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"[read failed: {exc}]"
    return text[:max_chars]


def edge_trimmed_lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def trailing_newline(text: str) -> str:
    if text.endswith("\r\n"):
        return "\r\n"
    if text.endswith("\n"):
        return "\n"
    if text.endswith("\r"):
        return "\r"
    return ""


def replace_line_window(text: str, find: str, replace: str) -> tuple[str, str] | None:
    find_lines = edge_trimmed_lines(find)
    if not find_lines:
        return None

    find_keys = [line.strip() for line in find_lines]
    text_lines = text.splitlines(keepends=True)
    text_keys = [line.strip() for line in text.splitlines()]
    width = len(find_keys)
    if width > len(text_keys):
        return None

    matches = [
        start
        for start in range(len(text_keys) - width + 1)
        if text_keys[start:start + width] == find_keys
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError("find text matched multiple indentation-normalized locations")

    start = matches[0]
    start_offset = sum(len(line) for line in text_lines[:start])
    end_offset = sum(len(line) for line in text_lines[:start + width])
    matched_text = text[start_offset:end_offset]
    replacement = replace
    newline = trailing_newline(matched_text)
    if newline and not trailing_newline(replacement):
        replacement += newline
    return text[:start_offset] + replacement + text[end_offset:], "indentation-normalized"


def contains_line_window(text: str, snippet: str) -> bool:
    snippet_lines = edge_trimmed_lines(snippet)
    if not snippet_lines:
        return False

    snippet_keys = [line.strip() for line in snippet_lines]
    text_keys = [line.strip() for line in text.splitlines()]
    width = len(snippet_keys)
    if width > len(text_keys):
        return False

    return any(
        text_keys[start:start + width] == snippet_keys
        for start in range(len(text_keys) - width + 1)
    )


def replacement_already_applied(text: str, replace: str) -> bool:
    replace = str(replace or "")
    replace_lines = edge_trimmed_lines(replace)
    if not replace_lines:
        return False

    meaningful_chars = sum(len(line.strip()) for line in replace_lines)
    if meaningful_chars < 20 and len(replace_lines) < 2:
        return False

    normalized_replace = replace.replace("\r\n", "\n").replace("\r", "\n")
    candidates = [
        replace,
        normalized_replace,
        normalized_replace.strip(),
    ]
    if any(candidate and candidate in text for candidate in dict.fromkeys(candidates)):
        return True
    return contains_line_window(text, normalized_replace)


def replace_fuzzy_line_window(text: str, find: str, replace: str) -> tuple[str, str] | None:
    find_lines = edge_trimmed_lines(find)
    if len(find_lines) < 3:
        return None

    find_keys = [line.strip() for line in find_lines]
    text_lines = text.splitlines(keepends=True)
    text_keys = [line.strip() for line in text.splitlines()]
    width = len(find_keys)
    if width > len(text_keys):
        return None

    exact_anchors = {
        key.lower()
        for key in find_keys
        if len(key) >= FUZZY_EDIT_MIN_EXACT_ANCHOR_LEN
    }
    if not exact_anchors:
        return None

    find_blob = "\n".join(find_keys).lower()
    matches = []
    for start in range(len(text_keys) - width + 1):
        window_keys = text_keys[start:start + width]
        anchor_count = sum(1 for key in window_keys if key.lower() in exact_anchors)
        if not anchor_count:
            continue
        score = SequenceMatcher(None, find_blob, "\n".join(window_keys).lower()).ratio()
        if score >= FUZZY_EDIT_MIN_SCORE:
            matches.append((score, anchor_count, start))

    if not matches:
        return None

    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_score, _anchor_count, start = matches[0]
    if len(matches) > 1 and best_score - matches[1][0] < FUZZY_EDIT_MIN_MARGIN:
        raise ValueError("find text matched multiple fuzzy locations")

    start_offset = sum(len(line) for line in text_lines[:start])
    end_offset = sum(len(line) for line in text_lines[:start + width])
    matched_text = text[start_offset:end_offset]
    replacement = str(replace or "")
    newline = trailing_newline(matched_text)
    if newline and not trailing_newline(replacement):
        replacement += newline
    return text[:start_offset] + replacement + text[end_offset:], "fuzzy-line-window"


def replace_edit_text(text: str, find: str, replace: str) -> tuple[str, str]:
    find = str(find or "")
    replace = str(replace or "")
    if not find.strip():
        raise ValueError("edit_file requires a non-empty find string")

    if find in text:
        return text.replace(find, replace, 1), "exact"

    normalized_find = find.replace("\r\n", "\n").replace("\r", "\n")
    if normalized_find != find and normalized_find in text:
        return text.replace(normalized_find, replace, 1), "line-ending-normalized"

    trimmed_find = normalized_find.strip()
    if trimmed_find and trimmed_find != normalized_find and trimmed_find in text:
        return text.replace(trimmed_find, replace, 1), "edge-trimmed"

    line_window_result = replace_line_window(text, normalized_find, replace)
    if line_window_result:
        return line_window_result

    if replacement_already_applied(text, replace):
        return text, "already-applied"

    fuzzy_window_result = replace_fuzzy_line_window(text, normalized_find, replace)
    if fuzzy_window_result:
        return fuzzy_window_result

    raise ValueError("find text not found")


def compact_snippet(text: str, max_chars: int = 240) -> str:
    snippet = " ".join(str(text or "").split())
    if len(snippet) > max_chars:
        return snippet[:max_chars - 3] + "..."
    return snippet


def numbered_excerpt(lines: list[str], start: int, end: int) -> str:
    return "\n".join(f"L{line_no + 1}: {lines[line_no][:240]}" for line_no in range(start, end))


def closest_current_excerpt(text: str, find: str, context_lines: int = 2) -> str:
    lines = text.splitlines()
    if not lines:
        return "Current file is empty."

    needles = [line.strip() for line in str(find or "").splitlines() if len(line.strip()) >= 8]
    if not needles:
        preview_end = min(len(lines), 6)
        return "Current file starts with:\n" + numbered_excerpt(lines, 0, preview_end)

    best_index = -1
    best_score = 0.0
    for needle in sorted(needles, key=len, reverse=True)[:8]:
        needle_lower = needle.lower()[:300]
        for index, line in enumerate(lines):
            current = line.strip().lower()[:300]
            if not current:
                continue
            score = 1.0 if needle_lower in current else SequenceMatcher(None, needle_lower, current).ratio()
            if score > best_score:
                best_score = score
                best_index = index

    if best_index < 0 or best_score < 0.55:
        preview_end = min(len(lines), 6)
        return "No close line match. Current file starts with:\n" + numbered_excerpt(lines, 0, preview_end)

    start = max(0, best_index - context_lines)
    end = min(len(lines), best_index + context_lines + 1)
    return f"Closest current excerpt around L{best_index + 1}:\n{numbered_excerpt(lines, start, end)}"


def find_miss_message(path: str, text: str, find: str) -> str:
    parts = [
        f"find text not found in {path}. The file exists, but the requested find snippet does not match its current contents.",
    ]
    snippet = compact_snippet(find)
    if snippet:
        parts.append(f"Find snippet: {snippet}")
    parts.append(closest_current_excerpt(text, find))
    parts.append("Read the file or search for a stable anchor before retrying the edit.")
    return "\n".join(parts)


def collect_context(root: Path, state: dict, agent_id: str) -> dict:
    core_files = [
        "project_context.md",
        "README.md",
        "package.json",
        "pyproject.toml",
        "docs/PRD.md",
        "docs/backlog.md",
        "docs/design_brief.md",
        "docs/wireframes.md",
        "docs/api.md",
        "openapi.yaml",
        "docs/database.md",
        "docs/test_plan.md",
        RESUME_NOTES_PATH,
    ]
    files_to_read = list(dict.fromkeys(core_files + AGENTS[agent_id].get("artifacts", [])))
    excerpts = {}
    for rel in files_to_read:
        path = root / rel
        if path.exists() and path.is_file():
            excerpts[rel] = read_excerpt(path, 12000)
    handoffs = state.get("handoffs", [])
    return {
        "project": {
            "name": state["name"],
            "vision": state["vision"],
            "directory": str(root),
            "status": state["status"],
            "iteration": state.get("iteration", 0),
            "stop_requested": bool(state.get("stop_requested")),
            "graceful_stop_active": bool(state.get("graceful_stop_active")),
            "resume_notes_file": RESUME_NOTES_PATH,
            "preview_url": preferred_preview_url(root),
        },
        "current_agent": agent_id,
        "agents": state.get("agents", {}),
        "tickets": state.get("tickets", []),
        "pending_decisions": state.get("pending_decisions", []),
        "handoffs": {
            "inbox": [item for item in handoffs if item.get("to") in {agent_id, "all"} and unresolved_handoff(item)][-15:],
            "visible_recent": [item for item in handoffs if handoff_visible_to(item, agent_id)][-25:],
        },
        "reviews": state.get("reviews", [])[-20:],
        "command_runs": state.get("command_runs", [])[-10:],
        "previews": state.get("previews", [])[-10:],
        "approval_history": state.get("approval_history", [])[-10:],
        "pm_chat": state.get("pm_chat", [])[-30:],
        "po_messages": state.get("po_messages", [])[-20:],
        "open_po_messages": [
            item for item in state.get("po_messages", [])
            if item.get("status") in {"open", "received"}
        ][-10:],
        "recent_logs": state.get("logs", [])[-20:],
        "file_tree": project_tree(root),
        "file_excerpts": excerpts,
        "recently_modified_files": recent_files(root, 8),
        "search_results": state.get("search_results", [])[-3:],
        "project_stats": state.get("project_stats", {}),
        "allowed_actions": [
            "write_file",
            "append_file",
            "edit_file",
            "read_file",
            "search_files",
            "get_project_stats",
            "run_command",
            "capture_preview",
            "add_ticket",
            "update_ticket",
            "handoff",
            "resolve_handoff",
            "record_review",
            "request_po_approval",
            "reply_to_po",
            "set_status",
            "complete_task",
        ],
    }


def agent_system_instruction(agent_id: str) -> str:
    agent = AGENTS[agent_id]
    return f"""
You are the {agent['role']} in Nicullah.
Mission: {agent['mission']}

Work like a real senior programming team member. Continue proactively when the path is clear.
Ask PO approval only for product direction, external paid services, credentials/secrets,
destructive filesystem operations, irreversible architecture choices, or mutually exclusive UX choices.
If you need approval, return approval_required and mark only your own work blocked; other agents may continue.
You must request PO approval instead of silently choosing when PO asks for a broad UI/design/brand direction
or when you want to present multiple product/design candidates. Example: if PO says "UI를 더 모던하게 바꿔줘",
PM should say it will present design candidates, then return approval_required or request_po_approval with options.
Approval options are generated by you for the current project context; do not rely on fixed default candidates.
Include colors only when the decision is actually about palette, brand, theme, visual style, or another color-bearing UI choice.
For non-color decisions, omit colors and do not set allow_colors.
If open_po_messages contains PO instructions and you are PM, acknowledge them, update the plan/backlog,
and hand off revised work. If the instruction is exact, such as adding dark mode, revise the plan and continue.
If open_po_messages contains a PO question, status check, clarification, or casual chat that does not require
creating/changing project work, answer it with reply_to_po and do not create tickets, handoffs, files, or commands.
Use pm_chat as the remembered PO/PM conversation for this project session.
If project.graceful_stop_active is true, do not start new feature scope. Finish the smallest useful slice,
make the project runnable if possible, update {RESUME_NOTES_PATH} with resume notes, and stop cleanly.
When you need a UI screenshot, use project.preview_url when present unless a current handoff names a different
verified URL. capture_preview can start local npm dev/preview servers for localhost URLs when package.json exposes them.

Role responsibilities:
{bullet_list(agent['responsibilities'])}

Expected artifacts:
{bullet_list(agent['artifacts'])}

Collaboration duties:
{bullet_list(agent['collaboration'])}

Team protocol:
{bullet_list(TEAM_PROTOCOL)}

Return strict JSON only:
{{
  "summary": "short status update",
  "agent_status": "short status visible in UI",
  "approval_required": null or {{
    "question": "question for PO",
    "options": [
      {{"id": "option-a", "title": "Option A", "description": "tradeoff", "recommendation": "Recommended"}}
    ],
    "allow_colors": false,
    "blocks_agent": true
  }},
  "actions": [
    {{"type": "write_file", "path": "relative/path", "content": "full file content"}},
    {{"type": "append_file", "path": "relative/path", "content": "text to append"}},
    {{"type": "edit_file", "path": "relative/path", "find": "old text", "replace": "new text"}},
    {{"type": "read_file", "path": "relative/path"}},
    {{"type": "search_files", "query": "text to search across all project files"}},
    {{"type": "get_project_stats"}},
    {{"type": "run_command", "command": ["python", "-m", "unittest"]}},
    {{"type": "capture_preview", "url": "http://127.0.0.1:5173", "note": "what to inspect"}},
    {{"type": "add_ticket", "title": "task", "owner": "frontend", "status": "ready|progress|review|blocked|done", "priority": "P0|P1|P2|P3", "description": "scope", "acceptance_criteria": ["criterion"]}},
    {{"type": "update_ticket", "id": "ticket-id", "status": "ready|progress|review|blocked|done", "notes": "what changed"}},
    {{"type": "handoff", "to": "pm|designer|backend|frontend|qa|all", "message": "next action needed", "files": ["path"], "ticket_id": "ticket-id", "priority": "normal|high"}},
    {{"type": "resolve_handoff", "id": "handoff-id", "resolution": "what was done"}},
    {{"type": "record_review", "target": "pm|designer|backend|frontend|qa", "verdict": "pass|needs_changes|blocked", "findings": ["finding"], "files": ["path"]}},
    {{"type": "request_po_approval", "question": "question for PO", "options": [{{"id": "option-a", "title": "Option A", "description": "tradeoff", "recommendation": "Recommended"}}], "allow_colors": false, "blocks_agent": true}},
    {{"type": "reply_to_po", "message_id": "po-message-id", "message": "direct PM chat reply when no project work is required"}},
    {{"type": "set_status", "status": "running|waiting_for_approval|complete"}},
    {{"type": "complete_task", "summary": "what is complete"}}
  ]
}}

Prefer small, safe file edits. Write complete files when creating new files.
Never invent that a command passed unless you ran it. If a role-specific artifact is missing, create or update it before relying on it.
Only use set_status complete when every ticket is done and there are no unresolved handoffs, open PO messages, or pending PO decisions.
Use agent_status for role-level states such as idle, waiting, or waiting_for_agent; do not send those through set_status.

Code navigation guidance:
- Use search_files before editing to locate existing implementations, imports, or usages.
- Use get_project_stats on first turn to understand project scope and language breakdown.
- Results from search_files and get_project_stats appear in your next turn's context under search_results and project_stats.
- recently_modified_files in your context shows what changed most recently — read those before writing to avoid conflicts.
- Use edit_file find text copied from current file_excerpts, read_file output, or search_files results, not from memory.
- If edit_file reports "find text not found", do not retry the same edit. First read_file or search_files for the current file,
  then retry with a current stable anchor or stop if the intended change is already present.
"""


def extract_json(text: str) -> dict:
    last_error: json.JSONDecodeError | None = None
    for candidate in json_candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            repaired = repair_json_text(candidate)
            if repaired != candidate:
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError as repaired_exc:
                    last_error = repaired_exc
    if last_error:
        raise last_error
    raise json.JSONDecodeError("No JSON object found", text, 0)


def json_candidates(text: str) -> list[str]:
    stripped = text.strip()
    candidates: list[str] = []
    if stripped:
        candidates.append(stripped)

    candidates.extend(fenced_json_blocks(stripped))
    candidates.extend(balanced_json_objects(stripped))

    if stripped.startswith("`") and stripped.endswith("`"):
        candidates.append(stripped.strip("`").strip())

    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def fenced_json_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    for match in re.finditer(r"```(?:json|jsonc)?\s*(.*?)```", text, flags=re.S | re.I):
        block = match.group(1).strip()
        if block:
            blocks.append(block)
    return blocks


def balanced_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        candidate = first_balanced_json_object(text[index:])
        if candidate:
            objects.append(candidate)
    return objects


def first_balanced_json_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = in_string
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def repair_json_text(text: str) -> str:
    repaired = text.strip()
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r"}\s*{", "},{", repaired)
    repaired = re.sub(r'([}\]"])\s+("[^"\n\r]+":)', r"\1, \2", repaired)
    repaired = re.sub(r'([}\]"])\s*\n\s*"', r'\1,\n"', repaired)
    return repaired


def empty_agent_response(reason: str) -> dict:
    return {
        "summary": f"Skipped turn because Gemini returned no valid JSON: {reason}",
        "agent_status": "Skipped invalid Gemini response",
        "approval_required": None,
        "actions": [],
    }


def call_agent(root: Path, state: dict, agent_id: str) -> dict:
    client = require_gemini_client()
    handler = GeminiRetryHandler(client)
    context = collect_context(root, state, agent_id)
    system_instruction = agent_system_instruction(agent_id)
    text = handler.generate_response(
        history=[],
        message=json.dumps(context, ensure_ascii=False),
        system_instruction=system_instruction,
        temperature=0.25,
        response_mime_type="application/json",
    )
    try:
        return extract_json(text)
    except (json.JSONDecodeError, TypeError) as original_error:
        if not (text or "").strip():
            retry_text = handler.generate_response(
                history=[],
                message=(
                    "Your previous response was empty. Return exactly one valid JSON object "
                    "using the required team protocol schema.\n\n"
                    f"{json.dumps(context, ensure_ascii=False)}"
                ),
                system_instruction=system_instruction,
                temperature=0,
                response_mime_type="application/json",
            )
            try:
                return extract_json(retry_text)
            except (json.JSONDecodeError, TypeError) as retry_error:
                return empty_agent_response(str(retry_error))
        repaired = handler.generate_response(
            history=[],
            message=(
                "Repair this malformed JSON into one valid JSON object only. "
                "Preserve all fields and string content. Do not add markdown.\n\n"
                f"{text}"
            ),
            system_instruction="You repair malformed JSON. Return valid JSON only.",
            temperature=0,
            response_mime_type="application/json",
        )
        try:
            return extract_json(repaired)
        except (json.JSONDecodeError, TypeError) as repair_error:
            return empty_agent_response(str(repair_error or original_error))


def classify_pm_chat_message(root: Path, state: dict, message: str) -> dict:
    client = require_gemini_client()
    handler = GeminiRetryHandler(client)
    context = {
        "project": {
            "name": state.get("name", ""),
            "vision": state.get("vision", ""),
            "status": state.get("status", ""),
            "iteration": state.get("iteration", 0),
            "directory": str(root),
        },
        "agents": state.get("agents", {}),
        "tickets": state.get("tickets", [])[-20:],
        "pending_decisions": state.get("pending_decisions", [])[-10:],
        "handoffs": [
            item for item in state.get("handoffs", [])
            if unresolved_handoff(item)
        ][-15:],
        "approval_history": state.get("approval_history", [])[-10:],
        "pm_chat": state.get("pm_chat", [])[-30:],
        "recent_logs": state.get("logs", [])[-20:],
        "po_message": message,
    }
    system_instruction = """
You are the PM in Nicullah. Decide whether the PO message can be answered as chat
without creating, changing, or sequencing project work.

Return mode "chat" for questions, status checks, clarifications, acknowledgements,
or casual conversation. Answer directly as PM using the project context and pm_chat memory.
Return mode "work" when the PO asks to add, change, fix, build, design, test, document,
or otherwise perform project work. For work mode, leave reply empty.

Return strict JSON only:
{"mode":"chat|work","reply":"PM answer when mode is chat","agent_status":"short visible PM status"}
"""
    text = handler.generate_response(
        history=[],
        message=json.dumps(context, ensure_ascii=False),
        system_instruction=system_instruction,
        temperature=0.2,
        response_mime_type="application/json",
    )
    try:
        result = extract_json(text)
    except (json.JSONDecodeError, TypeError):
        return {"mode": "work", "reply": "", "agent_status": "Queued for PM"}

    mode = str(result.get("mode") or "work").strip().lower()
    reply = str(result.get("reply") or "").strip()
    if mode == "chat" and reply:
        return {
            "mode": "chat",
            "reply": reply,
            "agent_status": str(result.get("agent_status") or "Answered PO chat"),
        }
    return {"mode": "work", "reply": "", "agent_status": str(result.get("agent_status") or "Queued for PM")}


PYTHON_COMMAND_NAMES = {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}


def python_environment_candidates(root: Path) -> list[Path]:
    env_roots = [root, ROOT]
    candidates = []
    for env_root in env_roots:
        if os.name == "nt":
            candidates.extend([
                env_root / ".venv" / "Scripts" / "python.exe",
                env_root / "venv" / "Scripts" / "python.exe",
            ])
        candidates.extend([
            env_root / ".venv" / "bin" / "python",
            env_root / "venv" / "bin" / "python",
        ])
    return candidates


def preferred_python_executable(root: Path) -> str | None:
    for candidate in python_environment_candidates(root):
        if candidate.is_file():
            return str(candidate)
    return None


def is_python_command(raw_executable: str) -> bool:
    return Path(raw_executable).name.lower() in PYTHON_COMMAND_NAMES


def resolve_command(root: Path, command: list[str]) -> list[str]:
    if not command or not isinstance(command, list):
        raise ValueError("run_command requires a command array")
    blocked = {"rm", "del", "erase", "rmdir", "rd", "format", "shutdown", "powershell", "cmd"}
    raw_executable = str(command[0])
    executable = raw_executable.lower()
    if executable in blocked:
        raise ValueError(f"Command is not allowed: {command[0]}")
    if is_python_command(raw_executable):
        preferred_python = preferred_python_executable(root)
        if preferred_python:
            return [preferred_python] + [str(item) for item in command[1:]]
        if Path(raw_executable).name.lower() in {"python", "python.exe", "python3", "python3.exe"}:
            return [sys.executable] + [str(item) for item in command[1:]]

    resolved = shutil.which(raw_executable)
    if not resolved and ("/" in raw_executable or "\\" in raw_executable):
        executable_path = Path(raw_executable)
        if not executable_path.is_absolute():
            executable_path = root / executable_path
        candidates = [executable_path]
        if os.name == "nt":
            windows_path = raw_executable.replace("/", "\\")
            if "\\bin\\" in windows_path:
                scripts_path = Path(windows_path.replace("\\bin\\", "\\Scripts\\"))
                if not scripts_path.is_absolute():
                    scripts_path = root / scripts_path
                candidates.append(scripts_path)
            candidates.extend(candidate.with_suffix(".exe") for candidate in list(candidates) if not candidate.suffix)
        resolved = next((str(candidate) for candidate in candidates if candidate.is_file()), None)
    if resolved:
        return [resolved] + [str(item) for item in command[1:]]
    return [str(item) for item in command]


def run_command(root: Path, command: list[str]) -> dict:
    command = resolve_command(root, command)
    preview_url = dev_server_command_url(command)
    if preview_url:
        try:
            status = ensure_preview_reachable(root, preview_url)
            return {
                "command": command,
                "returncode": 0,
                "stdout": f"{status}\nPreview available at {preview_url}",
                "stderr": "",
            }
        except Exception as exc:
            return {
                "command": command,
                "returncode": 1,
                "stdout": "",
                "stderr": str(exc)[-5000:],
            }
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": 124,
            "stdout": tail_process_output(exc.stdout),
            "stderr": (tail_process_output(exc.stderr) + f"\nCommand timed out after {COMMAND_TIMEOUT}s.").strip()[-5000:],
        }
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-5000:],
        "stderr": completed.stderr[-5000:],
    }


def dev_server_command_url(command: list[str]) -> str | None:
    if len(command) < 3:
        return None

    executable = Path(str(command[0])).name.lower()
    if executable not in {"npm", "npm.cmd", "npm.exe"}:
        return None
    if str(command[1]).lower() != "run":
        return None

    script = str(command[2]).lower()
    if script not in {"dev", "preview"}:
        return None

    host = "127.0.0.1"
    port = 5173 if script == "dev" else 4173
    args = [str(item) for item in command[3:]]
    for index, item in enumerate(args):
        if item == "--host" and index + 1 < len(args):
            host = args[index + 1]
        elif item.startswith("--host="):
            host = item.split("=", 1)[1]
        elif item == "--port" and index + 1 < len(args):
            try:
                port = int(args[index + 1])
            except ValueError:
                pass
        elif item.startswith("--port="):
            try:
                port = int(item.split("=", 1)[1])
            except ValueError:
                pass

    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def package_scripts(root: Path) -> dict[str, str]:
    package_path = root / "package.json"
    if not package_path.exists():
        return {}
    try:
        data = json.loads(package_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


def preferred_preview_url(root: Path) -> str:
    scripts = package_scripts(root)
    if "vite" in scripts.get("dev", ""):
        return "http://127.0.0.1:5173"
    if "vite" in scripts.get("preview", ""):
        return "http://127.0.0.1:4173"
    return ""


def local_http_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in LOCAL_PREVIEW_HOSTS:
        return None
    return parsed


def url_responds(url: str, timeout: float = 2.0) -> bool:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "Nicullah preview capture"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 100 <= response.status < 600
    except urllib.error.HTTPError as exc:
        return 100 <= exc.code < 600
    except (OSError, TimeoutError, urllib.error.URLError):
        return False


def tail_process_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value)[-5000:]


def npm_executable() -> str | None:
    names = ["npm.cmd", "npm.exe", "npm"] if os.name == "nt" else ["npm"]
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def local_preview_command(root: Path, parsed) -> list[str] | None:
    scripts = package_scripts(root)
    if "dev" in scripts:
        script = "dev"
    elif "preview" in scripts:
        script = "preview"
    else:
        return None

    npm = npm_executable()
    if not npm:
        return None

    host = parsed.hostname or "127.0.0.1"
    if host == "localhost":
        host = "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return [npm, "run", script, "--", "--host", host, "--port", str(port), "--strictPort"]


def preview_process_key(root: Path, parsed) -> str:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{root.resolve()}::{parsed.hostname}:{port}"


def start_local_preview_process(root: Path, parsed, command: list[str]) -> subprocess.Popen:
    key = preview_process_key(root, parsed)
    with preview_lock:
        existing = preview_processes.get(key)
        if existing and existing.poll() is None:
            return existing

        popen_kwargs = {
            "cwd": str(root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(command, **popen_kwargs)
        preview_processes[key] = process
        return process


def ensure_preview_reachable(root: Path, url: str) -> str:
    if url_responds(url):
        return "Preview URL was already reachable."

    parsed = local_http_url(url)
    if not parsed:
        return "Preview URL is not a local HTTP URL; capture will try it directly."

    command = local_preview_command(root, parsed)
    if not command:
        raise RuntimeError(
            f"Preview URL is not reachable: {url}. "
            "No package.json dev or preview script was found to start it automatically."
        )

    try:
        process = start_local_preview_process(root, parsed, command)
    except OSError as exc:
        raise RuntimeError(f"Could not start preview server with {' '.join(command)}: {exc}") from exc

    deadline = time.time() + PREVIEW_START_TIMEOUT
    while time.time() < deadline:
        if url_responds(url):
            return f"Started preview server with {' '.join(command)}."
        if process.poll() is not None:
            raise RuntimeError(
                f"Preview URL did not become reachable: {url}. "
                f"{' '.join(command)} exited with code {process.returncode}."
            )
        time.sleep(0.5)

    raise RuntimeError(
        f"Preview URL did not become reachable within {PREVIEW_START_TIMEOUT}s: {url}. "
        f"Started command: {' '.join(command)}"
    )


def capture_preview(root: Path, url: str, note: str) -> dict:
    screenshots = root / ".warroom" / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    output = screenshots / f"preview-{int(time.time())}.png"
    script = ROOT / "server" / "capture_preview.js"
    node = shutil.which("node")
    if not node:
        bundled = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"
        node = str(bundled) if bundled.exists() else None
    if not node:
        raise RuntimeError("Node.js is required for preview capture.")
    preview_status = ensure_preview_reachable(root, url)
    completed = subprocess.run(
        [node, str(script), url, str(output)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)

    client = require_gemini_client()
    my_file = client.files.upload(file=str(output))
    response = client.models.generate_content(
        model=GeminiRetryHandler.MODELS[0],
        contents=[
            my_file,
            (
                "Review this UI screenshot for layout bugs, overlapping text, spacing issues, "
                f"and design mismatches. Context: {note}"
            ),
        ],
    )
    return {
        "screenshot": str(output),
        "review": getattr(response, "text", "") or "",
        "preview_status": preview_status,
    }


def execute_actions(root: Path, state: dict, agent_id: str, actions: list[dict]) -> list[str]:
    observations = []
    for action in actions or []:
        action_type = action.get("type")
        try:
            if action_type == "write_file":
                target = safe_path(root, action["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(action.get("content", ""), encoding="utf-8")
                observations.append(f"wrote {action['path']}")
            elif action_type == "append_file":
                target = safe_path(root, action["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(action.get("content", ""))
                observations.append(f"appended {action['path']}")
            elif action_type == "edit_file":
                target = safe_path(root, action["path"])
                text = target.read_text(encoding="utf-8", errors="replace")
                find = action.get("find", "")
                try:
                    new_text, match_kind = replace_edit_text(text, find, action.get("replace", ""))
                except ValueError as exc:
                    if str(exc) == "find text not found":
                        raise ValueError(find_miss_message(action["path"], text, find)) from exc
                    raise
                if match_kind == "already-applied":
                    observations.append(f"edit skipped {action['path']} (already applied)")
                else:
                    target.write_text(new_text, encoding="utf-8")
                    detail = "" if match_kind == "exact" else f" ({match_kind} match)"
                    observations.append(f"edited {action['path']}{detail}")
            elif action_type == "read_file":
                target = safe_path(root, action["path"])
                observations.append(f"read {action['path']}:\n{read_excerpt(target, 3000)}")
            elif action_type == "search_files":
                query = action.get("query", "").strip()
                if not query or len(query) < 2:
                    observations.append("search_files: query must be at least 2 characters")
                else:
                    results = search_project(root, query, max_results=20)
                    entry = {
                        "agent": agent_id,
                        "time": now(),
                        "query": query,
                        "results": results,
                    }
                    state.setdefault("search_results", []).append(entry)
                    state["search_results"] = state["search_results"][-5:]
                    if results:
                        summary = "; ".join(
                            f"{r['file']}:L{r['matches'][0]['line']}" for r in results[:5]
                        )
                        observations.append(
                            f"search '{query}' → {len(results)} files: {summary}"
                        )
                    else:
                        observations.append(f"search '{query}' → no matches")
            elif action_type == "get_project_stats":
                stats = project_stats(root)
                state["project_stats"] = {**stats, "agent": agent_id, "time": now()}
                ext_summary = ", ".join(
                    f"{ext}:{cnt}"
                    for ext, cnt in sorted(stats["extensions"].items(), key=lambda x: -x[1])[:6]
                )
                observations.append(
                    f"project stats: {stats['total_files']} files, "
                    f"{stats['total_lines']} lines, "
                    f"{stats['total_size'] // 1024} KB | {ext_summary}"
                )
            elif action_type == "run_command":
                result = run_command(root, action.get("command", []))
                state.setdefault("command_runs", []).append({
                    "agent": agent_id,
                    "time": now(),
                    **result,
                })
                observations.append(
                    f"ran {' '.join(map(str, action.get('command', [])))} -> {result['returncode']}"
                )
            elif action_type == "capture_preview":
                result = capture_preview(root, action["url"], action.get("note", ""))
                state.setdefault("previews", []).append({
                    "agent": agent_id,
                    "time": now(),
                    **result,
                })
                status = result.get("preview_status", "Preview captured.")
                observations.append(f"{status} Captured preview {result['screenshot']}: {result['review'][:800]}")
            elif action_type == "add_ticket":
                ticket = {
                    "id": make_id("ticket"),
                    "title": action.get("title", "Untitled task"),
                    "owner": action.get("owner", agent_id),
                    "status": normalize_ticket_status(action.get("status")),
                    "priority": action.get("priority", "P2"),
                    "description": action.get("description", ""),
                    "acceptance_criteria": normalize_string_list(action.get("acceptance_criteria")),
                    "depends_on": normalize_string_list(action.get("depends_on")),
                    "notes": normalize_string_list(action.get("notes")),
                    "created_at": now(),
                    "updated_at": now(),
                }
                state.setdefault("tickets", []).append(ticket)
                observations.append(f"added ticket {action.get('title', 'Untitled task')}")
            elif action_type == "update_ticket":
                ticket_id = action.get("id")
                title = action.get("title")
                ticket = next(
                    (
                        item for item in state.setdefault("tickets", [])
                        if (ticket_id and item.get("id") == ticket_id) or (title and item.get("title") == title)
                    ),
                    None,
                )
                if not ticket:
                    raise ValueError(f"ticket not found: {ticket_id or title}")
                for key in ("title", "owner", "status", "priority", "description"):
                    if key in action:
                        ticket[key] = normalize_ticket_status(action[key]) if key == "status" else action[key]
                for key in ("acceptance_criteria", "depends_on"):
                    if key in action:
                        ticket[key] = normalize_string_list(action.get(key))
                if action.get("notes"):
                    ticket.setdefault("notes", []).extend(normalize_string_list(action.get("notes")))
                ticket["updated_at"] = now()
                observations.append(f"updated ticket {ticket.get('id')}")
            elif action_type == "handoff":
                target = action.get("to")
                if target not in set(AGENTS) | {"all"}:
                    raise ValueError(f"handoff target must be an agent or all: {target}")
                handoff = {
                    "id": make_id("handoff"),
                    "from": agent_id,
                    "to": target,
                    "message": action.get("message", ""),
                    "files": normalize_string_list(action.get("files")),
                    "ticket_id": action.get("ticket_id", ""),
                    "priority": action.get("priority", "normal"),
                    "status": "open",
                    "created_at": now(),
                }
                state.setdefault("handoffs", []).append(handoff)
                observations.append(f"handoff to {target}: {handoff['message'][:160]}")
            elif action_type == "resolve_handoff":
                handoff_id = action.get("id")
                handoff = next((item for item in state.setdefault("handoffs", []) if item.get("id") == handoff_id), None)
                if not handoff:
                    raise ValueError(f"handoff not found: {handoff_id}")
                handoff["status"] = "resolved"
                handoff["resolution"] = action.get("resolution", "")
                handoff["resolved_by"] = agent_id
                handoff["resolved_at"] = now()
                observations.append(f"resolved handoff {handoff_id}")
            elif action_type == "record_review":
                target = action.get("target")
                if target not in AGENTS:
                    raise ValueError(f"review target must be an agent: {target}")
                review = {
                    "id": make_id("review"),
                    "from": agent_id,
                    "target": target,
                    "verdict": action.get("verdict", "needs_changes"),
                    "findings": normalize_string_list(action.get("findings")),
                    "files": normalize_string_list(action.get("files")),
                    "created_at": now(),
                }
                state.setdefault("reviews", []).append(review)
                if review["verdict"] in {"needs_changes", "blocked"}:
                    state.setdefault("handoffs", []).append({
                        "id": make_id("handoff"),
                        "from": agent_id,
                        "to": target,
                        "message": "Review feedback requires action: " + "; ".join(review["findings"][:3]),
                        "files": review["files"],
                        "ticket_id": action.get("ticket_id", ""),
                        "priority": "high" if review["verdict"] == "blocked" else "normal",
                        "status": "open",
                        "created_at": now(),
                    })
                observations.append(f"recorded {review['verdict']} review for {target}")
            elif action_type == "request_po_approval":
                decision = create_pending_decision(state, agent_id, action, source="action")
                observations.append(f"requested PO approval {decision['id']}")
            elif action_type == "reply_to_po":
                if agent_id != "pm":
                    raise ValueError("Only PM can reply directly to PO chat.")
                reply = str(action.get("message") or action.get("reply") or "").strip()
                if not reply:
                    raise ValueError("reply_to_po requires a message")
                po_message = latest_unanswered_po_message(
                    state,
                    action.get("message_id") or action.get("po_message_id"),
                )
                record_pm_chat_reply(state, po_message, reply)
                observations.append("replied to PO chat")
            elif action_type == "set_status":
                requested_status = str(action.get("status") or "").strip().lower()
                if requested_status == "complete":
                    blockers = project_completion_blockers(state)
                    if blockers:
                        observations.append(f"ignored complete project status with unfinished work: {', '.join(blockers)}")
                    else:
                        state["status"] = requested_status
                        observations.append(f"status set to {state['status']}")
                elif requested_status in {"running", "waiting_for_approval"}:
                    if state.get("stop_requested") or state.get("status") in STOPPING_STATUSES:
                        observations.append(f"ignored {requested_status} project status while stop is requested")
                    else:
                        state["status"] = requested_status
                        observations.append(f"status set to {state['status']}")
                elif requested_status in AGENT_LEVEL_WAIT_STATUSES:
                    if (
                        not state.get("pending_decisions")
                        and not state.get("stop_requested")
                        and state.get("status") not in STOPPING_STATUSES
                    ):
                        state["status"] = "running"
                    observations.append(f"kept project running; {requested_status} is an agent-level status")
                elif requested_status in AGENT_SET_PROJECT_STATUSES:
                    if state.get("stop_requested") or state.get("status") in STOPPING_STATUSES:
                        observations.append(f"ignored {requested_status} project status while stop is requested")
                    else:
                        state["status"] = requested_status
                        observations.append(f"status set to {state['status']}")
                else:
                    observations.append(f"ignored invalid project status {requested_status or '(empty)'}")
            elif action_type == "complete_task":
                observations.append(action.get("summary", "task complete"))
            else:
                observations.append(f"ignored unknown action {action_type}")
        except Exception as exc:
            observations.append(f"{action_type} failed: {exc}")
            append_log(state, AGENTS[agent_id]["label"], f"{action_type} failed: {exc}", "error")
    return observations


def run_agent_turn(root: Path, state: dict, agent_id: str, runner_token: str | None = None) -> None:
    agent = AGENTS[agent_id]
    agent_state = state.setdefault("agents", {}).setdefault(agent_id, {})
    if agent_state.get("blocked"):
        return
    received = acknowledge_handoffs(state, agent_id)
    agent_state.update({"status": "running", "message": "Working", "updated_at": now()})
    append_log(state, agent["label"], "started a work turn")
    if received:
        append_log(state, agent["label"], f"received {received} team handoff(s)")
    save_state(root, state)

    try:
        response = call_agent(root, state, agent_id)
    except Exception as exc:
        if runner_token and load_state(root).get("runner_token") != runner_token:
            return
        if GeminiRetryHandler.is_retryable_exception(exc):
            agent_state["status"] = "idle"
            agent_state["message"] = f"Gemini temporarily unavailable; will retry ({str(exc)[:96]})"
            agent_state["updated_at"] = now()
            append_log(state, agent["label"], f"agent turn deferred: {exc}", "warning")
            save_state(root, state)
            return
        agent_state["status"] = "error"
        agent_state["message"] = str(exc)[:160]
        append_log(state, agent["label"], f"agent turn failed: {exc}", "error")
        save_state(root, state)
        return

    if runner_token and load_state(root).get("runner_token") != runner_token:
        return

    state = load_state(root)
    agent_state = state.setdefault("agents", {}).setdefault(agent_id, {})
    agent_state["message"] = response.get("agent_status") or response.get("summary") or "Updated"
    append_log(state, agent["label"], response.get("summary", "completed a work turn"))

    approval = response.get("approval_required")
    if approval:
        create_pending_decision(state, agent_id, approval, source="approval_required")

    observations = execute_actions(root, state, agent_id, response.get("actions", []))
    for observation in observations[-5:]:
        append_log(state, agent["label"], observation)
    if agent_id == "pm":
        for message in state.get("po_messages", []):
            if message.get("status") in {"open", "received"}:
                message["status"] = "handled"
                message["handled_at"] = now()
    if not agent_state.get("blocked"):
        agent_state["status"] = "idle"
    save_state(root, state)


def complete_graceful_stop(root: Path, state: dict, runner_token: str | None = None) -> bool:
    append_log(state, "System", "Graceful stop requested. Agents are stabilizing the current work.", "warning")
    state["status"] = "stopping"
    state["graceful_stop_active"] = True
    save_state(root, state)

    for agent_id in AGENT_ORDER:
        state = load_state(root)
        if runner_token and state.get("runner_token") != runner_token:
            return False
        if state.get("agents", {}).get(agent_id, {}).get("blocked"):
            continue
        run_agent_turn(root, state, agent_id, runner_token)

    state = load_state(root)
    if runner_token and state.get("runner_token") != runner_token:
        return False

    state.pop("graceful_stop_active", None)
    state["stop_requested"] = False
    state["status"] = "stopped"
    state["running"] = False
    state["runner_token"] = None
    state["stopped_at"] = now()
    for agent_state in state.get("agents", {}).values():
        if agent_state.get("status") == "running":
            agent_state["status"] = "idle"
            agent_state["message"] = "Stopped after current work"
            agent_state["updated_at"] = now()
    resume_path = write_resume_notes(root, state)
    append_log(state, "System", f"Gracefully stopped. Resume notes written to {resume_path.relative_to(root)}.", "warning")
    save_state(root, state)
    return True


def run_project_loop(project_id: str, runner_token: str) -> None:
    root = project_root(project_id)
    try:
        for _ in range(MAX_AGENT_STEPS):
            state = load_state(root)
            if state.get("runner_token") not in {None, runner_token}:
                return
            state["runner_token"] = runner_token
            state["running"] = True
            if state.get("status") not in STOPPING_STATUSES:
                state["status"] = "running"
            state["iteration"] = state.get("iteration", 0) + 1
            save_state(root, state)

            if state.get("stop_requested") or state.get("status") == "stopping":
                complete_graceful_stop(root, state, runner_token)
                return

            runnable = [
                agent_id for agent_id in AGENT_ORDER
                if not state.get("agents", {}).get(agent_id, {}).get("blocked")
            ]
            if not runnable:
                state["status"] = "waiting_for_approval"
                append_log(state, "System", "All active work is blocked by PO approval.", "approval")
                save_state(root, state)
                break

            for agent_id in AGENT_ORDER:
                state = load_state(root)
                if state.get("runner_token") != runner_token:
                    return
                if state.get("agents", {}).get(agent_id, {}).get("blocked"):
                    continue
                run_agent_turn(root, state, agent_id, runner_token)
                state = load_state(root)
                if state.get("stop_requested") or state.get("status") == "stopping":
                    complete_graceful_stop(root, state, runner_token)
                    return
                time.sleep(0.2)

            state = load_state(root)
            if state.get("runner_token") != runner_token:
                return
            if state.get("status") == "complete":
                break
            if state.get("pending_decisions"):
                state["status"] = "waiting_for_approval"
            save_state(root, state)
            time.sleep(0.5)
        else:
            state = load_state(root)
            if state.get("runner_token") == runner_token and resumable_project_status(state.get("status")):
                state["status"] = "waiting_for_handoff"
                append_log(
                    state,
                    "System",
                    f"Agent loop paused after {MAX_AGENT_STEPS} steps. Resume when you want another pass.",
                )
                save_state(root, state)
    except Exception as exc:
        state = load_state(root)
        if state.get("runner_token") != runner_token:
            return
        state["status"] = "error"
        append_log(state, "System", str(exc), "error")
        save_state(root, state)
    finally:
        state = load_state(root)
        if state.get("runner_token") == runner_token:
            state["running"] = False
            state["runner_token"] = None
            save_state(root, state)


def runner_thread_alive(project_id: str) -> bool:
    thread = runner_threads.get(project_id)
    return bool(thread and thread.is_alive())


def mark_stale_runner_recovered(root: Path, state: dict, age: float | None, resume: bool = True) -> None:
    stale_agents = []
    for agent_id, agent_state in state.get("agents", {}).items():
        if agent_state.get("status") == "running":
            stale_agents.append(agent_id)
            agent_state["status"] = "idle"
            agent_state["message"] = "Recovered stalled turn; retrying"
            agent_state["updated_at"] = now()

    state["running"] = False
    if resume:
        state["status"] = "running"
    state["runner_token"] = None
    detail = f"{int(age)}s" if age is not None else "unknown duration"
    names = ", ".join(stale_agents) if stale_agents else "project"
    append_log(state, "System", f"Recovered stale runner state for {names} after {detail}.", "warning")
    save_state(root, state)


def maybe_recover_or_resume_runner(root: Path, state: dict) -> None:
    project_id = state.get("id")
    if not project_id:
        return
    if state.get("pending_decisions") and state.get("status") != "stopping":
        return

    age = seconds_since(state.get("updated_at"))
    thread_alive = runner_thread_alive(project_id)
    stale = bool(state.get("running")) and (not thread_alive or age is None or age > RUNNER_STALE_SECONDS)
    if stale:
        should_resume = resumable_project_status(state.get("status"))
        mark_stale_runner_recovered(root, state, age, resume=should_resume)
        if should_resume:
            ensure_runner(project_id, force=True)
        elif state.get("status") == "stopping":
            state = load_state(root)
            state["stop_requested"] = False
            state["status"] = "stopped"
            state["running"] = False
            state["runner_token"] = None
            write_resume_notes(root, state, "Recovered stale graceful stop")
            append_log(state, "System", "Recovered stale graceful stop and wrote resume notes.", "warning")
            save_state(root, state)
    elif resumable_project_status(state.get("status")) and not state.get("running") and not thread_alive:
        ensure_runner(project_id)


def ensure_runner(project_id: str, force: bool = False) -> None:
    thread = runner_threads.get(project_id)
    if thread and thread.is_alive() and not force:
        return
    runner_token = make_id("runner")
    thread = threading.Thread(target=run_project_loop, args=(project_id, runner_token), daemon=True)
    runner_threads[project_id] = thread
    thread.start()


def create_project(payload: dict) -> dict:
    require_gemini_client()
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    name = (payload.get("name") or "Untitled Project").strip()
    vision = (payload.get("vision") or "").strip()
    if not vision:
        raise ValueError("Project vision is required.")
    base_slug = slugify(name)
    slug = base_slug
    counter = 2
    while (PROJECTS_DIR / slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1
    root = PROJECTS_DIR / slug
    (root / ".warroom").mkdir(parents=True, exist_ok=True)

    project_id = f"project-{int(time.time() * 1000)}"
    initial_context = f"""# Project Context

## PO Vision
{vision}

## Operating Model
- PM owns docs/PRD.md, docs/backlog.md, ticket slicing, acceptance criteria, and sequencing.
- Designer owns docs/design_brief.md, docs/wireframes.md, code-based wireframes, and screenshot visual reviews.
- Backend owns docs/api.md/openapi.yaml, docs/database.md, server architecture, APIs, data validation, and business logic.
- Frontend owns the runnable UI, API integration, responsive states, preview URLs, and design-to-code execution.
- QA / SET owns docs/test_plan.md, automated tests, smoke scripts, command execution, screenshot analysis, and defect routing.
- Agents communicate through tickets, handoffs, reviews, logs, command results, previews, and this shared context.
- When one agent needs PO approval, only that agent blocks; other non-dependent work continues.
- PO approval is reserved for product direction, credentials/secrets, paid external services, destructive changes, or mutually exclusive UX/product choices.
- All project files live in this directory.

## Team Communication Protocol
- Create a handoff whenever another role needs to act on your artifact.
- Record reviews with pass/needs_changes/blocked verdicts.
- QA defects must include reproduction steps, expected/actual behavior, accountable owner, and a verification path.
- Designer visual feedback must reference screenshots or preview notes when available.
- Backend/Frontend contract mismatches must reference exact files, endpoints, props, payloads, or states.
"""
    (root / "project_context.md").write_text(initial_context, encoding="utf-8")
    (root / "README.md").write_text(f"# {name}\n\n{vision}\n", encoding="utf-8")

    state = {
        "id": project_id,
        "name": name,
        "slug": slug,
        "vision": vision,
        "directory": str(root),
        "status": "created",
        "running": False,
        "stop_requested": False,
        "iteration": 0,
        "created_at": now(),
        "updated_at": now(),
        "agents": {
            agent_id: {
                "label": AGENTS[agent_id]["label"],
                "role": AGENTS[agent_id]["role"],
                "status": "idle",
                "message": "Ready",
                "blocked": False,
            }
            for agent_id in AGENT_ORDER
        },
        "tickets": [],
        "pending_decisions": [],
        "handoffs": [],
        "reviews": [],
        "approval_history": [],
        "pm_chat": [],
        "po_messages": [],
        "logs": [],
        "command_runs": [],
        "previews": [],
    }
    append_log(state, "System", f"Created project directory: {root}")
    save_state(root, state)
    ensure_runner(project_id)
    return public_state(state)


def approve_decision(project_id: str, payload: dict) -> dict:
    root = project_root(project_id)
    state = load_state(root)
    decision_id = payload.get("decision_id")
    choice_id = payload.get("choice_id")
    note = payload.get("note", "")
    custom_colors = normalize_colors(payload.get("custom_colors"))
    pending = state.get("pending_decisions", [])
    decision = next((item for item in pending if item["id"] == decision_id), None)
    if not decision:
        raise ValueError("Decision not found.")
    state["pending_decisions"] = [item for item in pending if item["id"] != decision_id]
    agent_id = decision.get("agent")
    if agent_id in state.get("agents", {}):
        state["agents"][agent_id]["blocked"] = False
        state["agents"][agent_id]["status"] = "idle"
        state["agents"][agent_id]["message"] = "PO approved; resuming"
    history_entry = {
        "decision_id": decision_id,
        "agent": agent_id,
        "question": decision.get("question", ""),
        "choice_id": choice_id,
        "note": note,
        "time": now(),
    }
    if custom_colors:
        history_entry["custom_colors"] = custom_colors
    state.setdefault("approval_history", []).append(history_entry)
    append_log(
        state,
        "PO",
        f"Approved {decision.get('question')} with choice={choice_id}. {note}".strip(),
        "approval",
    )
    state["status"] = "running"
    save_state(root, state)
    ensure_runner(project_id)
    return public_state(state)


def stop_project(project_id: str) -> dict:
    root = project_root(project_id)
    state = load_state(root)
    if state.get("status") == "stopped":
        return public_state(state)

    state["stop_requested"] = True
    state["stop_requested_at"] = now()
    state["status"] = "stopping"
    append_log(
        state,
        "PO",
        "Requested work stop. Agents will finish the current turn, stabilize runnable work, and write resume notes.",
        "warning",
    )

    if not state.get("running") or not runner_thread_alive(project_id):
        state["graceful_stop_active"] = False
        state["stop_requested"] = False
        state["status"] = "stopped"
        state["running"] = False
        state["runner_token"] = None
        state["stopped_at"] = now()
        resume_path = write_resume_notes(root, state)
        append_log(state, "System", f"Stopped immediately. Resume notes written to {resume_path.relative_to(root)}.", "warning")

    save_state(root, state)
    return public_state(state)


def resume_project(project_id: str) -> dict:
    root = project_root(project_id)
    state = load_state(root)
    state["stop_requested"] = False
    state.pop("graceful_stop_active", None)
    state["status"] = "running"
    for agent_state in state.get("agents", {}).values():
        if agent_state.get("status") not in {"blocked", "error"}:
            agent_state["status"] = "idle"
            agent_state["message"] = f"Resuming from {RESUME_NOTES_PATH}"
            agent_state["updated_at"] = now()
    append_log(state, "PO", f"Resumed work. PM should consult {RESUME_NOTES_PATH}.", "info")
    save_state(root, state)
    ensure_runner(project_id)
    return public_state(load_state(root))


def send_po_message(project_id: str, payload: dict) -> dict:
    message = str(payload.get("message") or "").strip()
    if not message:
        raise ValueError("Message is required.")

    root = project_root(project_id)
    state = load_state(root)

    chat_result = None
    chat_classification_error = None
    if po_message_chat_candidate(message):
        try:
            chat_result = classify_pm_chat_message(root, state, message)
        except Exception as exc:
            chat_classification_error = exc

    entry = {
        "id": make_id("po-message"),
        "from": "PO",
        "to": "pm",
        "message": message,
        "status": "open",
        "created_at": now(),
    }
    state.setdefault("po_messages", []).append(entry)
    state["po_messages"] = state["po_messages"][-100:]
    if chat_classification_error:
        append_log(
            state,
            "PM",
            f"PM chat classification unavailable; queued message for PM work turn: {chat_classification_error}",
            "warning",
        )

    if chat_result and chat_result.get("mode") == "chat":
        append_log(state, "PO", message, "po_message")
        record_pm_chat_reply(state, entry, chat_result.get("reply", ""))
        if "pm" in state.get("agents", {}):
            state["agents"]["pm"]["message"] = chat_result.get("agent_status") or "Answered PO chat"
        save_state(root, state)
        return public_state(load_state(root))

    state.setdefault("handoffs", []).append({
        "id": make_id("handoff"),
        "from": "po",
        "to": "pm",
        "message": message,
        "files": [],
        "ticket_id": "",
        "priority": "high",
        "status": "open",
        "created_at": now(),
    })
    append_log(state, "PO", message, "po_message")

    fallback_needed = (
        po_message_needs_design_selection(message)
        and not state.get("pending_decisions")
        and not gemini_status()["ready"]
    )
    if fallback_needed:
        append_log(state, "PM", "AI is unavailable, so fallback design options were prepared.", "approval")
        create_pending_decision(state, "pm", fallback_design_selection(message), source="fallback_po_message")
        entry["status"] = "awaiting_po_decision"
        entry["decision_id"] = state["pending_decisions"][-1]["id"]

    state["stop_requested"] = False
    state.pop("graceful_stop_active", None)
    if state.get("status") in {"stopped", "stopping", "waiting_for_handoff", "created"}:
        state["status"] = "running"

    save_state(root, state)
    ensure_runner(project_id)
    return public_state(load_state(root))


class WarRoomHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        clean_path = unquote(parsed.path).lstrip("/")
        if clean_path == "":
            clean_path = "index.html"
        target = (ROOT / clean_path).resolve()
        if not str(target).startswith(str(ROOT)):
            return str(ROOT / "index.html")
        return str(target)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        try:
            if parsed.path == "/api/health":
                status = gemini_status()
                self.write_json({
                    "ok": status["ready"],
                    "gemini_ready": status["ready"],
                    "reason": status["reason"],
                    "projects_dir": str(PROJECTS_DIR),
                }, 200)
                return
            if parsed.path == "/api/projects":
                self.write_json({"projects": list_projects()})
                return
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                root = project_root(parts[2])
                state_data = load_state(root)
                maybe_recover_or_resume_runner(root, state_data)
                self.write_json(public_state(load_state(root)))
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "files":
                root = project_root(parts[2])
                state_data = load_state(root)
                directory = state_data.get("directory", "")
                self.write_json({"root": directory, "files": _list_dir(directory)})
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "stats":
                root = project_root(parts[2])
                self.write_json(project_stats(root))
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "search":
                qs = parse_qs(parsed.query)
                query = qs.get("q", [""])[0]
                root = project_root(parts[2])
                self.write_json({"results": search_project(root, query), "query": query})
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "file":
                qs = parse_qs(parsed.query)
                rel = qs.get("path", [""])[0]
                root = project_root(parts[2])
                target = safe_path(root, rel)
                self.write_json({"path": rel, "content": read_excerpt(target, 50_000)})
                return
        except CLIENT_DISCONNECT_ERRORS:
            return
        except Exception as exc:
            self.write_error_json(exc)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        try:
            payload = self.read_json()
            if parsed.path == "/api/projects":
                self.write_json(create_project(payload), 201)
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "resume":
                self.write_json(resume_project(parts[2]))
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "stop":
                self.write_json(stop_project(parts[2]))
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "messages":
                self.write_json(send_po_message(parts[2], payload))
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "approve":
                self.write_json(approve_decision(parts[2], payload))
                return
            self.send_error(404)
        except RuntimeError as exc:
            self.write_error_json(exc, 503, {"gemini_required": True})
        except CLIENT_DISCONNECT_ERRORS:
            return
        except Exception as exc:
            self.write_error_json(exc)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def write_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_error_json(self, exc: Exception, status: int = 500, extra: dict | None = None) -> None:
        payload = {"error": str(exc)}
        if extra:
            payload.update(extra)
        try:
            self.write_json(payload, status)
        except CLIENT_DISCONNECT_ERRORS:
            return

    def guess_type(self, path: str) -> str:
        if path.endswith(".js"):
            return "text/javascript; charset=utf-8"
        if path.endswith(".css"):
            return "text/css; charset=utf-8"
        if path.endswith(".html"):
            return "text/html; charset=utf-8"
        return mimetypes.guess_type(path)[0] or "application/octet-stream"


def main() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), WarRoomHandler)
    print(f"Nicullah running at http://{HOST}:{PORT}")
    print(f"Project directories: {PROJECTS_DIR}")
    print("Gemini is required. Set GEMINI_API_KEY and install google-genai.")
    server.serve_forever()


if __name__ == "__main__":
    main()
