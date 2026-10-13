from app.pipeline.evaluate import _inside_word, _precision, _recall
from tests.conftest import make_transcript


def test_recall_and_precision():
    moments = [(10, 20), (50, 60)]
    clips = [(9, 21), (30, 40), (52, 70)]
    assert _recall(moments, clips, 1) == 0.5
    assert _recall(moments, clips, 3) == 1.0
    assert abs(_precision(moments, clips, 3) - 2 / 3) < 1e-9
    assert _recall([], clips, 3) is None


def test_inside_word():
    t = make_transcript("uma frase curta.")
    w = t.words[1]
    assert _inside_word((w.start + w.end) / 2, t)
    assert not _inside_word(w.start, t)
