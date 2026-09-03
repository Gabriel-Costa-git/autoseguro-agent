"""Exporta os transcripts do Claude Code para `ai-logs/sessions/`, higienizados.

Transparência de uso de IA é exigência do desafio: as sessões do terminal
orquestrador e dos executores paralelos precisam ficar no repo, mas nunca com
segredo, PII ou arquivo pessoal de configuração junto.

O exportador é um conjunto de regras, não uma cópia:

1. **Fontes** — diretórios de `~/.claude/projects` cujo nome derive do workspace
   deste repo (descoberto pelo próprio git, para não hardcodar caminho pessoal)
   ou de um workspace irmão de mesmo nome-base. Sessões com menos de
   `MIN_LINHAS` linhas ou inteiramente fora da janela `[1º commit − 2 h, agora]`
   ficam de fora.
2. **Nomes de saída neutros** — `orquestrador/`, `executor-<n>/`, `outro/`.
   Nenhum caminho ou nome de máquina do autor vira nome de pasta.
3. **Uma linha JSON entra, uma linha JSON sai** — `json.loads` → transformação
   nos valores → `json.dumps`. A máscara de PII nunca toca a linha crua (era o
   que gerava JSON inválido: o regex de telefone comia `"resetsAt":<número>`).
   Linha que já entra inválida é descartada e contada.
4. **Redações por conteúdo** — arquivo pessoal de configuração, memórias do
   agente, leitura de diretórios pessoais, imagens em base64 e a linha de
   `deferred_tools_delta` (que lista as ferramentas/MCPs pessoais da máquina).
5. **Denylist externa** — literais que só o autor conhece (hosts, ids, nomes de
   terceiros) ficam num arquivo FORA do repo e viram `[redigido]`.

Uso:
    uv run python scripts/export_ai_logs.py [--projects-glob G ...] [--dest DIR]
    uv run python scripts/export_ai_logs.py --check [--dir ai-logs/]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.pii import mask_text

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

MIN_LINHAS = 10  # abaixo disso a sessão é um terminal aberto por engano, não uma conversa
JANELA_ANTES = timedelta(hours=2)  # o trabalho começa antes do 1º commit
IMAGEM_MIN_BYTES = 2048

REDACTED = "<REDACTED>"
MARCA_DENYLIST = "[redigido]"
MARCA_PESSOAL = "[redigido: arquivo pessoal de configuração, fora do escopo do desafio]"
MARCA_IMAGEM = "[imagem removida: screenshot]"
MARCA_BASE64 = "[binário removido: blob base64]"

# Âncoras de conteúdo: redigir pelo que o texto É, não pelo caminho de onde veio.
# O mesmo arquivo pessoal chegou aos transcripts por três caminhos (leitura direta,
# `cat` de um dump intermediário e leitura desse dump), então o path não basta.
ANCORA_CONFIG_PESSOAL = "protocolo de orquestração (Gabriel)"
ANCORA_MEMORIA_INICIO = "---"
ANCORA_MEMORIA_CAMPOS = ("name:", "metadata:")

_GOOGLE_KEY_RE = re.compile(r"(?:AIza[0-9A-Za-z_-]{30,}|AQ\.[0-9A-Za-z_-]{30,})")
_SECRET_NAME_RE = re.compile(r"KEY|SECRET|TOKEN|PASS|PWD|CREDENTIAL|URL|HOST|NUMBER|INSTANCE", re.IGNORECASE)
_PREFIX_MIN = 8  # prefixos de segredo que sobram em comandos de busca também viram <REDACTED>
_SECRET_MIN_LEN = 12
_APIKEY_RE = re.compile(r"apikey[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}", re.IGNORECASE)
_EMAIL_COUNT_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_MAGIC_IMAGEM = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"BM")
_UUID_FINAL_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TS_RE = re.compile(r'"timestamp"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2}T[^"]+)"')

# Valores locais não são segredo; sem esta exceção `QUOTE_API_URL=http://localhost:8000`
# (nome casa `URL`, valor tem 21 chars) fazia `--check` sair 1 em qualquer clone com `.env`.
_HOSTS_LOCAIS = ("localhost", "127.", "0.0.0.0", "::1")


# --------------------------------------------------------------------------- descoberta de fontes
def _repo_dir() -> Path:
    """Diretório do repositório principal, mesmo rodando de um worktree."""
    try:
        saida = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ROOT
    return Path(saida).parent if saida else ROOT


def _slug(caminho: Path) -> str:
    """Mesma convenção do Claude Code para nomear a pasta de um projeto."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(caminho))


