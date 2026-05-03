import sys
import shutil
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import app  # noqa: E402
from gemini_retry_handler import GeminiRetryHandler  # noqa: E402


@contextmanager
def workspace_tempdir():
    base = ROOT / "test_tmp"
    path = base / f"test-{time.time_ns()}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


class TeamProtocolTests(unittest.TestCase):
    def test_gemini_request_timeout_does_not_block_agent_loop(self):
        class SlowChat:
            def send_message(self, message):
                time.sleep(1)
                return type("Response", (), {"text": "{}"})()

        class SlowClient:
            class Chats:
                def create(self, **kwargs):
                    return SlowChat()

            chats = Chats()

        handler = GeminiRetryHandler(
            SlowClient(),
            sleep_func=lambda _: None,
            request_timeout=0.01,
        )

        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            handler.generate_response(history=[], message="{}", system_instruction="test")

        self.assertLess(time.monotonic() - started, 0.5)

    def test_extract_json_accepts_fenced_json_and_trailing_commas(self):
        payload = app.extract_json(
            """```json
{
  "summary": "ok",
  "agent_status": "ready",
  "approval_required": null,
  "actions": [
    {"type": "set_status", "status": "running"},
  ],
}
```"""
        )

        self.assertEqual(payload["summary"], "ok")
        self.assertEqual(payload["actions"][0]["type"], "set_status")

    def test_extract_json_accepts_fenced_json_with_extra_words(self):
        payload = app.extract_json(
            """Here is the JSON you asked for:

```json
{"summary": "ok", "agent_status": "ready", "actions": []}
```

Hope this helps."""
        )

        self.assertEqual(payload["summary"], "ok")

    def test_extract_json_skips_bad_blocks_until_valid_json(self):
        payload = app.extract_json(
            """Do not use this example:

```text
{not valid json}
```

Use this:

```json
{"summary": "real", "actions": []}
```"""
        )

        self.assertEqual(payload["summary"], "real")

    def test_extract_json_accepts_inline_backtick_json(self):
        payload = app.extract_json('`{"summary": "inline", "actions": []}`')

        self.assertEqual(payload["summary"], "inline")

    def test_extract_json_uses_first_balanced_object(self):
        payload = app.extract_json(
            'Here is the update: {"summary": "ok", "actions": []}\nExtra note.'
        )

        self.assertEqual(payload["summary"], "ok")

    def test_extract_json_repairs_missing_comma_between_fields(self):
        payload = app.extract_json(
            '{"summary": "Long status with punctuation." "agent_status": "ready", "actions": []}'
        )

        self.assertEqual(payload["agent_status"], "ready")

    def test_empty_agent_response_uses_valid_protocol_shape(self):
        payload = app.empty_agent_response("empty response")

        self.assertIsNone(payload["approval_required"])
        self.assertEqual(payload["actions"], [])

    def test_stale_runner_state_is_recovered_and_resumed(self):
        root = ROOT / "demo"
        state = {
            "id": "project-demo",
            "status": "running",
            "running": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "pending_decisions": [],
            "agents": {
                "frontend": {
                    "status": "running",
                    "message": "Working",
                    "blocked": False,
                }
            },
            "logs": [],
        }

        with patch("app.runner_thread_alive", return_value=False), patch("app.save_state") as save_state, patch(
            "app.ensure_runner"
        ) as ensure_runner:
            app.maybe_recover_or_resume_runner(root, state)

        self.assertFalse(state["running"])
        self.assertEqual(state["agents"]["frontend"]["status"], "idle")
        self.assertIn("Recovered stale runner state", state["logs"][-1]["message"])
        save_state.assert_called_once_with(root, state)
        ensure_runner.assert_called_once_with("project-demo", force=True)

    def test_stale_paused_runner_is_recovered_without_resuming(self):
        root = ROOT / "demo"
        state = {
            "id": "project-demo",
            "status": "waiting_for_handoff",
            "running": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "pending_decisions": [],
            "agents": {
                "frontend": {
                    "status": "running",
                    "message": "Working",
                    "blocked": False,
                }
            },
            "logs": [],
        }

        with patch("app.runner_thread_alive", return_value=False), patch("app.save_state") as save_state, patch(
            "app.ensure_runner"
        ) as ensure_runner:
            app.maybe_recover_or_resume_runner(root, state)

        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "waiting_for_handoff")
        self.assertEqual(state["agents"]["frontend"]["status"], "idle")
        save_state.assert_called_once_with(root, state)
        ensure_runner.assert_not_called()

    def test_ticket_status_aliases_are_normalized(self):
        state = {"tickets": [], "handoffs": [], "reviews": [], "logs": []}

        app.execute_actions(
            ROOT,
            state,
            "pm",
            [
                {"type": "add_ticket", "title": "Old todo", "status": "todo"},
                {"type": "add_ticket", "title": "Old progress", "status": "in_progress"},
            ],
        )

        self.assertEqual(state["tickets"][0]["status"], "ready")
        self.assertEqual(state["tickets"][1]["status"], "progress")

    def test_resolve_command_maps_unix_venv_path_on_windows(self):
        root = ROOT / "demo"
        expected = str(root / "backend" / "venv" / "Scripts" / "pip.exe")
        with patch("app.shutil.which", return_value=None), patch(
            "app.Path.is_file",
            lambda path: str(path) == expected,
        ):
            command = app.resolve_command(
                root,
                ["backend/venv/bin/pip", "install", "-r", "requirements.txt"],
            )

        if sys.platform == "win32":
            self.assertEqual(command[0], expected)
        else:
            self.assertEqual(command[0], "backend/venv/bin/pip")

    def test_resolve_command_prefers_workspace_venv_for_python(self):
        root = ROOT / "demo"
        expected_path = ROOT / ".venv"
        expected_path = expected_path / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        expected = str(expected_path)

        with patch("app.Path.is_file", lambda path: str(path) == expected):
            command = app.resolve_command(root, ["python", "-m", "backend.main"])

        self.assertEqual(command[0], expected)

    def test_resolve_command_replaces_absolute_system_python_with_workspace_venv(self):
        root = ROOT / "demo"
        system_python = r"C:\Users\eremb\AppData\Local\Programs\Python\Python312\python.exe"
        expected_path = ROOT / ".venv"
        expected_path = expected_path / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        expected = str(expected_path)

        with patch("app.Path.is_file", lambda path: str(path) == expected):
            command = app.resolve_command(root, [system_python, "-m", "backend.main"])

        self.assertEqual(command[0], expected)

    def test_team_actions_create_ticket_handoff_and_review(self):
        state = {"tickets": [], "handoffs": [], "reviews": [], "logs": []}

        observations = app.execute_actions(
            ROOT,
            state,
            "qa",
            [
                {
                    "type": "add_ticket",
                    "title": "Fix CTA contrast",
                    "owner": "frontend",
                    "status": "ready",
                    "acceptance_criteria": ["CTA passes visual review"],
                },
                {
                    "type": "handoff",
                    "to": "frontend",
                    "message": "Fix CTA contrast before release.",
                    "files": ["docs/test_plan.md"],
                },
                {
                    "type": "record_review",
                    "target": "frontend",
                    "verdict": "needs_changes",
                    "findings": ["Primary CTA contrast is too low"],
                    "files": [".warroom/screenshots/preview.png"],
                },
            ],
        )

        self.assertIn("added ticket Fix CTA contrast", observations)
        self.assertEqual(len(state["tickets"]), 1)
        self.assertEqual(state["tickets"][0]["owner"], "frontend")
        self.assertEqual(len(state["handoffs"]), 2)
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(app.acknowledge_handoffs(state, "frontend"), 2)
        self.assertTrue(all(item["status"] == "received" for item in state["handoffs"]))

    def test_set_status_complete_is_ignored_with_unfinished_work(self):
        state = {
            "status": "running",
            "tickets": [{"id": "T1", "title": "Fix API", "status": "progress"}],
            "handoffs": [],
            "pending_decisions": [],
            "po_messages": [],
            "logs": [],
        }

        observations = app.execute_actions(
            ROOT,
            state,
            "designer",
            [{"type": "set_status", "status": "complete"}],
        )

        self.assertEqual(state["status"], "running")
        self.assertIn("ignored complete project status with unfinished work: 1 open ticket(s)", observations)

    def test_set_status_complete_is_allowed_when_project_has_no_blockers(self):
        state = {
            "status": "running",
            "tickets": [{"id": "T1", "title": "Fix API", "status": "done"}],
            "handoffs": [{"id": "H1", "status": "resolved"}],
            "pending_decisions": [],
            "po_messages": [{"id": "P1", "status": "handled"}],
            "logs": [],
        }

        observations = app.execute_actions(
            ROOT,
            state,
            "pm",
            [{"type": "set_status", "status": "complete"}],
        )

        self.assertEqual(state["status"], "complete")
        self.assertIn("status set to complete", observations)

    def test_agent_level_wait_status_keeps_project_running(self):
        state = {
            "status": "running",
            "tickets": [],
            "handoffs": [],
            "pending_decisions": [],
            "logs": [],
        }

        observations = app.execute_actions(
            ROOT,
            state,
            "qa",
            [{"type": "set_status", "status": "waiting_for_agent"}],
        )

        self.assertEqual(state["status"], "running")
        self.assertIn("kept project running; waiting_for_agent is an agent-level status", observations)

    def test_set_status_running_is_ignored_while_stop_requested(self):
        state = {
            "status": "stopping",
            "stop_requested": True,
            "tickets": [],
            "handoffs": [],
            "pending_decisions": [],
            "logs": [],
        }

        observations = app.execute_actions(
            ROOT,
            state,
            "qa",
            [
                {"type": "set_status", "status": "running"},
                {"type": "set_status", "status": "waiting_for_agent"},
            ],
        )

        self.assertEqual(state["status"], "stopping")
        self.assertIn("ignored running project status while stop is requested", observations)
        self.assertIn("kept project running; waiting_for_agent is an agent-level status", observations)

    def test_run_agent_turn_preserves_concurrent_stop_request(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            (root / ".warroom").mkdir(parents=True)
            state = {
                "id": "project-demo",
                "status": "running",
                "running": True,
                "runner_token": "runner-1",
                "stop_requested": False,
                "agents": {"pm": {"status": "idle", "message": "Ready", "blocked": False}},
                "handoffs": [],
                "pending_decisions": [],
                "po_messages": [],
                "logs": [],
            }
            app.save_state(root, state)

            def request_stop(*_):
                latest = app.load_state(root)
                latest["stop_requested"] = True
                latest["status"] = "stopping"
                app.save_state(root, latest)
                return {
                    "summary": "Finished after stop was requested",
                    "agent_status": "Trying to resume",
                    "approval_required": None,
                    "actions": [{"type": "set_status", "status": "running"}],
                }

            with patch("app.call_agent", side_effect=request_stop):
                app.run_agent_turn(root, app.load_state(root), "pm", "runner-1")

            final_state = app.load_state(root)
            self.assertTrue(final_state["stop_requested"])
            self.assertEqual(final_state["status"], "stopping")
            self.assertTrue(any(
                "ignored running project status while stop is requested" in item["message"]
                for item in final_state["logs"]
            ))

    def test_edit_file_allows_indentation_normalized_match(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            target = root / "backend" / "services.py"
            target.parent.mkdir(parents=True)
            target.write_text("def load():\n    return {'ok': True}\n", encoding="utf-8")

            observations = app.execute_actions(
                root,
                {"logs": []},
                "backend",
                [
                    {
                        "type": "edit_file",
                        "path": "backend/services.py",
                        "find": "def load():\nreturn {'ok': True}",
                        "replace": "def load():\n    return {'ok': False}",
                    }
                ],
            )

            self.assertIn("edited backend/services.py (indentation-normalized match)", observations)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "def load():\n    return {'ok': False}\n",
            )

    def test_edit_file_find_miss_reports_current_context(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            target = root / "backend" / "services.py"
            target.parent.mkdir(parents=True)
            target.write_text("def service():\n    return 'current'\n", encoding="utf-8")
            state = {"logs": []}

            observations = app.execute_actions(
                root,
                state,
                "backend",
                [
                    {
                        "type": "edit_file",
                        "path": "backend/services.py",
                        "find": "def service():\n    return 'old'\n",
                        "replace": "def service():\n    return 'new'\n",
                    }
                ],
            )

            self.assertIn("edit_file failed: find text not found in backend/services.py", observations[0])
            self.assertIn("Closest current excerpt", observations[0])
            self.assertIn("L1: def service():", observations[0])
            self.assertIn("Read the file or search for a stable anchor", observations[0])
            self.assertEqual(target.read_text(encoding="utf-8"), "def service():\n    return 'current'\n")
            self.assertEqual(state["logs"][-1]["level"], "error")

    def test_edit_file_treats_already_applied_replacement_as_noop(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            target = root / "docs" / "backlog.md"
            target.parent.mkdir(parents=True)
            current = (
                "* **Ticket:** `ticket-ui-spacing` - **Spacing**\n"
                "  * **Status:** Done\n"
                "  * **Notes:** Resolved by Frontend.\n"
            )
            target.write_text(current, encoding="utf-8")
            state = {"logs": []}

            observations = app.execute_actions(
                root,
                state,
                "pm",
                [
                    {
                        "type": "edit_file",
                        "path": "docs/backlog.md",
                        "find": (
                            "* **Ticket:** `ticket-ui-spacing` - **Spacing**\n"
                            "  * **Status:** Ready\n"
                            "  * **Notes:** Resolved by Frontend.\n"
                        ),
                        "replace": current,
                    }
                ],
            )

            self.assertIn("edit skipped docs/backlog.md (already applied)", observations)
            self.assertEqual(target.read_text(encoding="utf-8"), current)
            self.assertEqual(state["logs"], [])

    def test_edit_file_uses_unique_fuzzy_window_for_stale_multiline_anchor(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            target = root / "docs" / "backlog.md"
            target.parent.mkdir(parents=True)
            target.write_text(
                "* **Ticket:** `ticket-ui-spacing` - **Spacing**\n"
                "  * **Owner:** frontend\n"
                "  * **Priority:** P3\n"
                "  * **Status:** Done\n"
                "  * **Description:** Buttons are inconsistently spaced.\n"
                "  * **Acceptance Criteria:**\n"
                "    * Buttons use consistent spacing.\n",
                encoding="utf-8",
            )
            replacement = (
                "* **Ticket:** `ticket-ui-spacing` - **Spacing**\n"
                "  * **Owner:** frontend\n"
                "  * **Priority:** P3\n"
                "  * **Status:** Done\n"
                "  * **Description:** Buttons are inconsistently spaced.\n"
                "  * **Acceptance Criteria:**\n"
                "    * Buttons use consistent spacing.\n"
                "  * **Notes:** Resolved by Frontend.\n"
            )

            observations = app.execute_actions(
                root,
                {"logs": []},
                "pm",
                [
                    {
                        "type": "edit_file",
                        "path": "docs/backlog.md",
                        "find": (
                            "* **Ticket:** `ticket-ui-spacing` - **Spacing**\n"
                            "  * **Owner:** frontend\n"
                            "  * **Priority:** P3\n"
                            "  * **Status:** Ready\n"
                            "  * **Description:** Buttons are inconsistently spaced.\n"
                            "  * **Acceptance Criteria:**\n"
                            "    * Buttons use consistent spacing.\n"
                        ),
                        "replace": replacement,
                    }
                ],
            )

            self.assertIn("edited docs/backlog.md (fuzzy-line-window match)", observations)
            self.assertEqual(target.read_text(encoding="utf-8"), replacement)

    def test_edit_file_rejects_ambiguous_fuzzy_window(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            target = root / "docs" / "backlog.md"
            target.parent.mkdir(parents=True)
            block = (
                "* **Ticket:** `ticket-ui-spacing` - **Spacing**\n"
                "  * **Owner:** frontend\n"
                "  * **Priority:** P3\n"
                "  * **Status:** Done\n"
                "  * **Description:** Buttons are inconsistently spaced.\n"
            )
            target.write_text(block + "\n" + block, encoding="utf-8")
            original = target.read_text(encoding="utf-8")
            state = {"logs": []}

            observations = app.execute_actions(
                root,
                state,
                "pm",
                [
                    {
                        "type": "edit_file",
                        "path": "docs/backlog.md",
                        "find": (
                            "* **Ticket:** `ticket-ui-spacing` - **Spacing**\n"
                            "  * **Owner:** frontend\n"
                            "  * **Priority:** P3\n"
                            "  * **Status:** Ready\n"
                            "  * **Description:** Buttons are inconsistently spaced.\n"
                        ),
                        "replace": block + "  * **Notes:** Resolved by Frontend.\n",
                    }
                ],
            )

            self.assertIn("edit_file failed: find text matched multiple fuzzy locations", observations[0])
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(state["logs"][-1]["level"], "error")

    def test_request_po_approval_action_blocks_only_requesting_agent(self):
        state = {
            "agents": {
                "pm": {"status": "idle", "message": "Ready", "blocked": False},
                "frontend": {"status": "idle", "message": "Ready", "blocked": False},
            },
            "pending_decisions": [],
            "logs": [],
        }

        observations = app.execute_actions(
            ROOT,
            state,
            "pm",
            [
                {
                    "type": "request_po_approval",
                    "question": "Choose a UI direction",
                    "options": [{"id": "modern", "title": "Modern", "description": "Cleaner"}],
                    "blocks_agent": True,
                }
            ],
        )

        self.assertIn("requested PO approval", observations[0])
        self.assertEqual(state["pending_decisions"][0]["question"], "Choose a UI direction")
        self.assertTrue(state["agents"]["pm"]["blocked"])
        self.assertFalse(state["agents"]["frontend"]["blocked"])
        self.assertEqual(state["pending_decisions"][0]["options"][0]["colors"], [])

    def test_request_po_approval_can_enable_color_selection(self):
        state = {
            "agents": {"designer": {"status": "idle", "message": "Ready", "blocked": False}},
            "pending_decisions": [],
            "logs": [],
        }

        app.execute_actions(
            ROOT,
            state,
            "designer",
            [
                {
                    "type": "request_po_approval",
                    "question": "Choose a palette",
                    "options": [
                        {
                            "id": "calm",
                            "title": "Calm",
                            "description": "Muted palette",
                            "colors": ["#101413", "not-a-color", "#4cc9f0"],
                        }
                    ],
                    "allow_colors": True,
                }
            ],
        )

        decision = state["pending_decisions"][0]
        self.assertTrue(decision["allow_colors"])
        self.assertEqual(decision["options"][0]["colors"], ["#101413", "#4cc9f0"])

    def test_po_message_resumes_stopped_project_and_handoffs_to_pm(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            (root / ".warroom").mkdir(parents=True)
            state = {
                "id": "project-demo",
                "name": "Demo",
                "vision": "Build a product",
                "directory": str(root),
                "status": "stopped",
                "running": False,
                "agents": {"pm": {"status": "idle", "message": "Ready", "blocked": False}},
                "pending_decisions": [],
                "handoffs": [],
                "po_messages": [],
                "logs": [],
            }
            app.save_state(root, state)

            with patch("app.project_root", return_value=root), patch("app.ensure_runner") as ensure_runner:
                public = app.send_po_message("project-demo", {"message": "다크 모드를 추가해줘"})

        self.assertEqual(public["status"], "running")
        self.assertEqual(public["po_messages"][0]["message"], "다크 모드를 추가해줘")
        self.assertEqual(public["handoffs"][0]["to"], "pm")
        ensure_runner.assert_called_once_with("project-demo")

    def test_broad_ui_po_message_queues_pm_when_ai_ready(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            (root / ".warroom").mkdir(parents=True)
            state = {
                "id": "project-demo",
                "name": "Demo",
                "vision": "Build a product",
                "directory": str(root),
                "status": "running",
                "running": False,
                "agents": {"pm": {"status": "idle", "message": "Ready", "blocked": False}},
                "pending_decisions": [],
                "handoffs": [],
                "po_messages": [],
                "logs": [],
            }
            app.save_state(root, state)

            with patch("app.project_root", return_value=root), patch("app.ensure_runner"), patch(
                "app.gemini_status",
                return_value={"ready": True, "reason": ""},
            ):
                public = app.send_po_message("project-demo", {"message": "UI를 더 모던하게 바꿔줘"})

        self.assertEqual(public["pending_decisions"], [])
        self.assertFalse(public["agents"]["pm"]["blocked"])
        self.assertEqual(public["po_messages"][0]["status"], "open")

    def test_broad_ui_po_message_uses_fallback_only_when_ai_unavailable(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            (root / ".warroom").mkdir(parents=True)
            state = {
                "id": "project-demo",
                "name": "Demo",
                "vision": "Build a product",
                "directory": str(root),
                "status": "running",
                "running": False,
                "agents": {"pm": {"status": "idle", "message": "Ready", "blocked": False}},
                "pending_decisions": [],
                "handoffs": [],
                "po_messages": [],
                "logs": [],
            }
            app.save_state(root, state)

            with patch("app.project_root", return_value=root), patch("app.ensure_runner"), patch(
                "app.gemini_status",
                return_value={"ready": False, "reason": "offline"},
            ):
                public = app.send_po_message("project-demo", {"message": "UI를 더 모던하게 바꿔줘"})

        self.assertEqual(len(public["pending_decisions"]), 1)
        self.assertEqual(public["pending_decisions"][0]["agent"], "pm")
        self.assertTrue(public["agents"]["pm"]["blocked"])
        self.assertEqual(public["po_messages"][0]["status"], "awaiting_po_decision")

    def test_question_po_message_records_pm_chat_without_resuming_work(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            (root / ".warroom").mkdir(parents=True)
            state = {
                "id": "project-demo",
                "name": "Demo",
                "vision": "Build a product",
                "directory": str(root),
                "status": "stopped",
                "running": False,
                "agents": {"pm": {"status": "idle", "message": "Ready", "blocked": False}},
                "pending_decisions": [],
                "handoffs": [],
                "po_messages": [],
                "pm_chat": [],
                "logs": [],
            }
            app.save_state(root, state)

            with patch("app.project_root", return_value=root), patch("app.ensure_runner") as ensure_runner, patch(
                "app.classify_pm_chat_message",
                return_value={
                    "mode": "chat",
                    "reply": "The project is currently stopped.",
                    "agent_status": "Answered status question",
                },
            ):
                public = app.send_po_message("project-demo", {"message": "What is the current status?"})

        self.assertEqual(public["status"], "stopped")
        self.assertEqual(public["po_messages"][0]["status"], "chat_replied")
        self.assertEqual([item["from"] for item in public["pm_chat"]], ["PO", "PM"])
        self.assertEqual(public["pm_chat"][1]["message"], "The project is currently stopped.")
        self.assertEqual(public["agents"]["pm"]["message"], "Answered status question")
        ensure_runner.assert_not_called()

    def test_reply_to_po_action_records_chat_memory(self):
        state = {
            "agents": {"pm": {"status": "idle", "message": "Ready", "blocked": False}},
            "po_messages": [
                {
                    "id": "po-message-1",
                    "from": "PO",
                    "to": "pm",
                    "message": "Can we ship today?",
                    "status": "open",
                }
            ],
            "pm_chat": [],
            "logs": [],
        }

        observations = app.execute_actions(
            ROOT,
            state,
            "pm",
            [{"type": "reply_to_po", "message_id": "po-message-1", "message": "Not yet; QA is still open."}],
        )

        self.assertIn("replied to PO chat", observations)
        self.assertEqual(state["po_messages"][0]["status"], "chat_replied")
        self.assertEqual([item["from"] for item in state["pm_chat"]], ["PO", "PM"])
        self.assertEqual(state["pm_chat"][1]["message"], "Not yet; QA is still open.")

    def test_stop_project_writes_resume_notes_when_not_running(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            (root / ".warroom").mkdir(parents=True)
            state = {
                "id": "project-demo",
                "name": "Demo",
                "vision": "Build a product",
                "directory": str(root),
                "status": "running",
                "running": False,
                "agents": {"pm": {"status": "idle", "message": "Ready", "blocked": False}},
                "tickets": [{"id": "T1", "title": "Next task", "status": "ready", "owner": "pm"}],
                "pending_decisions": [],
                "handoffs": [],
                "po_messages": [],
                "logs": [],
            }
            app.save_state(root, state)

            with patch("app.project_root", return_value=root):
                public = app.stop_project("project-demo")

            resume_notes = root / app.RESUME_NOTES_PATH
            self.assertEqual(public["status"], "stopped")
            self.assertTrue(resume_notes.exists())
            self.assertIn("Resume Notes", resume_notes.read_text(encoding="utf-8"))

    def test_collect_context_exposes_shared_team_memory(self):
        state = {
            "name": "Demo",
            "vision": "Build a product",
            "status": "running",
            "iteration": 2,
            "agents": {},
            "tickets": [],
            "pending_decisions": [],
            "handoffs": [
                {
                    "id": "handoff-1",
                    "from": "backend",
                    "to": "frontend",
                    "message": "Use docs/api.md for contract.",
                    "status": "open",
                }
            ],
            "reviews": [{"target": "frontend", "verdict": "pass"}],
            "command_runs": [{"returncode": 0}],
            "previews": [{"review": "layout ok"}],
            "approval_history": [],
            "pm_chat": [{"from": "PO", "message": "What is left?"}],
            "logs": [],
        }

        context = app.collect_context(ROOT, state, "frontend")

        self.assertEqual(context["handoffs"]["inbox"][0]["message"], "Use docs/api.md for contract.")
        self.assertIn("README.md", context["file_excerpts"])
        self.assertIn("record_review", context["allowed_actions"])
        self.assertEqual(context["previews"][0]["review"], "layout ok")
        self.assertEqual(context["pm_chat"][0]["message"], "What is left?")

    def test_project_tree_prunes_dependency_directories(self):
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "App.jsx").write_text("export default function App() {}", encoding="utf-8")
            (root / "node_modules" / "huge-package").mkdir(parents=True)
            (root / "node_modules" / "huge-package" / "index.js").write_text("ignored", encoding="utf-8")

            tree = app.project_tree(root)
            stats = app.project_stats(root)

        self.assertIn("src/App.jsx", tree)
        self.assertNotIn("node_modules", tree)
        self.assertEqual(stats["total_files"], 1)


if __name__ == "__main__":
    unittest.main()
