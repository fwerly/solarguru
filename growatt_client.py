"""Cliente para a API do servidor Growatt (a mesma usada pelo ShinePhone).

Envolve a lib `growattServer` com tratamento de erro e cache de sessao.
Suporta inversores da serie MIN/TLX (ex.: MIN 6000TL-X2).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import growattServer


@dataclass
class Inverter:
    serial: str
    plant_id: str
    alias: str
    model: str
    is_tlx: bool


class GrowattClient:
    # Servidores conhecidos. server.growatt.com e o usado pelo app ShinePhone
    # comum (BR/EU/US). openapi.growatt.com so funciona com contas de parceiro.
    SERVERS = (
        "https://server.growatt.com/",
        "https://server-api.growatt.com/",
        "https://server-us.growatt.com/",
    )

    def __init__(self, username: str, password: str, server_url: str | None = None):
        self.username = username
        self.password = password
        self.api = growattServer.GrowattApi(add_random_user_id=True)
        self.api.server_url = server_url or self.SERVERS[0]
        self.user_id: str | None = None
        self._logged_in = False

    def login(self) -> None:
        """Tenta logar; se o servidor padrao recusar, tenta os fallbacks."""
        last_error: Exception | None = None
        servers_to_try = [self.api.server_url] + [s for s in self.SERVERS if s != self.api.server_url]
        for server in servers_to_try:
            self.api.server_url = server
            try:
                result = self.api.login(self.username, self.password)
            except Exception as e:
                last_error = e
                continue
            if result.get("success"):
                self.user_id = result["user"]["id"]
                self._logged_in = True
                return
            last_error = RuntimeError(f"{server}: {result.get('msg') or result}")
        raise RuntimeError(f"Falha no login Growatt em todos os servidores. Ultimo erro: {last_error}")

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def list_plants(self) -> list[dict[str, Any]]:
        self._ensure_login()
        return self.api.plant_list(self.user_id).get("data", [])

    def list_inverters(self, plant_id: str) -> list[Inverter]:
        self._ensure_login()
        devices = self.api.device_list(plant_id)
        inverters: list[Inverter] = []
        for d in devices:
            serial = d.get("deviceSn") or d.get("sn") or ""
            model = (d.get("deviceModel") or d.get("deviceTypeName") or "").upper()
            type_id = d.get("deviceType") or d.get("deviceTypeName", "")
            is_tlx = "TLX" in model or "MIN" in model or "tlx" in str(type_id).lower()
            inverters.append(
                Inverter(
                    serial=serial,
                    plant_id=str(plant_id),
                    alias=d.get("deviceAilas") or d.get("alias") or serial,
                    model=model or "MIN-TLX",
                    is_tlx=is_tlx,
                )
            )
        return inverters

    def realtime(self, inv: Inverter) -> dict[str, Any]:
        """Estado atual + energia acumulada. Combina tlx_detail e tlx_energy_overview."""
        self._ensure_login()
        out: dict[str, Any] = {}

        if inv.is_tlx:
            try:
                detail = self.api.tlx_detail(inv.serial)
                if isinstance(detail, dict):
                    out.update(detail.get("data", detail))
            except Exception:
                pass
            try:
                ov = self.api.tlx_energy_overview(inv.plant_id, inv.serial)
                if isinstance(ov, dict):
                    out.setdefault("eToday", ov.get("epvToday"))
                    out.setdefault("eTotal", ov.get("epvTotal"))
                    out["energy_overview"] = ov
            except Exception:
                pass
        else:
            try:
                out.update(self.api.inverter_detail(inv.serial))
            except Exception:
                pass
        return out

    def plant_summary(self, plant_id: str) -> dict[str, Any]:
        """Resumo da planta direto do plant_list (potencia atual, hoje, total)."""
        self._ensure_login()
        plants = self.list_plants()
        for p in plants:
            if str(p.get("plantId")) == str(plant_id):
                return p
        return {}

    def day_curve(self, inv: Inverter, date: dt.date) -> dict[str, float]:
        """Curva de potencia do dia (intervalos de 5min). dict: 'HH:MM' -> watts."""
        self._ensure_login()
        if inv.is_tlx:
            try:
                raw = self.api.tlx_data(inv.serial, date)
                return self._extract_power_series(raw)
            except Exception:
                pass
        try:
            raw = self.api.inverter_data(inv.serial, date)
            return self._extract_power_series(raw)
        except Exception:
            return {}

    @staticmethod
    def _extract_power_series(raw: Any) -> dict[str, float]:
        """A API retorna {'invPacData': {'YYYY-MM-DD HH:MM': watts, ...}} para TLX."""
        if not isinstance(raw, dict):
            return {}
        for key in ("invPacData", "pac", "ppv", "power"):
            series = raw.get(key)
            if isinstance(series, dict) and series:
                out: dict[str, float] = {}
                for ts, val in series.items():
                    if val in (None, "", "null"):
                        continue
                    try:
                        watts = float(val)
                    except (TypeError, ValueError):
                        continue
                    label = ts.split(" ")[-1] if " " in ts else ts
                    out[label] = watts
                if out:
                    return out
        return {}

    def daily_history_month(self, plant_id: str, month: dt.date | None = None) -> dict[str, float]:
        """kWh por dia do mes informado (default: mes atual). Retorna {'YYYY-MM-DD': kwh}.

        Nota: a API trata Timespan.month como "dias do mes" (chave = dia 01-31),
        e Timespan.day retorna dados horarios (vazio neste plano).
        """
        self._ensure_login()
        ref = month or dt.date.today()
        try:
            data = self.api.plant_detail(plant_id, growattServer.Timespan.month, ref)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        days = data.get("data") or {}
        out: dict[str, float] = {}
        for day_str, val in days.items():
            try:
                day_int = int(day_str)
                kwh = float(val)
            except (TypeError, ValueError):
                continue
            try:
                full_date = dt.date(ref.year, ref.month, day_int).isoformat()
            except ValueError:
                continue
            out[full_date] = kwh
        return out

    def monthly_history_year(self, plant_id: str, year: int | None = None) -> dict[str, float]:
        """kWh por mes do ano informado. Retorna {'YYYY-MM': kwh}.

        Como a API nao tem endpoint year direto, somamos os dias de cada mes.
        """
        self._ensure_login()
        ano = year or dt.date.today().year
        hoje = dt.date.today()
        ultimo_mes = hoje.month if ano == hoje.year else 12

        out: dict[str, float] = {}
        for mes in range(1, ultimo_mes + 1):
            ref = dt.date(ano, mes, 1)
            try:
                data = self.api.plant_detail(plant_id, growattServer.Timespan.month, ref)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            # plantData.currentEnergy ja vem como total formatado: "603.6 kWh"
            total_str = (data.get("plantData") or {}).get("currentEnergy", "0")
            total_kwh = 0.0
            for token in str(total_str).split():
                try:
                    total_kwh = float(token)
                    break
                except ValueError:
                    continue
            # fallback: somar valores dos dias
            if total_kwh == 0:
                days = data.get("data") or {}
                for v in days.values():
                    try:
                        total_kwh += float(v)
                    except (TypeError, ValueError):
                        pass
            out[f"{ano:04d}-{mes:02d}"] = total_kwh
        return out

    # =========================================================================
    # Ciclo de faturamento (concessionaria nao usa mes civil)
    # =========================================================================

    @staticmethod
    def cycle_window(today: dt.date, dia_fechamento: int) -> tuple[dt.date, dt.date]:
        """Retorna (inicio, fim) do ciclo de faturamento que contem `today`.

        Convencao: dia_fechamento e o ultimo dia do ciclo (data da leitura).
        O ciclo seguinte comeca em dia_fechamento+1 do mesmo mes.

        Exemplo: dia_fechamento=11, today=2026-05-08 -> (2026-04-12, 2026-05-11).
        """
        if today.day > dia_fechamento:
            # ja passou da leitura deste mes -> ciclo atual comeca neste mes
            inicio = today.replace(day=dia_fechamento + 1)
            mes_seguinte = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
            try:
                fim = mes_seguinte.replace(day=dia_fechamento)
            except ValueError:
                fim = mes_seguinte + dt.timedelta(days=dia_fechamento - 1)
        else:
            # antes da leitura deste mes -> ciclo comecou no mes passado
            mes_passado = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
            try:
                inicio = mes_passado.replace(day=dia_fechamento + 1)
            except ValueError:
                inicio = mes_passado + dt.timedelta(days=dia_fechamento)
            try:
                fim = today.replace(day=dia_fechamento)
            except ValueError:
                fim = today
        return inicio, fim

    def cycle_daily_history(
        self, plant_id: str, dia_fechamento: int, today: dt.date | None = None
    ) -> tuple[dict[str, float], dt.date, dt.date]:
        """kWh por dia do ciclo de faturamento atual. Retorna (dados, inicio, fim).

        Mescla os dias dos meses civis que compoe o ciclo.
        """
        ref = today or dt.date.today()
        inicio, fim = self.cycle_window(ref, dia_fechamento)

        out: dict[str, float] = {}
        # ciclo pode atravessar 1-2 meses civis -> consultar ambos
        meses_a_consultar = {(inicio.year, inicio.month), (fim.year, fim.month), (ref.year, ref.month)}
        for ano, mes in meses_a_consultar:
            mensal = self.daily_history_month(plant_id, dt.date(ano, mes, 1))
            for date_iso, kwh in mensal.items():
                d = dt.date.fromisoformat(date_iso)
                if inicio <= d <= fim:
                    out[date_iso] = kwh
        return out, inicio, fim

    def cycle_history_year(
        self, plant_id: str, dia_fechamento: int, year: int | None = None
    ) -> dict[str, dict]:
        """Para cada ciclo do ano corrente, retorna metadados.

        Resultado: {'2026-04': {'kwh': 211.3, 'inicio': date, 'fim': date, 'label': '12/03-11/04'}}
        Cada ciclo e nomeado pelo mes do dia de fechamento.
        """
        ano = year or dt.date.today().year
        hoje = dt.date.today()
        out: dict[str, dict] = {}

        # do ciclo de janeiro (que termina no dia_fechamento de jan) ate o ciclo atual
        ultimo_mes = hoje.month if ano == hoje.year else 12
        for mes in range(1, ultimo_mes + 2):  # +2 pra incluir ciclo em andamento
            try:
                fim_ciclo = dt.date(ano, mes, dia_fechamento)
            except ValueError:
                continue
            if fim_ciclo > hoje:
                # so inclui ciclo em andamento se ja comecou
                pass
            # ciclo: (dia_fechamento+1 do mes anterior) -> (dia_fechamento deste mes)
            mes_anterior = fim_ciclo.replace(day=1) - dt.timedelta(days=1)
            try:
                inicio_ciclo = mes_anterior.replace(day=dia_fechamento + 1)
            except ValueError:
                inicio_ciclo = mes_anterior + dt.timedelta(days=dia_fechamento)

            if inicio_ciclo > hoje:
                continue

            # somar kWh do periodo (consultar ambos os meses civis)
            kwh_total = 0.0
            for (a, m) in {(inicio_ciclo.year, inicio_ciclo.month), (fim_ciclo.year, fim_ciclo.month)}:
                mensal = self.daily_history_month(plant_id, dt.date(a, m, 1))
                for date_iso, kwh in mensal.items():
                    d = dt.date.fromisoformat(date_iso)
                    if inicio_ciclo <= d <= min(fim_ciclo, hoje):
                        kwh_total += kwh

            label = f"{inicio_ciclo.strftime('%d/%m')}–{fim_ciclo.strftime('%d/%m')}"
            out[f"{ano:04d}-{mes:02d}"] = {
                "kwh": kwh_total,
                "inicio": inicio_ciclo,
                "fim": fim_ciclo,
                "label": label,
                "em_andamento": fim_ciclo > hoje,
            }
        return out
