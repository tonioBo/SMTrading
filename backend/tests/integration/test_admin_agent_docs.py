"""«Istruzioni agenti»: the insert-only seed from docs/paperclip-agents and the admin API
(/api/agent-docs): append-only versions, the base_version conflict, restore, audit, CSRF."""

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

PASSWORD = "correct-horse-battery"
DOCS_DIR = Path(__file__).resolve().parents[3] / "docs" / "paperclip-agents"
SLUGS = {
    "readme", "common", "ceo", "portfolio-manager", "risk-manager", "execution-supervisor",
    "market-analyst", "news-analyst", "quant-researcher", "performance-auditor", "notifier",
    "appendice",
}


# --- seed ---------------------------------------------------------------------------------

async def _docs(session):
    from smtrading.agentdocs.models import AgentDoc

    return {d.slug: d for d in await session.scalars(select(AgentDoc))}


async def _bodies(session, slug):
    from smtrading.agentdocs.models import AgentDocVersion

    return list(await session.scalars(
        select(AgentDocVersion.body).where(AgentDocVersion.slug == slug)
        .order_by(AgentDocVersion.version)))


async def test_seed_loads_the_twelve_docs_at_version_1(db_session):
    from smtrading.seed import run_seed

    await run_seed(db_session)
    docs = await _docs(db_session)
    assert set(docs) == SLUGS
    assert {d.current_version for d in docs.values()} == {1}
    assert docs["readme"].title == "Guida all'installazione"
    assert docs["common"].title == "Regole comuni"
    assert docs["ceo"].title == "CEO SMTrading"
    assert docs["notifier"].title == "Notifier"
    assert docs["appendice"].title == "Appendice delle istruzioni (casi rari)"
    assert await _bodies(db_session, "ceo") == [(DOCS_DIR / "01-ceo.md").read_text("utf-8")]


async def test_seed_is_insert_only(db_session, tmp_path):
    from smtrading.agentdocs import service
    from smtrading.seed import run_seed

    await run_seed(db_session)
    await service.save_doc(db_session, "ceo", body="mio testo", note=None, base_version=1,
                           author="admin@example.com")
    (tmp_path / "01-ceo.md").write_text("# 01 · CEO\nnuovo dal file", "utf-8")
    (tmp_path / "10-nuovo-ruolo.md").write_text("# 10 · Nuovo ruolo\ntesto", "utf-8")
    (tmp_path / "appunti.txt").write_text("ignorato", "utf-8")
    assert await service.seed_agent_docs(db_session, tmp_path) == 1
    await run_seed(db_session)
    docs = await _docs(db_session)
    assert docs["ceo"].current_version == 2
    assert await _bodies(db_session, "ceo") == [
        (DOCS_DIR / "01-ceo.md").read_text("utf-8"), "mio testo"]
    assert docs["nuovo-ruolo"].title == "Nuovo ruolo"
    assert len(docs) == len(SLUGS) + 1


async def test_seed_without_the_folder_is_a_no_op(db_session, tmp_path):
    from smtrading.agentdocs import service

    assert await service.seed_agent_docs(db_session, tmp_path / "missing") == 0
    assert await _docs(db_session) == {}


async def test_versions_are_append_only(db_session):
    from smtrading.seed import run_seed

    await run_seed(db_session)
    with pytest.raises(DBAPIError, match="AGENT_DOC_VERSION_IMMUTABLE"):
        async with db_session.begin_nested():
            await db_session.execute(text("update agent_doc_versions set body = 'x'"))
    with pytest.raises(DBAPIError, match="AGENT_DOC_VERSION_IMMUTABLE"):
        async with db_session.begin_nested():
            await db_session.execute(text("delete from agent_doc_versions"))


# --- admin API ----------------------------------------------------------------------------

@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.mcp.runtime import McpRuntime
    from smtrading.seed import run_seed

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()

    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        await run_seed(s)
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD)))
        await s.commit()

    qs = FakeMarketData()
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]

            class NS:
                pass

            ns = NS()
            ns.client, ns.sm, ns.email = client, sm, email
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


async def _audits(admin, method):
    from smtrading.agents.models import McpAuditLog

    async with admin.sm() as s:
        rows = list(await s.scalars(select(McpAuditLog.params)
                                    .where(McpAuditLog.method == method)
                                    .order_by(McpAuditLog.id)))
    # a portal request's row carries its activity log request_id (see test_user_activity)
    return [{k: v for k, v in p.items() if k != "request_id"} for p in rows]


