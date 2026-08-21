import tempfile
import unittest
from pathlib import Path

from src.agent_runtime import AgentRuntimeStore, build_goal_snapshot


def finance_fixture(task_status="已完成"):
    return {
        "close": {"tasks": [
            {
                "id": "C01", "name": "锁定账期与资料清单", "stream": "月结准备",
                "owner": "Agent", "status": "已完成", "blockers": [], "deadline": "D+1",
                "evidence_required": False,
            },
            {
                "id": "C05", "name": "归集采购、外包、报销及待摊费用", "stream": "成本费用",
                "owner": "业务负责人", "status": task_status,
                "blockers": [] if task_status == "已完成" else ["缺少验收事实"], "deadline": "D+3",
                "evidence_required": True,
                "decision_support": {"recommendation": "按实际验收进度确认成本。"},
            },
            {
                "id": "C15", "name": "负责人确认关账并冻结账期", "stream": "关账",
                "owner": "公司负责人", "status": "待处理", "blockers": [], "deadline": "D+7",
                "evidence_required": True,
            },
        ]},
        "data_coverage": {"settlement_records": 120000, "bank_transactions": 3000},
        "vouchers": [],
    }


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AgentRuntimeStore(Path(self.temp.name) / "agent")

    def tearDown(self):
        self.temp.cleanup()

    def test_goal_is_persistent_and_snapshot_is_data_driven(self):
        goal = self.store.create("完成2026年1月整套财务", "2026-01", "负责人")
        snapshot = build_goal_snapshot(
            goal, finance_fixture(), {"readiness_score": 80, "blockers": ["期初余额"]}, {"status": "开放"}
        )
        self.store.save(snapshot)
        loaded = self.store.load(goal["id"])
        self.assertEqual(loaded["status"], "等待确认")
        self.assertEqual(loaded["readiness"]["data_volume"]["settlement_records"], 120000)
        self.assertTrue(any(action["status"] == "等待确认" for action in loaded["actions"]))

    def test_human_decision_is_auditable_and_changes_action_state(self):
        goal = self.store.create("完成2026年1月整套财务", "2026-01")
        goal = build_goal_snapshot(goal, finance_fixture(), {"readiness_score": 100}, {"status": "开放"})
        self.store.save(goal)
        decision = self.store.decide(goal["id"], "C05", "同意", "业务负责人", "交付成果已核验")
        refreshed = build_goal_snapshot(
            self.store.load(goal["id"]), finance_fixture(), {"readiness_score": 100}, {"status": "开放"}
        )
        self.assertEqual(decision["decision"], "同意")
        self.assertEqual(next(item for item in refreshed["actions"] if item["id"] == "C05")["status"], "已完成")
        self.assertEqual(self.store.events(goal["id"])[0]["type"], "HUMAN_DECISION")

    def test_blocked_fact_is_not_misrepresented_as_approval_request(self):
        goal = self.store.create("完成2026年1月整套财务", "2026-01")
        snapshot = build_goal_snapshot(
            goal, finance_fixture("阻塞"), {"readiness_score": 30}, {"status": "开放"}
        )
        action = next(item for item in snapshot["actions"] if item["id"] == "C05")
        self.assertEqual(action["status"], "阻塞")
        self.assertIn("缺少验收事实", action["blockers"])

    def test_closed_period_completes_goal(self):
        goal = self.store.create("完成2026年1月整套财务", "2026-01")
        snapshot = build_goal_snapshot(
            goal, finance_fixture(), {"readiness_score": 100}, {"status": "已关账"}
        )
        self.assertEqual(snapshot["status"], "已完成")

    def test_foreign_currency_review_is_skipped_when_no_foreign_activity(self):
        finance = finance_fixture()
        finance["close"]["tasks"].insert(2, {
            "id": "C10", "name": "外币余额折算与汇兑损益", "stream": "外币",
            "owner": "会计服务机构", "status": "已完成", "blockers": [], "deadline": "D+5",
            "evidence_required": False,
        })
        goal = self.store.create("完成2026年1月整套财务", "2026-01")
        snapshot = build_goal_snapshot(goal, finance, {"readiness_score": 100}, {"status": "开放"})
        action = next(item for item in snapshot["actions"] if item["id"] == "C10")
        self.assertEqual(action["status"], "已完成")
        self.assertEqual(action["automation"]["level"], "自动执行")

    def test_rejects_path_traversal_goal_id(self):
        with self.assertRaises(ValueError):
            self.store.load("../../etc/passwd")

    def test_business_flow_alerts_are_visible_to_goal_agent(self):
        goal = self.store.create("完成2026年1月整套财务", "2026-01")
        snapshot = build_goal_snapshot(
            goal, finance_fixture(), {"readiness_score": 100}, {"status": "开放"},
            {
                "alerts": [{"type": "未认领流水", "count": 3}],
                "receivables": {"overdue_count": 2}, "payables": {"pending_payment_count": 4},
                "bank_unallocated_count": 3,
                "payment_requests": {"pending_approval": 1}, "expense_claims": {"pending_approval": 2},
            },
        )
        self.assertEqual(snapshot["business_flow_status"]["bank_unallocated"], 3)
        self.assertEqual(snapshot["business_flow_status"]["pending_approvals"], 3)
        self.assertEqual(snapshot["operating_alerts"][0]["type"], "未认领流水")

    def test_missed_collection_promise_becomes_explicit_agent_blocker(self):
        goal = self.store.create("完成2026年1月整套财务", "2026-01")
        snapshot = build_goal_snapshot(
            goal, finance_fixture(), {"readiness_score": 100}, {"status": "开放"},
            {
                "alerts": [],
                "receivables": {"overdue_count": 2, "missed_promise_count": 1, "disputed_count": 1},
                "payables": {"pending_payment_count": 0}, "bank_unallocated_count": 0,
                "payment_requests": {"pending_approval": 0}, "expense_claims": {"pending_approval": 0},
            },
        )
        action = next(item for item in snapshot["actions"] if item["id"] == "R01")
        self.assertEqual(action["status"], "阻塞")
        self.assertIn("承诺回款", action["blockers"][0])
        self.assertEqual(snapshot["business_flow_status"]["missed_collection_promises"], 1)

    def test_waiting_artifact_is_explicitly_a_draft(self):
        goal = self.store.create("完成2026年1月整套财务", "2026-01")
        snapshot = build_goal_snapshot(
            goal, finance_fixture(), {"readiness_score": 100}, {"status": "开放"}
        )
        action = next(item for item in snapshot["actions"] if item["id"] == "C05")
        self.assertEqual(action["artifacts"][0]["status"], "草稿待确认")
        self.assertEqual(action["artifacts"][0]["evidence_state"], "待人工确认")

    def test_goal_records_demo_or_live_data_mode(self):
        goal = self.store.create(
            "完成2026年1月整套财务", "2026-01", data_mode="demo", origin="system_default"
        )
        self.assertEqual(goal["data_mode"], "demo")
        self.assertEqual(goal["origin"], "system_default")
        self.assertEqual(goal["demo_scenario"], "domestic")

    def test_goal_plan_includes_cash_safety_and_management_analysis(self):
        goal = self.store.create("完成2026年1月整套财务", "2026-01")
        snapshot = build_goal_snapshot(
            goal, finance_fixture(), {"readiness_score": 100}, {"status": "开放"}, {},
            {
                "forecast": [{"period": "2026-02"}, {"period": "2026-03"}, {"period": "2026-04"}],
                "opening_cash_cny": 8_000_000, "buffer_breach_period": None,
            },
            {"totals": {"revenue": 12_000_000}, "proactive_insights": []},
        )
        actions = {item["id"]: item for item in snapshot["actions"]}
        self.assertEqual(actions["P01"]["status"], "已完成")
        self.assertEqual(actions["A01"]["status"], "已完成")
        self.assertEqual(actions["P01"]["artifacts"][0]["status"], "已生成")

    def test_live_first_close_coverage_is_a_real_agent_blocker(self):
        goal = self.store.create("完成首月财务上线", "2026-01")
        snapshot = build_goal_snapshot(
            goal, finance_fixture(), {
                "readiness_score": 50, "blockers": [],
                "first_close": {"blockers": ["银行与真实现金", "期初余额"]},
            }, {"status": "开放"},
        )
        action = next(item for item in snapshot["actions"] if item["id"] == "F01")
        self.assertEqual(action["status"], "阻塞")
        self.assertIn("候选文件不能当作已入台账", action["decision_support"]["agent_recommendation"])


if __name__ == "__main__":
    unittest.main()
