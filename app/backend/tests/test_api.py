"""Fluxo da API + worker de ponta a ponta, com vídeo sintético, transcrição e curadoria simuladas (sem whisper, sem rede)."""

import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import db
from app.core.config import get_settings
from tests.conftest import make_transcript

TEXT = ("Você sabe por que isso acontece? Esta frase explica um detalhe importante do assunto. "
        "Outra frase traz um exemplo prático para você. Nunca esqueça esta dica valiosa! "
        "Agora vem a conclusão de tudo que falamos aqui.")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("API_TOKEN", "segredo-de-teste")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "falsa")
    monkeypatch.setenv("GEMINI_VISUAL", "false")
    get_settings.cache_clear()
    db.reset_caches()

    import app.pipeline.process as process
    from app.core.models import ClipCandidate
    monkeypatch.setattr(process, "transcribe", lambda wav, duration, **kw: make_transcript(TEXT, wps=2.5))
    monkeypatch.setattr(process, "curate_llm", lambda t, p, *a: [
        ClipCandidate(start=0.0, end=8.0, title="Por que acontece", score=90, source="llm"),
        ClipCandidate(start=8.0, end=16.0, title="Dica valiosa", score=70, source="llm"),
    ])

    video = tmp_path / "in.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=16", "-f", "lavfi", "-i", "sine=frequency=330:duration=16",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(video),
    ], check=True)

    from app.api.main import app
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer segredo-de-teste"
        yield client, video
    get_settings.cache_clear()
    db.reset_caches()


def _upload(client, video: Path, chunk: int) -> dict:
    data = video.read_bytes()
    r = client.post("/projects", json={"filename": "in.mp4", "size": len(data), "content_type": "video/mp4",
                                       "prefs": {"min_duration": 4, "max_duration": 12, "max_clips": 2}})
    assert r.status_code == 201, r.text
    up = r.json()["upload"]
    total = len(data)

    # parte 1
    r = client.put(up["url"], content=data[:chunk], headers={"Content-Range": f"bytes 0-{chunk - 1}/{total}"})
    assert r.status_code == 308 and r.headers["Range"] == f"bytes=0-{chunk - 1}"
    # cliente "perdeu" o estado e reenvia de um offset errado: servidor manda retomar do ponto certo
    r = client.put(up["url"], content=data[5:chunk + 5], headers={"Content-Range": f"bytes 5-{chunk + 4}/{total}"})
    assert r.status_code == 308 and r.headers["Range"] == f"bytes=0-{chunk - 1}"
    # consulta de estado
    r = client.put(up["url"], headers={"Content-Range": f"bytes */{total}"})
    assert r.status_code == 308
    offset = int(r.headers["Range"].split("-")[1]) + 1
    while offset < total:
        end = min(total, offset + chunk) - 1
        r = client.put(up["url"], content=data[offset:end + 1], headers={"Content-Range": f"bytes {offset}-{end}/{total}"})
        offset = end + 1
    assert r.status_code == 201, r.text
    return r.json()["project"]


@pytest.mark.integration
def test_full_flow_upload_process_edit_export(env):
    from app.media.qa import check_clip
    from app.worker import run

    client, video = env
    project = _upload(client, video, chunk=100_000)
    assert project["status"] == "UPLOADED" and project["duration"] > 15

    r = client.post(f"/projects/{project['id']}/start")
    assert r.status_code == 202 and r.json()["status"] == "QUEUED"
    assert run(once=True) == 1

    p = client.get(f"/projects/{project['id']}").json()
    assert p["status"] == "COMPLETE", p
    assert set(p["timings"]) >= {"TRANSCRIBE", "ANALYZE", "RENDER"}
    clips = client.get(f"/projects/{project['id']}/clips").json()
    assert 1 <= len(clips) <= 2
    c = clips[0]
    assert c["preview"]["url"] and c["reframe"] == "fit" and c["qa"]["ok"]

    r = client.get(c["preview"]["url"], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100

    # edição: trim → nova versão e preview re-renderizado
    r = client.patch(f"/clips/{c['id']}", json={"start": c["start"] + 0.5, "end": c["end"]})
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["render_pending"]
    assert r.json()["preview"]["stale"]
    assert run(once=True) == 1
    c2 = client.get(f"/clips/{c['id']}").json()
    assert c2["preview"]["version"] == 2 and not c2["preview"]["stale"] and not c2["render_pending"]

    # export 1080x1920
    r = client.post(f"/clips/{c['id']}/render", params={"quality": "export"})
    assert r.status_code == 202
    assert run(once=True) == 1
    c3 = client.get(f"/clips/{c['id']}").json()
    assert c3["export"]["version"] == 2
    data_dir = Path(get_settings().data_dir)
    exported = next((data_dir / "projects" / project["id"] / "work" / "clips").glob("*_export.mp4"))
    rep = check_clip(exported, (1080, 1920), c3["duration"], expect_audio=True)
    assert rep.ok, rep.problems

    assert client.get(f"/projects/{project['id']}/transcript.srt").status_code == 200
    assert client.delete(f"/projects/{project['id']}").status_code == 204
    assert client.get(f"/projects/{project['id']}").status_code == 404


def test_auth_required(env):
    client, _ = env
    del client.headers["Authorization"]
    assert client.get("/projects").status_code == 401
    assert client.get("/health").status_code == 200


def test_invalid_upload_marks_project_failed(env, tmp_path):
    client, _ = env
    junk = b"isto nao e um video" * 10
    r = client.post("/projects", json={"filename": "x.mp4", "size": len(junk)})
    up = r.json()["upload"]
    r = client.put(up["url"], content=junk, headers={"Content-Range": f"bytes 0-{len(junk) - 1}/{len(junk)}"})
    assert r.status_code == 422 and r.json()["project"]["status"] == "FAILED"


def test_expired_lease_is_reclaimed(env):
    from app.api.jobs import claim, enqueue

    client, _ = env
    r = client.post("/projects", json={"filename": "x.mp4", "size": 10})
    pid = r.json()["project"]["id"]
    job = enqueue(pid, "render", "nao-existe", {"quality": "preview"})
    first = claim("w1")
    assert first.id == job.id and claim("w2") is None  # ocupado
    with db.session() as s:
        j = s.get(db.Job, job.id)
        j.lease_until = db.utcnow() - timedelta(seconds=1)  # w1 "morreu"
        s.commit()
    again = claim("w2")
    assert again.id == job.id and again.worker == "w2" and again.attempts == 2


def test_job_abandoned_after_max_attempts(env):
    """Worker morto (ex.: OOM-kill) em todas as tentativas: o job falha em vez de voltar à fila para sempre."""
    from app.api.jobs import MAX_ATTEMPTS, claim, enqueue

    client, _ = env
    r = client.post("/projects", json={"filename": "x.mp4", "size": 10})
    pid = r.json()["project"]["id"]
    job = enqueue(pid, "process")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claimed = claim(f"w{attempt}")
        assert claimed.id == job.id and claimed.attempts == attempt
        with db.session() as s:
            s.get(db.Job, job.id).lease_until = db.utcnow() - timedelta(seconds=1)  # o worker "morreu"
            s.commit()
    assert claim("w-final") is None
    with db.session() as s:
        assert s.get(db.Job, job.id).status == "failed"
    p = client.get(f"/projects/{pid}").json()
    assert p["status"] == "FAILED" and "worker parou" in p["error"]
