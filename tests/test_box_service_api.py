import unittest
from pathlib import Path

from src.box_runtime import BoxRuntime
from src.box_service_api import (
    BoxServiceRequestError,
    build_box_bootstrap,
    dispatch_box_service_request,
)
from src.default_services import build_default_service_registry
from src.pack_services import PackServiceError


ROOT = Path(__file__).resolve().parents[1]


class BoxServiceApiTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(
            ROOT / "examples" / "boxes" / "global_game_studio.json",
            ROOT / "packs",
        )
        self.registry = build_default_service_registry()

    def test_bootstrap_combines_context_and_filtered_services(self):
        result = build_box_bootstrap(self.runtime, self.registry)
        self.assertEqual(result["context"]["scope"]["scope"], "management")
        service_ids = {service["service_id"] for service in result["services"]}
        self.assertIn("game.analyze_kpis", service_ids)
        self.assertIn("tax.sg.build_calendar", service_ids)
        self.assertNotIn("commerce.analyze", service_ids)

    def test_dispatch_request_executes_selected_service(self):
        result = dispatch_box_service_request(self.runtime, self.registry, {
            "service_id": "tax.sg.build_calendar",
            "entity_id": "sg_publisher",
            "payload": {"period_year": 2026, "as_of": "2026-08-13"},
        })
        self.assertEqual(result["output"]["entity"]["entity_id"], "sg_publisher")

    def test_dispatch_request_rejects_malformed_boundary_inputs(self):
        invalid_requests = [
            {},
            {"service_id": "game.analyze_kpis", "payload": []},
            {"service_id": "game.analyze_kpis", "entity_ids": []},
            {"service_id": "game.analyze_kpis", "entity_ids": ["cn_studio", "cn_studio"]},
            {"service_id": "game.analyze_kpis", "approval": "yes"},
        ]
        for request in invalid_requests:
            with self.subTest(request=request), self.assertRaises(BoxServiceRequestError):
                dispatch_box_service_request(self.runtime, self.registry, request)

    def test_registry_controls_still_apply_through_api_boundary(self):
        with self.assertRaises(PackServiceError):
            dispatch_box_service_request(self.runtime, self.registry, {
                "service_id": "commerce.analyze",
                "payload": {},
            })


if __name__ == "__main__":
    unittest.main()