async def test_list_and_get(admin):
    c = admin.client
    r = await c.get("/api/agent-docs")
    assert r.status_code == 200
    listed = {d["slug"]: d for d in r.json()}
    assert set(listed) == SLUGS
    assert listed["risk-manager"] == {
        "slug": "risk-manager", "title": "Risk Manager", "current_version": 1,
        "updated_at": listed["risk-manager"]["updated_at"], "updated_by": "seed",
    }
    doc = (await c.get("/api/agent-docs/common")).json()
    assert doc["body"] == (DOCS_DIR / "00-regole-comuni.md").read_text("utf-8")
    assert doc["current_version"] == 1 and doc["title"] == "Regole comuni"
    assert (await c.get("/api/agent-docs/nessuno")).status_code == 404


async def test_save_creates_a_version_and_audits(admin):
    c = admin.client
    r = await c.put("/api/agent-docs/ceo",
                    json={"body": "# CEO\nnuovo", "note": "  più breve  ", "base_version": 1})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["changed"] is True
    assert out["doc"]["current_version"] == 2 and out["doc"]["body"] == "# CEO\nnuovo"
    assert out["doc"]["updated_by"] == admin.email

    versions = (await c.get("/api/agent-docs/ceo/versions")).json()
    assert [(v["version"], v["author"], v["note"]) for v in versions] == [
        (2, admin.email, "più breve"), (1, "seed", None)]
    assert versions[0]["size"] == len(b"# CEO\nnuovo")
    v1 = (await c.get("/api/agent-docs/ceo/versions/1")).json()
    assert v1["body"] == (DOCS_DIR / "01-ceo.md").read_text("utf-8")
    assert (await c.get("/api/agent-docs/ceo/versions/9")).status_code == 404

    # the same text again: no new version
    r = await c.put("/api/agent-docs/ceo", json={"body": "# CEO\nnuovo", "base_version": 2})
    assert r.json()["changed"] is False and r.json()["doc"]["current_version"] == 2
    assert await _audits(admin, "admin:agent_doc.update") == [
        {"slug": "ceo", "version": 2, "note": "più breve", "by": admin.email}]


async def test_save_with_a_stale_base_version_is_a_conflict(admin):
    c = admin.client
    assert (await c.put("/api/agent-docs/notifier",
                        json={"body": "primo", "base_version": 1})).status_code == 200
    r = await c.put("/api/agent-docs/notifier", json={"body": "secondo", "base_version": 1})
    assert r.status_code == 409
    assert r.json()["message"] == "Il testo è stato modificato da qualcun altro, ricarica"
    assert r.json()["details"] == {"current_version": 2}
    assert (await c.get("/api/agent-docs/notifier")).json()["body"] == "primo"


async def test_conflict_code_is_an_error_code(admin):
    from smtrading.common.errors import ErrorCode

    c = admin.client
    await c.put("/api/agent-docs/notifier", json={"body": "primo", "base_version": 1})
    r = await c.put("/api/agent-docs/notifier", json={"body": "secondo", "base_version": 1})
    assert r.status_code == 409
    assert r.json()["code"] == ErrorCode.VERSION_CONFLICT == "VERSION_CONFLICT"


async def test_huge_bodies_are_refused_by_the_schema(admin):
    """A hard length bound on the request model (far above the 200 kB byte limit the
    service checks with its own message): the text is never processed."""
    from smtrading.admin.routes_agent_docs import MAX_BODY_CHARS, AgentDocIn

    assert AgentDocIn.model_fields["body"].metadata  # max_length set
    r = await admin.client.put("/api/agent-docs/ceo",
                               json={"body": "x" * (MAX_BODY_CHARS + 1), "base_version": 1})
    assert r.status_code == 400 and r.json()["details"]["fields"] == ["body"]
    r = await admin.client.put("/api/agent-docs/ceo",
                               json={"body": "x", "note": "n" * 5000, "base_version": 1})
    assert r.status_code == 400 and r.json()["details"]["fields"] == ["note"]
    assert (await admin.client.get("/api/agent-docs/ceo")).json()["current_version"] == 1


@pytest.mark.parametrize("body,field", [
    ({"body": "   ", "base_version": 1}, "body"),
    ({"body": "x" * (200 * 1024 + 1), "base_version": 1}, "body"),
    ({"body": "x", "note": "n" * 201, "base_version": 1}, "note"),
])
async def test_invalid_saves(admin, body, field):
    r = await admin.client.put("/api/agent-docs/ceo", json=body)
    assert r.status_code == 400 and r.json()["details"]["field"] == field
    assert (await admin.client.get("/api/agent-docs/ceo")).json()["current_version"] == 1


