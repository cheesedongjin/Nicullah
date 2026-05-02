import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import app  # noqa: E402


class TeamProtocolTests(unittest.TestCase):
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
