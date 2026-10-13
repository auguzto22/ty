"""Fila de jobs numa tabela SQLite, com lease/heartbeat: um job "running" cujo lease venceu volta a ser elegível
(o worker morreu). Os estágios do pipeline reaproveitam artefatos em disco, então reexecutar é barato."""

import logging
import os
import socket
import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select, update

from app.api.db import Clip, Job, Project, project_dir, session, utcnow
from app.core.models import CaptionStyle, CurationPrefs, EditSpec, Quality
from app.media.probe import probe
from app.pipeline.process import ClipResult, ProcessResult, Stage, load_transcript, process_video, render_and_check

log = logging.getLogger(__name__)

LEASE = timedelta(seconds=90)
HEARTBEAT_EVERY = 20.0
MAX_ATTEMPTS = 3
PROGRESS_EVERY = 1.0  # s entre gravações de progresso


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue(project_id: str, kind: str, clip_id: str | None = None, payload: dict | None = None) -> Job:
    with session() as s:
        job = Job(project_id=project_id, kind=kind, clip_id=clip_id, payload=payload or {})
        s.add(job)
        s.commit()
        return job


def claim(worker: str) -> Job | None:
    """Pega o job elegível mais antigo de forma atômica (UPDATE condicional)."""
    now = utcnow()
    with session() as s:
        candidates = s.scalars(
            select(Job.id).where(or_(Job.status == "queued", (Job.status == "running") & (Job.lease_until < now)))
            .order_by(Job.created_at).limit(5)
        ).all()
        for job_id in candidates:
            res = s.execute(
                update(Job)
                .where(Job.id == job_id, or_(Job.status == "queued", (Job.status == "running") & (Job.lease_until < now)))
                .values(status="running", lease_until=now + LEASE, worker=worker, attempts=Job.attempts + 1)
            )
            s.commit()
            if res.rowcount == 1:
                return s.get(Job, job_id)
    return None


class Heartbeat:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_EVERY):
            with session() as s:
                s.execute(update(Job).where(Job.id == self.job_id, Job.status == "running")
                          .values(lease_until=utcnow() + LEASE))
                s.commit()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


def _set_project(project_id: str, **values) -> None:
    with session() as s:
        s.execute(update(Project).where(Project.id == project_id).values(**values))
        s.commit()


def _progress_writer(project_id: str):
    last = {"t": 0.0, "stage": ""}

    def on_stage(stage: Stage, progress: float, msg: str) -> None:
        now = time.monotonic()
        if stage.value == last["stage"] and now - last["t"] < PROGRESS_EVERY and progress < 1.0:
            return
        last.update(t=now, stage=stage.value)
        _set_project(project_id, stage=stage.value, progress=round(progress, 3), message=msg[:500])

    return on_stage


def _source_path(project_id: str) -> Path:
    found = sorted(project_dir(project_id).glob("source.*"))
    if not found:
        raise FileNotFoundError("vídeo de origem não encontrado")
    return found[0]


def _clip_spec(clip: Clip) -> EditSpec:
    return EditSpec.model_validate(clip.edit_spec)


def run_process(job: Job) -> None:
    with session() as s:
        project = s.get(Project, job.project_id)
        prefs_data = dict(project.prefs or {})
    render_data = prefs_data.pop("render", {})
    prefs_data.pop("use_llm", None)  # campo de projetos antigos; a IA agora é sempre usada
    prefs = CurationPrefs.model_validate(prefs_data)
    base = EditSpec(start=0, end=0, aspect=render_data.get("aspect", "9:16"), reframe=render_data.get("reframe", "auto"),
                    captions=CaptionStyle.model_validate(render_data.get("captions", {})))
    _set_project(job.project_id, status="PROCESSING", error="")

    work = project_dir(job.project_id) / "work"
    result: ProcessResult = process_video(
        _source_path(job.project_id), work, prefs, base,
        render_quality=Quality.PREVIEW,
        on_stage=_progress_writer(job.project_id),
    )
    with session() as s:
        for old in s.scalars(select(Clip).where(Clip.project_id == job.project_id)).all():
            s.delete(old)
        for c in result.clips:
            s.add(Clip(
                project_id=job.project_id, rank=c.rank, score=c.candidate.score,
                scores=c.candidate.scores.model_dump(), title=c.candidate.title, hook=c.candidate.hook,
                reason=c.candidate.reason, edit_spec=c.spec.model_dump(mode="json"), version=1,
                preview_path=c.preview or "", preview_version=1 if c.preview else 0,
                thumb_path=c.thumbnail or "", qa=c.qa.model_dump() if c.qa else {},
            ))
        project = s.get(Project, job.project_id)
        project.status, project.stage, project.progress = "COMPLETE", Stage.COMPLETE.value, 1.0
        project.duration, project.language, project.curation = result.info.duration, result.language, result.curation
        project.warnings, project.timings = result.warnings, result.timings
        project.message = f"{len(result.clips)} cortes prontos"
        s.commit()


def run_render(job: Job) -> None:
    quality = Quality(job.payload.get("quality", "export"))
    with session() as s:
        clip = s.get(Clip, job.clip_id)
        if clip is None:
            return
        version = clip.version
        res = ClipResult(rank=clip.rank, candidate={"start": 0, "end": 0}, spec=_clip_spec(clip))
    work = project_dir(job.project_id) / "work"
    source = _source_path(job.project_id)
    render_and_check(source, probe(source), load_transcript(work), res, work, quality)
    path = res.export if quality is Quality.EXPORT else res.preview
    with session() as s:
        clip = s.get(Clip, job.clip_id)
        if clip is None or clip.version != version:
            return  # editado durante o render: um novo job já foi enfileirado
        if quality is Quality.EXPORT:
            clip.export_path, clip.export_version = path, version
        else:
            clip.preview_path, clip.preview_version = path, version
        clip.thumb_path = res.thumbnail or clip.thumb_path
        clip.qa = res.qa.model_dump() if res.qa else {}
        clip.render_pending = s.scalar(
            select(Job.id).where(Job.clip_id == clip.id, Job.status.in_(["queued", "running"]), Job.id != job.id).limit(1)
        ) is not None
        s.commit()


HANDLERS = {"process": run_process, "render": run_render}


def execute(job: Job) -> None:
    """Executa um job já reivindicado e registra o resultado; erros reenfileiram até MAX_ATTEMPTS."""
    with Heartbeat(job.id):
        try:
            HANDLERS[job.kind](job)
        except Exception as e:  # o worker não pode morrer por causa de um vídeo
            log.exception("job %s (%s) falhou", job.id, job.kind)
            final = job.attempts >= MAX_ATTEMPTS or isinstance(e, (ValueError, FileNotFoundError))
            with session() as s:
                s.execute(update(Job).where(Job.id == job.id).values(
                    status="failed" if final else "queued", error=str(e)[:2000], lease_until=None))
                s.commit()
            if job.kind == "process" and final:
                _set_project(job.project_id, status="FAILED", stage=Stage.FAILED.value, error=str(e)[:2000])
            elif job.kind == "render" and final:
                with session() as s:
                    clip = s.get(Clip, job.clip_id)
                    if clip:
                        clip.render_pending = False
                        s.commit()
            return
    with session() as s:
        s.execute(update(Job).where(Job.id == job.id).values(status="done", lease_until=None, error=""))
        s.commit()
