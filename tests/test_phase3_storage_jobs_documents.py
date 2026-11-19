"""Phase 3 (Storage/Docs) tests: migrations, task queue, document parsing.

Offline, fixture-based. Network tests are Phase 2\'s (the
``@pytest.mark.network`` suite); nothing here touches the network.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def test_migrations_idempotent_including_phase3(tmp_path):
    from papercli.storage.migrations import apply_migrations, discover_migrations

    assert [m.name for m in discover_migrations()] == [
        "0001.sql", "0002.sql", "0003.sql", "0004.sql", "0005.sql",
        "0006.sql", "0007.sql", "0008.sql"]
    db = tmp_path / "phase3.db"
    applied = apply_migrations(db)
    assert [m.name for m in applied] == [
        "0001.sql", "0002.sql", "0003.sql", "0004.sql", "0005.sql",
        "0006.sql", "0007.sql", "0008.sql"]
    # Idempotent: a second run applies nothing.
    assert apply_migrations(db) == []

    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = \'table\'")}
        assert {"task_submissions", "document_text"} <= tables
        conn.close()
    finally:
        pass


def test_migrations_0002_0003_columns_on_legacy_0001_db(tmp_path):
    """A 0001-only database must migrate 0002/0003 in place (ALTERs only)."""
    from papercli.storage.migrations import MigrationRunner, MIGRATIONS_DIR

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            (MIGRATIONS_DIR / "0001.sql").read_text(encoding="utf-8"))
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY NOT NULL, "
            " name TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT (datetime(\'now\')))")
        conn.execute("INSERT INTO schema_migrations (version, name) VALUES (1, \'0001.sql\')")
        conn.commit()
    finally:
        conn.close()

    runner = MigrationRunner(db)
    applied = runner.run()
    # A 0001-only legacy DB must apply the Phase-3 migrations in place.
    # Phase 4+ may add further migrations (0004.sql, ...), so assert the
    # REQUIRED ones are present rather than hardcoding the exact file set.
    names = [m.name for m in applied]
    assert "0002.sql" in names and "0003.sql" in names
    assert runner.run() == []  # idempotent

    conn = sqlite3.connect(str(db))
    try:
        task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert {"idempotency_key", "payload_json", "protocol_revision",
                "max_attempts", "next_attempt_at", "last_error"} <= task_cols
        doc_cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        assert {"parse_warnings_json", "page_count", "scanned_flag",
                "identity_status", "identity_json"} <= doc_cols
        ev_cols = {r[1] for r in conn.execute("PRAGMA table_info(evidence)")}
        assert {"methods_json", "population_or_system"} <= ev_cols
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Task queue
# ---------------------------------------------------------------------------

def _store(tmp_path):
    from papercli.storage.db import PaperStore
    return PaperStore(tmp_path / "tasks.db")


def test_task_enqueue_idempotent_same_key():
    with _store(_tmpdir()) as store:
        from papercli.jobs.tasks import TaskQueue
        rid = store.create_run()
        q = TaskQueue(store)
        t1 = q.enqueue(rid, "retrieve", "h1", protocol_revision="7")
        t2 = q.enqueue(rid, "retrieve", "h1", protocol_revision="7")
        assert t1.task_id == t2.task_id, "same payload must return the prior task"
        # A different protocol revision is a DIFFERENT task (spec 11).
        t3 = q.enqueue(rid, "retrieve", "h1", protocol_revision="8")
        assert t3.task_id != t1.task_id
        # Two distinct tasks: (h1, rev 7) returned twice, (h1, rev 8) new.
        assert len(q.list(rid)) == 2
        assert all(t.state == "pending" for t in q.list(rid))


def _tmpdir():
    import tempfile
    return Path(tempfile.mkdtemp(prefix="papercli-task-"))


def test_task_lease_lifecycle_claim_complete_release():
    with _store(_tmpdir()) as store:
        from papercli.jobs.tasks import TaskQueue
        rid = store.create_run()
        q = TaskQueue(store)
        task = q.enqueue(rid, "retrieve", "h1")
        assert task.state == "pending"

        # Claim (lease) -> running -> complete.
        claimed = q.claim(rid, "worker-a", lease_ttl=60)
        assert len(claimed) == 1 and claimed[0].task_id == task.task_id
        assert claimed[0].state == "leased"
        assert claimed[0].lease_owner == "worker-a"
        assert claimed[0].lease_expires_at is not None
        started = q.start(rid, task.task_id, "worker-a")
        assert started.state == "running"
        done = q.complete(rid, task.task_id, "worker-a", result={"n": 1})
        assert done.state == "succeeded"
        assert done.result_hash is not None

        # A finished task cannot be re-claimed.
        assert q.claim(rid, "worker-b") == []
        row = store.connection.execute(
            "SELECT lease_owner FROM tasks WHERE id = ?", (task.task_id,)
        ).fetchone()
        assert row["lease_owner"] is None


def test_task_lease_expiry_recovers_lost_worker():
    with _store(_tmpdir()) as store:
        from papercli.jobs.tasks import TaskQueue
        rid = store.create_run()
        q = TaskQueue(store)
        task = q.enqueue(rid, "extract", "h9")
        q.claim(rid, "worker-gone", lease_ttl=0.05)
        assert q.get(rid, task.task_id).state == "leased"
        # Worker vanishes (crash): the lease lapses.
        time.sleep(0.12)
        released = q.release_expired(rid)
        assert released == 1
        assert q.get(rid, task.task_id).state == "pending"
        # A different worker re-claims the recovered task.
        again = q.claim(rid, "worker-new", lease_ttl=60)
        assert len(again) == 1 and again[0].task_id == task.task_id
        assert again[0].lease_owner == "worker-new"
        q.complete(rid, task.task_id, "worker-new", result={"ok": True})
        assert q.get(rid, task.task_id).state == "succeeded"


def test_task_fail_retries_then_fails():
    with _store(_tmpdir()) as store:
        from papercli.jobs.tasks import TaskQueue
        rid = store.create_run()
        q = TaskQueue(store)
        task = q.enqueue(rid, "screen", "h2", max_attempts=2)
        q.claim(rid, "w", lease_ttl=60)
        q.fail(rid, task.task_id, "w", error="first boom", retry_delay=0.01)
        assert q.get(rid, task.task_id).state == "retry_wait"
        time.sleep(0.03)
        q.claim(rid, "w", lease_ttl=60)
        q.fail(rid, task.task_id, "w", error="second boom")
        final = q.get(rid, task.task_id)
        assert final.state == "failed"
        assert final.attempt == 2


def test_task_submission_protocol():
    with _store(_tmpdir()) as store:
        from papercli.jobs.tasks import TaskQueue, submit, SubmissionOutcome
        rid = store.create_run()
        q = TaskQueue(store)
        task = q.enqueue(rid, "extract", "hX")
        q.claim(rid, "wA", lease_ttl=60)
        ok = submit(q, rid, task.task_id, "wA",
                    {"input_hash": "hX", "decision": "include"},
                    source_input_hash="hX")
        assert ok["status"] == SubmissionOutcome.ACCEPTED
        # Same payload hash again -> the prior result, not a new one.
        dup = submit(q, rid, task.task_id, "wA",
                     {"input_hash": "hX", "decision": "include"})
        assert dup["status"] == SubmissionOutcome.DUPLICATE
        assert dup["prior_result"] == {"input_hash": "hX", "decision": "include"}
        # A foreign worker cannot submit the lease it does not own.
        t2 = q.enqueue(rid, "extract", "hY")
        q.claim(rid, "wA", lease_ttl=60)
        st = submit(q, rid, t2.task_id, "wB", {"x": 1})
        assert st["status"] == SubmissionOutcome.REJECTED_STALE
        # Source-inconsistent payloads are rejected explicitly.
        t3 = q.enqueue(rid, "extract", "hZ")
        q.claim(rid, "wC", lease_ttl=60)
        mis = submit(q, rid, t3.task_id, "wC", {"input_hash": "OTHER"},
                     source_input_hash="hZ")
        assert mis["status"] == SubmissionOutcome.REJECTED_SOURCE
        # Unknown task.
        bad = submit(q, rid, "task-does-not-exist", "wA", {"x": 1})
        assert bad["status"] == SubmissionOutcome.REJECTED_INVALID


# ---------------------------------------------------------------------------
# Document parsing (PDF)
# ---------------------------------------------------------------------------

def test_pdf_fixture_parses_pages_and_identity():
    import papercli.documents.parser as dparser
    data = (FIXTURES / "sample_paper.pdf").read_bytes()
    rep = dparser.parse_document(data, "pdf")
    assert rep["kind"] == "pdf"
    assert rep["page_count"] == 3
    texts = [s["text"] for s in rep["text_index"]]
    assert any("The effect size was 0.42." in t for t in texts)
    # Embedded identity used by the wrong-paper guard.
    assert rep["identity"]["doi"] == "10.9999/rev.2023"
    assert rep["identity"]["title"] == "Effects of X on Y: A Systematic Review"
    # Section locations: "Results" appears as a section label.
    assert any(s["section"] == "Results" for s in rep["text_index"])


def test_pdf_child_process_isolation():
    """Heavy parsing runs in a child process with a runtime limit (spec 16)."""
    import papercli.documents.parser as dparser
    from papercli.documents.parser import ParseError

    data = (FIXTURES / "sample_paper.pdf").read_bytes()
    rep = dparser.parse_document(data, "pdf", timeout=60)
    assert rep["page_count"] == 3
    # A content-size breach is rejected, not parsed.
    big = b"%PDF-1.4\n" + b"\x00" * 4096
    try:
        dparser.parse_document(big, "pdf", max_bytes=1024)
        raise AssertionError("expected ParseError for oversized content")
    except ParseError as e:
        assert "exceeds limit" in str(e)
    # The worker is spawned with an argument array (no shell) -- the module
    # must not reference shell=True anywhere.
    src = Path(dparser.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src


def test_scanned_page_flagged_not_silent():
    import papercli.documents.parser as dparser
    data = (FIXTURES / "scanned_sample.pdf").read_bytes()
    rep = dparser.parse_document(data, "pdf")
    assert rep["scanned"] is True
    pages = {p["page"]: p for p in rep["pages"]}
    assert pages[2]["has_images"] is True and pages[2]["text"] == ""
    assert pages[1]["text"].strip(), "page 1 must still have its text"
    # The absence of OCR must be disclosed.
    assert any("OCR" in w or "scanned" in w.lower() for w in rep["warnings"])


def test_pdf_read_text_via_ingest_report_shape():
    """The `papers read` data path: text + locations + warnings envelope body."""
    import papercli.documents.parser as dparser
    data = (FIXTURES / "sample_paper.pdf").read_bytes()
    rep = dparser.parse_document(data, "pdf")
    wanted = [1, 2, 3]
    sections = [s for s in rep["text_index"] if s["page"] in wanted]
    assert sections, "all three pages carry extractable sections"
    combined = "\n\n".join(s["text"] for s in sections)
    assert "The effect size was 0.42." in combined


# ---------------------------------------------------------------------------
# Document parsing (XML / HTML)
# ---------------------------------------------------------------------------

def test_repository_xml_preferred_parse():
    import papercli.documents.parser as dparser
    data = (FIXTURES / "repository_sample.xml").read_bytes()
    rep = dparser.parse_document(data, "xml")
    assert rep["kind"] == "xml"
    secs = {s["section"] for s in rep["text_index"]}
    assert "Methods" in secs and "Results" in secs
    assert rep["identity"]["doi"] == "10.9999/rev.2023"
    # The paper title is the article title, not a section title.
    assert rep["identity"]["title"] == "Effects of X on Y: A Systematic Review"


def test_html_fulltext_parse():
    import papercli.documents.parser as dparser
    data = (FIXTURES / "landing_sample.html").read_bytes()
    rep = dparser.parse_document(data, "html")
    assert rep["kind"] == "html"
    assert rep["identity"]["doi"] == "10.9999/rev.2023"
    texts = " ".join(s["text"] for s in rep["text_index"])
    assert "The effect size was 0.30." in texts


# ---------------------------------------------------------------------------
# Wrong-paper guard (acceptance test 'Identity')
# ---------------------------------------------------------------------------

def test_identity_check_verdicts():
    from papercli.documents.identity import check_identity
    # DOI mismatch is a definite rejection.
    r = check_identity(
        {"doi": "10.9999/rev.2023"}, {"doi": "10.1234/other"})
    assert r["status"] == "unmatched"
    # Agreed identity.
    r2 = check_identity(
        {"doi": "10.9999/rev.2023", "title": "Effects of X on Y: A Systematic Review"},
        {"doi": "10.9999/rev.2023", "title": "Effects of X on Y"})
    assert r2["status"] == "matched"
    # No embedded evidence -> flag, do not reject.
    r3 = check_identity({"title": None, "doi": None}, {"doi": "10.9999/rev.2023"})
    assert r3["status"] == "inconclusive"
    # No requested context -> check skipped.
    r4 = check_identity({"doi": "10.9999/rev.2023"}, {})
    assert r4["status"] == "skipped"


def test_wrong_paper_fixture_is_rejected():
    from papercli.documents.ingest import IngestRejected, ingest_local_document
    with _store(_tmpdir()) as store:
        rid = store.create_run()
        wrong = str(FIXTURES / "wrong_paper.pdf")
        try:
            ingest_local_document(
                store, rid, wrong,
                requested_identity={"doi": "10.9999/rev.2023",
                                   "title": "Effects of X on Y"})
            raise AssertionError("a parseable-but-wrong PDF must be rejected")
        except IngestRejected as e:
            assert "wrong-paper" in e.reason
        # The matching fixture still ingests fine.
        good = str(FIXTURES / "sample_paper.pdf")
        rep = ingest_local_document(
            store, rid, good,
            requested_identity={"doi": "10.9999/rev.2023"})
        assert rep["rejected"] is False
        assert rep["identity_status"] == "matched"
        row = store.document_row(rep["document_id"])
        assert row["identity_status"] == "matched"
        assert row["page_count"] == 3


def test_ingest_unknown_kind_rejected():
    import tempfile
    from papercli.documents.ingest import IngestRejected, ingest_local_document
    with _store(_tmpdir()) as store:
        rid = store.create_run()
        junk = Path(tempfile.gettempdir()) / f"papercli-junk-{time.time_ns()}.bin"
        junk.write_bytes(b"\x00\x01\x02 binary junk" * 64)
        try:
            ingest_local_document(store, rid, junk)
            raise AssertionError("unknown kind must be rejected")
        except IngestRejected:
            pass
        finally:
            junk.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Evidence location resolution (acceptance test 'Extraction')
# ---------------------------------------------------------------------------

def test_evidence_location_resolves_to_exact_source_text():
    from papercli.documents.evidence import EvidenceIndex, record_evidence
    from papercli.documents.ingest import ingest_local_document

    with _store(_tmpdir()) as store:
        rid = store.create_run()
        rep = ingest_local_document(
            store, rid, str(FIXTURES / "sample_paper.pdf"))
        doc_id = rep["document_id"]
        paper_id = store.document_row(doc_id)["paper_id"]

        idx = EvidenceIndex(store, doc_id)
        row = idx.resolve_excerpt("The effect size was 0.42.")
        assert row is not None
        assert (row["page"], row["section"], row["paragraph"]) == (3, "Results", 0)
        # Exactness: the excerpt appears VERBATIM in the stored row text.
        assert "The effect size was 0.42." in row["text"]

        ev = record_evidence(
            store, paper_id, document_id=doc_id, access_level="full_text",
            location={"page": 3, "section": "Results", "paragraph": 0},
            supporting_excerpt="The effect size was 0.42.",
            finding="Pooled effect size 0.42 reported.",
            claim_type="reported_result",
        )
        assert ev["extraction_warnings"] == []
        assert ev["location"] == {"page": 3, "section": "Results", "paragraph": 0}

        # An excerpt that is NOT in the source resolves to None and the
        # record is still made -- but with a disclosed warning.
        ev2 = record_evidence(
            store, paper_id, document_id=doc_id, access_level="full_text",
            location=None,
            supporting_excerpt="the effect was 0.99",
            finding="unresolvable excerpt",
        )
        assert ev2["extraction_warnings"], "unresolvable excerpt must be disclosed"


# ---------------------------------------------------------------------------
# CLI envelopes
# ---------------------------------------------------------------------------

def test_cli_read_envelope(tmp_project):
    """`papers read` returns the text + locations + warnings envelope."""
    from tests.conftest import run_papers
    rc, out, err = run_papers(
        "read", str(FIXTURES / "sample_paper.pdf"),
        "--pages", "1-3", "--max-chars", "18000",
        cwd=tmp_project, env_strip={"PAPERS_EMAIL"},
    )
    assert rc == 0, f"read failed: {err}\n{out}"
    env = json.loads(out)
    assert env["status"] == "ok" and env["schema_version"] == "1.0"
    data = env["data"]
    assert data["kind"] == "pdf" and data["page_count"] == 3
    assert data["requested_pages"] == [1, 2, 3]
    assert "The effect size was 0.42." in data["text"]
    assert data["locations"][-1]["section"] == "Results"


def test_cli_read_rejects_unsupported_kind(tmp_project):
    from tests.conftest import run_papers
    junk = tmp_project / "junk.bin"
    junk.write_bytes(b"\x00\x01\x02 binary junk" * 64)
    rc, out, err = run_papers("read", str(junk), cwd=tmp_project,
                              env_strip={"PAPERS_EMAIL"})
    env = json.loads(out)
    assert rc != 0 and env["status"] == "error"
    assert env["error"]["code"] == "RETRIEVAL_UNAVAILABLE"


def test_cli_ingest_envelope(tmp_project):
    """`papers ingest` registers the document with hash + provenance."""
    from tests.conftest import run_papers
    from papercli.storage.db import PaperStore
    from papercli.documents.ingest import IngestRejected

    import os
    os.chdir(tmp_project)
    try:
        with PaperStore() as store:
            rid = store.create_run()
            rc, out, err = run_papers(
                "ingest", str(FIXTURES / "sample_paper.pdf"), "--run", rid,
                cwd=tmp_project, env_strip={"PAPERS_EMAIL"})
            assert rc == 0, f"ingest failed: {err}\n{out}"
            env = json.loads(out)
            assert env["status"] == "ok"
            data = env["data"]
            assert data["run_id"] == rid
            assert data["kind"] == "pdf"
            assert data["page_count"] == 3
            assert len(data["content_hash"]) == 64  # sha256 hex
            assert data["access_status"] == "retrieved_full_text"
            row = store.document_row(data["document_id"])
            assert row["content_hash"] == data["content_hash"]

            # Wrong-paper rejection surfaces as a RETRIEVAL_UNAVAILABLE
            # envelope (the file does not match the run's paper).
            store.upsert_paper(
                rid, {"doi": "10.9999/rev.2023"},
                title="Effects of X on Y: A Systematic Review")
            rc2, out2, err2 = run_papers(
                "ingest", str(FIXTURES / "wrong_paper.pdf"), "--run", rid,
                cwd=tmp_project, env_strip={"PAPERS_EMAIL"})
            env2 = json.loads(out2)
            assert env2["status"] == "error"
            assert env2["error"]["code"] == "RETRIEVAL_UNAVAILABLE"
    finally:
        os.chdir(REPO)


def test_cli_ingest_missing_run_envelope(tmp_project):
    from tests.conftest import run_papers
    rc, out, err = run_papers(
        "ingest", str(FIXTURES / "sample_paper.pdf"),
        "--run", "run-does-not-exist",
        cwd=tmp_project, env_strip={"PAPERS_EMAIL"})
    env = json.loads(out)
    assert rc == 2 and env["status"] == "error"
    assert env["error"]["code"] == "CONFIG_REQUIRED"
