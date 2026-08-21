import base64
import unittest
from pathlib import Path
from unittest.mock import patch

from src.box_runtime import BoxRuntime
from src.default_services import build_default_service_registry


ROOT = Path(__file__).resolve().parents[1]


class DocumentServiceTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BoxRuntime(ROOT / "examples" / "boxes" / "cn_dtc_store.json", ROOT / "packs")
        self.registry = build_default_service_registry()

    @patch("src.document_services.extract_document_text")
    def test_pdf_is_extracted_from_memory_payload_without_exposing_a_path(self, extract):
        extract.return_value = {
            "method": "pdf_text", "page_count": 1,
            "pages": [{"page": 1, "text": "Invoice", "confidence": 0.98, "method": "pdf_text"}],
            "text": "Invoice", "confidence": 0.98,
        }
        result = self.registry.dispatch(
            self.runtime,
            "connector.extract_text_pdf",
            {"filename": "invoice.pdf", "content_base64": base64.b64encode(b"pdf bytes").decode()},
            entity_id="cn_dtc_company",
        )
        self.assertEqual(result["output"]["output_status"], "extracted_text")
        self.assertFalse(result["output"]["requires_human_review"])
        self.assertNotIn("path", result["output"]["source"])
        called_path = Path(extract.call_args.args[0])
        self.assertFalse(called_path.exists())

    @patch("src.document_services.extract_document_text")
    def test_ocr_is_always_a_candidate_pending_human_review(self, extract):
        extract.return_value = {
            "method": "ocr", "page_count": 1,
            "pages": [{"page": 1, "text": "receipt", "confidence": 0.99, "method": "ocr"}],
            "text": "receipt", "confidence": 0.99,
        }
        result = self.registry.dispatch(
            self.runtime,
            "connector.extract_image_ocr",
            {"filename": "receipt.png", "content_base64": base64.b64encode(b"image bytes").decode()},
            entity_id="cn_dtc_company",
        )
        self.assertTrue(result["output"]["requires_human_review"])
        self.assertEqual(result["output"]["review_gate"], "low_confidence_document_extraction")

    def test_rejects_invalid_base64_extension_and_paths(self):
        invalid_payloads = (
            {"filename": "invoice.pdf", "content_base64": "not-base64!"},
            {"filename": "invoice.exe", "content_base64": base64.b64encode(b"x").decode()},
            {"filename": "../invoice.pdf", "content_base64": base64.b64encode(b"x").decode()},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.registry.dispatch(
                    self.runtime, "connector.extract_text_pdf", payload, entity_id="cn_dtc_company"
                )


if __name__ == "__main__":
    unittest.main()
