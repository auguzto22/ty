from app.core.models import AspectRatio, EditSpec, Reframe, VideoInfo
from app.media.render import _piecewise
from app.pipeline.analyze import FaceSample, Signals
from app.pipeline.reframe import crop_fraction, crop_positions, plan_reframe

LAND = VideoInfo(duration=60, width=1920, height=1080, fps=30, has_audio=True)
VERT = VideoInfo(duration=60, width=1080, height=1920, fps=30, has_audio=True)


def _signals(fn, cuts=(), until=20.0) -> Signals:
    samples, t = [], 0.0
    while t < until:
        face = fn(t)
        samples.append(FaceSample(t=round(t, 2), faces=[face] if face else []))
        t += 0.4
    return Signals(scene_cuts=list(cuts), face_samples=samples, faces_enabled=True)


def test_crop_fraction_vertical_from_landscape():
    fw, fh = crop_fraction(LAND, AspectRatio.VERTICAL)
    assert abs(fw - (1080 * 9 / 16) / 1920) < 1e-6 and fh == 1.0


def test_static_face_off_center_gives_face_mode_centered_on_face():
    sig = _signals(lambda t: (0.1, 0.4, 0.06, 0.12, 0.9))
    spec = plan_reframe(sig, LAND, EditSpec(start=2, end=12))
    assert spec.reframe == Reframe.FACE and spec.track
    assert all(abs(p.cx - 0.1) < 0.01 for p in spec.track)
    cw = round(1080 * 9 / 16 / 2) * 2
    xs = [x for _, x, _ in crop_positions(spec, LAND, cw, 1080)]
    assert all(x == 0 for x in xs)  # rosto perto da borda: crop encostado à esquerda, sem sair da imagem


def test_moving_face_is_followed_smoothly_and_simplified():
    sig = _signals(lambda t: (0.2 + 0.03 * t, 0.4, 0.1, 0.18, 0.9))
    spec = plan_reframe(sig, LAND, EditSpec(start=0, end=20))
    assert spec.reframe == Reframe.FACE
    cxs = [p.cx for p in spec.track]
    assert cxs == sorted(cxs)  # nunca volta
    assert cxs[-1] > 0.6
    assert len(spec.track) < len(sig.face_samples)  # RDP simplificou
    steps = [(b.cx - a.cx) / (b.t - a.t) for a, b in zip(spec.track, spec.track[1:]) if b.t > a.t]
    assert max(steps) <= 0.35 + 1e-6  # respeita a velocidade máxima


def test_scene_cut_jumps_instead_of_panning():
    sig = _signals(lambda t: (0.2, 0.4, 0.1, 0.18, 0.9) if t < 10 else (0.8, 0.4, 0.1, 0.18, 0.9), cuts=[10.0])
    spec = plan_reframe(sig, LAND, EditSpec(start=5, end=15))
    before = [p for p in spec.track if p.t < 10.0]
    after = [p for p in spec.track if p.t >= 10.0]
    assert abs(before[-1].cx - 0.2) < 0.01 and abs(after[0].cx - 0.8) < 0.01
    assert after[0].t - before[-1].t <= 0.05


def test_no_faces_landscape_uses_fit_and_vertical_uses_center():
    sig = _signals(lambda t: None)
    assert plan_reframe(sig, LAND, EditSpec(start=0, end=10)).reframe == Reframe.FIT
    assert plan_reframe(sig, VERT, EditSpec(start=0, end=10)).reframe == Reframe.CENTER
    assert plan_reframe(None, LAND, EditSpec(start=0, end=10)).reframe == Reframe.FIT


def test_explicit_mode_is_respected():
    sig = _signals(lambda t: (0.5, 0.4, 0.1, 0.18, 0.9))
    spec = plan_reframe(sig, LAND, EditSpec(start=0, end=10, reframe=Reframe.CENTER))
    assert spec.reframe == Reframe.CENTER and spec.track == []


def test_piecewise_expression_shape():
    assert _piecewise([(0.0, 10)]) == "10"
    expr = _piecewise([(0.0, 0), (2.0, 100)])
    assert expr.startswith("lt(t,0.000)*0+") and "gte(t,2.000)*100" in expr and "," in expr
