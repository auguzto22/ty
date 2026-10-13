"""API HTTP do ClipStudio (FastAPI). Contrato pensado para o app Android: upload retomável com Content-Range,
progresso por polling, cortes versionados e download com Range.

    uvicorn app.api.main:app --host 0.0.0.0 --port 8000
"""

import shutil
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.api.auth import Principal, authenticate, create_user, current_principal, issue_token
from app.api.db import Clip, Job, Project, Upload, UsageEvent, User, engine, project_dir, session, utcnow
from app.api.jobs import enqueue
from app.core.config import get_settings
from app.core.models import AspectRatio, CaptionStyle, CurationPrefs, EditSpec, Reframe
from app.media.faces import face_detection_available
from app.media.ffmpeg import FFmpegError
from app.media.probe import probe
from app.pipeline.analyze import load_signals
from app.pipeline.reframe import plan_reframe

CHUNK_SIZE = 8 * 1024 * 1024  # sugerido ao cliente; o servidor aceita qualquer tamanho
EXTENSIONS = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv", "video/webm": ".webm"}

@asynccontextmanager
async def lifespan(_: FastAPI):
    engine()  # cria o banco/tabelas se preciso
    yield


app = FastAPI(title="ClipStudio API", version="0.4.0", lifespan=lifespan)


Who = Annotated[Principal, Depends(current_principal)]


# ---------- esquemas ----------

class RenderPrefs(BaseModel):
    aspect: AspectRatio = AspectRatio.VERTICAL
    reframe: Reframe = Reframe.AUTO
    captions: CaptionStyle = Field(default_factory=CaptionStyle)


class ProjectPrefs(CurationPrefs):
    max_clips: int = Field(3, ge=1, le=20)
    min_duration: float = Field(20.0, ge=3)
    max_duration: float = Field(60.0, le=180)
    render: RenderPrefs = Field(default_factory=RenderPrefs)


class CreateProject(BaseModel):
    title: str = ""
    filename: str
    size: int = Field(gt=0)
    content_type: str = "video/mp4"
    prefs: ProjectPrefs = Field(default_factory=ProjectPrefs)


class ClipEdit(BaseModel):
    start: float | None = None
    end: float | None = None
    aspect: AspectRatio | None = None
    reframe: Reframe | None = None
    captions: CaptionStyle | None = None
    word_overrides: dict[int, str] | None = None


# ---------- serialização ----------

def project_json(p: Project, s=None) -> dict:
    out = {
        "id": p.id, "title": p.title, "status": p.status, "stage": p.stage, "progress": p.progress,
        "message": p.message, "error": p.error, "duration": p.duration, "language": p.language,
        "curation": p.curation, "warnings": p.warnings or [], "timings": p.timings or {},
        "prefs": p.prefs, "source_name": p.source_name, "source_deleted": p.source_deleted,
        "created_at": p.created_at.isoformat() + "Z", "updated_at": p.updated_at.isoformat() + "Z",
    }
    if s is not None and p.status == "QUEUED":
        mine = s.scalar(select(Job.created_at).where(Job.project_id == p.id, Job.status == "queued").limit(1))
        out["queue_position"] = s.scalar(select(func.count()).select_from(Job).where(
            Job.status.in_(["queued", "running"]), Job.created_at < mine)) if mine else 0
    return out


def clip_json(c: Clip) -> dict:
    spec = EditSpec.model_validate(c.edit_spec)

    def media(kind: str, path: str, version: int) -> dict | None:
        if not path:
            return None
        return {"url": f"/clips/{c.id}/file?kind={kind}&v={version}", "version": version, "stale": version != c.version}

    return {
        "id": c.id, "project_id": c.project_id, "rank": c.rank, "score": c.score, "scores": c.scores,
        "title": c.title, "hook": c.hook, "reason": c.reason,
        "start": spec.start, "end": spec.end, "duration": round(spec.end - spec.start, 3),
        "aspect": spec.aspect.value, "reframe": spec.reframe.value, "captions": spec.captions.model_dump(), "word_overrides": {str(k): v for k, v in spec.word_overrides.items()},
        "version": c.version, "render_pending": c.render_pending,
        "preview": media("preview", c.preview_path, c.preview_version),
        "export": media("export", c.export_path, c.export_version),
        "thumbnail_url": f"/clips/{c.id}/file?kind=thumb&v={c.preview_version}" if c.thumb_path else None,
        "qa": c.qa,
    }


def _owns(who: Principal, p: Project | None) -> bool:
    return p is not None and (who.is_admin or p.owner_id == who.user_id)


def _get_project(s, project_id: str, who: Principal) -> Project:
    p = s.get(Project, project_id)
    if not _owns(who, p):  # projeto de outro usuário responde igual a inexistente
        raise HTTPException(404, "projeto não encontrado")
    return p


