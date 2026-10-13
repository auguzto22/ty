"""Cliente Gemini (SDK oficial google-genai). A chave vem de Settings.gemini_api_key e nunca é registrada em log."""

import logging
import time
from dataclasses import dataclass

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from app.core.config import get_settings

log = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    pass


TRANSIENT = {429, 500, 503, 504}  # cota/sobrecarga: tenta de novo e depois o modelo de fallback
RETRIES_PER_MODEL = 2
RETRY_DELAY = 3.0  # s, dobra a cada tentativa


@dataclass
class ModelInfo:
    name: str  # sem o prefixo "models/"
    display_name: str
    input_token_limit: int | None
    output_token_limit: int | None
    thinking: bool | None


def client() -> genai.Client:
    key = get_settings().gemini_api_key.get_secret_value()
    if not key:
        raise GeminiError("GEMINI_API_KEY não configurada")
    return genai.Client(api_key=key)


def _describe(e: errors.APIError) -> str:
    return f"{e.code} {e.status or ''}: {e.message or ''}".strip()


def list_generate_models(c: genai.Client | None = None) -> list[ModelInfo]:
    """Modelos que a chave enxerga e que aceitam generateContent. Só metadados: não consome tokens."""
    c = c or client()
    out = []
    try:
        for m in c.models.list(config={"page_size": 100}):
            if "generateContent" not in (m.supported_actions or []):
                continue
            out.append(ModelInfo(
                name=(m.name or "").removeprefix("models/"),
                display_name=m.display_name or "",
                input_token_limit=m.input_token_limit,
                output_token_limit=m.output_token_limit,
                thinking=m.thinking,
            ))
    except errors.APIError as e:
        raise GeminiError(f"Falha ao listar modelos ({_describe(e)})") from e
    return out


def _config(system: str, schema: type[BaseModel] | None) -> types.GenerateContentConfig:
    settings = get_settings()
    cfg = types.GenerateContentConfig(system_instruction=system or None)
    if schema is not None:
        cfg.response_mime_type = "application/json"
        cfg.response_schema = schema
    if settings.gemini_thinking_level:
        cfg.thinking_config = types.ThinkingConfig(thinking_level=settings.gemini_thinking_level)
    return cfg


def generate(prompt: "str | list", system: str = "", schema: type[BaseModel] | None = None, model: str | None = None) -> types.GenerateContentResponse:
    """Uma chamada generateContent. Sobrecarga (5xx) é repetida com espera; cota esgotada (429) ou
    sobrecarga persistente passam ao modelo de fallback."""
    settings = get_settings()
    c = client()
    models = [model or settings.gemini_model]
    if model is None and settings.gemini_fallback_model and settings.gemini_fallback_model != models[0]:
        models.append(settings.gemini_fallback_model)

    last: errors.APIError | None = None
    for name in models:
        for attempt in range(RETRIES_PER_MODEL + 1):
            try:
                response = c.models.generate_content(model=name, contents=prompt, config=_config(system, schema))
            except errors.APIError as e:
                if e.code in (401, 403):
                    raise GeminiError(f"GEMINI_API_KEY recusada ({_describe(e)})") from e
                if e.code not in TRANSIENT:
                    raise GeminiError(f"Requisição rejeitada pelo Gemini ({_describe(e)})") from e
                last = e
                if e.code == 429 or attempt == RETRIES_PER_MODEL:
                    log.warning("Gemini %s: %s; %s", name, _describe(e), "tentando fallback" if name != models[-1] else "sem fallback")
                    break  # cota esgotada não melhora em segundos: vai direto ao próximo modelo
                time.sleep(RETRY_DELAY * 2**attempt)
                continue

            u = response.usage_metadata
            if u:
                log.info("Gemini %s: %s tokens in / %s out / %s thinking", name, u.prompt_token_count, u.candidates_token_count, u.thoughts_token_count)
            return response
    raise GeminiError(f"Gemini indisponível ou sem cota ({_describe(last)})" if last else "Nenhum modelo Gemini configurado")


def generate_json(prompt: "str | list", system: str, schema: type[BaseModel]) -> BaseModel:
    response = generate(prompt, system, schema)
    cand = response.candidates[0] if response.candidates else None
    reason = cand.finish_reason if cand else None
    if reason not in (None, types.FinishReason.STOP):
        raise GeminiError(f"Resposta incompleta do Gemini (finish_reason={reason})")
    parsed = response.parsed
    if isinstance(parsed, schema):
        return parsed
    try:
        return schema.model_validate_json(response.text or "")
    except ValueError as e:
        raise GeminiError("Resposta do Gemini fora do schema esperado") from e
