"""Curadoria por LLM (Gemini por padrão, Claude opcional): o modelo lê a transcrição numerada por frase e escolhe os melhores trechos."""

import logging

from pydantic import BaseModel

from app.core.config import get_settings
import numpy as np

from app.core.models import ClipCandidate, ClipScores, CurationPrefs, Transcript
from app.pipeline.analyze import Signals
from app.pipeline.transcribe import sentences

log = logging.getLogger(__name__)

MAX_SENTENCES_PER_CALL = 2500  # ~2-3 h de fala; acima disso a transcrição é dividida em blocos


class CurationError(RuntimeError):
    pass


class LLMClip(BaseModel):
    start_sentence: int
    end_sentence: int
    title: str
    hook: str
    reason: str
    hook_score: int
    coherence_score: int
    value_score: int
    trend_score: int
    overall_score: int


class LLMCuration(BaseModel):
    clips: list[LLMClip]


SYSTEM = """Você é editor de vídeos curtos (Reels, Shorts, TikTok). Recebe a transcrição de um vídeo longo, \
numerada por frase com o tempo de início [mm:ss], e escolhe os trechos com maior potencial de viralizar \
como cortes independentes.

Algumas frases trazem marcas de sinais medidos no vídeo:
- ⏸N.Ns: pausa antes da frase (bom ponto de início);
- ↑energia / ↓energia: fala bem mais alta (ênfase, emoção, risada) ou mais baixa que a média;
- ✂cena: troca de cena/câmera no início da frase;
- sem rosto: ninguém aparece na tela nesse trecho (tela, slide, paisagem).
Use as marcas como evidência adicional, sem ignorar o conteúdo.

Um bom corte:
- começa com um gancho forte nos 3 primeiros segundos (pergunta, afirmação surpreendente, conflito, promessa);
- se sustenta sozinho, sem depender de contexto anterior, e termina numa ideia concluída;
- entrega valor claro (insight, história, humor, emoção, dica prática).

Regras:
- start_sentence e end_sentence são os números das frases (inclusive) e devem respeitar a duração pedida;
- cortes não devem se sobrepor;
- se o mesmo conteúdo aparece mais de uma vez no vídeo (repetição, vinheta, trecho reapresentado), escolha só uma ocorrência;
- title: título curto e chamativo no idioma da transcrição (máx. 60 caracteres);
- hook: a frase de abertura que prende a atenção;
- reason: por que o trecho funciona (uma frase);
- scores de 0 a 100; overall_score resume o potencial do corte.
Ordene do melhor para o pior."""


def _fmt(t: float) -> str:
    return f"{int(t // 60):02}:{int(t % 60):02}"


def _marks(transcript: Transcript, spans: list[tuple[int, int]], envelope: np.ndarray | None, window: float,
           signals: Signals | None) -> list[str]:
    """Marcas compactas por frase (pausa, energia relativa, corte de cena, ausência de rosto)."""
    w = transcript.words
    energies = []
    for a, b in spans:
        if envelope is not None and len(envelope):
            seg = envelope[int(w[a].start / window): max(int(w[a].start / window) + 1, int(w[b - 1].end / window))]
            energies.append(float(seg.mean()) if len(seg) else 0.0)
    hi = float(np.percentile(energies, 80)) if energies else 0.0
    lo = float(np.percentile(energies, 20)) if energies else 0.0
    out = []
    for i, (a, b) in enumerate(spans):
        m = []
        pause = w[a].start - w[spans[i - 1][1] - 1].end if i else 0.0
        if pause >= 0.8:
            m.append(f"⏸{pause:.1f}s")
        if energies and len(energies) >= 5:
            if energies[i] >= hi and hi > lo:
                m.append("↑energia")
            elif energies[i] <= lo and hi > lo:
                m.append("↓energia")
        if signals is not None:
            if any(abs(c - w[a].start) <= 0.5 for c in signals.scene_cuts):
                m.append("✂cena")
            if signals.faces_enabled and signals.face_coverage(w[a].start, w[b - 1].end) == 0.0 and w[b - 1].end - w[a].start >= 2:
                m.append("sem rosto")
        out.append(" ".join(m))
    return out


