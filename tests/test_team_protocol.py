import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import app  # noqa: E402


class TeamProtocolTests(unittest.TestCase):
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
            "logs": [],
        }

        context = app.collect_context(ROOT, state, "frontend")

        self.assertEqual(context["handoffs"]["inbox"][0]["message"], "Use docs/api.md for contract.")
        self.assertIn("README.md", context["file_excerpts"])
        self.assertIn("record_review", context["allowed_actions"])
        self.assertEqual(context["previews"][0]["review"], "layout ok")


if __name__ == "__main__":
    unittest.main()
