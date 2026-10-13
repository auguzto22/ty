"""Limpeza de armazenamento (roda no worker a cada hora e via `clipstudio cleanup`).

- vídeo original e intermediários (áudio, folhas de contato) de projetos concluídos/falhos há mais de
  SOURCE_RETENTION_DAYS: os cortes já renderizados continuam disponíveis, mas não dá para re-renderizar;
- uploads abandonados (nunca concluídos) há mais de 1 dia: projeto inteiro;
- projetos com mais de PROJECT_RETENTION_DAYS (se > 0): projeto inteiro.
"""

import logging
import shutil
from datetime import timedelta

from sqlalchemy import delete, select

from app.api.db import Clip, Job, Project, Upload, UsageEvent, project_dir, session, utcnow
from app.core.config import get_settings

log = logging.getLogger(__name__)
ABANDONED_UPLOAD = timedelta(days=1)


def _dir_size(path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def _delete_project(s, project_id: str) -> int:
    freed = _dir_size(project_dir(project_id))
    for model in (Clip, Job, Upload, Project):
        key = model.id if model is Project else model.project_id
        s.execute(delete(model).where(key == project_id))
    shutil.rmtree(project_dir(project_id), ignore_errors=True)
    return freed


def run_cleanup(dry_run: bool = False) -> dict:
    settings = get_settings()
    now = utcnow()
    stats = {"sources_removed": 0, "projects_removed": 0, "bytes_freed": 0, "dry_run": dry_run}
    with session() as s:
        abandoned = s.scalars(select(Project).where(
            Project.status.in_(["CREATED", "UPLOADING"]), Project.updated_at < now - ABANDONED_UPLOAD)).all()
        expired = []
        if settings.project_retention_days > 0:
            expired = s.scalars(select(Project).where(
                Project.created_at < now - timedelta(days=settings.project_retention_days),
                Project.status.notin_(["QUEUED", "PROCESSING"]))).all()
        for p in {p.id: p for p in [*abandoned, *expired]}.values():
            stats["projects_removed"] += 1
            stats["bytes_freed"] += _dir_size(project_dir(p.id)) if dry_run else _delete_project(s, p.id)

        if settings.source_retention_days > 0:
            old = s.scalars(select(Project).where(
                Project.status.in_(["COMPLETE", "FAILED"]), Project.source_deleted.is_(False),
                Project.updated_at < now - timedelta(days=settings.source_retention_days))).all()
            for p in old:
                if s.get(Project, p.id) is None:
                    continue  # já removido acima
                busy = s.scalar(select(Job.id).where(Job.project_id == p.id, Job.status.in_(["queued", "running"])).limit(1))
                if busy:
                    continue
                d = project_dir(p.id)
                targets = [*d.glob("source.*"), d / "work" / "audio.wav", d / "work" / "sheets"]
                size = sum(_dir_size(t) if t.is_dir() else t.stat().st_size for t in targets if t.exists())
                stats["sources_removed"] += 1
                stats["bytes_freed"] += size
                if not dry_run:
                    for t in targets:
                        shutil.rmtree(t, ignore_errors=True) if t.is_dir() else t.unlink(missing_ok=True)
                    p.source_deleted = True
        if not dry_run:
            s.execute(delete(UsageEvent).where(UsageEvent.created_at < now - timedelta(days=2)))  # cotas olham 24 h
            s.commit()
    if stats["projects_removed"] or stats["sources_removed"]:
        log.info("limpeza: %s", stats)
    return stats
