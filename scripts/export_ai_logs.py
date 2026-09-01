"""Exporta os transcripts do Claude Code para `ai-logs/sessions/`, higienizados.

Transparência de uso de IA é exigência do desafio: os transcripts do orquestrador e
dos executores (executores, cada um numa role própria) precisam ficar
no repo, mas nunca com segredo — daí o scrub linha a linha (os `.jsonl` de
sessão podem ser grandes; não carregamos o arquivo inteiro em memória).

Uso:
    uv run python scripts/export_ai_logs.py [--src DIR ...] [--dest DIR] [--env-file ARQ]
    uv run python scripts/export_ai_logs.py --check [--dir ai-logs/]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.pii import mask_text

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
MAESTRO_DIR = CLAUDE_PROJECTS_DIR / "-workspace-autoseguro-agent"
EXECUTOR_DIRS_GLOB = "-workspace-autoseguro-agent--exec-roles-*"

_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{30,}")
_APIKEY_RE = re.compile(r"apikey[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}", re.IGNORECASE)
_EMAIL_COUNT_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

REDACTED = "<REDACTED>"


def _default_source_dirs() -> list[Path]:
    """Pasta do orquestrador + uma por executor (cada recruta do Orq tem a sua)."""
    dirs = []
    if MAESTRO_DIR.is_dir():
        dirs.append(MAESTRO_DIR)
    if CLAUDE_PROJECTS_DIR.is_dir():
        dirs.extend(sorted(CLAUDE_PROJECTS_DIR.glob(EXECUTOR_DIRS_GLOB)))
    return dirs


def _load_env_secrets(env_path: Path) -> list[str]:
    """Valores (não as chaves) de todas as variáveis do `.env`, maiores primeiro.

    Maiores primeiro evita que um valor curto que seja prefixo de outro mais
    longo consuma a substituição antes da hora.
    """
    if not env_path.exists():
        return []
    valores: set[str] = set()
    for linha in env_path.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        _, _, valor = linha.partition("=")
        valor = valor.strip().strip('"').strip("'")
        if valor:
            valores.add(valor)
    return sorted(valores, key=len, reverse=True)


def _scrub_line(linha: str, segredos: list[str], counts: Counter[str]) -> str:
    """Aplica os 4 padrões do brief nesta ordem: valores do .env, chave Google,
    padrão apikey, e-mail (via `agent.pii.mask_text`, que de brinde também
    mascara CPF/telefone/placa/CEP presentes na linha)."""
    for valor in segredos:
        n = linha.count(valor)
        if n:
            counts["env_secrets"] += n
            linha = linha.replace(valor, REDACTED)

    linha, n = _GOOGLE_KEY_RE.subn(REDACTED, linha)
    counts["google_api_key"] += n

    linha, n = _APIKEY_RE.subn(REDACTED, linha)
    counts["apikey_pattern"] += n

    counts["email"] += len(_EMAIL_COUNT_RE.findall(linha))
    linha = mask_text(linha)

    return linha


def _export_file(src: Path, dest: Path, segredos: list[str], counts: Counter[str]) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    bytes_out = 0
    with src.open("r", encoding="utf-8", errors="replace") as fin, dest.open("w", encoding="utf-8") as fout:
        for linha in fin:
            limpa = _scrub_line(linha, segredos, counts)
            fout.write(limpa)
            bytes_out += len(limpa.encode("utf-8"))
    return bytes_out


def export_logs(dirs: list[Path], dest_root: Path, segredos: list[str]) -> tuple[int, int, Counter[str]]:
    """Copia `*.jsonl` de cada diretório em `dirs` para `dest_root/<nome-da-pasta>/`.

    Idempotente: cada arquivo é reescrito do zero (`open("w")`), rodar de novo
    com a mesma origem produz o mesmo resultado.
    """
    counts: Counter[str] = Counter()
    arquivos = 0
    bytes_total = 0

    for origem in dirs:
        if not origem.is_dir():
            print(f"[aviso] origem não encontrada, pulando: {origem}")
            continue
        for arquivo in sorted(origem.glob("*.jsonl")):
            destino = dest_root / origem.name / arquivo.name
            bytes_total += _export_file(arquivo, destino, segredos, counts)
            arquivos += 1
            print(f"copiado: {arquivo} -> {destino}")

    return arquivos, bytes_total, counts


def check_logs(ai_logs_dir: Path, segredos: list[str]) -> int:
    """Varre `ai_logs_dir` já existente à procura de segredo; 1 se achar, 0 se limpo.

    Vira gate do orquestrador antes do push — não escreve nada, só relata.
    """
    achados: list[str] = []
    if not ai_logs_dir.is_dir():
        print(f"{ai_logs_dir} não existe; nada para checar.")
        return 0

    for arquivo in sorted(ai_logs_dir.rglob("*")):
        if not arquivo.is_file():
            continue
        try:
            texto = arquivo.open("r", encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        with texto:
            try:
                for numero, linha in enumerate(texto, start=1):
                    for valor in segredos:
                        if valor and valor in linha:
                            achados.append(f"{arquivo}:{numero}: valor de variável do .env exposto")
                    if _GOOGLE_KEY_RE.search(linha):
                        achados.append(f"{arquivo}:{numero}: possível chave Google (AIza...) exposta")
                    if _APIKEY_RE.search(linha):
                        achados.append(f"{arquivo}:{numero}: possível apikey exposta")
            except UnicodeDecodeError:
                continue

    if achados:
        print("segredos encontrados em ai-logs/:")
        for item in achados:
            print(f"  {item}")
        return 1

    print(f"{ai_logs_dir} limpo: nenhum segredo encontrado.")
    return 0


def _imprimir_resumo(arquivos: int, bytes_total: int, counts: Counter[str]) -> None:
    print(f"\narquivos copiados: {arquivos}")
    print(f"bytes escritos: {bytes_total}")
    if counts:
        print("substituições por padrão:")
        for padrao in sorted(counts):
            print(f"  {padrao}: {counts[padrao]}")
    else:
        print("substituições por padrão: nenhuma")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exporta e higieniza os transcripts do Claude Code para ai-logs/sessions/"
    )
    parser.add_argument(
        "--src", action="append", default=[], type=Path, help="diretório extra de origem (procura *.jsonl nele)"
    )
    parser.add_argument("--dest", type=Path, default=ROOT / "ai-logs" / "sessions", help="raiz de destino")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="arquivo .env de onde tirar segredos")
    parser.add_argument(
        "--check", action="store_true", help="só varre --dir (padrão ai-logs/) por segredo; sai 1 se achar"
    )
    parser.add_argument("--dir", type=Path, default=ROOT / "ai-logs", help="diretório varrido por --check")
    args = parser.parse_args(argv)

    segredos = _load_env_secrets(args.env_file)

    if args.check:
        return check_logs(args.dir, segredos)

    dirs = _default_source_dirs() + list(args.src)
    arquivos, bytes_total, counts = export_logs(dirs, args.dest, segredos)
    _imprimir_resumo(arquivos, bytes_total, counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
