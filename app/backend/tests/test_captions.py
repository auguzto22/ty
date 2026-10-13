from pathlib import Path

from app.core.models import CaptionStyle
from app.media.captions import build_caption_track, clip_words, paginate
from tests.conftest import make_transcript


def test_paginate_limits_words_and_breaks_on_sentence():
    t = make_transcript("um dois três quatro cinco. seis sete")
    pages = paginate(clip_words(t, 0, t.duration), CaptionStyle(max_words=3, max_chars=40))
    assert [[w.text for w in p.words] for p in pages] == [["um", "dois", "três"], ["quatro", "cinco."], ["seis", "sete"]]


def test_word_overrides_edit_and_hide():
    t = make_transcript("olá mundo cruel")
    words = clip_words(t, 0, t.duration, {1: "planeta", 2: ""})
    assert [w.text for w in words] == ["olá", "planeta"]


def test_caption_track_covers_full_duration(tmp_path: Path):
    t = make_transcript("legenda de teste com várias palavras para cobrir o tempo.")
    duration = t.duration
    concat, band_h = build_caption_track(clip_words(t, 0, duration), duration, CaptionStyle(), 540, tmp_path)
    lines = concat.read_text().splitlines()
    total = sum(float(l.split()[1]) for l in lines if l.startswith("duration"))
    assert abs(total - duration) < 0.05
    assert band_h > 0 and len(list(tmp_path.glob("cap_*.png"))) == len(t.words)
