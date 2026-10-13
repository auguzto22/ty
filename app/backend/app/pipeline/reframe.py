"""REFRAME: decide o enquadramento de cada corte e calcula a trajetória do crop.

- FACE: há rosto na maior parte do corte → o crop segue o rosto principal como uma "câmera virtual"
  (zona morta + velocidade máxima, para não tremer; salta direto nos cortes de cena).
- FIT: pouco ou nenhum rosto e fonte bem mais larga que o alvo → quadro inteiro sobre fundo desfocado.
- CENTER: pouco rosto e proporções parecidas → crop central.
"""

from app.core.models import AspectRatio, EditSpec, Reframe, TrackPoint, VideoInfo
from app.media.faces import Face
from app.pipeline.analyze import Signals

MIN_COVERAGE = 0.4  # fração das amostras com rosto para usar FACE
MIN_SPARSE_COVERAGE = 0.15  # cobertura menor ainda vale se o rosto fica sempre no mesmo lugar (por cena)
MAX_SPARSE_SPREAD = 0.5  # "mesmo lugar": dispersão horizontal ≤ metade da largura do crop
FIT_IF_WIDER = 1.4  # FIT só quando a fonte é ≥40% mais larga (relativamente) que o alvo
DEAD_ZONE = 0.12  # fração da largura do crop em que a câmera não se move
SMOOTH = 0.35  # quanto da distância restante a câmera percorre por amostra
MAX_SPEED = 0.35  # deslocamento máximo, em larguras da fonte por segundo
CONTINUITY = 0.12  # distância (normalizada) para considerar o mesmo rosto da amostra anterior
SIMPLIFY_EPS = 0.004  # tolerância do Ramer–Douglas–Peucker (fração da fonte)
HEADROOM = 0.12  # rosto um pouco acima do centro quando o crop também corta na vertical


def crop_fraction(info: VideoInfo, aspect: AspectRatio) -> tuple[float, float]:
    """Tamanho do crop na proporção alvo como fração da fonte (largura, altura)."""
    src_w, src_h = info.display_size
    rw, rh = aspect.ratio
    if src_w / src_h > rw / rh:
        return (src_h * rw / rh) / src_w, 1.0
    return 1.0, (src_w * rh / rw) / src_h


def _pick(faces: list[Face], prev: tuple[float, float] | None) -> tuple[float, float] | None:
    if not faces:
        return None

    def weight(f: Face) -> float:
        w = f.area * f.score
        if prev and abs(f.cx - prev[0]) < CONTINUITY and abs(f.cy - prev[1]) < CONTINUITY:
            w *= 2.0  # prefere continuar no mesmo rosto
        return w

    best = max(faces, key=weight)
    return best.cx, best.cy


def _rdp(points: list[TrackPoint], eps: float) -> list[TrackPoint]:
    if len(points) < 3:
        return points
    a, b = points[0], points[-1]
    span = b.t - a.t or 1e-9

    def err(p: TrackPoint) -> float:
        k = (p.t - a.t) / span
        return max(abs(p.cx - (a.cx + k * (b.cx - a.cx))), abs(p.cy - (a.cy + k * (b.cy - a.cy))))

    idx, worst = max(((i, err(p)) for i, p in enumerate(points[1:-1], 1)), key=lambda x: x[1])
    if worst <= eps:
        return [a, b]
    return _rdp(points[: idx + 1], eps)[:-1] + _rdp(points[idx:], eps)


def _consistent(targets: list[tuple[float, tuple[float, float] | None]], cuts: list[float], fw: float) -> bool:
    """Rostos detectados de forma esparsa, mas sempre na mesma região dentro de cada cena."""
    by_scene: dict[int, list[float]] = {}
    for t, tg in targets:
        if tg:
            by_scene.setdefault(sum(1 for c in cuts if c <= t), []).append(tg[0])
    return bool(by_scene) and all(max(xs) - min(xs) <= MAX_SPARSE_SPREAD * fw for xs in by_scene.values())