async def test_restore_copies_an_old_version(admin):
    c = admin.client
    original = (await c.get("/api/agent-docs/ceo")).json()["body"]
    await c.put("/api/agent-docs/ceo", json={"body": "rovinato", "base_version": 1})
    r = await c.post("/api/agent-docs/ceo/restore/1")
    assert r.status_code == 200, r.text
    doc = r.json()["doc"]
    assert doc["current_version"] == 3 and doc["body"] == original
    versions = (await c.get("/api/agent-docs/ceo/versions")).json()
    assert versions[0]["note"] == "Ripristino della versione 1"
    # the current version, or one that does not exist
    assert (await c.post("/api/agent-docs/ceo/restore/3")).status_code == 400
    assert (await c.post("/api/agent-docs/ceo/restore/42")).status_code == 404
    assert await _audits(admin, "admin:agent_doc.restore") == [
        {"slug": "ceo", "version": 3, "restored_from": 1, "by": admin.email}]


async def test_routes_require_login(admin):
    async with httpx.AsyncClient(transport=admin.client._transport,
                                 base_url="http://test") as anon:
        for method, path in (("GET", "/api/agent-docs"), ("GET", "/api/agent-docs/ceo"),
                             ("GET", "/api/agent-docs/ceo/versions"),
                             ("GET", "/api/agent-docs/ceo/versions/1"),
                             ("PUT", "/api/agent-docs/ceo"),
                             ("POST", "/api/agent-docs/ceo/restore/1")):
            assert (await anon.request(method, path, json={})).status_code == 401


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/api/agent-docs/ceo", {"body": "x", "base_version": 1}),
    ("POST", "/api/agent-docs/ceo/restore/1", None),
])
async def test_mutating_routes_require_csrf(admin, method, path, body):
    async with httpx.AsyncClient(transport=admin.client._transport, base_url="http://test",
                                 cookies=admin.client.cookies) as no_csrf:
        r = await no_csrf.request(method, path, json=body)
    assert r.status_code == 403, r.text
    assert (await admin.client.get("/api/agent-docs/ceo")).json()["current_version"] == 1


# --- export for Paperclip (External instructions mode) ------------------------------------

AGENTS = sorted(SLUGS - {"readme", "common", "appendice"})


@pytest.fixture
def export_dir(tmp_path, monkeypatch):
    from smtrading.agentdocs import export

    d = tmp_path / "agent-instructions"
    d.mkdir()
    monkeypatch.setenv("SMT_AGENT_DOCS_EXPORT_DIR", str(d))
    export.reset_state()
    return d


def _composed(common: str, role: str) -> str:
    return common.strip() + "\n\n---\n\n" + role.strip() + "\n"


async def test_startup_exports_the_copy_composition(export_dir, admin):
    common = (DOCS_DIR / "00-regole-comuni.md").read_text("utf-8")
    assert sorted(p.name for p in export_dir.iterdir()) == sorted(AGENTS + ["README.txt"])
    ceo = (DOCS_DIR / "01-ceo.md").read_text("utf-8")
    assert (export_dir / "ceo" / "AGENTS.md").read_text("utf-8") == _composed(common, ceo)
    appendix = (DOCS_DIR / "10-appendice.md").read_text("utf-8")
    for slug in AGENTS:  # the appendix next to every entry file, never composed into it
        assert sorted(p.name for p in (export_dir / slug).iterdir()) == ["AGENTS.md",
                                                                          "APPENDICE.md"]
        assert (export_dir / slug / "APPENDICE.md").read_text("utf-8") == appendix
        assert "## §C1 ·" not in (export_dir / slug / "AGENTS.md").read_text("utf-8")
    assert "Non modificarli" in (export_dir / "README.txt").read_text("utf-8")
    assert oct((export_dir / "ceo").stat().st_mode & 0o777) == "0o755"
    assert oct((export_dir / "ceo" / "AGENTS.md").stat().st_mode & 0o777) == "0o644"


async def test_export_is_idempotent_and_atomic(export_dir, admin):
    f = export_dir / "ceo" / "AGENTS.md"
    before = f.stat().st_mtime_ns
    ino = f.stat().st_ino
    r = await admin.client.post("/api/agent-docs/export")
    assert r.status_code == 200
    assert r.json() == {"exported": 9, "dir": str(export_dir), "ok": True}
    assert f.stat().st_mtime_ns == before and f.stat().st_ino == ino  # unchanged: not rewritten
    assert not [p for d in export_dir.iterdir() if d.is_dir() for p in d.glob(".tmp-*")]
    await admin.client.put("/api/agent-docs/ceo", json={"body": "# CEO\nnuovo", "base_version": 1})
    assert f.stat().st_ino != ino  # replaced via temp file + os.replace
    assert not list((export_dir / "ceo").glob(".tmp-*"))


