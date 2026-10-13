"""Worker: processa a fila de jobs. Rode um por máquina (o pipeline já usa todos os núcleos no ffmpeg/whisper).

    python -m app.worker            # laço contínuo
    python -m app.worker --once     # processa o que houver e sai (útil em testes)
"""

import argparse
import logging
import signal
import time

from app.api.cleanup import run_cleanup
from app.api.db import engine
from app.api.jobs import claim, execute, worker_name

log = logging.getLogger("clipstudio.worker")
POLL_SECONDS = 2.0
CLEANUP_EVERY = 3600.0


def run(once: bool = False) -> int:
    engine()  # cria o banco/tabelas se preciso
    name = worker_name()
    stopping = {"flag": False}

    def stop(*_):
        stopping["flag"] = True
        log.info("encerrando após o job atual…")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    done = 0
    last_cleanup = 0.0
    log.info("worker %s pronto", name)
    while not stopping["flag"]:
        if not once and time.monotonic() - last_cleanup > CLEANUP_EVERY:
            last_cleanup = time.monotonic()
            try:
                run_cleanup()
            except Exception:  # limpeza nunca derruba o worker
                log.exception("falha na limpeza")
        job = claim(name)
        if job is None:
            if once:
                break
            time.sleep(POLL_SECONDS)
            continue
        log.info("job %s: %s (projeto %s, tentativa %d)", job.id, job.kind, job.project_id, job.attempts)
        t0 = time.monotonic()
        execute(job)
        done += 1
        log.info("job %s terminou em %.1fs", job.id, time.monotonic() - t0)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker do ClipStudio")
    parser.add_argument("--once", action="store_true", help="processa a fila atual e sai")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(once=args.once)


if __name__ == "__main__":
    main()
