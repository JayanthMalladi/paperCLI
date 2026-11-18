"""Document parsing, hashing, page/section locations, and local ingestion.

Phase 3 modules:

* :mod:`papercli.documents.pdf_core`   -- stdlib PDF text extraction
* :mod:`papercli.documents.parser`     -- PDF child-process isolation
* :mod:`papercli.documents.xml_html`   -- repository XML / HTML parsing
* :mod:`papercli.documents.identity`   -- wrong-paper guard
* :mod:`papercli.documents.evidence`   -- location/excerpt resolution
* :mod:`papercli.documents.ingest`     -- local ingestion into the store
"""

from papercli.documents.ingest import (
    IngestRejected,
    ingest_local_document,
    parse_document_bytes,
)

__all__ = [
    "IngestRejected",
    "ingest_local_document",
    "parse_document_bytes",
]
