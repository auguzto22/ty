from collections.abc import Callable
from pathlib import Path

from app.core.config import get_settings
from app.core.models import Transcript, Word
from app.media.audio import SAMPLE_RATE, load_wav

SENTENCE_END = (".", "?", "!", "…")


def transcribe(
    audio_path: Path,
    duration: float,
    *,
    language: str = "",
    on_progress: Callable[[float], None] | None = None,
) -> Transcript:
    from faster_whisper import WhisperModel  # import tardio: carregamento é pesado

    settings = get_settings()
    model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
    # Passa o áudio já decodificado: evita o decoder PyAV do faster-whisper (incompatível com av>=17).
    samples, rate = load_wav(audio_path)
    if rate != SAMPLE_RATE:
        raise ValueError(f"WAV deve ter {SAMPLE_RATE} Hz (tem {rate})")
    segments, info = model.transcribe(
        samples,
        language=(language or settings.whisper_language or None),
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
    )

    words: list[Word] = []
    for seg in segments:  # gerador: a transcrição acontece durante a iteração
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(text=text, start=round(w.start, 3), end=round(w.end, 3), prob=round(w.probability, 3)))
        if on_progress and duration:
            on_progress(min(1.0, seg.end / duration))

    return Transcript(language=info.language, duration=duration, words=words)


def sentences(words: list[Word], max_gap: float = 1.0) -> list[tuple[int, int]]:
    """Agrupa palavras em frases [i, j) por pontuação final ou pausa longa."""
    spans: list[tuple[int, int]] = []
    start = 0
    for i, w in enumerate(words):
        is_last = i == len(words) - 1
        gap = (words[i + 1].start - w.end) if not is_last else 0.0
        if is_last or w.text.endswith(SENTENCE_END) or gap > max_gap:
            spans.append((start, i + 1))
            start = i + 1
    return spans


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def to_srt(transcript: Transcript, max_chars: int = 42) -> str:
    cues: list[tuple[float, float, str]] = []
    for a, b in sentences(transcript.words):
        line: list[Word] = []
        for w in transcript.words[a:b]:
            if line and len(" ".join(x.text for x in line + [w])) > max_chars:
                cues.append((line[0].start, line[-1].end, " ".join(x.text for x in line)))
                line = []
            line.append(w)
        if line:
            cues.append((line[0].start, line[-1].end, " ".join(x.text for x in line)))
    return "\n".join(
        f"{i}\n{_srt_time(s)} --> {_srt_time(e)}\n{text}\n" for i, (s, e, text) in enumerate(cues, 1)
    )


def to_txt(transcript: Transcript) -> str:
    return "\n".join(" ".join(w.text for w in transcript.words[a:b]) for a, b in sentences(transcript.words))
