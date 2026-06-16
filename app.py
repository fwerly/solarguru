"""SolarGuru - renderiza o template HTML (Claude Design) com dados reais.

Foco: consumo, graficos e explicacao didatica da conta. Sem previsoes de valor.

Como rodar:
    streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from bill_analyzer import TarifaConfig, load_bills
from bill_pdf_parser import add_bill_to_json, parse_enel_pdf
from growatt_client import GrowattClient, Inverter

load_dotenv(Path(__file__).parent / ".env")

st.set_page_config(
    page_title="SolarGuru",
    page_icon="☀️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        html, body, .stApp, [class*="css"] {
            font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif !important;
            -webkit-font-smoothing: antialiased;
        }
        /* esconde toda a moldura do Streamlit (barra branca do topo, menu, rodape) */
        header[data-testid="stHeader"], [data-testid="stHeader"], .stApp > header { display: none !important; }
        [data-testid="stDecoration"] { display: none !important; }
        [data-testid="stToolbar"] { display: none !important; }
        [data-testid="stStatusWidget"] { display: none !important; }
        #MainMenu { visibility: hidden; }
        footer { display: none !important; }
        .block-container { padding: 0 !important; max-width: 100% !important; }
        .stApp { margin-top: 0 !important; }
    </style>
    """,
    unsafe_allow_html=True,
)


# =============================================================================
# HELPERS
# =============================================================================

DIAS_PT = {0: "Segunda-feira", 1: "Terça-feira", 2: "Quarta-feira", 3: "Quinta-feira",
           4: "Sexta-feira", 5: "Sábado", 6: "Domingo"}
