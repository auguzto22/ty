from google.genai import errors, types

from app.core.config import get_settings
from app.pipeline import curate_llm, llm_gemini
from app.core.models import CurationPrefs


class _FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class _FakeClient:
    def __init__(self, outcomes):
        self.models = _FakeModels(outcomes)


def _response(payload: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=payload)]),
        finish_reason=types.FinishReason.STOP,
    )])


def _settings(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "teste-falso")
    monkeypatch.setenv("GEMINI_MODEL", "principal")
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "reserva")


def test_gemini_curation_maps_sentences_and_falls_back_on_429(monkeypatch, long_transcript):
    _settings(monkeypatch)
    payload = ('{"clips":[{"start_sentence":7,"end_sentence":12,"title":"Por que o erro acontece","hook":"Você sabe",'
               '"reason":"pergunta forte","hook_score":90,"coherence_score":80,"value_score":70,"trend_score":60,"overall_score":85}]}')
    quota = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    fake = _FakeClient([quota, _response(payload)])
    monkeypatch.setattr(llm_gemini, "client", lambda: fake)

    clips = curate_llm.curate_llm(long_transcript, CurationPrefs(min_duration=5, max_duration=60))

    assert fake.models.calls == ["principal", "reserva"]
    assert len(clips) == 1 and clips[0].source == "llm" and clips[0].score == 85
    assert clips[0].start < clips[0].end
    get_settings.cache_clear()


def test_gemini_auth_error_becomes_curation_error(monkeypatch, long_transcript):
    _settings(monkeypatch)
    denied = errors.ClientError(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}})
    monkeypatch.setattr(llm_gemini, "client", lambda: _FakeClient([denied]))
    try:
        curate_llm.curate_llm(long_transcript, CurationPrefs())
        raise AssertionError("esperava CurationError")
    except curate_llm.CurationError as e:
        assert "recusada" in str(e) and "teste-falso" not in str(e)
    get_settings.cache_clear()


def test_gemini_retries_503_then_falls_back(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.setattr(llm_gemini.time, "sleep", lambda s: None)
    busy = errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
    fake = _FakeClient([busy, busy, busy, _response("OK")])
    monkeypatch.setattr(llm_gemini, "client", lambda: fake)
    r = llm_gemini.generate("ping")
    assert fake.models.calls == ["principal"] * 3 + ["reserva"] and r.text == "OK"
    get_settings.cache_clear()