def _get_clip(s, clip_id: str, who: Principal) -> Clip:
    c = s.get(Clip, clip_id)
    if c is None or not _owns(who, s.get(Project, c.project_id)):
        raise HTTPException(404, "corte não encontrado")
    return c


def _usage_today(s, who: Principal) -> tuple[int, float]:
    """(projetos criados, minutos de vídeo enviados) nas últimas 24 h pelo usuário."""
    since = utcnow() - timedelta(days=1)

    def total(kind: str) -> float:
        return float(s.scalar(select(func.coalesce(func.sum(UsageEvent.amount), 0.0)).where(
            UsageEvent.user_id == who.user_id, UsageEvent.kind == kind, UsageEvent.created_at >= since)))

    return int(total("project")), total("seconds") / 60.0


def _require_source(p: Project) -> None:
    if p.source_deleted:
        raise HTTPException(409, "o vídeo original deste projeto expirou; não é possível renderizar de novo")


# ---------- rotas ----------

@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {"ok": True, "llm": s.llm_provider if s.llm_enabled else "off", "faces": face_detection_available(),
            "auth": bool(s.api_token.get_secret_value()), "signup": s.allow_signup}


class Credentials(BaseModel):
    email: str
    password: str


def _session_json(user: User) -> dict:
    return {"token": issue_token(user), "user": {"id": user.id, "email": user.email, "is_admin": user.is_admin}}


@app.post("/auth/register", status_code=201)
def register(body: Credentials) -> dict:
    if not get_settings().allow_signup:
        raise HTTPException(403, "cadastro fechado; peça uma conta ao administrador")
    try:
        user = create_user(body.email, body.password)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return _session_json(user)


@app.post("/auth/login")
def login(body: Credentials) -> dict:
    user = authenticate(body.email, body.password)
    if user is None:
        raise HTTPException(401, "e-mail ou senha incorretos")
    return _session_json(user)


@app.get("/me")
def me(who: Who) -> dict:
    settings = get_settings()
    with session() as s:
        count, minutes = _usage_today(s, who) if who.user_id else (0, 0.0)
    return {
        "id": who.user_id, "email": who.email, "is_admin": who.is_admin,
        "usage_24h": {"projects": count, "minutes": round(minutes, 1)},
        "quota": None if who.is_admin else {"projects_per_day": settings.quota_projects_per_day,
                                           "minutes_per_day": settings.quota_minutes_per_day},
    }


@app.post("/projects", status_code=201)
def create_project(who: Who, body: CreateProject) -> dict:
    settings = get_settings()
    if not settings.llm_enabled:  # sem IA não há como escolher cortes; evita um upload inútil
        raise HTTPException(503, f"curadoria por IA não configurada no servidor (LLM_PROVIDER={settings.llm_provider} sem chave)")
    if not body.content_type.startswith("video/"):
        raise HTTPException(415, "envie um arquivo de vídeo")
    if body.size > settings.max_upload_bytes:
        raise HTTPException(413, f"arquivo maior que {settings.max_upload_bytes} bytes")
    if body.prefs.min_duration > body.prefs.max_duration:
        raise HTTPException(422, "min_duration > max_duration")
    ext = EXTENSIONS.get(body.content_type) or (Path(body.filename).suffix.lower()[:6] or ".mp4")
    with session() as s:
        if not who.is_admin and settings.quota_projects_per_day:
            count, _ = _usage_today(s, who)
            if count >= settings.quota_projects_per_day:
                raise HTTPException(429, f"limite de {settings.quota_projects_per_day} projetos por dia atingido")
        p = Project(title=body.title or Path(body.filename).stem, source_name=body.filename, owner_id=who.user_id,
                    prefs=body.prefs.model_dump(mode="json"), status="CREATED")
        s.add(p)
        if who.user_id:
            s.add(UsageEvent(user_id=who.user_id, kind="project"))
        s.flush()
        d = project_dir(p.id)
        d.mkdir(parents=True, exist_ok=True)
        u = Upload(project_id=p.id, size=body.size, content_type=body.content_type, path=str(d / f"source{ext}"))
        s.add(u)
        s.commit()
        return {"project": project_json(p), "upload": {"id": u.id, "url": f"/uploads/{u.id}", "chunk_size": CHUNK_SIZE}}


def _range_header(received: int) -> dict:
    return {"Range": f"bytes=0-{received - 1}"} if received > 0 else {}