DIAS_PT_SHORT = {0: "seg", 1: "ter", 2: "qua", 3: "qui", 4: "sex", 5: "sáb", 6: "dom"}
MESES_PT = {1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril", 5: "maio", 6: "junho",
            7: "julho", 8: "agosto", 9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro"}
MESES_ABBR = {1: "jan", 2: "fev", 3: "mar", 4: "abr", 5: "mai", 6: "jun",
              7: "jul", 8: "ago", 9: "set", 10: "out", 11: "nov", 12: "dez"}


def fmt_brl(v: float) -> str:
    s = f"R$ {abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("− " + s) if v < 0 else s


def fmt_num(v: float, decimals: int = 1) -> str:
    return f"{v:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def to_float(v, default=0.0) -> float:
    if isinstance(v, str):
        v = v.replace("kWh", "").replace("W", "").replace("R$", "").strip()
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return default


def saudacao(hora: int) -> str:
    return "Bom dia" if hora < 12 else ("Boa tarde" if hora < 18 else "Boa noite")


def cfg(key: str, default=None):
    """Lê configuração: st.secrets (nuvem/Streamlit Cloud) primeiro, depois .env (local)."""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


def label_mes(mes_ano: str) -> str:
    """'2026-06' -> 'jun/26'."""
    try:
        y, m = mes_ano.split("-")
        return f"{MESES_ABBR[int(m)]}/{y[2:]}"
    except Exception:
        return mes_ano


# =============================================================================
# DATA LAYER (resiliente — nunca deixa a tela quebrar)
# =============================================================================

@st.cache_resource
def get_client() -> GrowattClient | None:
    user = cfg("GROWATT_USERNAME")
    pw = cfg("GROWATT_PASSWORD")
    if not user or not pw:
        return None
    try:
        c = GrowattClient(user, pw)
        c.login()
        return c
    except Exception:
        return None


def safe(fn, default):
    """Executa fn() e retorna default se qualquer coisa falhar."""
    try:
        r = fn()
        return r if r is not None else default
    except Exception:
        return default


@st.cache_data(ttl=60)
def load_plants(_c):
    return safe(lambda: _c.list_plants(), [])


@st.cache_data(ttl=30)
def load_realtime(_c, _sn, _tlx, _pid):
    inv = Inverter(serial=_sn, plant_id=_pid, alias="", model="", is_tlx=_tlx)
    return safe(lambda: _c.realtime(inv), {})


@st.cache_data(ttl=30)
def load_summary(_c, pid):
    return safe(lambda: _c.plant_summary(pid), {})


@st.cache_data(ttl=60)
def load_day_curve(_c, _sn, _tlx, _pid, date_iso):
    inv = Inverter(serial=_sn, plant_id=_pid, alias="", model="", is_tlx=_tlx)
    return safe(lambda: _c.day_curve(inv, dt.date.fromisoformat(date_iso)), {})


@st.cache_data(ttl=300)
def load_cycle(_c, pid, dia, today_iso):
    return safe(lambda: _c.cycle_daily_history(pid, dia, dt.date.fromisoformat(today_iso)),
                ({}, dt.date.fromisoformat(today_iso), dt.date.fromisoformat(today_iso)))


# =============================================================================
# UPLOAD DE FATURA
# =============================================================================

def render_upload_section():
    with st.expander("📄 Importar fatura da Enel (PDF)", expanded=False):
        st.markdown(
            "Arraste o **PDF da sua fatura** Enel RJ. O programa lê tudo automaticamente "
            "(consumo, injeção, créditos, tarifas) e atualiza a explicação da conta."
        )
        uploaded = st.file_uploader(
            "Selecione o PDF", type=["pdf"], label_visibility="collapsed", accept_multiple_files=True,
        )
        if uploaded:
            password = cfg("ENEL_PDF_PASSWORD", "")
            for upl in uploaded:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(upl.read())
                    tmp_path = tmp.name
                try:
                    fatura = parse_enel_pdf(tmp_path, password=password)
                    foi_nova = add_bill_to_json(fatura)
                    tag = "✅ Importada" if foi_nova else "🔁 Atualizada"
                    st.success(
                        f"{tag}: **{fatura.mes_ano}** · consumiu {fatura.consumo_rede_kwh} kWh, "
                        f"injetou {fatura.injetado_kwh} kWh · pagou {fmt_brl(fatura.total_pago)}"
                    )
                except Exception as e:
                    st.error(f"❌ Erro lendo {upl.name}: {e}")
                finally:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
            st.cache_data.clear()

        bills = load_bills()
        if bills:
            st.caption(f"{len(bills)} faturas no histórico · "
                       + " · ".join(label_mes(b["mes_ano"]) for b in sorted(bills, key=lambda x: x["mes_ano"])[-6:]))


# =============================================================================
# TEMPLATE
# =============================================================================

TEMPLATE_PATH = Path(__file__).parent / "dashboard_template.html"


def build_template_vars() -> dict:
    now = dt.datetime.now()
    today = now.date()
    hora_atual = now.hour + now.minute / 60

    # --- Tarifa atual ---
    tarifa = TarifaConfig(
        te_kwh=to_float(cfg("TARIFA_TE_KWH", "0.46701"), 0.46701),
        tusd_kwh=to_float(cfg("TARIFA_TUSD_KWH", "1.03743"), 1.03743),
        te_compensada_kwh=to_float(cfg("TARIFA_TE_COMPENSADA_KWH", "0.46697"), 0.46697),
        tusd_compensada_kwh=to_float(cfg("TARIFA_TUSD_COMPENSADA_KWH", "0.78845"), 0.78845),
        bandeira_kwh=to_float(cfg(f"BANDEIRA_{str(cfg('BANDEIRA_ATUAL', 'AMARELA')).upper()}_KWH", "0.01028"), 0.01028),
        cip_mensal=to_float(cfg("CIP_MENSAL", "48.20"), 48.20),
    )
    dia_fechamento = int(to_float(cfg("DIA_FECHAMENTO_CICLO", "11"), 11))
    bandeira_nome = str(cfg("BANDEIRA_ATUAL", "amarela")).lower()
    nome_user = cfg("NOME_USUARIO", "Frederico")
    concessionaria = cfg("CONCESSIONARIA", "Enel RJ")

    # --- API (resiliente) ---
    client = get_client()
    api_ok = client is not None
    plants = load_plants(client) if api_ok else []
    api_ok = api_ok and bool(plants)

    if api_ok:
        plant_id = str(plants[0]["plantId"])
        plant_name = plants[0].get("plantName", "Planta")
        inverters = safe(lambda: client.list_inverters(plant_id), [])
        inv = inverters[0] if inverters else Inverter("—", plant_id, "—", "MIN-TLX", True)
        rt = load_realtime(client, inv.serial, inv.is_tlx, plant_id)
        summary = load_summary(client, plant_id)
    else:
        plant_id, plant_name = "—", "SolarGuru"
        inv = Inverter("—", "—", "—", "MIN-TLX", True)
        rt, summary = {}, {}

    pac_w = to_float(first(rt, "pac", default=0)) or to_float(summary.get("currentPower", "0"))
    today_kwh = to_float(first(rt, "eToday", default=0)) or to_float(summary.get("todayEnergy", "0"))
    total_kwh = to_float(first(rt, "eTotal", default=0)) or to_float(summary.get("totalEnergy", "0"))
    status_raw = str(first(rt, "status", default="")).lower()
    is_online = status_raw in {"1", "normal", "online"} or pac_w > 0
    is_generating = pac_w > 100
    temp = to_float(first(rt, "temperature", default=0))

    pv1_v = to_float(first(rt, "vpv1", default=0)); pv1_a = to_float(first(rt, "ipv1", default=0))
    pv1_w = to_float(first(rt, "ppv1", default=pv1_v * pv1_a))
    pv2_v = to_float(first(rt, "vpv2", default=0)); pv2_a = to_float(first(rt, "ipv2", default=0))
    pv2_w = to_float(first(rt, "ppv2", default=pv2_v * pv2_a))
    vac = to_float(first(rt, "vac1", default=0)); iac = to_float(first(rt, "iac1", default=0))
    fac = to_float(first(rt, "fac", default=0))

    # --- Ciclo ---
    daily_map, ciclo_inicio, ciclo_fim = load_cycle(client, plant_id, dia_fechamento, today.isoformat()) if api_ok else ({}, today, today)
    ciclo_kwh = sum(daily_map.values()) if daily_map else 0
    ciclo_dias_passados = max(1, (today - ciclo_inicio).days + 1)
    ciclo_dias_total = max(1, (ciclo_fim - ciclo_inicio).days + 1)
    ciclo_dias_left = max(0, (ciclo_fim - today).days)
    ciclo_progress_pct = min(100, (ciclo_dias_passados / ciclo_dias_total) * 100)
    dias_geracao = sum(1 for v in daily_map.values() if v > 0) if daily_map else 0
    media_diaria = ciclo_kwh / dias_geracao if dias_geracao else 0
    projecao_ciclo = media_diaria * ciclo_dias_total

    # --- Curva do dia ---
    curve = load_day_curve(client, inv.serial, inv.is_tlx, plant_id, today.isoformat()) if api_ok else {}
    real_points = []
    for hm, watts in sorted(curve.items()):
        try:
            h, m = hm.split(":")
            real_points.append([round(int(h) + int(m) / 60, 3), round(float(watts) / 1000, 3)])
        except (ValueError, AttributeError):
            continue
    real_points = [p for p in real_points if p[0] <= hora_atual + 0.1]
    proj_points = []
    if hora_atual < 17.8:
        steps = int((17.8 - hora_atual) / 0.25) + 1
        for i in range(steps + 1):
            h = hora_atual + i * 0.25
            kw = 5.4 * math.exp(-((h - 12.75) / 2.55) ** 2)
            if h > 17.2:
                kw *= max(0, (17.8 - h) / 0.6)
            proj_points.append([round(h, 3), round(max(0, kw), 3)])
        proj_points.append([24, 0])
    now_kw = real_points[-1][1] if real_points else 0
    if real_points:
        peak = max(real_points, key=lambda p: p[1])
        ph, pm = int(peak[0]), int((peak[0] - int(peak[0])) * 60)
        peak_str = f"{fmt_num(peak[1], 2)} kW"
        peak_time = f"{ph:02d}:{pm:02d}"
    else:
        peak_str, peak_time = "—", "—"
    energia_dia = (sum(p[1] for p in real_points) * 0.083) if real_points else today_kwh

    # --- Sparkline (viewBox 320 wide) ---
    SW = 320
    day_bars = []
    d = ciclo_inicio
    while d <= min(ciclo_fim, today):
        day_bars.append([d.strftime("%d/%m"), round(daily_map.get(d.isoformat(), 0.0), 1)])
        d += dt.timedelta(days=1)
    if day_bars and len(day_bars) > 1:
        n = len(day_bars)
        mx = max((v for _, v in day_bars), default=1) or 1
        pts = []
        for i, (_, v) in enumerate(day_bars):
            x = (i / (n - 1)) * (SW * 0.88)
            y = 36 - 4 - (v / mx) * (36 - 8)
            pts.append((x, y))
        line_path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        area_path = line_path + f" L{pts[-1][0]:.1f},36 L0,36 Z"
        dot_x, dot_y = pts[-1]
        proj_y = 36 - 4 - (media_diaria / mx) * (36 - 8)
        proj_path = f"M{dot_x:.1f},{dot_y:.1f} L{SW:.1f},{proj_y:.1f}"
    else:
        line_path = area_path = proj_path = ""
        dot_x = dot_y = 0

    # =====================================================================
    # CONTA EXPLICADA (a estrela) — usa dados REAIS extraidos do PDF
    # =====================================================================
    bills = load_bills()
    ultima = sorted(bills, key=lambda b: b["mes_ano"], reverse=True)[0] if bills else None

    bill_vars = {}
    if ultima:
        y, m = ultima["mes_ano"].split("-")
        bill_vars["LAST_BILL_MONTH"] = f"{MESES_PT[int(m)]}/{y[2:]}"
        ci = dt.date.fromisoformat(ultima["ciclo_inicio"])
        cf = dt.date.fromisoformat(ultima["ciclo_fim"])
        bill_vars["LAST_CYCLE_RANGE"] = f"{ci.strftime('%d/%m')} a {cf.strftime('%d/%m')}"
        bill_vars["LAST_BILL_DUE_FULL"] = dt.date.fromisoformat(ultima["vencimento"]).strftime("%d/%m/%Y")
        total_pago = ultima["total_pago"]
        bill_vars["LAST_BILL_PAID"] = fmt_brl(total_pago)

        consumed = ultima.get("consumo_rede_kwh", 0)
        injected = ultima.get("injetado_kwh", 0)
        net = ultima.get("consumo_liquido_kwh") or max(0, consumed - injected)
        used = ultima.get("saldo_utilizado_kwh") or min(consumed, injected)
        banked = ultima.get("saldo_creditos_kwh", 0)

        # tarifa da fatura
        te = ultima.get("tarifa_te_kwh", tarifa.te_kwh)
        tusd = ultima.get("tarifa_tusd_kwh", tarifa.tusd_kwh)
        tusd_comp = ultima.get("tarifa_tusd_compensada_kwh", tarifa.tusd_compensada_kwh)
        te_comp = ultima.get("tarifa_te_compensada_kwh", tarifa.te_compensada_kwh)
        tarifa_cheia = te + tusd
        fio_b = (te - te_comp) + (tusd - tusd_comp)
        cip = ultima.get("cip", tarifa.cip_mensal)
        dmic = ultima.get("dmic", 0) or 0
        multa = ultima.get("multa", 0) or 0
        juros = ultima.get("juros", 0) or 0

        # decomposicao da parte de energia (residual garante soma exata)
        outros = cip + multa + juros + dmic
        energia_total = total_pago - outros
        min_value = net * tarifa_cheia
        fio_b_value = used * fio_b
        bandeira_value = energia_total - min_value - fio_b_value  # residual

        bill_vars.update({
            "BAL_INJECTED": fmt_num(injected, 0),
            "BAL_USED_CREDITS": fmt_num(used, 0),
            "BAL_BANKED": fmt_num(banked, 0),
            "CREDITS_TOTAL": fmt_num(banked, 0),
            "LAST_CONSUMED": fmt_num(consumed, 0),
            "LAST_USED": fmt_num(used, 0),
            "LAST_NET": fmt_num(net, 0),
            "LAST_TARIFA_CHEIA": fmt_brl(tarifa_cheia),
            "LAST_FIO_B": fmt_brl(fio_b),
            "LAST_BANDEIRA": ultima.get("bandeira", "verde"),
            "REC_MIN_KWH": fmt_num(net, 0),
            "REC_MIN_VALUE": fmt_brl(min_value),
            "REC_FIO_B_VALUE": fmt_brl(fio_b_value),
            "REC_BANDEIRA_VALUE": fmt_brl(bandeira_value),
            "REC_CIP_VALUE": fmt_brl(cip),
        })

        # linhas extras do recibo (multa, juros, DMIC) — so se existirem
        extra_rows = ""
        if multa:
            extra_rows += _receipt_row("Multa por atraso", "conta anterior paga após o vencimento", fmt_brl(multa))
        if juros:
            extra_rows += _receipt_row("Juros de mora", "encargo de atraso", fmt_brl(juros))
        if dmic:
            extra_rows += _receipt_row(
                "DMIC — compensação por falta de luz",
                "indenização que a Enel paga quando as quedas de energia passam do limite",
                fmt_brl(dmic), credit=dmic < 0,
            )
        bill_vars["REC_EXTRA_ROWS"] = extra_rows

        # quanto o solar abateu (baseado na conta, sem prever futuro)
        solar_saved = used * tarifa_cheia
        bill_vars["SOLAR_SAVED_BRL"] = fmt_brl(solar_saved)
        bill_vars["SOLAR_SAVED_NOTE"] = f"{fmt_num(used,0)} kWh compensados + {fmt_num(banked,0)} kWh guardados"

        # headline note
        contas_pagas = [b["total_pago"] for b in bills if b.get("total_pago", 0) > 0]
        if total_pago > 0 and contas_pagas and total_pago <= min(contas_pagas):
            bill_vars["HEADLINE_NOTE"] = "Sua menor conta desde que o solar entrou 🎉"
        else:
            bill_vars["HEADLINE_NOTE"] = f"Conta de {bill_vars['LAST_BILL_MONTH']}"

        # créditos explain
        validade = ci.replace(year=ci.year + 5)
        bill_vars["CREDITS_EXPLAIN"] = (
            f"Você gerou mais do que gastou, então sobraram <b>{fmt_num(banked,0)} kWh</b> de crédito "
            f"que abatem suas próximas contas (valem até {validade.strftime('%m/%Y')})."
            if banked > 0 else
            "Neste mês você usou seus créditos para abater o consumo."
        )

        # paragrafo explicativo dinamico
        bill_vars["EXPLAIN_PARAGRAPH"] = _gerar_explicacao(
            net=net, used=used, banked=banked, consumed=consumed, injected=injected,
            dmic=dmic, total_pago=total_pago, energia_total=energia_total,
            disponibilidade=int(to_float(cfg("CUSTO_DISPONIBILIDADE_KWH", "100"), 100)),
        )
    else:
        for k in ("LAST_BILL_MONTH", "LAST_CYCLE_RANGE", "LAST_BILL_DUE_FULL", "LAST_BILL_PAID",
                  "BAL_INJECTED", "BAL_USED_CREDITS", "BAL_BANKED", "CREDITS_TOTAL", "CREDITS_EXPLAIN",
                  "LAST_CONSUMED", "LAST_USED", "LAST_NET", "LAST_TARIFA_CHEIA", "LAST_FIO_B",
                  "LAST_BANDEIRA", "REC_MIN_KWH", "REC_MIN_VALUE", "REC_FIO_B_VALUE",
                  "REC_BANDEIRA_VALUE", "REC_CIP_VALUE", "REC_EXTRA_ROWS", "SOLAR_SAVED_BRL",
                  "SOLAR_SAVED_NOTE", "HEADLINE_NOTE", "EXPLAIN_PARAGRAPH"):
            bill_vars[k] = "—"
        bill_vars["EXPLAIN_PARAGRAPH"] = "Importe uma fatura PDF para ver a explicação detalhada."

    # --- Histórico consumo vs geração (de bills.json) ---
    hist_bars = [
        {"mes": label_mes(b["mes_ano"]),
         "consumo": b.get("consumo_rede_kwh", 0),
         "injetado": b.get("injetado_kwh", 0)}
        for b in sorted(bills, key=lambda x: x["mes_ano"])[-9:]
    ]
    com_solar = [b for b in bills if b.get("injetado_kwh", 0) > 0]
    if com_solar:
        history_note = f"Injeção começou em {label_mes(sorted(com_solar, key=lambda x: x['mes_ano'])[0]['mes_ano'])}"
    else:
        history_note = ""

    # --- Status pills / formatacoes ---
    if not api_ok:
        power_color, ppbg, ppfg, ppdot, pptext = "#8e8e93", "#f0f0f5", "#6e6e73", "#8e8e93", "Sem conexão"
        sbg, sfg, sdot, stext = "#fff4e5", "#a15c00", "#ff9f0a", "Offline"
    elif is_generating:
        power_color, ppbg, ppfg, ppdot, pptext = "#ff9f0a", "#e8f9ee", "#117a3d", "#30d158", "Gerando"
        sbg, sfg, sdot, stext = "#e8f9ee", "#117a3d", "#30d158", "Online"
    elif is_online:
        power_color, ppbg, ppfg, ppdot, pptext = "#1d1d1f", "#e8f9ee", "#117a3d", "#30d158", "Online"
        sbg, sfg, sdot, stext = "#e8f9ee", "#117a3d", "#30d158", "Online"
    else:
        power_color, ppbg, ppfg, ppdot, pptext = "#8e8e93", "#f0f0f5", "#6e6e73", "#8e8e93", "Offline"
        sbg, sfg, sdot, stext = "#f0f0f5", "#6e6e73", "#8e8e93", "Offline"

    if pac_w >= 1000:
        power_value, power_unit = fmt_num(pac_w / 1000, 2), "kW"
    else:
        power_value, power_unit = fmt_num(pac_w, 0), "W"
    if total_kwh >= 1000:
        total_value, total_unit = fmt_num(total_kwh / 1000, 2), "MWh"
    else:
        total_value, total_unit = fmt_num(total_kwh, 0), "kWh"

    if temp == 0:
        temp_color, temp_text = "#a1a1a6", "—"
    elif temp < 60:
        temp_color, temp_text = "#117a3d", "normal"
    elif temp < 75:
        temp_color, temp_text = "#c79900", "aquecido"
    else:
        temp_color, temp_text = "#c01f1f", "alerta"

    install_short = label_mes(sorted(bills, key=lambda x: x["mes_ano"])[0]["mes_ano"]) if bills else ""

    # --- Card de creditos (substitui temperatura na linha de destaque) ---
    creditos_now = ultima.get("saldo_creditos_kwh", 0) if ultima else 0
    if creditos_now > 0:
        credits_color = "#0a84ff"
        credits_note = "Energia que você gerou a mais 🎉"
    else:
        credits_color = "#a1a1a6"
        credits_note = "Sem créditos guardados ainda"

    vars_ = {
        "GREETING": saudacao(now.hour), "NAME": nome_user,
        "DATE_FULL": f"{DIAS_PT[today.weekday()]}, {today.day} de {MESES_PT[today.month]} de {today.year}",
        "DATE_SHORT": f"{DIAS_PT_SHORT[today.weekday()]}, {today.day} de {MESES_PT[today.month]} de {today.year}",
        "PLANT_NAME": plant_name, "CONCESSIONARIA": concessionaria,
        "STATUS_BG": sbg, "STATUS_FG": sfg, "STATUS_DOT": sdot, "STATUS_TEXT": stext,
        "POWER_VALUE": power_value, "POWER_UNIT": power_unit, "POWER_COLOR": power_color,
        "POWER_PILL_BG": ppbg, "POWER_PILL_FG": ppfg, "POWER_PILL_DOT": ppdot, "POWER_PILL_TEXT": pptext,
        "TODAY_KWH": fmt_num(today_kwh, 1), "TODAY_DAY_MONTH": f"{today.day:02d}/{today.month:02d}",
        "TODAY_VALUE_BRL": fmt_brl(today_kwh * tarifa.tarifa_cheia),
        "TOTAL_VALUE": total_value, "TOTAL_UNIT": total_unit, "TOTAL_CO2_KG": fmt_num(total_kwh * 0.0817, 0),
        "INSTALL_DATE_SHORT": f"desde {install_short}" if install_short else "",
        "CREDITS_NOW": fmt_num(creditos_now, 0), "CREDITS_COLOR": credits_color, "CREDITS_NOW_NOTE": credits_note,
        "TEMP_VALUE": fmt_num(temp, 0) if temp else "—",
        "TEMP_STATUS_COLOR": temp_color, "TEMP_STATUS_TEXT": temp_text, "INVERTER_SN": inv.serial,
        "CYCLE_START": ciclo_inicio.strftime("%d/%m"), "CYCLE_END": ciclo_fim.strftime("%d/%m"),
        "CYCLE_DAY": str(ciclo_dias_passados), "CYCLE_DAYS_TOTAL": str(ciclo_dias_total),
        "CYCLE_DAYS_LEFT": str(ciclo_dias_left), "CYCLE_PROGRESS_PCT": f"{ciclo_progress_pct:.1f}",
        "CYCLE_KWH": fmt_num(ciclo_kwh, 1), "CYCLE_PROJECTED": fmt_num(projecao_ciclo, 1),
        "CYCLE_AVG_DAILY": fmt_num(media_diaria, 0),
        "SPARK_LINE_PATH": line_path, "SPARK_AREA_PATH": area_path, "SPARK_PROJ_PATH": proj_path,
        "SPARK_DOT_X": f"{dot_x:.1f}", "SPARK_DOT_Y": f"{dot_y:.1f}",
        "TIME_NOW": now.strftime("%H:%M"),
        "DAY_PEAK": peak_str, "DAY_PEAK_TIME": peak_time,
        "DAY_ENERGY": f"{fmt_num(energia_dia, 1)} kWh", "DAY_ENERGY_BRL": fmt_brl(energia_dia * tarifa.tarifa_cheia),
        "DAY_SUN_HOURS": f"{fmt_num(energia_dia / 6.0, 1)} h",
        "DAY_CURVE_REAL_JSON": json.dumps(real_points), "DAY_CURVE_PROJ_JSON": json.dumps(proj_points),
        "NOW_HOUR_DECIMAL": f"{hora_atual:.3f}", "NOW_KW": f"{now_kw:.3f}",
        "PV1_V": fmt_num(pv1_v, 1), "PV1_A": fmt_num(pv1_a, 2), "PV1_W": fmt_num(pv1_w, 0),
        "PV2_V": fmt_num(pv2_v, 1), "PV2_A": fmt_num(pv2_a, 2), "PV2_W": fmt_num(pv2_w, 0),
        "AC_V": fmt_num(vac, 1), "AC_A": fmt_num(iac, 2), "AC_HZ": fmt_num(fac, 2),
        "TARIFA_CHEIA": fmt_brl(tarifa.tarifa_cheia), "BANDEIRA_NOME": bandeira_nome,
        "FIO_B": fmt_brl(tarifa.fio_b_kwh), "CIP": fmt_brl(tarifa.cip_mensal),
        "DAY_BARS_JSON": json.dumps(day_bars),
        "HISTORY_BARS_JSON": json.dumps(hist_bars), "HISTORY_NOTE": history_note,
        "LAST_SYNC": now.strftime("%H:%M:%S") if api_ok else "sem conexão",
    }
    vars_.update(bill_vars)
    return vars_


def _receipt_row(title: str, sub: str, value: str, credit: bool = False) -> str:
    color = "color:#0e7c2c;" if credit else ""
    return f"""
        <div class="receipt-row" style="border-top: 1px solid var(--border);">
          <div>
            <div style="font-size:14px; font-weight:500;">{title}</div>
            <div class="receipt-sub">{sub}</div>
          </div>
          <div class="num" style="font-size: 15px; font-weight: 600; {color}">{value}</div>
        </div>"""


def _gerar_explicacao(net, used, banked, consumed, injected, dmic, total_pago, energia_total, disponibilidade) -> str:
    partes = []
    if net <= disponibilidade + 1:
        partes.append(
            f"Sua placa gerou tanto que você bateu o <b>piso de {disponibilidade} kWh</b> que todo cliente "
            f"trifásico paga obrigatoriamente — não dá pra pagar menos de energia do que isso."
        )
    if banked > 0:
        partes.append(
            f"Você ainda injetou <b>{fmt_num(injected,0)} kWh</b> na rede: {fmt_num(used,0)} abateram seu consumo "
            f"e <b>{fmt_num(banked,0)} kWh viraram crédito</b> guardado para os meses mais nublados."
        )
    if dmic and dmic < 0:
        partes.append(
            f"Este mês teve uma surpresa boa: a Enel te devolveu <b>{fmt_brl(abs(dmic))}</b> de DMIC "
            f"(indenização por quedas de energia acima do limite). Sem esse crédito, a conta teria sido "
            f"cerca de <b>{fmt_brl(total_pago - dmic)}</b> — então não conte com isso todo mês."
        )
    if not partes:
        partes.append("Sua geração solar abateu boa parte do consumo este mês.")
    return " ".join(partes)


def render_dashboard():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    try:
        vars_ = build_template_vars()
    except Exception as e:
        st.error(f"Não consegui montar o painel: {e}")
        st.info("Tente recarregar a página. Se persistir, a API do Growatt pode estar instável no momento.")
        return
    for key, val in vars_.items():
        template = template.replace("{{" + key + "}}", str(val))
    components.html(template, height=3400, scrolling=False)


# =============================================================================
# MAIN
# =============================================================================

render_upload_section()
render_dashboard()

st.markdown(
    "<script>setTimeout(function(){ window.location.reload(); }, 60000);</script>",
    unsafe_allow_html=True,
)
