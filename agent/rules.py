"""Regras de negócio locais, derivadas de `GET /planos` — nunca hardcoded.

A API é a fonte de verdade das faixas (idade, ano do veículo); aqui só
replicamos os limites para dar feedback rápido ao lead ANTES de gastar uma
chamada de cotação. Se `plans.json` mudar, `Rules.from_planos` acompanha.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from agent.models import PlanoResumo, QuoteRequest, Violation

_CEP_TEXT_RE = re.compile(r"(\d{5})[\s.-]?(\d{3})")


@dataclass(frozen=True)
class Rules:
    planos: dict
    today: date
    idade_min: int
    idade_max: int
    ano_min: int
    ano_max: int

    @classmethod
    def from_planos(cls, planos: dict, today: date) -> Rules:
        """Deriva idade_min/max e ano_min/max das faixas SEM `recusar` do JSON."""
        regras = planos["regras"]

        faixas_idade = [f for f in regras["faixa_etaria"] if not f.get("recusar")]
        idade_min = min(f["idade_min"] for f in faixas_idade)
        idade_max = max(f["idade_max"] for f in faixas_idade)

        faixas_veiculo = [f for f in regras["idade_veiculo"] if not f.get("recusar")]
        anos_max = max(f["anos_max"] for f in faixas_veiculo)
        ano_min = today.year - anos_max
        ano_max = today.year

        return cls(
            planos=planos,
            today=today,
            idade_min=idade_min,
            idade_max=idade_max,
            ano_min=ano_min,
            ano_max=ano_max,
        )

    def validate_idade(self, idade: int) -> Violation | None:
        if idade < self.idade_min or idade > self.idade_max:
            return Violation(
                campo="idade",
                tipo="fora_da_faixa",
                motivo=f"Só conseguimos cotar para condutores de {self.idade_min} a {self.idade_max} anos.",
            )
        return None

    def validate_veiculo_ano(self, ano: int) -> Violation | None:
        if ano > self.ano_max:
            return Violation(
                campo="veiculo_ano",
                tipo="futuro",
                motivo="Esse ano parece ser o ano-modelo; me diz o ano de fabricação do veículo?",
            )
        if ano < self.ano_min:
            return Violation(
                campo="veiculo_ano",
                tipo="fora_da_faixa",
                motivo=f"Só conseguimos cotar veículos fabricados a partir de {self.ano_min}.",
            )
        return None

    def validate_data_inicio(self, d: date) -> Violation | None:
        if d < self.today:
            return Violation(
                campo="data_inicio",
                tipo="passado",
                motivo="A data de início não pode ser no passado.",
            )
        return None

    def normalize_cep(self, texto: str) -> str | None:
        """Extrai 8 dígitos de CEP de texto livre; None se não achar."""
        if not texto:
            return None
        m = _CEP_TEXT_RE.search(texto)
        if not m:
            return None
        return m.group(1) + m.group(2)

    def validate_request(self, req: QuoteRequest) -> list[Violation]:
        """Agrega as violações de um `QuoteRequest` já montado (última checagem antes de cotar)."""
        violacoes: list[Violation] = []

        if v := self.validate_idade(req.idade):
            violacoes.append(v)
        if v := self.validate_veiculo_ano(req.veiculo_ano):
            violacoes.append(v)
        if req.cep is not None and (len(req.cep) != 8 or not req.cep.isdigit()):
            violacoes.append(
                Violation(campo="cep", tipo="formato", motivo="Esse CEP não parece válido; pode conferir?")
            )
        try:
            data_inicio = date.fromisoformat(req.data_inicio)
        except ValueError:
            violacoes.append(
                Violation(campo="data_inicio", tipo="formato", motivo="Essa data de início não é válida.")
            )
        else:
            if v := self.validate_data_inicio(data_inicio):
                violacoes.append(v)

        return violacoes

    def planos_resumo(self) -> list[PlanoResumo]:
        """Lista os planos na ordem do JSON, para apresentar ao lead."""
        return [
            PlanoResumo(id=p["id"], nome=p["nome"], franquia=p["franquia"], coberturas=p["coberturas"])
            for p in self.planos["planos"]
        ]
