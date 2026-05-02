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
from datetime import datetime, timezone
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
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)

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
]

state_lock = threading.RLock()
runner_threads: dict[str, threading.Thread] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def unresolved_handoff(handoff: dict) -> bool:
    return handoff.get("status") not in {"resolved", "closed"}


def handoff_visible_to(handoff: dict, agent_id: str) -> bool:
    return handoff.get("to") in {agent_id, "all"} or handoff.get("from") == agent_id


def acknowledge_handoffs(state: dict, agent_id: str) -> int:
    count = 0
    for handoff in state.get("handoffs", []):
        if handoff.get("to") in {agent_id, "all"} and handoff.get("status") == "open":
            handoff["status"] = "received"
            handoff["received_at"] = now()
            count += 1
    return count


def public_state(state: dict) -> dict:
    public = dict(state)
    public["directory"] = str(Path(state["directory"]))
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
                node["children"] = _list_dir(entry.path, depth + 1)
            result.append(node)
    except PermissionError:
        pass
    return result


def recent_files(root: Path, limit: int = 10) -> list[dict]:
    ignored = {".warroom", ".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
    files = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if any(part in ignored for part in rel.parts):
            continue
        try:
            files.append({"path": str(rel).replace("\\", "/"), "mtime": int(path.stat().st_mtime)})
        except OSError:
            continue
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files[:limit]


def project_stats(root: Path) -> dict:
    ignored = {".warroom", ".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
    ext_counts: dict[str, int] = {}
    total_lines = 0
    total_files = 0
    total_size = 0
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if any(part in ignored for part in rel.parts):
            continue
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
    ignored = {".warroom", ".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
    try:
        pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)
    except re.error:
        return []
    results = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if any(part in ignored for part in rel.parts):
            continue
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
            projects.append(public_state(json.loads(sp.read_text(encoding="utf-8"))))
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
    ignored = {".warroom", ".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in ignored for part in rel.parts):
            continue
        lines.append(("/" if path.is_dir() else "") + str(rel).replace("\\", "/"))
        if len(lines) >= limit:
            lines.append("... truncated ...")
            break
    return "\n".join(lines) or "(empty project directory)"


def read_excerpt(path: Path, max_chars: int = 10000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"[read failed: {exc}]"
    return text[:max_chars]


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
            "set_status",
            "complete_task",
        ],
    }


def agent_system_instruction(agent_id: str) -> str:
    agent = AGENTS[agent_id]
    return f"""
You are the {agent['role']} in AI Agent War Room.
Mission: {agent['mission']}

Work like a real senior programming team member. Continue proactively when the path is clear.
Ask PO approval only for product direction, external paid services, credentials/secrets,
destructive filesystem operations, irreversible architecture choices, or mutually exclusive UX choices.
If you need approval, return approval_required and mark only your own work blocked; other agents may continue.

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
    {{"type": "capture_preview", "url": "http://127.0.0.1:3000", "note": "what to inspect"}},
    {{"type": "add_ticket", "title": "task", "owner": "frontend", "status": "ready|progress|review|blocked|done", "priority": "P0|P1|P2|P3", "description": "scope", "acceptance_criteria": ["criterion"]}},
    {{"type": "update_ticket", "id": "ticket-id", "status": "ready|progress|review|blocked|done", "notes": "what changed"}},
    {{"type": "handoff", "to": "pm|designer|backend|frontend|qa|all", "message": "next action needed", "files": ["path"], "ticket_id": "ticket-id", "priority": "normal|high"}},
    {{"type": "resolve_handoff", "id": "handoff-id", "resolution": "what was done"}},
    {{"type": "record_review", "target": "pm|designer|backend|frontend|qa", "verdict": "pass|needs_changes|blocked", "findings": ["finding"], "files": ["path"]}},
    {{"type": "set_status", "status": "running|waiting_for_approval|complete"}},
    {{"type": "complete_task", "summary": "what is complete"}}
  ]
}}

Prefer small, safe file edits. Write complete files when creating new files.
Never invent that a command passed unless you ran it. If a role-specific artifact is missing, create or update it before relying on it.

Code navigation guidance:
- Use search_files before editing to locate existing implementations, imports, or usages.
- Use get_project_stats on first turn to understand project scope and language breakdown.
- Results from search_files and get_project_stats appear in your next turn's context under search_results and project_stats.
- recently_modified_files in your context shows what changed most recently — read those before writing to avoid conflicts.
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


def resolve_command(root: Path, command: list[str]) -> list[str]:
    if not command or not isinstance(command, list):
        raise ValueError("run_command requires a command array")
    blocked = {"rm", "del", "erase", "rmdir", "rd", "format", "shutdown", "powershell", "cmd"}
    executable = str(command[0]).lower()
    if executable in blocked:
        raise ValueError(f"Command is not allowed: {command[0]}")
    if executable == "python":
        return [sys.executable] + [str(item) for item in command[1:]]

    raw_executable = str(command[0])
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
    completed = subprocess.run(
        command,
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
        shell=False,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-5000:],
        "stderr": completed.stderr[-5000:],
    }


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
                if find not in text:
                    raise ValueError(f"find text not found in {action['path']}")
                target.write_text(text.replace(find, action.get("replace", ""), 1), encoding="utf-8")
                observations.append(f"edited {action['path']}")
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
                observations.append(f"captured preview {result['screenshot']}: {result['review'][:800]}")
            elif action_type == "add_ticket":
                ticket = {
                    "id": make_id("ticket"),
                    "title": action.get("title", "Untitled task"),
                    "owner": action.get("owner", agent_id),
                    "status": action.get("status", "ready"),
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
                        ticket[key] = action[key]
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
            elif action_type == "set_status":
                state["status"] = action.get("status", state.get("status", "running"))
                observations.append(f"status set to {state['status']}")
            elif action_type == "complete_task":
                observations.append(action.get("summary", "task complete"))
            else:
                observations.append(f"ignored unknown action {action_type}")
        except Exception as exc:
            observations.append(f"{action_type} failed: {exc}")
            append_log(state, AGENTS[agent_id]["label"], f"{action_type} failed: {exc}", "error")
    return observations


def run_agent_turn(root: Path, state: dict, agent_id: str) -> None:
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
        agent_state["status"] = "error"
        agent_state["message"] = str(exc)[:160]
        append_log(state, agent["label"], f"agent turn failed: {exc}", "error")
        save_state(root, state)
        return

    agent_state["message"] = response.get("agent_status") or response.get("summary") or "Updated"
    append_log(state, agent["label"], response.get("summary", "completed a work turn"))

    approval = response.get("approval_required")
    if approval:
        decision = {
            "id": f"decision-{int(time.time() * 1000)}",
            "agent": agent_id,
            "question": approval.get("question", "Approval required"),
            "options": approval.get("options", []),
            "created_at": now(),
        }
        state.setdefault("pending_decisions", []).append(decision)
        if approval.get("blocks_agent", True):
            agent_state["blocked"] = True
            agent_state["status"] = "blocked"
            agent_state["message"] = "Waiting for PO approval"
        append_log(state, agent["label"], f"requested PO approval: {decision['question']}", "approval")

    observations = execute_actions(root, state, agent_id, response.get("actions", []))
    for observation in observations[-5:]:
        append_log(state, agent["label"], observation)
    if not agent_state.get("blocked"):
        agent_state["status"] = "idle"
    save_state(root, state)


def run_project_loop(project_id: str) -> None:
    root = project_root(project_id)
    try:
        for _ in range(MAX_AGENT_STEPS):
            state = load_state(root)
            state["running"] = True
            state["status"] = "running"
            state["iteration"] = state.get("iteration", 0) + 1
            save_state(root, state)

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
                if state.get("agents", {}).get(agent_id, {}).get("blocked"):
                    continue
                run_agent_turn(root, state, agent_id)
                time.sleep(0.2)

            state = load_state(root)
            if state.get("status") == "complete":
                break
            if state.get("pending_decisions"):
                state["status"] = "waiting_for_approval"
            save_state(root, state)
            time.sleep(0.5)
    except Exception as exc:
        state = load_state(root)
        state["status"] = "error"
        append_log(state, "System", str(exc), "error")
        save_state(root, state)
    finally:
        state = load_state(root)
        state["running"] = False
        save_state(root, state)


def ensure_runner(project_id: str) -> None:
    thread = runner_threads.get(project_id)
    if thread and thread.is_alive():
        return
    thread = threading.Thread(target=run_project_loop, args=(project_id,), daemon=True)
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
    state.setdefault("approval_history", []).append({
        "decision_id": decision_id,
        "agent": agent_id,
        "question": decision.get("question", ""),
        "choice_id": choice_id,
        "note": note,
        "time": now(),
    })
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
                ensure_runner(parts[2])
                self.write_json(public_state(load_state(project_root(parts[2]))))
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
    print(f"AI Agent War Room running at http://{HOST}:{PORT}")
    print(f"Project directories: {PROJECTS_DIR}")
    print("Gemini is required. Set GEMINI_API_KEY and install google-genai.")
    server.serve_forever()


if __name__ == "__main__":
    main()
