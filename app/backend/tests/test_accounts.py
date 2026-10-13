"""Contas (JWT), isolamento entre usuários, cotas e limpeza de armazenamento."""

import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from fastapi.testclient import TestClient

from app.api import auth, db
from app.core.config import get_settings


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    clients = []

    def factory(**env):
        base = {"DATA_DIR": str(tmp_path / "data"), "LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "falsa", "API_TOKEN": "admin-token",
                "ALLOW_SIGNUP": "true", "QUOTA_PROJECTS_PER_DAY": "20", "QUOTA_MINUTES_PER_DAY": "180"}
        base.update(env)
        for k, v in base.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        db.reset_caches()
        auth.reset_caches()
        from app.api.main import app
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield factory
    for c in clients:
        c.__exit__(None, None, None)
    get_settings.cache_clear()
    db.reset_caches()
    auth.reset_caches()


def _login(client, email, password="senha-forte-123"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email.upper(), "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _new_project(client, headers, size=10):
    return client.post("/projects", json={"filename": "a.mp4", "size": size}, headers=headers)


def test_register_login_and_isolation(make_client):
    c = make_client()
    alice, bob = _login(c, "alice@ex.com"), _login(c, "bob@ex.com")
    admin = {"Authorization": "Bearer admin-token"}

    pa = _new_project(c, alice).json()["project"]["id"]
    pb = _new_project(c, bob).json()["project"]["id"]
    assert [p["id"] for p in c.get("/projects", headers=alice).json()] == [pa]
    assert c.get(f"/projects/{pb}", headers=alice).status_code == 404  # projeto alheio = inexistente
    assert c.delete(f"/projects/{pb}", headers=alice).status_code == 404
    assert {p["id"] for p in c.get("/projects", headers=admin).json()} == {pa, pb}

    me = c.get("/me", headers=alice).json()
    assert me["email"] == "alice@ex.com" and me["usage_24h"]["projects"] == 1 and me["quota"]["projects_per_day"] == 20
    assert c.get("/me", headers=admin).json()["quota"] is None


def test_bad_credentials_and_tokens(make_client):
    c = make_client()
    _login(c, "carol@ex.com")
    assert c.post("/auth/login", json={"email": "carol@ex.com", "password": "errada-123"}).status_code == 401
    assert c.post("/auth/register", json={"email": "carol@ex.com", "password": "outra-senha-1"}).status_code == 422
    assert c.post("/auth/register", json={"email": "x@ex.com", "password": "curta"}).status_code == 422
    assert c.get("/projects", headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401
    assert c.get("/projects").status_code == 401


def test_signup_closed_by_default(make_client):
    c = make_client(ALLOW_SIGNUP="false")
    assert c.post("/auth/register", json={"email": "d@ex.com", "password": "senha-forte-123"}).status_code == 403
    auth.create_user("d@ex.com", "senha-forte-123")  # conta criada pelo administrador (CLI)
    assert c.post("/auth/login", json={"email": "d@ex.com", "password": "senha-forte-123"}).status_code == 200


def test_project_quota(make_client):
    c = make_client(QUOTA_PROJECTS_PER_DAY="2")
    h = _login(c, "e@ex.com")
    assert _new_project(c, h).status_code == 201
    assert _new_project(c, h).status_code == 201
    r = _new_project(c, h)
    assert r.status_code == 429 and "limite" in r.json()["detail"]
    for p in c.get("/projects", headers=h).json():  # apagar projetos não devolve a cota
        assert c.delete(f"/projects/{p['id']}", headers=h).status_code == 204
    assert _new_project(c, h).status_code == 429
    assert _new_project(c, {"Authorization": "Bearer admin-token"}).status_code == 201  # admin sem cota


@pytest.mark.integration
def test_minutes_quota_rejects_upload(make_client, tmp_path):
    c = make_client(QUOTA_MINUTES_PER_DAY="0.1")  # 6 s
    h = _login(c, "f@ex.com")
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=15:d=10",
                    "-f", "lavfi", "-i", "sine=duration=10", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(video)], check=True)
    data = video.read_bytes()
    up = _new_project(c, h, size=len(data)).json()["upload"]
    r = c.put(up["url"], content=data, headers={**h, "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}"})
    assert r.status_code == 422 and "limite diário" in r.json()["error"]


def test_dev_mode_without_credentials_is_open(make_client):
    c = make_client(API_TOKEN="")
    assert c.get("/projects").status_code == 200  # sem token e sem contas: modo local aberto
    _login(c, "g@ex.com")
    assert c.get("/projects").status_code == 401  # existindo contas, exige login


def test_cleanup_removes_old_sources_and_abandoned_uploads(make_client):
    from app.api.cleanup import run_cleanup
    from app.core.models import EditSpec

    c = make_client(SOURCE_RETENTION_DAYS="7")
    old = db.utcnow() - timedelta(days=10)
    with db.session() as s:
        done = db.Project(status="COMPLETE", title="velho")
        stale = db.Project(status="UPLOADING", title="abandonado")
        fresh = db.Project(status="COMPLETE", title="novo")
        s.add_all([done, stale, fresh])
        s.flush()
        for p in (done, stale, fresh):
            d = db.project_dir(p.id)
            (d / "work" / "clips").mkdir(parents=True)
            (d / "source.mp4").write_bytes(b"x" * 1000)
            (d / "work" / "audio.wav").write_bytes(b"y" * 500)
            (d / "work" / "clips" / "clip_01_export.mp4").write_bytes(b"z" * 100)
        clip = db.Clip(project_id=done.id, rank=1, edit_spec=EditSpec(start=0, end=5).model_dump(mode="json"))
        s.add(clip)
        s.commit()
        ids = (done.id, stale.id, fresh.id, clip.id)
    with db.session() as s:  # "updated_at" no passado (onupdate não interfere em UPDATE explícito)
        s.execute(update(db.Project).where(db.Project.id.in_(ids[:2])).values(updated_at=old))
        s.commit()

    assert run_cleanup(dry_run=True)["sources_removed"] == 1
    assert db.project_dir(ids[0]).joinpath("source.mp4").exists()  # simulação não apaga
    stats = run_cleanup()
    assert stats == {"sources_removed": 1, "projects_removed": 1, "bytes_freed": 1500 + 1600, "dry_run": False}
    d = db.project_dir(ids[0])
    assert not (d / "source.mp4").exists() and not (d / "work" / "audio.wav").exists()
    assert (d / "work" / "clips" / "clip_01_export.mp4").exists()  # cortes ficam
    assert not db.project_dir(ids[1]).exists()
    assert db.project_dir(ids[2]).joinpath("source.mp4").exists()

    admin = {"Authorization": "Bearer admin-token"}
    assert c.get(f"/projects/{ids[0]}", headers=admin).json()["source_deleted"] is True
    r = c.patch(f"/clips/{ids[3]}", json={"start": 1.0, "end": 4.0}, headers=admin)
    assert r.status_code == 409 and "expirou" in r.json()["detail"]
