import logging
from pathlib import Path

import typer

from app.core.models import AspectRatio, CaptionStyle, CurationPrefs, EditSpec, Quality, Reframe
from app.media.ffmpeg import FFmpegError
from app.media.probe import probe
from app.pipeline.analyze import load_signals
from app.pipeline.curate_llm import CurationError
from app.pipeline.process import ProcessResult, Stage, load_transcript, process_video, render_and_check, save_result
from app.pipeline.reframe import plan_reframe

app = typer.Typer(help="ClipStudio — cortes automáticos com legendas", no_args_is_help=True)


def _progress(stage: Stage, progress: float, msg: str) -> None:
    typer.echo(f"\r[{stage.value:<10}] {progress * 100:5.1f}%  {msg:<50}", nl=progress >= 1.0)


@app.command()
def process(
    video: Path = typer.Argument(..., exists=True, dir_okay=False, help="Vídeo de origem"),
    out: Path = typer.Option(Path("out"), help="Pasta de saída"),
    min_duration: float = typer.Option(20.0, "--min", help="Duração mínima do corte (s)"),
    max_duration: float = typer.Option(60.0, "--max", help="Duração máxima do corte (s)"),
    max_clips: int = typer.Option(3, help="Quantidade máxima de cortes"),
    prompt: str = typer.Option("", help="Instrução livre para a curadoria por IA"),
    keyword: list[str] = typer.Option([], "--keyword", "-k", help="Tema a priorizar (repetível)"),
    language: str = typer.Option("", help="Idioma da fala (ex.: pt); vazio = automático"),
    aspect: AspectRatio = typer.Option(AspectRatio.VERTICAL, help="Proporção de saída"),
    reframe: Reframe = typer.Option(Reframe.AUTO, help="auto | face | fit | center"),
    captions: bool = typer.Option(True, help="Queimar legendas"),
    faces: bool = typer.Option(True, help="Detectar rostos (YuNet) para o enquadramento"),
    quality: str = typer.Option("export", help="export (1080x1920) | preview (540x960) | none (só seleciona)"),
    reuse: bool = typer.Option(True, help="Reaproveitar áudio, transcrição e sinais existentes em --out"),
) -> None:
    """Executa o pipeline completo e grava transcript.*, signals.json, clips.json e os MP4 em OUT/clips."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if quality not in ("export", "preview", "none"):
        raise typer.BadParameter("use export, preview ou none", param_hint="--quality")
    prefs = CurationPrefs(
        min_duration=min_duration, max_duration=max_duration, max_clips=max_clips,
        prompt=prompt, keywords=keyword, language=language,
    )
    spec = EditSpec(start=0, end=0, aspect=aspect, reframe=reframe, captions=CaptionStyle(enabled=captions))
    try:
        result = process_video(
            video, out, prefs, spec, detect_faces=faces, reuse=reuse, on_stage=_progress,
            render_quality=None if quality == "none" else Quality(quality),
        )
    except (ValueError, FFmpegError, CurationError) as e:
        typer.secho(f"\nerro: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)

    for w in result.warnings:
        typer.secho(f"aviso: {w}", fg=typer.colors.YELLOW)
    typer.echo(f"\nCuradoria: {result.curation} · idioma: {result.language}")
    for c in result.clips:
        cand = c.candidate
        qa = "" if c.qa is None else ("QA ok" if c.qa.ok else "QA: " + "; ".join(c.qa.problems))
        typer.echo(f"#{c.rank:<2} {cand.score:5.1f}  {cand.start:7.1f}s → {cand.end:7.1f}s ({cand.duration:4.1f}s)  "
                   f"[{c.spec.reframe.value}]  {cand.title}")
        for f in (c.export, c.preview):
            if f:
                typer.echo(f"     {out / f}  {qa}")


@app.command("render")
def render_cmd(
    out: Path = typer.Option(Path("out"), help="Pasta gerada por `process`"),
    rank: int = typer.Option(1, help="Corte (rank) a renderizar"),
    quality: Quality = typer.Option(Quality.EXPORT, help="preview (960p) ou export (1920p)"),
    start: float | None = typer.Option(None, help="Novo início (s) — trim"),
    end: float | None = typer.Option(None, help="Novo fim (s) — trim"),
    aspect: AspectRatio | None = typer.Option(None, help="Nova proporção"),
    reframe: Reframe | None = typer.Option(None, help="Novo enquadramento: auto | face | fit | center"),
) -> None:
    """Renderiza (ou re-renderiza com ajustes) um corte de clips.json."""
    result = ProcessResult.model_validate_json((out / "clips.json").read_text())
    clip = next((c for c in result.clips if c.rank == rank), None)
    if clip is None:
        raise typer.BadParameter(f"rank {rank} não existe em clips.json")
    updates = {k: v for k, v in {"start": start, "end": end, "aspect": aspect, "reframe": reframe}.items() if v is not None}
    clip.spec = clip.spec.model_copy(update=updates)
    source = Path(result.source)
    info = probe(source)
    if updates.keys() & {"start", "end", "aspect", "reframe"}:
        clip.spec = plan_reframe(load_signals(out / "signals.json"), info,
                                 clip.spec if reframe else clip.spec.model_copy(update={"reframe": Reframe.AUTO}))

    render_and_check(source, info, load_transcript(out), clip, out, quality,
                     lambda p: _progress(Stage.RENDER, p, f"corte {rank}"))
    save_result(result, out)
    typer.echo(str(out / (clip.export if quality is Quality.EXPORT else clip.preview)))
    if clip.qa and not clip.qa.ok:
        typer.secho("QA: " + "; ".join(clip.qa.problems), fg=typer.colors.YELLOW)


@app.command("eval")
def eval_cmd(
    dataset: Path = typer.Argument(..., exists=True, dir_okay=False, help="JSON com vídeos e momentos marcados"),
    work: Path = typer.Option(Path("out/eval"), help="Pasta de trabalho (reaproveita transcrições entre execuções)"),
    faces: bool = typer.Option(True, help="Detectar rostos"),
) -> None:
    """Mede a seleção de cortes pela IA (Recall@K, fronteiras, enquadramento, tempos) sem renderizar. Consome cota do LLM."""
    from app.pipeline.evaluate import run_eval

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    work.mkdir(parents=True, exist_ok=True)
    rows = run_eval(dataset, work, detect_faces=faces, on_video=lambda v: typer.echo(f"… {v.path}"))

    def fmt(v: float | None) -> str:
        return "  –  " if v is None else f"{v:5.2f}"

    typer.echo(f"\n{'vídeo':<34} {'R@3':>5} {'R@5':>5} {'P@3':>5} {'meio-palavra':>12} {'pontuação':>9}  enquadramento")
    for m in rows:
        typer.echo(f"{Path(m.path).name[:34]:<34} {fmt(m.recall_at_3)} {fmt(m.recall_at_5)} {fmt(m.precision_at_3)} "
                   f"{m.cuts_inside_word:12.2f} {m.ends_with_punctuation:9.2f}  {m.reframe_modes}"
                   + (f" jitter={m.face_jitter}" if m.face_jitter is not None else ""))
    typer.echo(f"\nRelatório: {work / 'eval_report.json'}")


@app.command("user-add")
def user_add(
    email: str = typer.Argument(..., help="E-mail da conta"),
    admin: bool = typer.Option(False, help="Administrador (vê todos os projetos, sem cota)"),
) -> None:
    """Cria uma conta de usuário para o app (a senha é pedida no terminal)."""
    from app.api.auth import create_user

    password = typer.prompt("Senha", hide_input=True, confirmation_prompt=True)
    try:
        user = create_user(email, password, is_admin=admin)
    except ValueError as e:
        typer.secho(f"erro: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"conta criada: {user.email}{' (admin)' if user.is_admin else ''}")


@app.command("cleanup")
def cleanup_cmd(dry_run: bool = typer.Option(False, "--dry-run", help="Só mostra o que seria apagado")) -> None:
    """Aplica a política de retenção (vídeos originais antigos, uploads abandonados, projetos expirados)."""
    from app.api.cleanup import run_cleanup

    stats = run_cleanup(dry_run=dry_run)
    typer.echo(f"{'[simulação] ' if dry_run else ''}originais removidos: {stats['sources_removed']} · "
               f"projetos removidos: {stats['projects_removed']} · {stats['bytes_freed'] / 1e6:.1f} MB liberados")


@app.command("gemini-check")
def gemini_check(
    ping: bool = typer.Option(False, help="Também envia um prompt mínimo ao modelo (consome cota; só no plano gratuito ou com sua autorização)"),
    model: str = typer.Option("", help="Modelo para o ping (vazio = GEMINI_MODEL)"),
) -> None:
    """Lista os modelos Gemini que GEMINI_API_KEY acessa (sem consumir tokens) e, com --ping, testa a geração."""
    from app.core.config import get_settings
    from app.pipeline import llm_gemini

    settings = get_settings()
    try:
        models = llm_gemini.list_generate_models()
    except llm_gemini.GeminiError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"{len(models)} modelos com generateContent:")
    for m in sorted(models, key=lambda m: m.name):
        mark = "*" if m.name in (settings.gemini_model, settings.gemini_fallback_model) else " "
        typer.echo(f" {mark} {m.name:<40} in={m.input_token_limit or '?':>8} out={m.output_token_limit or '?':>7}  {m.display_name}")
    names = {m.name for m in models}
    for label, name in (("GEMINI_MODEL", settings.gemini_model), ("GEMINI_FALLBACK_MODEL", settings.gemini_fallback_model)):
        if name and name not in names:
            typer.secho(f"aviso: {label}={name} não está disponível para esta chave", fg=typer.colors.YELLOW)
    if not ping:
        return
    try:
        response = llm_gemini.generate("Responda apenas: OK", model=model or None)  # vazio = principal + fallback
    except llm_gemini.GeminiError as e:
        typer.secho(f"ping falhou: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    u = response.usage_metadata
    typer.echo(f"ping {response.model_version}: {response.text!r} "
               f"(tokens in={u.prompt_token_count if u else '?'} out={u.candidates_token_count if u else '?'} "
               f"thinking={u.thoughts_token_count if u else '?'})")


if __name__ == "__main__":
    app()