def descobrir_workspaces(repo: Path) -> list[Path]:
    """Workspace do repo (primeiro = principal) + irmãos de mesmo nome-base.

    O trabalho aconteceu em dois workspaces: o do projeto e um irmão usado para
    outro workspace. Descobrir em vez de hardcodar mantém caminho pessoal fora do
    código-fonte (regra de higiene do repo) e faz o script rodar noutra máquina.
    """
    base = repo.parent
    workspaces = [base]
    pai = base.parent
    if pai.is_dir():
        for irmao in sorted(pai.iterdir()):
            if irmao != base and irmao.is_dir() and irmao.name.endswith(base.name):
                workspaces.append(irmao)
    return workspaces


def globs_padrao(workspaces: list[Path]) -> list[str]:
    return [_slug(ws) + "*" for ws in workspaces]


def dirs_de_origem(projects_dir: Path, globs: list[str]) -> list[Path]:
    if not projects_dir.is_dir():
        return []
    achados: dict[str, Path] = {}
    for padrao in globs:
        for d in projects_dir.glob(padrao):
            if d.is_dir():
                achados[d.name] = d
    return [achados[nome] for nome in sorted(achados)]


def classificar(dir_name: str, workspaces: list[Path]) -> tuple[str, str]:
    """(`papel`, `chave`) do diretório de origem.

    `papel` é `orquestrador`, `executor` ou `outro-workspace`. A distinção é
    estrutural: a pasta de um executor é uma subpasta OCULTA do workspace, e a
    convenção de nomes do Claude Code transforma o ponto do diretório oculto num
    `--` no meio do slug. `chave` é o id da role (para numerar os executores).
    """
    principal = workspaces[0] if workspaces else None
    for ws in sorted(workspaces, key=lambda p: len(_slug(p)), reverse=True):
        s = _slug(ws)
        if dir_name != s and not dir_name.startswith(s + "-"):
            continue
        resto = dir_name[len(s) :]
        m = _UUID_FINAL_RE.search(dir_name)
        chave = m.group(0) if m else resto
        if ws != principal:
            return "outro-workspace", chave
        return ("executor", chave) if "--" in resto else ("orquestrador", chave)
    return "outro-workspace", dir_name


def mapear_destinos(dirs: list[Path], workspaces: list[Path]) -> dict[str, str]:
    """Nome de pasta neutro para cada diretório de origem."""
    papeis = {d.name: classificar(d.name, workspaces) for d in dirs}
    chaves_exec = sorted({chave for papel, chave in papeis.values() if papel == "executor"})
    numero = {chave: i for i, chave in enumerate(chaves_exec, start=1)}
    destinos = {}
    for nome, (papel, chave) in papeis.items():
        destinos[nome] = f"executor-{numero[chave]}" if papel == "executor" else papel
    return destinos


