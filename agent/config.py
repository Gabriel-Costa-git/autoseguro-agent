"""Configuração por variável de ambiente (.env carregado se existir). Editado só pelo orquestrador."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # API de cotação (desafio)
    quote_api_url: str = os.getenv("QUOTE_API_URL", "http://localhost:8000")
    quote_timeout_s: float = _f("QUOTE_TIMEOUT_S", 3.5)
    quote_max_attempts: int = _i("QUOTE_MAX_ATTEMPTS", 4)
    quote_budget_s: float = _f("QUOTE_BUDGET_S", 15.0)
    quote_backoff_base_s: float = _f("QUOTE_BACKOFF_BASE_S", 0.5)

    # ViaCEP (validação suave)
    viacep_url: str = os.getenv("VIACEP_URL", "https://viacep.com.br/ws")
    viacep_timeout_s: float = _f("VIACEP_TIMEOUT_S", 2.0)

    # LLM
    google_api_key: str | None = os.getenv("GOOGLE_API_KEY")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    agent_db_path: Path = ROOT / os.getenv("AGENT_DB_PATH", "data/agent.db")

    # Evolution API (canal WhatsApp) — só o adaptador usa
    evolution_url: str | None = os.getenv("EVOLUTION_URL")
    evolution_apikey: str | None = os.getenv("EVOLUTION_APIKEY")
    evolution_instance: str | None = os.getenv("EVOLUTION_INSTANCE")
    consultor_number: str | None = os.getenv("CONSULTOR_NUMBER")  # opcional: aviso de handoff

    # Observabilidade / política
    log_dir: Path = ROOT / os.getenv("LOG_DIR", "logs")
    max_turnos_sem_progresso: int = _i("MAX_TURNOS_SEM_PROGRESSO", 3)
    max_cep_tentativas: int = _i("MAX_CEP_TENTATIVAS", 2)


settings = Settings()
