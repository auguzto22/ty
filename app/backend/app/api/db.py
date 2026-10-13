"""Persistência em SQLite (WAL) com SQLAlchemy 2. Um arquivo `clipstudio.db` em DATA_DIR."""

import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import get_settings


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UsageEvent(Base):
    """Consumo para cotas, independente dos projetos (apagar um projeto não devolve a cota)."""
    __tablename__ = "usage_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # "project" | "seconds"
    amount: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, default=None)
    source_deleted: Mapped[bool] = mapped_column(default=False)  # vídeo original removido pela limpeza
    title: Mapped[str] = mapped_column(String(200), default="")
    # CREATED → UPLOADING → UPLOADED → QUEUED → PROCESSING → COMPLETE | FAILED
    status: Mapped[str] = mapped_column(String(16), default="CREATED")
    stage: Mapped[str] = mapped_column(String(16), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)  # CurationPrefs + render (aspect, reframe, captions)
    source_name: Mapped[str] = mapped_column(String(300), default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    language: Mapped[str] = mapped_column(String(8), default="")
    curation: Mapped[str] = mapped_column(String(16), default="")
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    size: Mapped[int] = mapped_column(Integer)
    received: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(100), default="video/mp4")
    path: Mapped[str] = mapped_column(Text)
    complete: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # "process" | "render"
    clip_id: Mapped[str | None] = mapped_column(String(32), default=None)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued | running | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(default=None)
    worker: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Clip(Base):
    __tablename__ = "clips"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    title: Mapped[str] = mapped_column(String(200), default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    edit_spec: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    preview_path: Mapped[str] = mapped_column(Text, default="")
    preview_version: Mapped[int] = mapped_column(Integer, default=0)
    export_path: Mapped[str] = mapped_column(Text, default="")
    export_version: Mapped[int] = mapped_column(Integer, default=0)
    thumb_path: Mapped[str] = mapped_column(Text, default="")
    render_pending: Mapped[bool] = mapped_column(default=False)
    qa: Mapped[dict] = mapped_column(JSON, default=dict)


def project_dir(project_id: str) -> Path:
    return get_settings().data_dir / "projects" / project_id


def _add_missing_columns(eng: Engine) -> None:
    """Migração mínima: adiciona colunas novas (todas anuláveis ou com default) a tabelas já existentes."""
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(dialect=eng.dialect)
                default = ""
                if col.default is not None and col.default.is_scalar:
                    v = col.default.arg
                    default = f" DEFAULT {int(v) if isinstance(v, bool) else repr(v)}"
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}{default}'))


@lru_cache
def engine() -> Engine:
    settings = get_settings()
    if settings.database_url:
        eng = create_engine(settings.database_url, pool_pre_ping=True)
    else:
        data = settings.data_dir
        data.mkdir(parents=True, exist_ok=True)
        eng = create_engine(f"sqlite:///{data / 'clipstudio.db'}", connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    _add_missing_columns(eng)
    Base.metadata.create_all(eng)
    return eng


@lru_cache
def _factory() -> sessionmaker[Session]:
    return sessionmaker(engine(), expire_on_commit=False)


def session() -> Session:
    return _factory()()


def reset_caches() -> None:
    """Para testes: troca de DATA_DIR."""
    if engine.cache_info().currsize:
        engine().dispose()
    engine.cache_clear()
    _factory.cache_clear()
