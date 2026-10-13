"""Cliente de referência da API (o mesmo protocolo que o app Android vai usar).

Uso: python scripts/api_smoke.py <video.mp4> [--url http://127.0.0.1:8000] [--token T] [--chunk-mb 8] [--out pasta]
Envia em partes (upload retomável), inicia o processamento, acompanha o progresso, pede o export dos cortes
e baixa os MP4 (com Range, retomando se houver arquivo parcial)."""

import argparse
import sys
import time
from pathlib import Path

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--token", default="")
    ap.add_argument("--chunk-mb", type=float, default=8)
    ap.add_argument("--min", type=float, default=10)
    ap.add_argument("--max", type=float, default=25)
    ap.add_argument("--clips", type=int, default=3)
    ap.add_argument("--language", default="pt")
    ap.add_argument("--out", type=Path, default=Path("downloads"))
    a = ap.parse_args()

    headers = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    c = httpx.Client(base_url=a.url, headers=headers, timeout=120)
    size = a.video.stat().st_size
    r = c.post("/projects", json={"filename": a.video.name, "size": size, "content_type": "video/mp4",
                                  "prefs": {"min_duration": a.min, "max_duration": a.max, "max_clips": a.clips,
                                            "language": a.language}})
    r.raise_for_status()
    pid, up = r.json()["project"]["id"], r.json()["upload"]
    print(f"projeto {pid}; enviando {size / 1e6:.1f} MB")

    chunk = int(a.chunk_mb * 1024 * 1024)
    offset = 0
    with a.video.open("rb") as f:
        while True:
            f.seek(offset)
            data = f.read(chunk)
            end = offset + len(data) - 1
            r = c.put(up["url"], content=data, headers={"Content-Range": f"bytes {offset}-{end}/{size}"})
            if r.status_code == 201:
                break
            if r.status_code != 308:
                print("upload falhou:", r.status_code, r.text)
                return 1
            offset = int(r.headers["Range"].split("-")[1]) + 1 if "Range" in r.headers else 0
            print(f"  {offset / size:6.1%}")

    c.post(f"/projects/{pid}/start").raise_for_status()
    last = ""
    while True:
        p = c.get(f"/projects/{pid}").json()
        line = f"  {p['status']:<10} {p['stage']:<10} {p['progress'] * 100:5.1f}%  {p['message']}"
        if line != last:
            print(line)
            last = line
        if p["status"] in ("COMPLETE", "FAILED"):
            break
        time.sleep(2)
    if p["status"] == "FAILED":
        print("erro:", p["error"])
        return 1

    clips = c.get(f"/projects/{pid}/clips").json()
    for clip in clips:
        c.post(f"/clips/{clip['id']}/render", params={"quality": "export"}).raise_for_status()
    a.out.mkdir(parents=True, exist_ok=True)
    pending = {clip["id"] for clip in clips}
    while pending:
        for cid in sorted(pending):
            clip = c.get(f"/clips/{cid}").json()
            if clip["export"] and not clip["export"]["stale"]:
                target = a.out / f"{pid}_clip_{clip['rank']:02}.mp4"
                part = target.with_suffix(".partial")
                have = part.stat().st_size if part.exists() else 0
                with c.stream("GET", clip["export"]["url"], headers={"Range": f"bytes={have}-"}) as r:
                    with part.open("ab") as f:
                        for b in r.iter_bytes():
                            f.write(b)
                part.rename(target)
                print(f"#{clip['rank']} {clip['start']:.1f}–{clip['end']:.1f}s [{clip['reframe']}] QA={'ok' if clip['qa'].get('ok') else clip['qa']} → {target}")
                pending.discard(cid)
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
