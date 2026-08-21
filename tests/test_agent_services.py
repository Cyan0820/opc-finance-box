import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


def finance_fixture():
    return {
        "close": {"tasks": [{
            "id": "C15", "name": "负责人确认关账并冻结账期", "stream": "关账",
            "owner": "公司负责人", "status": "待处理", "blockers": [], "deadline": "D+7",
            "evidence_required": True,
        }]},
        "data_coverage": {"bank_transactions": 2},
        "vouchers": [{"entity_id": "cn_studio", "original_currency": "CNY"}],
    }


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "global_game_studio.json", ROOT / "packs")
        self.registry = build_default_service_registry()

    def test_goal_draft_is_scoped_deterministic_and_not_persisted(self):
        payload = {"objective": "完成七月月结", "period": "2026-07", "actor": "负责人"}
        first = self.registry.dispatch(
            self.runtime, "agent.create_goal_draft", payload, entity_ids=["cn_studio"]
        )["output"]
        second = self.registry.dispatch(
            self.runtime, "agent.create_goal_draft", payload, entity_ids=["cn_studio"]
        )["output"]
        self.assertEqual(first["goal"]["id"], second["goal"]["id"])
        self.assertEqual(first["goal"]["entity_ids"], ["cn_studio"])
        self.assertFalse(first["state_changed"])
        self.assertEqual(first["output_status"], "draft_not_persisted")

    def test_plan_is_recomputed_from_scoped_facts_without_mutating_input(self):
        goal = {
            "id": "GOAL-DRAFT-1", "objective": "完成七月月结", "period": "2026-07",
            "entity_ids": ["cn_studio"], "status": "draft", "decisions": [],
        }
        result = self.registry.dispatch(
            self.runtime,
            "agent.build_plan_snapshot",
            {"goal": goal, "finance": finance_fixture(), "period_state": {"status": "开放"}},
            entity_ids=["cn_studio"],
        )["output"]
        self.assertFalse(result["state_changed"])
        self.assertEqual(result["plan"]["status"], "等待确认")
        self.assertEqual(goal["status"], "draft")

    def test_plan_rejects_cross_entity_facts(self):
        finance = finance_fixture()
        finance["vouchers"][0]["entity_id"] = "sg_publisher"
        with self.assertRaises(ValueError):
            self.registry.dispatch(
                self.runtime,
                "agent.build_plan_snapshot",
                {"goal": {"period": "2026-07", "entity_ids": ["cn_studio"]}, "finance": finance},
                entity_ids=["cn_studio"],
            )

    def test_approval_is_a_draft_and_only_accepts_configured_gate(self):
        result = self.registry.dispatch(
            self.runtime,
            "agent.create_approval_event_draft",
            {
                "target_id": "C15", "gate": "period_close", "decision": "同意",
                "actor": "负责人", "rationale": "底稿已经核验", "evidence": ["close-checklist"],
            },
            entity_ids=["cn_studio"],
        )["output"]
        self.assertEqual(result["approval_event"]["decision"], "approved")
        self.assertFalse(result["state_changed"])
        with self.assertRaises(ValueError):
            self.registry.dispatch(
                self.runtime,
                "agent.create_approval_event_draft",
                {"target_id": "C15", "gate": "unknown", "decision": "同意", "actor": "负责人", "rationale": "充分依据"},
                entity_ids=["cn_studio"],
            )


if __name__ == "__main__":
    unittest.main()