def _render_transcript(transcript: Transcript, spans: list[tuple[int, int]], first: int, last: int,
                       marks: list[str] | None = None) -> str:
    w = transcript.words
    lines = []
    for idx in range(first, last):
        a, b = spans[idx]
        dur = w[b - 1].end - w[a].start
        mark = f" {{{marks[idx]}}}" if marks and marks[idx] else ""
        lines.append(f"{idx} [{_fmt(w[a].start)}] ({dur:.0f}s){mark} " + " ".join(x.text for x in w[a:b]))
    return "\n".join(lines)


def _user_prompt(transcript_text: str, prefs: CurationPrefs, wanted: int) -> str:
    parts = [
        f"Escolha até {wanted} cortes com duração entre {prefs.min_duration:.0f}s e {prefs.max_duration:.0f}s.",
    ]
    if prefs.prompt:
        parts.append(f"Instrução do usuário: {prefs.prompt}")
    if prefs.keywords:
        parts.append("Priorize trechos sobre: " + ", ".join(prefs.keywords))
    parts.append("<transcricao>\n" + transcript_text + "\n</transcricao>")
    return "\n\n".join(parts)


def _call_claude(prompt: str) -> LLMCuration:
    import anthropic  # opcional: só necessário com LLM_PROVIDER=claude

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    try:
        response = client.messages.parse(
            model=settings.claude_model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": settings.claude_effort},
            output_format=LLMCuration,
        )
    except anthropic.AuthenticationError as e:
        raise CurationError("ANTHROPIC_API_KEY inválida") from e
    except anthropic.BadRequestError as e:
        raise CurationError(f"Requisição rejeitada: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise CurationError("Limite de taxa da Claude API atingido") from e
    except anthropic.APIStatusError as e:
        raise CurationError(f"Erro da Claude API ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise CurationError("Sem conexão com a Claude API") from e

    if response.stop_reason == "refusal":
        raise CurationError("A Claude API recusou a solicitação")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise CurationError(f"Resposta incompleta da Claude API (stop_reason={response.stop_reason})")
    log.info("Claude: %s tokens in / %s out (req %s)", response.usage.input_tokens, response.usage.output_tokens, response._request_id)
    return response.parsed_output


def _call_gemini(prompt: str) -> LLMCuration:
    from app.pipeline import llm_gemini

    try:
        return llm_gemini.generate_json(prompt, SYSTEM, LLMCuration)
    except llm_gemini.GeminiError as e:
        raise CurationError(str(e)) from e


def _call(prompt: str) -> LLMCuration:
    provider = get_settings().llm_provider
    if provider == "gemini":
        return _call_gemini(prompt)
    if provider == "claude":
        return _call_claude(prompt)
    raise CurationError(f"LLM_PROVIDER desconhecido ou desativado: {provider!r}")


def curate_llm(
    transcript: Transcript,
    prefs: CurationPrefs,
    envelope: np.ndarray | None = None,
    envelope_window: float = 0.5,
    signals: Signals | None = None,
) -> list[ClipCandidate]:
    settings = get_settings()
    if not settings.llm_enabled:
        raise CurationError(f"LLM desativado ou sem chave (LLM_PROVIDER={settings.llm_provider})")
    spans = sentences(transcript.words)
    if not spans:
        return []

    w = transcript.words
    marks = _marks(transcript, spans, envelope, envelope_window, signals)
    candidates: list[ClipCandidate] = []
    chunks = range(0, len(spans), MAX_SENTENCES_PER_CALL)
    for first in chunks:
        last = min(len(spans), first + MAX_SENTENCES_PER_CALL)
        wanted = max(3, prefs.max_clips * 2 // len(chunks))
        text = _render_transcript(transcript, spans, first, last, marks)
        result = _call(_user_prompt(text, prefs, wanted))
        for c in result.clips:
            s, e = sorted((c.start_sentence, c.end_sentence))
            if not (first <= s < last and first <= e < last):
                log.warning("Corte com frases fora do intervalo ignorado: %s-%s", s, e)
                continue

            def pct(v: int) -> float:
                return float(max(0, min(100, v)))

            candidates.append(
                ClipCandidate(
                    start=w[spans[s][0]].start,
                    end=w[spans[e][1] - 1].end,
                    title=c.title.strip()[:80],
                    hook=c.hook.strip(),
                    reason=c.reason.strip(),
                    score=pct(c.overall_score),
                    scores=ClipScores(hook=pct(c.hook_score), coherence=pct(c.coherence_score), value=pct(c.value_score), trend=pct(c.trend_score)),
                    source="llm",
                )
            )
    return candidates
