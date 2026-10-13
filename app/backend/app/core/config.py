from functools import lru_cache
from pathlib import Path

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/.env, independente do diretório de onde o comando é executado.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # Provedor da curadoria por LLM (obrigatória: não há curadoria local): "gemini" (padrão) ou "claude".
    llm_provider: Literal["gemini", "claude"] = "gemini"

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, v):
        return (v.strip().lower() or "gemini") if isinstance(v, str) else v

    # Gemini (Google AI Studio). A chave vem de GEMINI_API_KEY (ambiente ou .env) e nunca é exibida.
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_model: str = "gemini-3.5-flash-lite"  # usado quando o principal esgota a cota (429)
    gemini_thinking_level: str = ""  # vazio = padrão do modelo; "low" | "medium" | "high"
    gemini_visual: bool = True  # passo 2: avaliação visual dos candidatos (folhas de contato)

    anthropic_api_key: SecretStr = SecretStr("")
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"

    whisper_model: str = "small"
    whisper_language: str = ""  # vazio = detecção automática

    data_dir: Path = Path("./data")
    # Token exigido em Authorization: Bearer <token>. Vazio = API aberta (só para desenvolvimento local).
    api_token: SecretStr = SecretStr("")
    max_upload_bytes: int = 10 * 1024**3
    max_video_seconds: float = 3 * 3600

    # Banco: vazio = SQLite em DATA_DIR/clipstudio.db. Produção: postgresql+psycopg://usuario:senha@host/banco
    database_url: str = ""
    # Contas: JWT assinado com JWT_SECRET (vazio = gerado e guardado em DATA_DIR/jwt_secret).
    jwt_secret: SecretStr = SecretStr("")
    jwt_days: int = 30
    allow_signup: bool = False  # cadastro aberto em POST /auth/register; senão, contas via `clipstudio user-add`
    # Cotas por usuário (0 = sem limite). O API_TOKEN (administrador) não tem cota.
    quota_projects_per_day: int = 20
    quota_minutes_per_day: float = 180.0
    # Limpeza: apaga o vídeo original e arquivos intermediários após N dias (cortes ficam);
    # apaga o projeto inteiro após M dias. 0 = nunca.
    source_retention_days: int = 0
    project_retention_days: int = 0
    caption_font: str = ""

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    @property
    def llm_enabled(self) -> bool:
        if self.llm_provider == "gemini":
            return bool(self.gemini_api_key.get_secret_value())
        if self.llm_provider == "claude":
            return bool(self.anthropic_api_key.get_secret_value())
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
