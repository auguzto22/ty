from app.core.models import ClipCandidate, CurationPrefs
from app.pipeline.refine import refine, snap_to_sentences
from app.pipeline.transcribe import sentences


def _boundaries(t):
    spans = sentences(t.words)
    return [t.words[a].start for a, _ in spans], [t.words[b - 1].end for _, b in spans]


def test_snap_respects_duration_and_sentence_boundaries(long_transcript):
    prefs = CurationPrefs(min_duration=10, max_duration=25)
    cand = ClipCandidate(start=13.3, end=31.7, score=50)
    out = snap_to_sentences(cand, long_transcript, prefs)
    assert out is not None
    assert prefs.min_duration - 1 <= out.duration <= prefs.max_duration + 0.5
    starts, ends = _boundaries(long_transcript)
    assert min(abs(out.start + 0.10 - s) for s in starts) < 0.11
    assert min(abs(out.end - 0.35 - e) for e in ends) < 0.36


def test_refine_removes_overlaps_and_caps_count(long_transcript):
    prefs = CurationPrefs(min_duration=8, max_duration=20, max_clips=3)
    cands = [ClipCandidate(start=s, end=s + 15, score=100 - s) for s in range(0, 120, 4)]
    out = refine(cands, long_transcript, prefs)
    assert 1 <= len(out) <= 3
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            inter = max(0, min(a.end, b.end) - max(a.start, b.start))
            assert inter <= 0.3 * min(a.duration, b.duration) + 1e-6
    assert [c.score for c in out] == sorted((c.score for c in out), reverse=True)
