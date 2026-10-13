"""REFINE: encaixa candidatos em fronteiras de frase, aplica limites de duração e remove sobreposições."""

from app.core.models import ClipCandidate, CurationPrefs, Transcript
from app.pipeline.transcribe import sentences

PAD_START = 0.10
PAD_END = 0.35
MAX_OVERLAP = 0.30  # fração do corte mais curto
MAX_TEXT_SIMILARITY = 0.6  # Jaccard das palavras: acima disso o corte repete outro já escolhido


def _nearest(values: list[float], target: float) -> int:
    return min(range(len(values)), key=lambda i: abs(values[i] - target))


def snap_to_sentences(
    cand: ClipCandidate, transcript: Transcript, prefs: CurationPrefs, spans: list[tuple[int, int]] | None = None
) -> ClipCandidate | None:
    words = transcript.words
    spans = spans if spans is not None else sentences(words)
    if not spans:
        return None
    starts = [words[a].start for a, _ in spans]
    ends = [words[b - 1].end for _, b in spans]

    s = _nearest(starts, cand.start)
    e = max(s, _nearest(ends, cand.end))

    def dur(s_: int, e_: int) -> float:
        return ends[e_] - starts[s_]

    # Encurta removendo frases do fim (preserva o gancho do início).
    while dur(s, e) > prefs.max_duration and e > s:
        e -= 1
    # Alonga anexando frases seguintes, depois anteriores.
    while dur(s, e) < prefs.min_duration:
        if e + 1 < len(spans) and dur(s, e + 1) <= prefs.max_duration:
            e += 1
        elif s > 0 and dur(s - 1, e) <= prefs.max_duration:
            s -= 1
        else:
            break

    start_word = spans[s][0]
    end_word = spans[e][1]  # exclusivo
    # Frase única mais longa que o máximo: corta na última palavra que cabe.
    if words[end_word - 1].end - words[start_word].start > prefs.max_duration:
        limit = words[start_word].start + prefs.max_duration
        end_word = max(i + 1 for i in range(start_word, end_word) if words[i].end <= limit or i == start_word)

    prev_end = words[start_word - 1].end if start_word > 0 else 0.0
    next_start = words[end_word].start if end_word < len(words) else transcript.duration
    start = max(prev_end, words[start_word].start - PAD_START, 0.0)
    end = min(next_start, words[end_word - 1].end + PAD_END, transcript.duration)
    return cand.model_copy(update={"start": round(start, 3), "end": round(end, 3)})


def _overlap(a: ClipCandidate, b: ClipCandidate) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(1e-6, min(a.duration, b.duration))


def _words(transcript: Transcript, c: ClipCandidate) -> set[str]:
    return {w.text.lower().strip(",.!?…:;\"'") for w in transcript.words if w.start >= c.start and w.end <= c.end}


def _similar(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) > MAX_TEXT_SIMILARITY


def refine(candidates: list[ClipCandidate], transcript: Transcript, prefs: CurationPrefs) -> list[ClipCandidate]:
    spans = sentences(transcript.words)
    snapped = [c for c in (snap_to_sentences(c, transcript, prefs, spans) for c in candidates) if c and c.duration > 1.0]
    chosen: list[ClipCandidate] = []
    chosen_words: list[set[str]] = []
    for cand in sorted(snapped, key=lambda c: c.score, reverse=True):
        if any(_overlap(cand, other) > MAX_OVERLAP for other in chosen):
            continue
        words = _words(transcript, cand)
        if any(_similar(words, other) for other in chosen_words):
            continue  # mesmo conteúdo dito de novo (repetição, vinheta, intro recorrente)
        chosen.append(cand)
        chosen_words.append(words)
        if len(chosen) >= prefs.max_clips:
            break
    return chosen
