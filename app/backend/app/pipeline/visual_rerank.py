"""Passo 2 da curadoria por IA: o Gemini vê uma folha de contato (3×3 quadros) de cada candidato e avalia
o interesse visual. Uma única requisição para todos os candidatos; falhas não bloqueiam o pipeline."""

import logging
from pathlib import Path

from pydantic import BaseModel

from app.core.models import ClipCandidate, Transcript
from app.media.contact_sheet import contact_sheet

log = logging.getLogger(__name__)

VISUAL_WEIGHT = 0.25  # score final = 75% texto + 25% visual
MAX_CANDIDATES = 12

SYSTEM = """Você avalia o potencial VISUAL de cortes curtos verticais (9:16) para Reels/Shorts/TikTok.
Para cada candidato você recebe uma grade 3×3 de quadros em ordem cronológica (esquerda→direita, cima→baixo)
e o texto falado no trecho. Avalie, de 0 a 100:
- presença e expressividade de pessoas (rosto visível, gestos, emoção);
- ação ou demonstração na tela; variedade visual sem ser caótica;
- problemas: tela preta, imagem estática por muito tempo, rosto cortado ou ausente, texto ilegível, quadro tremido.
visual_score alto = prende o olho num celular. keep=false só para cortes visualmente inaceitáveis.
Em issues, descreva problemas em poucas palavras ou deixe vazio. Responda para todos os ids recebidos."""


class VisualItem(BaseModel):
    id: int
    visual_score: int
    issues: str  # vazio se não houver problema
    keep: bool


class VisualReview(BaseModel):
    items: list[VisualItem]


def rerank_visual(
    source: Path, transcript: Transcript, candidates: list[ClipCandidate], work_dir: Path,
) -> list[ClipCandidate]:
    """Devolve os candidatos reordenados com o score visual incorporado. Em erro, devolve a lista intacta."""
    from google.genai import types

    from app.pipeline import llm_gemini

    cands = candidates[:MAX_CANDIDATES]
    if not cands:
        return candidates
    parts: list = []
    for i, c in enumerate(cands):
        sheet = contact_sheet(source, c.start, c.end, work_dir / "sheets" / f"cand_{i:02}.jpg")
        text = transcript.text_between(c.start, c.end)[:600]
        parts.append(f"Candidato id={i} ({c.duration:.0f}s): {text}")
        parts.append(types.Part.from_bytes(data=sheet.read_bytes(), mime_type="image/jpeg"))
    try:
        review = llm_gemini.generate_json(parts, SYSTEM, VisualReview)
    except llm_gemini.GeminiError as e:
        log.warning("Avaliação visual indisponível: %s", e)
        return candidates

    by_id = {it.id: it for it in review.items if 0 <= it.id < len(cands)}
    out: list[ClipCandidate] = []
    for i, c in enumerate(cands):
        it = by_id.get(i)
        if it is None:
            out.append(c)
            continue
        v = float(max(0, min(100, it.visual_score)))
        score = round((1 - VISUAL_WEIGHT) * c.score + VISUAL_WEIGHT * v, 1)
        if not it.keep:
            score = round(score * 0.5, 1)
        issues = it.issues.strip()
        if issues.lower().strip(".") in {"", "none", "nenhum", "nenhuma", "n/a", "-"}:
            issues = ""
        reason = (c.reason + f" · visual IA {v:.0f}" + (f" ({issues})" if issues else "")).strip(" ·")
        out.append(c.model_copy(update={"score": score, "reason": reason[:300],
                                        "scores": c.scores.model_copy(update={"visual": v})}))
    out.sort(key=lambda c: c.score, reverse=True)
    return out + candidates[MAX_CANDIDATES:]