def _finish_upload(s, u: Upload, who: Principal) -> JSONResponse:
    p = s.get(Project, u.project_id)
    settings = get_settings()
    try:
        info = probe(Path(u.path))
        if not info.has_audio:
            raise ValueError("o vídeo não tem áudio")
        if info.duration > settings.max_video_seconds:
            raise ValueError(f"vídeo mais longo que {settings.max_video_seconds:.0f}s")
        if not who.is_admin and settings.quota_minutes_per_day:
            _, minutes = _usage_today(s, who)
            if minutes + info.duration / 60 > settings.quota_minutes_per_day:
                raise ValueError(f"limite diário de {settings.quota_minutes_per_day:.0f} min de vídeo excedido")
    except (FFmpegError, ValueError) as e:
        p.status, p.error = "FAILED", f"arquivo inválido: {e}"
        s.commit()
        return JSONResponse({"error": p.error, "project": project_json(p)}, status_code=422)
    u.complete = True
    p.status, p.duration, p.error = "UPLOADED", info.duration, ""
    if who.user_id:
        s.add(UsageEvent(user_id=who.user_id, kind="seconds", amount=info.duration))
    s.commit()
    return JSONResponse({"project": project_json(p)}, status_code=201)


@app.put("/uploads/{upload_id}")
async def put_upload(who: Who, upload_id: str, request: Request, content_range: Annotated[str, Header()]) -> Response:
    """Upload retomável: `Content-Range: bytes a-b/total` envia uma parte; `bytes */total` (corpo vazio) consulta.
    Respostas: 308 + `Range: bytes=0-n` (incompleto), 201 (completo e válido), 422 (arquivo inválido)."""
    unit, _, spec = content_range.partition(" ")
    rng, _, total_s = spec.partition("/")
    if unit != "bytes" or not total_s.isdigit():
        raise HTTPException(400, "Content-Range inválido")
    with session() as s:
        u = s.get(Upload, upload_id)
        if u is None or not _owns(who, s.get(Project, u.project_id)):
            raise HTTPException(404, "upload não encontrado")
        if int(total_s) != u.size:
            raise HTTPException(400, f"tamanho total diferente do declarado ({u.size})")
        if u.complete:
            return JSONResponse({"project": project_json(s.get(Project, u.project_id))}, status_code=201)
        if rng == "*":
            return Response(status_code=308, headers=_range_header(u.received))
        a_s, _, b_s = rng.partition("-")
        if not (a_s.isdigit() and b_s.isdigit()) or int(b_s) < int(a_s) or int(b_s) >= u.size:
            raise HTTPException(416, "intervalo inválido")
        a, b = int(a_s), int(b_s)
        if a != u.received:  # cliente fora de sincronia: informa onde retomar
            return Response(status_code=308, headers=_range_header(u.received))
        p = s.get(Project, u.project_id)
        p.status = "UPLOADING"
        s.commit()

        expected = b - a + 1
        written = 0
        path = Path(u.path)
        mode = "r+b" if path.exists() else "wb"
        with open(path, mode) as f:
            f.seek(a)
            f.truncate()
            try:
                async for chunk in request.stream():
                    if written + len(chunk) > expected:
                        chunk = chunk[: expected - written]
                    f.write(chunk)
                    written += len(chunk)
                    if written >= expected:
                        break
            finally:  # conexão caiu no meio: guarda o que chegou para retomar dali
                f.flush()
                u.received = a + written
                s.commit()
        if u.received >= u.size:
            return _finish_upload(s, u, who)
        return Response(status_code=308, headers=_range_header(u.received))


@app.post("/projects/{project_id}/start", status_code=202)
def start_project(who: Who, project_id: str) -> dict:
    with session() as s:
        p = _get_project(s, project_id, who)
        if p.status not in ("UPLOADED", "COMPLETE", "FAILED"):
            raise HTTPException(409, f"projeto em estado {p.status}")
        if not s.scalar(select(Upload.complete).where(Upload.project_id == p.id)):
            raise HTTPException(409, "upload incompleto")
        p.status, p.stage, p.progress, p.error, p.message = "QUEUED", "QUEUED", 0.0, "", "Na fila"
        s.commit()
    enqueue(project_id, "process")
    with session() as s:
        return project_json(_get_project(s, project_id, who), s)


@app.get("/projects")
def list_projects(who: Who, limit: int = Query(50, le=200)) -> list[dict]:
    with session() as s:
        q = select(Project).order_by(Project.created_at.desc()).limit(limit)
        if not who.is_admin:
            q = q.where(Project.owner_id == who.user_id)
        return [project_json(p) for p in s.scalars(q)]


@app.get("/projects/{project_id}")
def get_project(who: Who, project_id: str) -> dict:
    with session() as s:
        return project_json(_get_project(s, project_id, who), s)


@app.delete("/projects/{project_id}", status_code=204)
def delete_project(who: Who, project_id: str) -> Response:
    with session() as s:
        p = _get_project(s, project_id, who)
        if p.status == "PROCESSING":
            raise HTTPException(409, "projeto em processamento")
        for model in (Clip, Job, Upload, Project):  # filhos antes do pai
            key = model.id if model is Project else model.project_id
            s.execute(delete(model).where(key == project_id))
        s.commit()
    shutil.rmtree(project_dir(project_id), ignore_errors=True)
    return Response(status_code=204)