def janela_do_projeto(repo: Path, agora: datetime | None = None) -> tuple[datetime, datetime]:
    """`[data do 1º commit − 2 h, agora]`, em UTC."""
    fim = agora or datetime.now(UTC)
    try:
        saida = subprocess.run(
            ["git", "-C", str(repo), "log", "--reverse", "--format=%cI"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        saida = []
    if not saida:
        return fim - timedelta(days=365), fim
    primeiro = datetime.fromisoformat(saida[0]).astimezone(UTC)
    return primeiro - JANELA_ANTES, fim


def _timestamps_e_linhas(arquivo: Path) -> tuple[int, str | None, str | None]:
    """Conta linhas e devolve o 1º/último `timestamp` sem parsear JSON (arquivos grandes)."""
    n = 0
    primeiro = ultimo = None
    with arquivo.open("r", encoding="utf-8", errors="replace") as f:
        for linha in f:
            n += 1
            m = _TS_RE.search(linha)
            if m:
                if primeiro is None:
                    primeiro = m.group(1)
                ultimo = m.group(1)
    return n, primeiro, ultimo


def _dentro_da_janela(primeiro: str | None, ultimo: str | None, janela: tuple[datetime, datetime]) -> bool:
    if primeiro is None or ultimo is None:
        return False
    ini, fim = janela
    a = datetime.fromisoformat(primeiro)
    b = datetime.fromisoformat(ultimo)
    return b >= ini and a <= fim


# --------------------------------------------------------------------------- segredos e denylist
def _valor_local(valor: str) -> bool:
    base = re.sub(r"^\w+://", "", valor)
    return base.startswith(_HOSTS_LOCAIS)


def _load_env_secrets(env_path: Path) -> list[str]:
    """Valores (não as chaves) de todas as variáveis do `.env`, maiores primeiro.

    Maiores primeiro evita que um valor curto que seja prefixo de outro mais
    longo consuma a substituição antes da hora. Endereços locais são descartados
    aqui — assim `--check` e o scrub enxergam exatamente a mesma lista.
    """
    if not env_path.exists():
        return []
    valores: set[str] = set()
    for linha in env_path.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        nome, _, valor = linha.partition("=")
        valor = valor.strip().strip('"').strip("'")
        # Só o que é segredo de verdade: nome com cara de credencial E valor com tamanho
        # de credencial. Valores curtos/config (porta, modelo, "logs") apareceriam em todo
        # lugar dos transcripts e o scrub cego os destruiria (44k substituições numa rodada).
        if not valor or len(valor) < _SECRET_MIN_LEN or not _SECRET_NAME_RE.search(nome.strip()):
            continue
        if _valor_local(valor):
            continue
        valores.add(valor)
    return sorted(valores, key=len, reverse=True)


def carregar_denylist(caminho: Path) -> list[str]:
    """Literais a redigir, um por linha, `#` é comentário. Maiores primeiro.

    O arquivo mora FORA do repo de propósito: ele é a lista do que não pode
    aparecer, copiá-lo para dentro seria publicar exatamente o que se quer
    esconder.
    """
    if not caminho or not Path(caminho).exists():
        return []
    literais = {
        linha.strip()
        for linha in Path(caminho).read_text(encoding="utf-8").splitlines()
        if linha.strip() and not linha.strip().startswith("#")
    }
    return sorted(literais, key=len, reverse=True)


def _prefix_res(segredos: list[str]) -> list[re.Pattern[str]]:
    """Um regex por segredo: token que começa com os primeiros `_PREFIX_MIN` chars dele."""
    out = []
    for s in segredos:
        base = re.sub(r"^https?://", "", s)  # de URL, o que identifica é o host, não o esquema
        if len(base) >= _PREFIX_MIN and not base.startswith(_HOSTS_LOCAIS):
            out.append(re.compile(re.escape(base[:_PREFIX_MIN]) + r"[A-Za-z0-9_.\-]*"))
    return out


def regras_de_caminho(repo: Path, home: str | None = None) -> list[tuple[re.Pattern[str], str]]:
    """Caminhos absolutos da máquina do autor viram marcadores estáveis.

    `home` é injetável para o teste não depender de onde a suíte roda.
    """
    home = home or str(Path.home())
    worktrees = repo.parent / "worktrees"
    regras: list[tuple[re.Pattern[str], str]] = []
    for prefixo in (home, "~"):
        wt = str(worktrees).replace(home, prefixo, 1)
        rp = str(repo).replace(home, prefixo, 1)
        regras.append((re.compile(re.escape(wt) + r"/[\w.\-]+"), "<worktree>"))
        regras.append((re.compile(re.escape(rp)), "<repo>"))
    regras.append((re.compile(r"(?:/private)?/tmp/claude-\d+(?:/[\w.\-/]*)?"), "<scratch>"))
    regras.append((re.compile(re.escape(home) + "/"), "<home>/"))
    return regras


# --------------------------------------------------------------------------- transformação
class Higienizador:
    """Todas as regras de transformação, com o contador do que cada uma pegou."""

    def __init__(
        self,
        segredos: list[str],
        denylist: list[str],
        regras_caminho: list[tuple[re.Pattern[str], str]],
        prefixos_pessoais: tuple[str, ...],
    ) -> None:
        self.segredos = segredos
        self.prefixos_segredo = _prefix_res(segredos)
        self.denylist = denylist
        self.regras_caminho = regras_caminho
        self.prefixos_pessoais = prefixos_pessoais
        self.counts: Counter[str] = Counter()
        self.ids_sensiveis: set[str] = set()

    # ---- texto
    def texto(self, s: str) -> str:
        blob = self._blob(s)
        if blob is not None:
            self.counts["imagem" if blob == MARCA_IMAGEM else "base64"] += 1
            return blob
        if ANCORA_CONFIG_PESSOAL in s:
            self.counts["config_pessoal"] += 1
            return MARCA_PESSOAL
        if self._e_memoria(s):
            self.counts["memoria"] += 1
            return MARCA_PESSOAL

        for literal in self.denylist:
            if literal in s:
                self.counts["denylist"] += s.count(literal)
                s = s.replace(literal, MARCA_DENYLIST)

        for rx, destino in self.regras_caminho:
            s, n = rx.subn(destino, s)
            self.counts["caminho"] += n

        for valor in self.segredos:
            n = s.count(valor)
            if n:
                self.counts["env_secrets"] += n
                s = s.replace(valor, REDACTED)
        for rx in self.prefixos_segredo:
            s, n = rx.subn(REDACTED, s)
            self.counts["secret_prefix"] += n

        s, n = _GOOGLE_KEY_RE.subn(REDACTED, s)
        self.counts["google_api_key"] += n
        s, n = _APIKEY_RE.subn(REDACTED, s)
        self.counts["apikey_pattern"] += n

        self.counts["email"] += len(_EMAIL_COUNT_RE.findall(s))
        return mask_text(s)

    @staticmethod
    def _blob(s: str) -> str | None:
        """`MARCA_IMAGEM` para imagem, `MARCA_BASE64` para o resto do base64 grande.

        Distinguir importa: além das imagens, o transcript carrega assinaturas
        opacas de raciocínio do modelo, também em base64 e também grandes.
        Chamar as duas coisas de "screenshot" seria mentira no artefato.
        """
        if len(s) <= IMAGEM_MIN_BYTES:
            return None
        if s.startswith("data:image/") and ";base64," in s[:100]:
            return MARCA_IMAGEM
        if not _BASE64_RE.match(s):
            return None
        try:
            cabeca = base64.b64decode(s[:64], validate=False)
        except (binascii.Error, ValueError):
            return None
        return MARCA_IMAGEM if cabeca.startswith(_MAGIC_IMAGEM) else MARCA_BASE64

    @staticmethod
    def _e_memoria(s: str) -> bool:
        cabeca = s.lstrip()[:400]
        return cabeca.startswith(ANCORA_MEMORIA_INICIO) and all(c in cabeca for c in ANCORA_MEMORIA_CAMPOS)

    # ---- objeto
    def valor(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.texto(obj)
        if isinstance(obj, list):
            return [self.valor(v) for v in obj]
        if isinstance(obj, dict):
            if obj.get("type") == "image":
                self.counts["imagem"] += 1
                return {"type": "text", "text": MARCA_IMAGEM}
            if obj.get("type") == "tool_result" and obj.get("tool_use_id") in self.ids_sensiveis:
                self.counts["leitura_pessoal"] += 1
                return {**{k: obj[k] for k in obj if k != "content"}, "content": MARCA_PESSOAL}
            # As chaves também carregam texto livre (perguntas viram chave de dicionário)
            # e caminho absoluto; passam pelas mesmas regras.
            return {self.texto(k) if isinstance(k, str) else k: self.valor(v) for k, v in obj.items()}
        return obj

    # ---- linha
    def _caminho_pessoal(self, texto: str) -> bool:
        return any(p in texto for p in self.prefixos_pessoais)

    def _registrar_tool_uses(self, obj: Any) -> None:
        """Guarda o id de leituras de diretório pessoal para redigir o resultado depois."""
        if isinstance(obj, list):
            for v in obj:
                self._registrar_tool_uses(v)
        elif isinstance(obj, dict):
            if obj.get("type") == "tool_use" and isinstance(obj.get("id"), str):
                entrada = obj.get("input") or {}
                alvo = ""
                if isinstance(entrada, dict):
                    alvo = " ".join(str(entrada.get(c, "")) for c in ("file_path", "path", "command", "notebook_path"))
                if self._caminho_pessoal(alvo):
                    self.ids_sensiveis.add(obj["id"])
            for v in obj.values():
                self._registrar_tool_uses(v)

    def _resultado_de_caminho_pessoal(self, obj: dict[str, Any]) -> bool:
        resultado = obj.get("toolUseResult")
        if not isinstance(resultado, dict):
            return False
        alvos = [resultado.get("filePath"), (resultado.get("file") or {}).get("filePath")]
        return any(isinstance(a, str) and self._caminho_pessoal(a) for a in alvos)

    def linha(self, obj: Any) -> Any | None:
        """Transforma um objeto de linha; `None` significa "não exporte esta linha"."""
        if isinstance(obj, dict):
            attachment = obj.get("attachment")
            if isinstance(attachment, dict) and attachment.get("type") == "deferred_tools_delta":
                self.counts["linha_removida"] += 1
                return None
            self._registrar_tool_uses(obj)
            if self._resultado_de_caminho_pessoal(obj):
                self.counts["leitura_pessoal"] += 1
                obj = {**obj, "toolUseResult": MARCA_PESSOAL}
        return self.valor(obj)


# --------------------------------------------------------------------------- export
def exportar_arquivo(src: Path, dest: Path, hig: Higienizador) -> tuple[int, int]:
    """(linhas escritas, linhas descartadas por JSON inválido)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    hig.ids_sensiveis = set()
    escritas = invalidas = 0
    with src.open("r", encoding="utf-8", errors="replace") as fin, dest.open("w", encoding="utf-8") as fout:
        for linha in fin:
            if not linha.strip():
                continue
            try:
                obj = json.loads(linha)
            except json.JSONDecodeError:
                invalidas += 1
                hig.counts["json_invalido"] += 1
                continue
            limpo = hig.linha(obj)
            if limpo is None:
                continue
            fout.write(json.dumps(limpo, ensure_ascii=False) + "\n")
            escritas += 1
    return escritas, invalidas


def export_logs(
    dirs: list[Path],
    dest_root: Path,
    hig: Higienizador,
    destinos: dict[str, str],
    janela: tuple[datetime, datetime],
) -> list[dict[str, Any]]:
    """Exporta cada sessão elegível e devolve uma linha de índice por arquivo.

    Idempotente: `dest_root` é recriada do zero, então rodar de novo com a mesma
    origem produz exatamente a mesma árvore (inclusive sem nomes antigos).
    """
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    index: list[dict[str, Any]] = []
    for origem in dirs:
        destino_nome = destinos.get(origem.name, "outro-workspace")
        for arquivo in sorted(origem.glob("*.jsonl")):
            n_linhas, primeiro, ultimo = _timestamps_e_linhas(arquivo)
            if n_linhas < MIN_LINHAS:
                print(f"[pulada] {destino_nome}/{arquivo.name}: {n_linhas} linha(s), abaixo de {MIN_LINHAS}")
                continue
            if not _dentro_da_janela(primeiro, ultimo, janela):
                print(f"[pulada] {destino_nome}/{arquivo.name}: fora da janela do projeto")
                continue
            destino = dest_root / destino_nome / arquivo.name
            escritas, invalidas = exportar_arquivo(arquivo, destino, hig)
            index.append(
                {
                    "arquivo": f"{destino_nome}/{arquivo.name}",
                    "origem": destino_nome,
                    "inicio": primeiro,
                    "fim": ultimo,
                    "linhas": escritas,
                    "descartadas": invalidas,
                }
            )
            print(f"exportado: {destino_nome}/{arquivo.name}  {escritas} linha(s)")
    return index


def escrever_index(dest_root: Path, index: list[dict[str, Any]], janela: tuple[datetime, datetime]) -> None:
    ini, fim = janela
    linhas = [
        "# Sessões exportadas",
        "",
        "Gerado por `scripts/export_ai_logs.py` — não edite à mão.",
        "",
        f"Janela do projeto: {ini.isoformat()} → {fim.isoformat()}",
        "",
        "| Arquivo | Origem | Primeira mensagem | Última mensagem | Linhas |",
        "|---|---|---|---|---|",
    ]
    for item in sorted(index, key=lambda i: i["arquivo"]):
        linhas.append(
            f"| `{item['arquivo']}` | {item['origem']} | {item['inicio']} | {item['fim']} | {item['linhas']} |"
        )
    total = sum(i["linhas"] for i in index)
    linhas += ["", f"{len(index)} sessão(ões), {total} linha(s) exportada(s)."]
    (dest_root / "INDEX.md").write_text("\n".join(linhas) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- check
def check_logs(ai_logs_dir: Path, segredos: list[str], denylist: list[str] | None = None) -> int:
    """Gate: 1 se achar segredo, literal da denylist, âncora pessoal ou JSON inválido.

    Não escreve nada, só relata. É o que roda antes do push.
    """
    denylist = denylist or []
    achados: list[str] = []
    prefixos = _prefix_res(segredos)
    if not ai_logs_dir.is_dir():
        print(f"{ai_logs_dir} não existe; nada para checar.")
        return 0

    for arquivo in sorted(ai_logs_dir.rglob("*")):
        if not arquivo.is_file():
            continue
        sessao = "sessions" in arquivo.parts and arquivo.suffix == ".jsonl"
        try:
            handle = arquivo.open("r", encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        with handle:
            try:
                for numero, linha in enumerate(handle, start=1):
                    for valor in segredos:
                        if valor and valor in linha:
                            achados.append(f"{arquivo}:{numero}: valor de variável do .env exposto")
                    for rx in prefixos:
                        if rx.search(linha):
                            achados.append(f"{arquivo}:{numero}: prefixo de segredo do .env exposto")
                            break
                    if _GOOGLE_KEY_RE.search(linha):
                        achados.append(f"{arquivo}:{numero}: possível chave Google (AIza...) exposta")
                    if _APIKEY_RE.search(linha):
                        achados.append(f"{arquivo}:{numero}: possível apikey exposta")
                    for literal in denylist:
                        if literal in linha:
                            achados.append(f"{arquivo}:{numero}: literal da denylist exposto")
                            break
                    if ANCORA_CONFIG_PESSOAL in linha:
                        achados.append(f"{arquivo}:{numero}: arquivo pessoal de configuração exposto")
                    if sessao and linha.strip():
                        try:
                            json.loads(linha)
                        except json.JSONDecodeError:
                            achados.append(f"{arquivo}:{numero}: linha não é JSON válido")
            except UnicodeDecodeError:
                continue

    if achados:
        print(f"{len(achados)} problema(s) em {ai_logs_dir}:")
        for item in achados[:50]:
            print(f"  {item}")
        if len(achados) > 50:
            print(f"  ... e mais {len(achados) - 50}")
        return 1

    print(f"{ai_logs_dir} limpo: nenhum segredo, literal da denylist ou linha inválida.")
    return 0


def _imprimir_resumo(index: list[dict[str, Any]], counts: Counter[str]) -> None:
    total = sum(i["linhas"] for i in index)
    print(f"\nsessões exportadas: {len(index)}")
    print(f"linhas escritas: {total}")
    if counts:
        print("regras aplicadas:")
        for regra in sorted(counts):
            print(f"  {regra}: {counts[regra]}")
    else:
        print("regras aplicadas: nenhuma")


def main(argv: list[str] | None = None) -> int:
    repo = _repo_dir()
    workspaces = descobrir_workspaces(repo)
    parser = argparse.ArgumentParser(
        description="Exporta e higieniza os transcripts do Claude Code para ai-logs/sessions/"
    )
    parser.add_argument(
        "--projects-glob",
        action="append",
        default=[],
        help="glob de pasta em ~/.claude/projects (repetível; padrão: workspaces deste repo)",
    )
    parser.add_argument("--projects-dir", type=Path, default=CLAUDE_PROJECTS_DIR, help="raiz das sessões do Claude Code")
    parser.add_argument("--dest", type=Path, default=ROOT / "ai-logs" / "sessions", help="raiz de destino")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="arquivo .env de onde tirar segredos")
    parser.add_argument(
        "--denylist",
        type=Path,
        default=repo.parent / "scrub-denylist.txt",
        help="arquivo (fora do repo) com os literais a redigir",
    )
    parser.add_argument(
        "--check", action="store_true", help="só varre --dir (padrão ai-logs/); sai 1 se achar problema"
    )
    parser.add_argument("--dir", type=Path, default=ROOT / "ai-logs", help="diretório varrido por --check")
    args = parser.parse_args(argv)

    segredos = _load_env_secrets(args.env_file)
    denylist = carregar_denylist(args.denylist)

    if args.check:
        return check_logs(args.dir, segredos, denylist)

    if not denylist:
        print(f"[aviso] denylist vazia ou ausente em {args.denylist}")

    globs = args.projects_glob or globs_padrao(workspaces)
    dirs = dirs_de_origem(args.projects_dir, globs)
    if not dirs:
        print(f"[aviso] nenhuma pasta de sessão encontrada em {args.projects_dir} para {globs}")
        return 0

    janela = janela_do_projeto(repo)
    destinos = mapear_destinos(dirs, workspaces)
    hig = Higienizador(
        segredos=segredos,
        denylist=denylist,
        regras_caminho=regras_de_caminho(repo),
        prefixos_pessoais=(str(Path.home() / ".claude"), str(Path.home() / "Desktop"), "~/.claude", "~/Desktop"),
    )
    index = export_logs(dirs, args.dest, hig, destinos, janela)
    escrever_index(args.dest, index, janela)
    _imprimir_resumo(index, hig.counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
