import pytest

from app.core.models import Transcript, Word


def make_transcript(text: str, wps: float = 3.0, sentence_gap: float = 0.3) -> Transcript:
    """Transcrição sintética: palavras com duração 1/wps e pausa extra após fim de frase."""
    words, t = [], 0.0
    for token in text.split():
        dur = 1.0 / wps
        words.append(Word(text=token, start=round(t, 3), end=round(t + dur * 0.9, 3)))
        t += dur + (sentence_gap if token.endswith((".", "?", "!")) else 0.0)
    return Transcript(language="pt", duration=round(t + 0.5, 3), words=words)


@pytest.fixture
def long_transcript() -> Transcript:
    sentences = []
    for i in range(40):
        if i % 7 == 0:
            sentences.append(f"Você sabe por que o erro número {i} acontece tanto?")
        else:
            sentences.append(f"Esta é a frase {i} que explica um detalhe importante do assunto em questão.")
    return make_transcript(" ".join(sentences))