@app.get("/projects/{project_id}/clips")
def list_clips(who: Who, project_id: str) -> list[dict]:
    with session() as s:
        _get_project(s, project_id, who)
        return [clip_json(c) for c in s.scalars(select(Clip).where(Clip.project_id == project_id).order_by(Clip.rank))]


@app.get("/projects/{project_id}/transcript.{fmt}")
def transcript(who: Who, project_id: str, fmt: Literal["srt", "txt", "json"]) -> FileResponse:
    with session() as s:
        _get_project(s, project_id, who)
    path = project_dir(project_id) / "work" / f"transcript.{fmt}"
    if not path.exists():
        raise HTTPException(404, "transcrição ainda não disponível")
    media = {"srt": "application/x-subrip", "txt": "text/plain; charset=utf-8", "json": "application/json"}[fmt]
    return FileResponse(path, media_type=media)


@app.get("/clips/{clip_id}")
def get_clip(who: Who, clip_id: str) -> dict:
    with session() as s:
        return clip_json(_get_clip(s, clip_id, who))


@app.patch("/clips/{clip_id}")
def edit_clip(who: Who, clip_id: str, body: ClipEdit) -> dict:
    """Edita o corte (trim, proporção, enquadramento, legendas). Gera nova versão e enfileira um preview."""
    with session() as s:
        c = _get_clip(s, clip_id, who)
        p = _get_project(s, c.project_id, who)
        _require_source(p)
        spec = EditSpec.model_validate(c.edit_spec)
        updates = body.model_dump(exclude_none=True)
        spec = spec.model_copy(update={k: v for k, v in {
            "start": body.start, "end": body.end, "aspect": body.aspect, "reframe": body.reframe,
            "captions": body.captions, "word_overrides": body.word_overrides}.items() if v is not None})
        if not 0 <= spec.start < spec.end or (p.duration and spec.end > p.duration + 0.5):
            raise HTTPException(422, "início/fim inválidos")
        if updates.keys() & {"start", "end", "aspect", "reframe"}:
            work = project_dir(c.project_id) / "work"
            source = next(iter(sorted(project_dir(c.project_id).glob("source.*"))), None)
            if source is None:
                raise HTTPException(409, "vídeo de origem ausente")
            if body.reframe is not None:
                spec = spec.model_copy(update={"reframe_auto": body.reframe == Reframe.AUTO})
            elif spec.reframe_auto:  # modo automático: decide de novo para o novo trecho; escolha do usuário fica
                spec = spec.model_copy(update={"reframe": Reframe.AUTO})
            spec = plan_reframe(load_signals(work / "signals.json"), probe(source), spec)
        c.edit_spec = spec.model_dump(mode="json")
        c.version += 1
        c.render_pending = True
        s.commit()
        project_id = c.project_id
    enqueue(project_id, "render", clip_id, {"quality": "preview"})
    with session() as s:
        return clip_json(_get_clip(s, clip_id, who))


@app.post("/clips/{clip_id}/render", status_code=202)
def render(who: Who, clip_id: str, quality: Literal["preview", "export"] = "export") -> dict:
    with session() as s:
        c = _get_clip(s, clip_id, who)
        current = c.export_version if quality == "export" else c.preview_version
        path = c.export_path if quality == "export" else c.preview_path
        if path and current == c.version:
            return clip_json(c)  # já existe para esta versão
        _require_source(s.get(Project, c.project_id))
        pending = s.scalar(select(Job.id).where(Job.clip_id == clip_id, Job.kind == "render",
                                                Job.status.in_(["queued", "running"])).limit(1))
        c.render_pending = True
        s.commit()
        project_id = c.project_id
    if pending is None or quality == "export":
        enqueue(project_id, "render", clip_id, {"quality": quality})
    with session() as s:
        return clip_json(_get_clip(s, clip_id, who))


@app.get("/clips/{clip_id}/file")
def clip_file(who: Who, clip_id: str, kind: Literal["preview", "export", "thumb"] = "preview") -> FileResponse:
    """Arquivo do corte. Suporta `Range` (download retomável, seek no player)."""
    with session() as s:
        c = _get_clip(s, clip_id, who)
        rel = {"preview": c.preview_path, "export": c.export_path, "thumb": c.thumb_path}[kind]
        work = project_dir(c.project_id) / "work"
        name = f"{c.rank:02d}_{(c.title or 'corte')[:40]}"
    if not rel or not (work / rel).exists():
        raise HTTPException(404, f"{kind} ainda não renderizado")
    path = work / rel
    if kind == "thumb":
        return FileResponse(path, media_type="image/jpeg")
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in name).strip()
    return FileResponse(path, media_type="video/mp4", filename=f"{safe}.mp4", content_disposition_type="inline")
