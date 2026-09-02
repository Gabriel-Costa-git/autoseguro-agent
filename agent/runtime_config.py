"""Configuração editável em tempo de execução (Studio), com hot-reload.

Quatro arquivos JSON em `config/`:
- `prompts.json`  — textos/prompts por SLOT, com versões nomeadas e uma ativa. A versão
  `default` é imutável e igual ao texto em `agent/defaults.py` (o comportamento entregue).
- `tools.json` e `settings.json` — SÓ overrides. Valor efetivo = override > `.env`/Settings
  > default do código. Assim o `.env` continua valendo e "voltar ao padrão" é apagar o override.
- `custom_tools.json` — REGISTRO das tools que o operador cria no painel (`http` ou `sql`), que
  viram function calling do Responder. Registro vazio = agente idêntico ao entregue. Segredo nunca
  entra aqui: o que se grava é a referência `${env:NOME}`, resolvida só no runtime da tool.

Regras:
- Nada é lido no import: todo consumidor chama `store.text(...)`/`store.param(...)` na hora.
- Hot-reload: cada leitura confere o mtime do arquivo (barato) e recarrega se mudou; o save
  atualiza a memória e o arquivo.
- Placeholders: `text(slot, **ctx)` renderiza com `format_map` seguro (placeholder ausente
  fica literal, em vez de estourar). No save, uma renderização de teste com o contexto de
  exemplo do slot rejeita placeholder desconhecido.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import string
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from agent.config import ROOT, settings
from agent.defaults import SLOTS

# Onde moram prompts.json/tools.json/settings.json e os arquivos de runtime do Studio
# (`atendimentos.json`, `models.json`). Quem precisa do diretório importa daqui — uma
# constante só, para o canal e o Studio nunca discordarem de onde a config está.
CONFIG_DIR = ROOT / "config"

DEFAULT_VERSION = "default"
_PLACEHOLDER_RE = re.compile(r"{([a-zA-Z_][a-zA-Z0-9_]*)}")

# --- tools do painel
NOME_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")        # vira o `name` da função no LLM
NOME_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")       # vira property do schema e `:param` no SQL
ENV_REF_RE = re.compile(r"\$\{env:([^}]*)\}")               # `${env:APOLICE_KEY}` — resolvido só em runtime
NOME_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
SQL_PARAM_RE = re.compile(r"(?<![:\w]):([a-z][a-z0-9_]*)")   # parâmetro nomeado do sqlite/psycopg

# Só leitura: a query precisa começar com SELECT/WITH e não pode carregar nenhuma destas palavras.
SQL_PROIBIDO = (
    "insert", "update", "delete", "drop", "alter", "create", "replace", "truncate",
    "attach", "detach", "pragma", "vacuum", "grant", "revoke", "commit", "rollback", "into",
)


# --------------------------------------------------------------------------- esquemas
class PromptVersion(BaseModel):
    text: str
    note: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


class PromptSlot(BaseModel):
    label: str
    grupo: str
    placeholders: list[str] = Field(default_factory=list)
    active: str = DEFAULT_VERSION
    versions: dict[str, PromptVersion]


class PromptsFile(BaseModel):
    slots: dict[str, PromptSlot] = Field(default_factory=dict)


class QuoteClientTools(BaseModel):
    base_url: str | None = None
    endpoints: dict[str, str] | None = None
    timeout_s: float | None = Field(None, gt=0)
    max_attempts: int | None = Field(None, ge=1, le=10)
    budget_s: float | None = Field(None, gt=0)
    backoff_base_s: float | None = Field(None, ge=0)


class ViacepTools(BaseModel):
    enabled: bool | None = None
    url: str | None = None
    timeout_s: float | None = Field(None, gt=0)


class PolicyTools(BaseModel):
    max_turnos_sem_progresso: int | None = Field(None, ge=1)
    max_cep_tentativas: int | None = Field(None, ge=0)
    objecoes_ate_handoff: int | None = Field(None, ge=1)


class RulesTools(BaseModel):
    pre_validacao_local: bool | None = None


class ToolsFile(BaseModel):
    quote_client: QuoteClientTools = Field(default_factory=QuoteClientTools)
    viacep: ViacepTools = Field(default_factory=ViacepTools)
    policy: PolicyTools = Field(default_factory=PolicyTools)
    rules: RulesTools = Field(default_factory=RulesTools)


# --------------------------------------------------------------------------- tools do painel
TipoParametro = Literal["string", "number", "integer", "boolean"]


class ParametroTool(BaseModel):
    """Um argumento da função, como o LLM vai vê-lo no JSON Schema."""

    tipo: TipoParametro = "string"
    descricao: str = ""
    obrigatorio: bool = False


class HttpTool(BaseModel):
    """Request HTTP. `{param}` é substituído em url/query/body; `${env:X}` em qualquer campo."""

    metodo: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body: Any | None = None
    resposta: Literal["json", "texto"] = "json"


class SqlTool(BaseModel):
    """Query SOMENTE LEITURA. `conexao` é caminho de sqlite ou `postgresql://…` (via `${env:X}`)."""

    conexao: str
    query: str
    max_linhas: int = Field(20, ge=1, le=500)


class CustomTool(BaseModel):
    """Uma tool criada no painel. Vira uma `agno.tools.Function` no Responder."""

    nome: str
    tipo: Literal["http", "sql"]
    enabled: bool = True
    descricao: str                       # o LLM lê isto para decidir QUANDO chamar
    instrucoes: str | None = None        # vai para o system prompt (Function.instructions)
    parametros: dict[str, ParametroTool] = Field(default_factory=dict)
    timeout_s: float = Field(5.0, gt=0, le=60)
    max_chars: int = Field(2000, ge=100, le=20000)
    http: HttpTool | None = None
    sql: SqlTool | None = None
    criado_em: str = ""
    atualizado_em: str = ""

    @field_validator("nome")
    @classmethod
    def _nome_valido(cls, v: str) -> str:
        if not NOME_TOOL_RE.fullmatch(v):
            raise ValueError("nome inválido: use 3–41 caracteres [a-z0-9_] começando por letra")
        return v

    @field_validator("descricao")
    @classmethod
    def _descricao_obrigatoria(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("descrição é obrigatória: é por ela que o modelo decide quando chamar a tool")
        return v

    @field_validator("parametros")
    @classmethod
    def _parametros_validos(cls, v: dict[str, ParametroTool]) -> dict[str, ParametroTool]:
        for nome in v:
            if not NOME_PARAM_RE.fullmatch(nome):
                raise ValueError(f"parâmetro inválido: {nome!r} (use [a-z0-9_] começando por letra)")
        return v

    @model_validator(mode="after")
    def _coerente(self) -> CustomTool:
        if self.tipo == "http":
            if self.http is None:
                raise ValueError("tool do tipo http precisa do bloco `http`")
            self.sql = None
            _validar_http(self.http, set(self.parametros))
        else:
            if self.sql is None:
                raise ValueError("tool do tipo sql precisa do bloco `sql`")
            self.http = None
            _validar_sql(self.sql, set(self.parametros))
        for texto in (self.descricao, self.instrucoes or ""):
            validar_env_refs(texto)
        return self


class CustomToolsFile(BaseModel):
    tools: dict[str, CustomTool] = Field(default_factory=dict)


def validar_env_refs(texto: str) -> None:
    """Toda referência `${env:X}` tem de ter nome de variável de ambiente plausível."""
    for nome in ENV_REF_RE.findall(texto or ""):
        if not NOME_ENV_RE.fullmatch(nome):
            raise ValueError(f"referência de ambiente inválida: ${{env:{nome}}} (use MAIUSCULAS_COM_UNDERSCORE)")


def _validar_templates(texto: str, parametros: set[str], onde: str) -> None:
    """`{param}` só pode citar parâmetro declarado — senão a tool quebraria só na hora da chamada."""
    validar_env_refs(texto)
    desconhecidos = placeholders_de(ENV_REF_RE.sub("", texto or "")) - parametros
    if desconhecidos:
        raise ValueError(f"{onde}: parâmetros desconhecidos {sorted(desconhecidos)}; declarados: {sorted(parametros)}")


def _validar_http(http: HttpTool, parametros: set[str]) -> None:
    if not http.url.startswith(("http://", "https://")):
        raise ValueError("url precisa começar com http:// ou https://")
    _validar_templates(http.url, parametros, "url")
    for chave, valor in http.headers.items():
        _validar_templates(str(valor), parametros, f"header {chave}")
    for chave, valor in http.query.items():
        _validar_templates(str(valor), parametros, f"query {chave}")
    if isinstance(http.body, str):
        _validar_templates(http.body, parametros, "body")
    elif isinstance(http.body, dict):
        for chave, valor in http.body.items():
            if isinstance(valor, str):
                _validar_templates(valor, parametros, f"body {chave}")


def _validar_sql(sql: SqlTool, parametros: set[str]) -> None:
    """Somente leitura: 1 statement, começa com SELECT/WITH, sem `;` e sem palavra de escrita."""
    validar_env_refs(sql.conexao)
    query = (sql.query or "").strip()
    if not query:
        raise ValueError("query vazia")
    if ";" in query:
        raise ValueError("query com `;`: só um statement por tool")
    if not re.match(r"^(select|with)\b", query, re.IGNORECASE):
        raise ValueError("query precisa começar com SELECT ou WITH (somente leitura)")
    baixo = query.lower()
    proibidas = [p for p in SQL_PROIBIDO if re.search(rf"\b{p}\b", baixo)]
    if proibidas:
        raise ValueError(f"query não é somente leitura: {sorted(proibidas)}")
    desconhecidos = set(SQL_PARAM_RE.findall(query)) - parametros
    if desconhecidos:
        raise ValueError(f"query cita parâmetros não declarados: {sorted(desconhecidos)}")


class SettingsFile(BaseModel):
    gemini_model: str | None = None
    responder_history_runs: int | None = Field(None, ge=0, le=50)
    extractor_temperature: float | None = Field(None, ge=0, le=2)
    responder_temperature: float | None = Field(None, ge=0, le=2)
    llm_max_tentativas: int | None = Field(None, ge=1, le=10)
    llm_budget_s: float | None = Field(None, gt=0)
    script_delay_s: float | None = Field(None, ge=0)
    agent_db_path: str | None = None


# --------------------------------------------------------------------------- defaults (= comportamento entregue)
def _code_defaults() -> dict[str, dict[str, Any]]:
    """Defaults do código, com os que têm variável de ambiente já resolvidos pelo `Settings`."""
    return {
        "tools": {
            "quote_client": {
                "base_url": settings.quote_api_url,
                "endpoints": {"docker (8000)": "http://localhost:8000", "falha forçada (8001)": "http://localhost:8001"},
                "timeout_s": settings.quote_timeout_s,
                "max_attempts": settings.quote_max_attempts,
                "budget_s": settings.quote_budget_s,
                "backoff_base_s": settings.quote_backoff_base_s,
            },
            "viacep": {"enabled": True, "url": settings.viacep_url, "timeout_s": settings.viacep_timeout_s},
            "policy": {
                "max_turnos_sem_progresso": settings.max_turnos_sem_progresso,
                "max_cep_tentativas": settings.max_cep_tentativas,
                "objecoes_ate_handoff": 2,
            },
            "rules": {"pre_validacao_local": True},
        },
        "settings": {
            "gemini_model": settings.gemini_model,
            "responder_history_runs": 8,
            "extractor_temperature": 0.0,
            "responder_temperature": 0.4,
            "llm_max_tentativas": 4,
            "llm_budget_s": 30.0,
            "script_delay_s": 0.0,
            "agent_db_path": str(settings.agent_db_path),
        },
    }


# Quais chaves vêm do .env (para a UI mostrar a origem certa)
_ENV_BACKED = {
    "tools.quote_client.base_url": "QUOTE_API_URL",
    "tools.quote_client.timeout_s": "QUOTE_TIMEOUT_S",
    "tools.quote_client.max_attempts": "QUOTE_MAX_ATTEMPTS",
    "tools.quote_client.budget_s": "QUOTE_BUDGET_S",
    "tools.quote_client.backoff_base_s": "QUOTE_BACKOFF_BASE_S",
    "tools.viacep.url": "VIACEP_URL",
    "tools.viacep.timeout_s": "VIACEP_TIMEOUT_S",
    "tools.policy.max_turnos_sem_progresso": "MAX_TURNOS_SEM_PROGRESSO",
    "tools.policy.max_cep_tentativas": "MAX_CEP_TENTATIVAS",
    "settings.gemini_model": "GEMINI_MODEL",
    "settings.agent_db_path": "AGENT_DB_PATH",
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(text: str, ctx: dict[str, Any]) -> str:
    return string.Formatter().vformat(text, (), _SafeDict(ctx)) if "{" in text else text


def placeholders_de(text: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(text))


class ConfigError(ValueError):
    """Erro de validação de uma edição feita pelo Studio."""


# --------------------------------------------------------------------------- store
class ConfigStore:
    def __init__(self, config_dir: Path | None = None, slots: dict[str, dict] | None = None) -> None:
        self.dir = Path(config_dir) if config_dir is not None else CONFIG_DIR
        self._slots_def = slots if slots is not None else SLOTS
        self._cache: dict[str, tuple[float, Any]] = {}
        self._listeners: list[Callable[[str], None]] = []

    # ---- arquivos
    def _path(self, nome: str) -> Path:
        return self.dir / f"{nome}.json"

    def _load(self, nome: str, modelo: type[BaseModel]) -> BaseModel:
        path = self._path(nome)
        mtime = path.stat().st_mtime if path.exists() else -1.0
        hit = self._cache.get(nome)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        if path.exists():
            try:
                dados = modelo.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (ValidationError, ValueError) as exc:
                raise ConfigError(f"{path.name} inválido: {exc}") from exc
        else:
            dados = modelo()
        if nome == "prompts":
            dados = self._sync_slots(dados)  # type: ignore[arg-type]
        self._cache[nome] = (mtime, dados)
        return dados

    def _save(self, nome: str, dados: BaseModel) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(nome)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dados.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        self._cache[nome] = (path.stat().st_mtime, dados)
        for fn in self._listeners:
            fn(nome)

    def on_change(self, fn: Callable[[str], None]) -> None:
        self._listeners.append(fn)

    def ensure_files(self) -> None:
        """Cria os quatro arquivos se faltarem (prompts com só a versão `default` em cada slot)."""
        for nome, modelo in (
            ("prompts", PromptsFile),
            ("tools", ToolsFile),
            ("settings", SettingsFile),
            ("custom_tools", CustomToolsFile),
        ):
            if not self._path(nome).exists():
                self._save(nome, self._load(nome, modelo))

    def _sync_slots(self, arquivo: PromptsFile) -> PromptsFile:
        """Garante que todo slot do código existe no arquivo e que `default` é o texto do código."""
        for key, d in self._slots_def.items():
            slot = arquivo.slots.get(key)
            if slot is None:
                arquivo.slots[key] = PromptSlot(
                    label=d["label"], grupo=d["grupo"], placeholders=list(d["placeholders"]),
                    versions={DEFAULT_VERSION: PromptVersion(text=d["default"], note="comportamento entregue")},
                )
                continue
            slot.label, slot.grupo, slot.placeholders = d["label"], d["grupo"], list(d["placeholders"])
            slot.versions[DEFAULT_VERSION] = PromptVersion(
                text=d["default"], note="comportamento entregue",
                created_at=slot.versions.get(DEFAULT_VERSION, PromptVersion(text="")).created_at,
            )
            if slot.active not in slot.versions:
                slot.active = DEFAULT_VERSION
        return arquivo

    # ---- prompts
    def prompts(self) -> PromptsFile:
        return self._load("prompts", PromptsFile)  # type: ignore[return-value]

    def slot(self, key: str) -> PromptSlot:
        slot = self.prompts().slots.get(key)
        if slot is None:
            raise ConfigError(f"slot desconhecido: {key}")
        return slot

    def raw_text(self, key: str) -> str:
        slot = self.slot(key)
        return slot.versions[slot.active].text

    def text(self, key: str, **ctx: Any) -> str:
        """Texto ativo do slot, renderizado. Slot sem placeholders devolve o texto como está."""
        return render_template(self.raw_text(key), ctx)

    def _validar_texto(self, key: str, text: str) -> None:
        permitidos = set(self.slot(key).placeholders)
        desconhecidos = placeholders_de(text) - permitidos
        if desconhecidos:
            raise ConfigError(f"placeholders desconhecidos em {key}: {sorted(desconhecidos)}; permitidos: {sorted(permitidos)}")
        try:
            render_template(text, dict.fromkeys(permitidos, "x"))
        except (ValueError, KeyError, IndexError) as exc:
            raise ConfigError(f"template inválido em {key}: {exc}") from exc

    def add_version(self, key: str, name: str, text: str, note: str = "", activate: bool = True) -> PromptSlot:
        name = name.strip()
        if not name or name == DEFAULT_VERSION or not re.fullmatch(r"[\w .-]{1,60}", name):
            raise ConfigError("nome de versão inválido (1–60 caracteres; 'default' é reservado)")
        arquivo = self.prompts()
        slot = arquivo.slots.get(key)
        if slot is None:
            raise ConfigError(f"slot desconhecido: {key}")
        if name in slot.versions:
            raise ConfigError(f"versão '{name}' já existe em {key}")
        self._validar_texto(key, text)
        slot.versions[name] = PromptVersion(text=text, note=note)
        if activate:
            slot.active = name
        self._save("prompts", arquivo)
        return slot

    def edit_version(self, key: str, name: str, text: str, note: str | None = None) -> PromptSlot:
        if name == DEFAULT_VERSION:
            raise ConfigError("a versão 'default' é imutável (comportamento entregue); crie uma versão nova")
        arquivo = self.prompts()
        slot = self.slot(key)
        if name not in slot.versions:
            raise ConfigError(f"versão '{name}' não existe em {key}")
        self._validar_texto(key, text)
        atual = slot.versions[name]
        slot.versions[name] = PromptVersion(text=text, note=atual.note if note is None else note, created_at=atual.created_at)
        self._save("prompts", arquivo)
        return slot

    def set_active(self, key: str, name: str) -> PromptSlot:
        arquivo = self.prompts()
        slot = self.slot(key)
        if name not in slot.versions:
            raise ConfigError(f"versão '{name}' não existe em {key}")
        slot.active = name
        self._save("prompts", arquivo)
        return slot

    def delete_version(self, key: str, name: str) -> PromptSlot:
        arquivo = self.prompts()
        slot = self.slot(key)
        if name == DEFAULT_VERSION:
            raise ConfigError("a versão 'default' não pode ser apagada")
        if name == slot.active:
            raise ConfigError("ative outra versão antes de apagar a ativa")
        if name not in slot.versions:
            raise ConfigError(f"versão '{name}' não existe em {key}")
        del slot.versions[name]
        self._save("prompts", arquivo)
        return slot

    # ---- tools do painel (registro, não override)
    def custom_tools(self) -> CustomToolsFile:
        """Registro das tools criadas no painel, com hot-reload por mtime como os outros arquivos."""
        return self._load("custom_tools", CustomToolsFile)  # type: ignore[return-value]

    def custom_tool(self, nome: str) -> CustomTool:
        tool = self.custom_tools().tools.get(nome)
        if tool is None:
            raise ConfigError(f"tool desconhecida: {nome}")
        return tool

    def custom_tools_version(self) -> str:
        """Impressão digital das tools HABILITADAS — o `Responder` reconstrói o Agent quando muda.

        Hash do conteúdo (e não mtime) para salvar/reabrir sem mexer em nada não jogar fora o
        Agent e o histórico em cache. Registro ilegível vale vazio: o turno não pode quebrar por
        causa de um JSON torto (o painel mostra o erro na hora de listar).
        """
        try:
            habilitadas = {n: t for n, t in self.custom_tools().tools.items() if t.enabled}
        except ConfigError:
            return ""
        if not habilitadas:
            return ""
        bruto = json.dumps(
            {n: habilitadas[n].model_dump(mode="json") for n in sorted(habilitadas)},
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:16]

    def upsert_custom_tool(self, nome: str, dados: dict[str, Any]) -> CustomTool:
        """Cria ou substitui uma tool. `criado_em` é preservado; `atualizado_em` é sempre agora."""
        if not NOME_TOOL_RE.fullmatch(nome):
            raise ConfigError("nome inválido: use 3–41 caracteres [a-z0-9_] começando por letra")
        corpo = dict(dados)
        corpo["nome"] = nome
        arquivo = self.custom_tools()
        atual = arquivo.tools.get(nome)
        agora = datetime.now(UTC).isoformat(timespec="seconds")
        corpo["criado_em"] = (atual.criado_em if atual else None) or corpo.get("criado_em") or agora
        corpo["atualizado_em"] = agora
        try:
            tool = CustomTool.model_validate(corpo)
        except ValidationError as exc:
            raise ConfigError(f"tool inválida: {_primeiro_erro(exc)}") from exc
        arquivo.tools[nome] = tool
        self._save("custom_tools", arquivo)
        return tool

    def delete_custom_tool(self, nome: str) -> None:
        arquivo = self.custom_tools()
        if nome not in arquivo.tools:
            raise ConfigError(f"tool desconhecida: {nome}")
        del arquivo.tools[nome]
        self._save("custom_tools", arquivo)

    # ---- tools / settings (overrides)
    def _overrides(self, nome: str) -> dict[str, Any]:
        modelo = ToolsFile if nome == "tools" else SettingsFile
        return self._load(nome, modelo).model_dump(mode="json", exclude_none=True)

    def param(self, path: str) -> Any:
        """Valor efetivo de `tools.quote_client.timeout_s`, `settings.gemini_model`, etc."""
        return self.effective(path)["value"]

    def effective(self, path: str) -> dict[str, Any]:
        partes = path.split(".")
        raiz = partes[0]
        if raiz not in ("tools", "settings"):
            raise ConfigError(f"caminho inválido: {path}")
        override = _get(self._overrides(raiz), partes[1:])
        default = _get(_code_defaults()[raiz], partes[1:])
        if default is _MISSING and override is _MISSING:
            raise ConfigError(f"caminho desconhecido: {path}")
        if override is not _MISSING:
            return {"value": override, "origem": "override", "default": default}
        env = _ENV_BACKED.get(path)
        origem = f"env:{env}" if env and os.getenv(env) is not None else "default"
        return {"value": default, "origem": origem, "default": default}

    def snapshot(self) -> dict[str, Any]:
        """Tudo efetivo, com origem, para a UI: {tools: {grupo: {chave: {value, origem, default}}}, settings: {...}}."""
        out: dict[str, Any] = {"tools": {}, "settings": {}}
        defaults = _code_defaults()
        for grupo, chaves in defaults["tools"].items():
            out["tools"][grupo] = {k: self.effective(f"tools.{grupo}.{k}") for k in chaves}
        out["settings"] = {k: self.effective(f"settings.{k}") for k in defaults["settings"]}
        return out

    def set_overrides(self, nome: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Aplica um patch parcial (chave ausente = não mexe; `null` = volta ao padrão). Valida pelo esquema."""
        if nome not in ("tools", "settings"):
            raise ConfigError(f"arquivo inválido: {nome}")
        modelo = ToolsFile if nome == "tools" else SettingsFile
        atual = self._overrides(nome)
        novo = _merge(atual, patch)
        try:
            dados = modelo.model_validate(novo)
        except ValidationError as exc:
            raise ConfigError(f"{nome} inválido: {exc.errors()[0]['msg']} em {'.'.join(str(p) for p in exc.errors()[0]['loc'])}") from exc
        self._save(nome, dados)
        return self._overrides(nome)

    def clear_override(self, path: str) -> None:
        partes = path.split(".")
        raiz = partes[0]
        patch: dict[str, Any] = {}
        cursor = patch
        for p in partes[1:-1]:
            cursor[p] = {}
            cursor = cursor[p]
        cursor[partes[-1]] = None
        self.set_overrides(raiz, patch)


def _primeiro_erro(exc: ValidationError) -> str:
    """Mensagem curta do 1º erro do pydantic, no formato que a UI mostra ao operador."""
    erro = exc.errors()[0]
    caminho = ".".join(str(p) for p in erro["loc"])
    msg = str(erro["msg"]).removeprefix("Value error, ")
    return f"{msg} em {caminho}" if caminho else msg


_MISSING = object()


def _get(d: Any, partes: list[str]) -> Any:
    for p in partes:
        if not isinstance(d, dict) or p not in d:
            return _MISSING
        d = d[p]
    return d


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


store = ConfigStore()
