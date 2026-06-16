"""Analise e simulacao de faturas Enel RJ com compensacao solar (Lei 14.300).

Decompoe o valor pago em parcelas didaticas e simula como ficaria a conta
sem o sistema solar.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TarifaConfig:
    te_kwh: float                  # R$/kWh TE com impostos
    tusd_kwh: float                # R$/kWh TUSD com impostos
    te_compensada_kwh: float       # R$/kWh TE quando energia e compensada
    tusd_compensada_kwh: float     # R$/kWh TUSD compensada (Fio B retido)
    bandeira_kwh: float            # adicional bandeira (R$/kWh)
    cip_mensal: float              # iluminacao publica fixa

    @property
    def tarifa_cheia(self) -> float:
        """TE + TUSD sem bandeira."""
        return self.te_kwh + self.tusd_kwh

    @property
    def fio_b_kwh(self) -> float:
        """Pedagio cobrado em cada kWh injetado (Lei 14.300)."""
        return (self.te_kwh - self.te_compensada_kwh) + (self.tusd_kwh - self.tusd_compensada_kwh)


@dataclass
class FaturaBreakdown:
    """Decomposicao didatica de uma fatura."""
    consumo_rede_kwh: float        # o que o medidor de consumo registrou
    injetado_kwh: float            # o que o medidor de injecao registrou
    consumo_liquido_kwh: float     # consumo_rede - injetado (max 0)
    creditos_usados_kwh: float     # min(consumo_rede, injetado)
    geracao_estimada_kwh: float    # injetado + autoconsumo_estimado (se fornecido)
    autoconsumo_kwh: float         # geracao - injetado

    # valores R$
    valor_consumo_liquido: float    # consumo_liquido × tarifa cheia
    valor_fio_b: float              # creditos_usados × fio_b_kwh
    valor_bandeira: float           # consumo_liquido × bandeira_kwh
    valor_cip: float                # CIP fixo
    total: float                    # soma de tudo

    # comparativos
    valor_sem_solar: float          # custo se nao tivesse solar (estima)
    economia: float                 # sem_solar - total

    @property
    def percentual_economia(self) -> float:
        if self.valor_sem_solar <= 0:
            return 0.0
        return (self.economia / self.valor_sem_solar) * 100


def calcular_fatura(
    consumo_rede_kwh: float,
    injetado_kwh: float,
    tarifa: TarifaConfig,
    geracao_total_kwh: float | None = None,
) -> FaturaBreakdown:
    """Calcula a fatura completa a partir das leituras do medidor.

    consumo_rede_kwh: kWh consumidos da rede (medidor de consumo)
    injetado_kwh: kWh injetados na rede (medidor de injecao)
    geracao_total_kwh: opcional, geracao total do inversor. Se nao informado,
        autoconsumo = 0 e geracao = injetado.
    """
    # parte do consumo que e compensada pelos creditos
    creditos_usados = min(consumo_rede_kwh, injetado_kwh)
    consumo_liquido = max(0, consumo_rede_kwh - injetado_kwh)

    # autoconsumo (energia gerada e consumida na hora, nem vai pra rede)
    geracao = geracao_total_kwh if geracao_total_kwh is not None else injetado_kwh
    autoconsumo = max(0, geracao - injetado_kwh)

    # valores
    valor_consumo_liquido = consumo_liquido * tarifa.tarifa_cheia
    valor_fio_b = creditos_usados * tarifa.fio_b_kwh
    # bandeira incide sobre consumo liquido (compensacao zera bandeira do que e compensado)
    valor_bandeira = consumo_liquido * tarifa.bandeira_kwh
    valor_cip = tarifa.cip_mensal

    total = valor_consumo_liquido + valor_fio_b + valor_bandeira + valor_cip

    # sem solar = tudo da casa (consumo + autoconsumo) pagaria tarifa cheia
    consumo_total_casa = consumo_rede_kwh + autoconsumo
    valor_sem_solar = (
        consumo_total_casa * (tarifa.tarifa_cheia + tarifa.bandeira_kwh)
        + tarifa.cip_mensal
    )
    economia = valor_sem_solar - total

    return FaturaBreakdown(
        consumo_rede_kwh=consumo_rede_kwh,
        injetado_kwh=injetado_kwh,
        consumo_liquido_kwh=consumo_liquido,
        creditos_usados_kwh=creditos_usados,
        geracao_estimada_kwh=geracao,
        autoconsumo_kwh=autoconsumo,
        valor_consumo_liquido=valor_consumo_liquido,
        valor_fio_b=valor_fio_b,
        valor_bandeira=valor_bandeira,
        valor_cip=valor_cip,
        total=total,
        valor_sem_solar=valor_sem_solar,
        economia=economia,
    )


def estimar_proxima_conta(
    geracao_atual_kwh: float,
    geracao_projetada_kwh: float,
    consumo_estimado_kwh: float,
    pct_autoconsumo: float,
    tarifa: TarifaConfig,
) -> FaturaBreakdown:
    """Projeta a proxima fatura.

    pct_autoconsumo: 0-1, fracao da geracao que e consumida na hora (resto vai pra rede)
    consumo_estimado_kwh: consumo total da casa no ciclo (estimado pelo historico)
    """
    autoconsumo = geracao_projetada_kwh * pct_autoconsumo
    injetado = geracao_projetada_kwh * (1 - pct_autoconsumo)
    consumo_da_rede = max(0, consumo_estimado_kwh - autoconsumo)

    return calcular_fatura(
        consumo_rede_kwh=consumo_da_rede,
        injetado_kwh=injetado,
        tarifa=tarifa,
        geracao_total_kwh=geracao_projetada_kwh,
    )


def load_bills(path: str | Path | None = None) -> list[dict]:
    """Carrega historico de faturas do bills.json."""
    p = Path(path) if path else Path(__file__).parent / "bills.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("bills", [])
    except Exception:
        return []


def consumo_medio_historico(bills: list[dict], n_meses: int = 6) -> float:
    """Media de consumo da rede dos ultimos n meses (kWh/mes)."""
    if not bills:
        return 0.0
    recentes = sorted(bills, key=lambda b: b.get("mes_ano", ""), reverse=True)[:n_meses]
    valores = [b.get("consumo_rede_kwh", 0) for b in recentes if b.get("consumo_rede_kwh")]
    return sum(valores) / len(valores) if valores else 0.0