async def test_save_and_restore_re_export(export_dir, admin):
    c = admin.client
    common = (DOCS_DIR / "00-regole-comuni.md").read_text("utf-8")
    await c.put("/api/agent-docs/ceo", json={"body": "# CEO\nnuovo", "base_version": 1})
    assert (export_dir / "ceo" / "AGENTS.md").read_text("utf-8") == _composed(common, "# CEO\nnuovo")
    await c.post("/api/agent-docs/ceo/restore/1")
    ceo = (DOCS_DIR / "01-ceo.md").read_text("utf-8")
    assert (export_dir / "ceo" / "AGENTS.md").read_text("utf-8") == _composed(common, ceo)


async def test_common_change_re_exports_all_agents(export_dir, admin):
    r = await admin.client.put("/api/agent-docs/common",
                               json={"body": "# Regole\nNUOVE", "base_version": 1})
    assert r.status_code == 200
    for slug in AGENTS:
        assert (export_dir / slug / "AGENTS.md").read_text("utf-8").startswith("# Regole\nNUOVE\n\n---\n\n")


async def test_appendix_change_re_exports_every_appendix_only(export_dir, admin):
    agents_md = {s: (export_dir / s / "AGENTS.md").read_text("utf-8") for s in AGENTS}
    r = await admin.client.put("/api/agent-docs/appendice",
                               json={"body": "# Appendice\n\n## §C1 · Uno\nnuovo", "base_version": 1})
    assert r.status_code == 200
    for slug in AGENTS:
        assert (export_dir / slug / "APPENDICE.md").read_text("utf-8") == (
            "# Appendice\n\n## §C1 · Uno\nnuovo")
        assert (export_dir / slug / "AGENTS.md").read_text("utf-8") == agents_md[slug]


async def test_failed_save_does_not_export(export_dir, admin):
    f = export_dir / "ceo" / "AGENTS.md"
    before = f.read_text("utf-8")
    c = admin.client
    assert (await c.put("/api/agent-docs/ceo", json={"body": "  ", "base_version": 1})).status_code == 400
    assert (await c.put("/api/agent-docs/ceo", json={"body": "x", "base_version": 7})).status_code == 409
    assert f.read_text("utf-8") == before
    f.write_text("manomesso", "utf-8")  # a no-op save (same text) neither exports
    body = (await c.get("/api/agent-docs/ceo")).json()["body"]
    await c.put("/api/agent-docs/ceo", json={"body": body, "base_version": 1})
    assert f.read_text("utf-8") == "manomesso"


@pytest.fixture
def missing_export_dir(tmp_path, monkeypatch):
    from smtrading.agentdocs import export

    monkeypatch.setenv("SMT_AGENT_DOCS_EXPORT_DIR", str(tmp_path / "missing"))
    export.reset_state()


async def test_unwritable_dir_reports_error_but_save_succeeds(missing_export_dir, admin):
    c = admin.client
    r = await c.put("/api/agent-docs/ceo", json={"body": "# CEO\nnuovo", "base_version": 1})
    assert r.status_code == 200 and r.json()["doc"]["current_version"] == 2
    st = (await c.get("/api/agent-docs/export-status")).json()
    assert st["ok"] is False and "Esportazione non riuscita" in st["error"]
    r = await c.post("/api/agent-docs/export")
    assert r.json()["ok"] is False and r.json()["exported"] == 0 and r.json()["error"]


async def test_export_status_and_endpoint_auth(export_dir, admin):
    c = admin.client
    st = (await c.get("/api/agent-docs/export-status")).json()
    assert st["ok"] is True and st["error"] is None and st["last_export_at"]
    assert st["entry_file"] == "AGENTS.md"
    assert st["agents"][0] == {"slug": "ceo", "path": "/smtrading-instructions/ceo"}
    assert len(st["agents"]) == 9
    async with httpx.AsyncClient(transport=c._transport, base_url="http://test",
                                 cookies=c.cookies) as no_csrf:
        assert (await no_csrf.post("/api/agent-docs/export")).status_code == 403
    async with httpx.AsyncClient(transport=c._transport, base_url="http://test") as anon:
        assert (await anon.post("/api/agent-docs/export", json={})).status_code == 401
        assert (await anon.get("/api/agent-docs/export-status")).status_code == 401
