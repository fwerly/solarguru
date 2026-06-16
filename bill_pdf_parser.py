"""Extrai dados estruturados de PDFs de fatura da Enel RJ.

Os PDFs sao protegidos por senha (geralmente os 5 primeiros digitos do CPF).
Esse parser le com pypdf, extrai os campos principais e retorna um dict
no mesmo formato do bills.json.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader


@dataclass
class FaturaExtraida:
    mes_ano: str                       # "2026-05"
    ciclo_inicio: str                  # "2026-04-12" (ISO)
    ciclo_fim: str                     # "2026-05-12"
    dias: int
    consumo_rede_kwh: int
    injetado_kwh: int
    saldo_creditos_kwh: int            # creditos restantes no proximo mes
    bandeira: str                      # "verde" | "amarela" | "vermelha"
    total_pago: float
    vencimento: str                    # "2026-05-25" (ISO)
    tarifa_te_kwh: float | None = None
    tarifa_tusd_kwh: float | None = None
    tarifa_te_compensada_kwh: float | None = None
    tarifa_tusd_compensada_kwh: float | None = None
    bandeira_kwh: float | None = None
    cip: float | None = None
    consumo_liquido_kwh: int | None = None
    saldo_utilizado_kwh: int | None = None      # creditos usados pra abater este mes
    # itens "outros" extras (DMIC = compensacao por falta de luz, multa, juros)
    dmic: float | None = None                    # negativo = credito a favor do cliente
    multa: float | None = None
    juros: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "mes_ano": self.mes_ano,
            "ciclo_inicio": self.ciclo_inicio,
            "ciclo_fim": self.ciclo_fim,
            "dias": self.dias,
            "consumo_rede_kwh": self.consumo_rede_kwh,
            "injetado_kwh": self.injetado_kwh,
            "saldo_creditos_kwh": self.saldo_creditos_kwh,
            "bandeira": self.bandeira,
            "total_pago": self.total_pago,
            "vencimento": self.vencimento,
        }
        for k in ("tarifa_te_kwh", "tarifa_tusd_kwh", "tarifa_te_compensada_kwh",
                  "tarifa_tusd_compensada_kwh", "bandeira_kwh", "cip",
                  "consumo_liquido_kwh", "saldo_utilizado_kwh",
                  "dmic", "multa", "juros"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


def _ddmmaaaa_to_iso(s: str) -> str:
    """Converte 'DD/MM/AAAA' para ISO 'AAAA-MM-DD'."""
    d, m, y = s.split("/")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def _parse_brl(s: str) -> float:
    """Converte 'R$ 1.234,56' ou '393,09' para float."""
    s = s.replace("R$", "").replace(".", "").replace(",", ".").strip()
    s = s.replace("−", "-")  # menos unicode
    return float(s)


def _find(pattern: str, text: str, flags: int = 0, group: int = 1) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(group) if m else None


def parse_enel_pdf(pdf_path: str | Path, password: str | None = None) -> FaturaExtraida:
    """Le um PDF de fatura Enel RJ e retorna a fatura estruturada.

    pdf_path: caminho do PDF
    password: senha do PDF (geralmente 5 primeiros do CPF). Se None, tenta sem.
    """
    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted:
        if not password:
            raise ValueError("PDF protegido por senha mas nenhuma senha foi informada.")
        result = reader.decrypt(password)
        if not result:
            raise ValueError(f"Senha incorreta para o PDF.")

    # extrai texto de todas as paginas, junto
    text = "\n".join((p.extract_text() or "") for p in reader.pages)

    # === Datas de leitura ===
    # padrao: "LEITURA ANTERIOR" ... "12/03/2026" ... "LEITURA ATUAL" ... "11/04/2026"
    # ou na forma corrida: "12/03/2026 11/04/2026 30 12/05/2026"
    datas = re.findall(r"\b(\d{2}/\d{2}/\d{4})\b", text)
    if len(datas) < 2:
        raise ValueError("Nao consegui identificar as datas de leitura na fatura.")

    # Estrategia: a sequencia (anterior, atual, proxima) costuma aparecer junta.
    # Procura por "LEITURA ANTERIOR" seguido por datas, ou usa as primeiras 2-3.
    bloco_leitura = _find(
        r"LEITURA ANTERIOR[\s\S]{0,200}?(\d{2}/\d{2}/\d{4})[\s\S]{0,80}?(\d{2}/\d{2}/\d{4})[\s\S]{0,80}?(?:(\d+)[\s\S]{0,80}?(\d{2}/\d{2}/\d{4}))?",
        text,
    )
    if bloco_leitura:
        m = re.search(
            r"LEITURA ANTERIOR[\s\S]{0,200}?(\d{2}/\d{2}/\d{4})[\s\S]{0,80}?(\d{2}/\d{2}/\d{4})[\s\S]{0,80}?(\d+)[\s\S]{0,80}?(\d{2}/\d{2}/\d{4})",
            text,
        )
        if m:
            leitura_anterior, leitura_atual, dias_str, proxima = m.groups()
        else:
            leitura_anterior, leitura_atual = datas[0], datas[1]
            dias_str = "30"
    else:
        leitura_anterior, leitura_atual = datas[0], datas[1]
        dias_str = _find(r"\b(\d{2,3})\s*(?=12/|11/|13/|14/)", text) or "30"

    ciclo_inicio_dt = _ddmmaaaa_to_iso(leitura_anterior)
    ciclo_fim_dt = _ddmmaaaa_to_iso(leitura_atual)
    dias = int(dias_str)

    # === Mes/ano da fatura ===
    mes_ano_raw = _find(r"\bMÊS/ANO\b[\s\S]{0,40}?(\d{2})/(\d{4})", text)
    if mes_ano_raw:
        m = re.search(r"\bMÊS/ANO\b[\s\S]{0,40}?(\d{2})/(\d{4})", text)
        mes_ano = f"{m.group(2)}-{m.group(1)}"
    else:
        # usa o mes da leitura atual
        d, m, y = leitura_atual.split("/")
        mes_ano = f"{y}-{m}"

    # === Vencimento ===
    # "Data Vencimento:" (bloco bancario) é o mais confiavel
    venc_raw = _find(r"Data Vencimento[:\s]+(\d{2}/\d{2}/\d{4})", text)
    if not venc_raw:
        # fallback: data DD/MM/YYYY que vem logo apos o MES/ANO no header
        # ex: "04/2026\n22/04/2026"
        m = re.search(r"(?<!\d/)\b\d{2}/\d{4}\b[\s\S]{0,20}?(\d{2}/\d{2}/\d{4})", text)
        if m:
            venc_raw = m.group(1)
    if not venc_raw:
        venc_raw = _find(r"VENCIMENTO[\s\S]{0,40}?(\d{2}/\d{2}/\d{4})", text)
    vencimento = _ddmmaaaa_to_iso(venc_raw) if venc_raw else ciclo_fim_dt

    # === Total a pagar ===
    total_raw = _find(r"TOTAL A PAGAR[\s\S]{0,80}?R\$\s*([\d.,]+)", text)
    if not total_raw:
        total_raw = _find(r"Valor do Documento:[\s\S]{0,40}?R\$\s*([\d.,]+)", text)
    total_pago = _parse_brl(total_raw) if total_raw else 0.0

    # === Consumo e injecao do medidor ===
    # padrao: "ENERGIA ATIVA - KWH HFP 0.00 259.00 1.00 572.00"
    # ou: "Consumo kWh ... 572.00"
    consumo_match = re.search(
        r"ENERGIA ATIVA(?:\s*-\s*KWH)?\s+HFP[\s\S]{0,100}?([\d.]+)\s+(\d+)\s*(?:\.\d+)?\s*(?=N°|\s|$)",
        text,
    )
    injecao_match = re.search(
        r"ENERGIA INJETADA[\s\S]{0,80}?([\d.]+)\s+([\d.]+)[\s\S]{0,40}?([\d.]+)",
        text,
    )

    # Fallback mais robusto: procurar pelos "consumos" do bloco DADOS DE MEDICAO
    # ou pelas linhas "Energia Consumida Faturada" e "Energia Atv Inj"
    consumo_rede = 0
    injetado = 0

    # busca linhas tipo: "Energia Consumida Faturada TE kWh 160 0,46675" -> consumo liquido
    # e "Energia Ativa Fornecida TE kWh 412" -> compensado
    # consumo da rede TOTAL = (faturada + fornecida) ou pode vir direto do medidor
    # mais facil: pegar do bloco DADOS DE MEDICAO ou MES/ANO CONSUMO
    consumo_hist = re.search(
        rf"\b(?:{mes_ano[5:7]})/?(?:{mes_ano[2:4]})\b\s*([\d.]+)\s*\d+\s*(?:LID|LIH)",
        text,
    )
    if consumo_hist:
        consumo_rede = int(float(consumo_hist.group(1)))

    # Tenta tambem pelo padrao do medidor mais recente (HFP sem INJ)
    if consumo_rede == 0:
        # padrao: linhas "Medidor ENERGIA ATIVA - KWH HFP 0.00 259.00 1.00 259.00"
        # mas pode ser repartido entre 2 medidores (troca de medidor)
        # somar TODAS as linhas "ENERGIA ATIVA - KWH" (consumo)
        consumos = re.findall(
            r"ENERGIA ATIVA\s*-\s*KWH\s+HFP\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+([\d.]+)",
            text,
        )
        # cuidado: nao confundir com linha de injetada
        # vamos pegar usando outro contexto
        consumos_v2 = re.findall(
            r"HFP\s+(\d{2}/\d{2}/\d{4})\s+([\d.]+)\s+(\d{2}/\d{2}/\d{4})\s+([\d.]+)\s+[\d.]+\s+([\d.]+)",
            text,
        )
        # consumos_v2: lista de (data_ant, leit_ant, data_atual, leit_atual, consumo)
        for tupla in consumos_v2:
            try:
                consumo_rede += int(float(tupla[4]))
            except ValueError:
                continue

    # Injetada: procura "ENERGIA INJETADA" + valor
    inj_match = re.search(
        r"ENERGIA INJETADA[\s\S]{0,200}?HFP\s*INJ\s+[\d.]+\s+([\d.]+)\s+[\d.]+\s+([\d.]+)",
        text,
    )
    if inj_match:
        injetado = int(float(inj_match.group(2)))
    else:
        # alternativa: "Energia Atv Inj TE mUC ..." e "Energia Atv Inj TUSD ..."
        # ambas tem mesma quantidade kWh; pegar a primeira
        alt = re.search(r"Energia Atv Inj\s+TE[\s\S]{0,80}?kWh\s+([\d.]+)", text)
        if alt:
            injetado = int(float(alt.group(1)))

    # === Saldo de creditos a expirar ===
    saldo = _find(r"Saldo atualizado[: ]*([\d.]+)", text)
    creditos_expirar = _find(r"Cr.ditos a Expirar.*?:\s*([\d.]+)", text)
    saldo_creditos = int(float(saldo)) if saldo else (int(float(creditos_expirar)) if creditos_expirar else 0)

    # === Bandeira ===
    if re.search(r"Bandeira amarela|Adicional Band\.?\s*Amarela", text, re.IGNORECASE):
        bandeira = "amarela"
    elif re.search(r"Bandeira vermelha", text, re.IGNORECASE):
        bandeira = "vermelha"
    else:
        bandeira = "verde"

    # === Tarifas (com tributos) ===
    # padrao: "Energia Ativa Fornecida TE kWh 412 0,46701 192,41"
    # padrao: "Energia Ativa Fornecida TUSD kWh 412 1,03743 427,42"
    te_match = re.search(
        r"Energia Ativa Fornecida TE\s+kWh\s+\d+\s+(\d+,\d+)",
        text,
    )
    tusd_match = re.search(
        r"Energia Ativa Fornecida TUSD\s+kWh\s+\d+\s+(\d+,\d+)",
        text,
    )
    te_comp_match = re.search(
        r"Energia Atv Inj TE[\s\S]{0,80}?kWh\s+\d+\s+-?(\d+,\d+)",
        text,
    )
    tusd_comp_match = re.search(
        r"Energia Atv Inj TUSD[\s\S]{0,80}?kWh\s+\d+\s+-?(\d+,\d+)",
        text,
    )
    band_match = re.search(
        r"Adicional Band\.?\s*Amarela\s+kWh\s+\d+\s+(\d+,\d+)",
        text,
    )
    cip_match = re.search(
        r"CIP\s*-\s*ILUM(?:INACAO)?\s*P[UÚ]B[\s\S]{0,80}?(\d+,\d+)",
        text,
        re.IGNORECASE,
    )

    def pf(m):  # parse-float helper
        return float(m.group(1).replace(",", ".")) if m else None

    # === Consumo liquido ===
    consumo_liq = _find(r"Energia Consumida Faturada TE\s+kWh\s+(\d+)", text)
    consumo_liq_int = int(consumo_liq) if consumo_liq else None

    # === Saldo de creditos utilizado neste mes ===
    saldo_util = _find(r"Saldo utilizado no m[eê]s[: ]*([\d.]+)", text)
    saldo_util_int = int(float(saldo_util)) if saldo_util else None

    # === Itens "outros" (valores que podem ter sufixo "-" = credito) ===
    def parse_signed(label_pattern):
        """Captura valor que pode ter '-' depois (formato Enel: '149,39-')."""
        m = re.search(label_pattern + r"\s+(\d[\d.]*,\d+)(-?)", text)
        if not m:
            return None
        val = float(m.group(1).replace(".", "").replace(",", "."))
        if m.group(2) == "-":  # sufixo negativo = credito a favor
            val = -val
        return val

    # DMIC = compensacao por interrupcao de energia (geralmente credito negativo)
    dmic = parse_signed(r"DMIC")
    multa = parse_signed(r"Multa")
    juros = parse_signed(r"Juros Morat[oó]rios")

    return FaturaExtraida(
        mes_ano=mes_ano,
        ciclo_inicio=ciclo_inicio_dt,
        ciclo_fim=ciclo_fim_dt,
        dias=dias,
        consumo_rede_kwh=consumo_rede,
        injetado_kwh=injetado,
        saldo_creditos_kwh=saldo_creditos,
        bandeira=bandeira,
        total_pago=total_pago,
        vencimento=vencimento,
        tarifa_te_kwh=pf(te_match),
        tarifa_tusd_kwh=pf(tusd_match),
        tarifa_te_compensada_kwh=pf(te_comp_match),
        tarifa_tusd_compensada_kwh=pf(tusd_comp_match),
        bandeira_kwh=pf(band_match),
        cip=pf(cip_match),
        consumo_liquido_kwh=consumo_liq_int,
        saldo_utilizado_kwh=saldo_util_int,
        dmic=dmic,
        multa=multa,
        juros=juros,
    )


def add_bill_to_json(fatura: FaturaExtraida, bills_path: str | Path | None = None) -> bool:
    """Adiciona ou atualiza uma fatura no bills.json. Retorna True se foi nova."""
    p = Path(bills_path) if bills_path else Path(__file__).parent / "bills.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = {"bills": []}

    bills = data.get("bills", [])
    nova = fatura.to_dict()
    # remove duplicata (mesmo mes_ano) e adiciona a nova
    novo_bills = [b for b in bills if b.get("mes_ano") != nova["mes_ano"]]
    foi_nova = len(novo_bills) == len(bills)
    novo_bills.append(nova)
    novo_bills.sort(key=lambda b: b.get("mes_ano", ""))
    data["bills"] = novo_bills

    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return foi_nova
