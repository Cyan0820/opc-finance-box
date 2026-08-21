from __future__ import annotations

import base64
import binascii
import hashlib
import tempfile
from pathlib import Path
from typing import Any

from .document_extraction import IMAGE_EXTENSIONS, extract_document_text
from .pack_services import ServiceContext


MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_RETURNED_TEXT_CHARS = 250_000


def _decode_document(payload: dict[str, Any], allowed_extensions: set[str]) -> tuple[str, bytes]:
    filename = str(payload.get("filename") or "").strip()
    if not filename:
        raise ValueError("filename is required")
    # The service accepts a logical filename, never a server-side path.
    if Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("filename must not contain a path")
    extension = Path(filename).suffix.lower()
    if extension not in allowed_extensions:
        supported = ", ".join(sorted(allowed_extensions))
        raise ValueError(f"Unsupported document extension; expected one of: {supported}")

    encoded = payload.get("content_base64")
    if not isinstance(encoded, str) or not encoded.strip():
        raise ValueError("content_base64 is required")
    # Reject obviously oversized input before allocating its decoded form.
    compact = "".join(encoded.split())
    if len(compact) > ((MAX_DOCUMENT_BYTES + 2) // 3) * 4 + 4:
        raise ValueError("Document exceeds the 20 MiB service limit")
    try:
        content = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("content_base64 is not valid base64") from exc
    if not content:
        raise ValueError("Document content is empty")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds the 20 MiB service limit")
    return extension, content


def _extract(payload: dict[str, Any], context: ServiceContext, allowed_extensions: set[str]) -> dict[str, Any]:
    extension, content = _decode_document(payload, allowed_extensions)
    with tempfile.NamedTemporaryFile(prefix="opc-box-document-", suffix=extension) as handle:
        handle.write(content)
        handle.flush()
        extraction = extract_document_text(handle.name)

    threshold = float(payload.get("review_confidence_threshold", 0.90))
    if not 0 <= threshold <= 1:
        raise ValueError("review_confidence_threshold must be between 0 and 1")
    full_text = str(extraction.get("text") or "")
    truncated = len(full_text) > MAX_RETURNED_TEXT_CHARS
    extraction = dict(extraction)
    extraction["text"] = full_text[:MAX_RETURNED_TEXT_CHARS]
    extraction["text_truncated"] = truncated
    confidence = float(extraction.get("confidence") or 0)
    requires_review = extraction.get("method") == "ocr" or confidence < threshold or truncated
    return {
        "entity_id": context.entity_id,
        "source": {
            "filename": str(payload["filename"]),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        "extraction": extraction,
        "output_status": "candidate_extraction_pending_review" if requires_review else "extracted_text",
        "requires_human_review": requires_review,
        "review_gate": "low_confidence_document_extraction" if requires_review else None,
        "control_note": "提取结果不构成记账、报税或付款依据；低置信度、OCR 与截断结果必须核对原件。",
    }


def extract_text_pdf(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    return _extract(payload, context, {".pdf"})


def extract_image_ocr(payload: dict[str, Any], context: ServiceContext) -> dict[str, Any]:
    return _extract(payload, context, set(IMAGE_EXTENSIONS))