def plan_reframe(signals: Signals | None, info: VideoInfo, spec: EditSpec) -> EditSpec:
    """Resolve `reframe=AUTO` (ou recalcula FACE) e devolve o EditSpec com modo e trajetória."""
    if spec.reframe in (Reframe.FIT, Reframe.CENTER):
        return spec.model_copy(update={"track": []})
    fw, fh = crop_fraction(info, spec.aspect)
    src_w, src_h = info.display_size
    rw, rh = spec.aspect.ratio
    much_wider = (src_w / src_h) / (rw / rh) >= FIT_IF_WIDER

    def fallback() -> EditSpec:
        mode = Reframe.FIT if much_wider else Reframe.CENTER
        return spec.model_copy(update={"reframe": mode, "track": []})

    if signals is None or not signals.faces_enabled or (fw >= 0.999 and fh >= 0.999):
        return fallback() if (fw < 0.999 or fh < 0.999) else spec.model_copy(update={"reframe": Reframe.CENTER, "track": []})

    samples = [s for s in signals.face_samples if spec.start - 0.5 <= s.t <= spec.end + 0.5]
    targets: list[tuple[float, tuple[float, float] | None]] = []
    prev = None
    for s in samples:
        tgt = _pick(s.as_faces(), prev)
        targets.append((s.t, tgt))
        prev = tgt or prev
    inside = [tg for t, tg in targets if spec.start <= t < spec.end]
    coverage = sum(1 for tg in inside if tg) / len(inside) if inside else 0.0
    cuts = signals.scene_cuts_between(spec.start - 0.5, spec.end + 0.5)
    if coverage < MIN_COVERAGE and not (coverage >= MIN_SPARSE_COVERAGE and _consistent(targets, cuts, fw)):
        return fallback()

    # Preenche lacunas: segura a última posição conhecida; antes da primeira, usa a primeira.
    first = next(tg for _, tg in targets if tg)
    filled, last = [], first
    for t, tg in targets:
        last = tg or last
        filled.append((t, last))

    # Câmera virtual por cena.
    dead_x, dead_y = DEAD_ZONE * fw, DEAD_ZONE * fh
    dt = 1.0 / (signals.sample_fps / 2)  # amostras de rosto a cada FACE_EVERY quadros
    max_step = MAX_SPEED * dt
    points: list[TrackPoint] = []
    pos: tuple[float, float] | None = None
    scene = -1
    for t, (tx, ty) in filled:
        ty = ty + HEADROOM * fh if fh < 0.999 else ty
        s_idx = sum(1 for c in cuts if c <= t)
        if pos is None or s_idx != scene:
            if pos is not None:  # corte de cena: mantém até o corte e salta
                cut_t = cuts[s_idx - 1]
                points.append(TrackPoint(t=round(cut_t - 0.04, 3), cx=pos[0], cy=pos[1]))
                points.append(TrackPoint(t=round(cut_t, 3), cx=tx, cy=ty))
            pos, scene = (tx, ty), s_idx
        else:
            px, py = pos
            if abs(tx - px) > dead_x:
                px += max(-max_step, min(max_step, (tx - px) * SMOOTH))
            if abs(ty - py) > dead_y:
                py += max(-max_step, min(max_step, (ty - py) * SMOOTH))
            pos = (px, py)
        points.append(TrackPoint(t=round(t, 3), cx=round(pos[0], 4), cy=round(pos[1], 4)))

    points.sort(key=lambda p: p.t)
    # RDP por cena, para preservar os saltos exatos.
    simplified: list[TrackPoint] = []
    seg: list[TrackPoint] = []
    for p in points:
        if seg and p.t - seg[-1].t < 0.05 and (abs(p.cx - seg[-1].cx) > 1e-6 or abs(p.cy - seg[-1].cy) > 1e-6):
            simplified += _rdp(seg, SIMPLIFY_EPS)
            seg = []
        seg.append(p)
    simplified += _rdp(seg, SIMPLIFY_EPS)
    return spec.model_copy(update={"reframe": Reframe.FACE, "track": simplified})


def crop_positions(spec: EditSpec, info: VideoInfo, cw: int, ch: int) -> list[tuple[float, int, int]]:
    """Trajetória em pixels (t relativo ao início do corte, x, y do canto superior esquerdo), limitada à imagem."""
    src_w, src_h = info.display_size
    out: list[tuple[float, int, int]] = []
    for p in spec.track:
        x = int(round(min(max(p.cx * src_w - cw / 2, 0), src_w - cw)))
        y = int(round(min(max(p.cy * src_h - ch / 2, 0), src_h - ch)))
        out.append((round(p.t - spec.start, 3), x, y))
    return out
