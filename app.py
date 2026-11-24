"""Run: streamlit run app.py

Configure Streamlit secrets (never commit the populated file):
[salesforce]
USER = ""
PASSWORD = ""
TOKEN = ""

Only Streamlit and Python's standard library are required. The official report's
business filters are preserved; only its date field and reporting period change.
"""
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import base64
from pathlib import Path
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import streamlit as st

REPORT = "00O3Z000005ZsPmUAK"
VERSION = "59.0"
START = date(2026, 9, 1)
DEADLINE = date(2027, 1, 1)
GOAL = Decimal("200000000")
DATE_FIELD = "Opportunity.DataConfirmacaoVenda2__c"
VALUE_FIELD = "Opportunity.Valor_Real_de_Venda__c"
ID_FIELD = "Opportunity.IDOportunidade__c"
SAO_PAULO = timezone(timedelta(hours=-3))


class DataError(Exception):
    """Safe, user-facing errors; never include tokens or raw API responses."""


class ApiError(DataError):
    def __init__(self, status, quota=False):
        self.status = status
        self.quota = quota
        super().__init__(
            "O Salesforce recusou a consulta. Confira o acesso ao relatório e tente novamente."
            if status in (401, 403) else "O Salesforce não concluiu a consulta. Tente novamente."
        )


def transport(url, headers, payload=None, xml=False):
    body = payload if xml else (json.dumps(payload).encode() if payload is not None else None)
    for attempt in range(3):
        try:
            request = Request(url, data=body, headers=headers)
            with urlopen(request, timeout=60) as response:
                raw = response.read()
            return raw if xml else json.loads(raw)
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            quota = "limit" in raw.lower() and ("synchronous" in raw.lower() or "60" in raw)
            if exc.code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise ApiError(exc.code, quota) from None
        except (URLError, socket.timeout, TimeoutError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise DataError("Sem conexão com o Salesforce. Confira a rede e tente novamente.") from None
        except (ValueError, ElementTree.ParseError):
            raise DataError("O Salesforce retornou uma resposta inválida.") from None


class Salesforce:
    def __init__(self, user, password, token):
        envelope = (
            '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:urn="urn:partner.soap.sforce.com"><env:Body><urn:login>'
            f'<urn:username>{escape(user)}</urn:username>'
            f'<urn:password>{escape(password + token)}</urn:password>'
            '</urn:login></env:Body></env:Envelope>'
        )
        try:
            raw = transport(
                f"https://direcional.my.salesforce.com/services/Soap/u/{VERSION}",
                {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "login"},
                envelope.encode(), xml=True,
            )
            root = ElementTree.fromstring(raw)
            session = root.find(".//{urn:partner.soap.sforce.com}sessionId")
            server = root.find(".//{urn:partner.soap.sforce.com}serverUrl")
            if session is None or server is None or not session.text or not server.text:
                raise ValueError()
            parsed = urlparse(server.text)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".salesforce.com"):
                raise ValueError()
            self.base = f"https://{parsed.netloc}/services/data/v{VERSION}/"
            self.headers = {"Authorization": "Bearer " + session.text, "Content-Type": "application/json"}
        except (DataError, ValueError, ElementTree.ParseError):
            raise DataError("Não foi possível autenticar no Salesforce. Confira USER, PASSWORD e TOKEN nos secrets.") from None

    def read(self, path, payload=None):
        return transport(self.base + path, self.headers, payload)

    def report(self, metadata):
        path = f"analytics/reports/{REPORT}"
        payload = {"reportMetadata": metadata}
        try:
            return self.read(path + "?includeDetails=true", payload)
        except ApiError as exc:
            if not exc.quota:
                raise
        instance = self.read(path + "/instances?includeDetails=true", payload)
        instance_id = instance.get("id", "")
        if not instance_id or not instance_id.isalnum():
            raise DataError("Não foi possível iniciar o relatório assíncrono.")
        until = time.monotonic() + 90
        while time.monotonic() < until:
            result = self.read(path + "/instances/" + instance_id)
            if result.get("status") == "Success":
                return result
            if result.get("status") in ("Error", "Failed"):
                raise DataError("O Salesforce não conseguiu executar o relatório.")
            time.sleep(2)
        raise DataError("O relatório ainda está sendo processado. Tente atualizar em alguns minutos.")


def amount(cell):
    value = cell.get("value")
    if isinstance(value, dict):
        currency = value.get("currency")
        if currency != "BRL" and not (currency is None and cell.get("label", "").startswith("R$")):
            raise DataError("Foi encontrada uma venda em moeda diferente de BRL.")
        value = value.get("amount")
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation()
        return number
    except InvalidOperation:
        raise DataError("Há vendas sem Valor Real de Venda válido. Corrija o cadastro antes de somar.") from None


def extract_period(sf, metadata, first, last):
    columns = list(dict.fromkeys(metadata["detailColumns"] + [ID_FIELD, DATE_FIELD, VALUE_FIELD]))
    period = {**metadata, "detailColumns": columns,
              "standardDateFilter": {**metadata.get("standardDateFilter", {}),
                  "column": DATE_FIELD, "durationValue": "CUSTOM",
                  "startDate": first.isoformat(), "endDate": last.isoformat()}}
    result = sf.report(period)
    fact = result.get("factMap", {}).get("T!T", {})
    rows = fact.get("rows", [])
    try:
        total = fact["aggregates"][metadata["aggregates"].index("RowCount")]["value"]
    except (KeyError, IndexError, ValueError):
        raise DataError("O relatório precisa disponibilizar a contagem total e as linhas detalhadas.") from None
    if not result.get("allData") or len(rows) != total:
        if first == last:
            raise DataError("O Salesforce truncou os dados de um dia. O total não será exibido incompleto.")
        midpoint = first + (last - first) // 2
        return (extract_period(sf, metadata, first, midpoint) +
                extract_period(sf, metadata, midpoint + timedelta(days=1), last))
    records = []
    for row in rows:
        cells = row["dataCells"]
        identity = cells[columns.index(ID_FIELD)]["value"]
        raw_date = cells[columns.index(DATE_FIELD)]["value"]
        try:
            confirmed = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            raise DataError("Há oportunidades com data de confirmação inválida.") from None
        if not identity or not first <= confirmed <= last:
            raise DataError("O relatório retornou uma oportunidade sem identificação ou fora do período.")
        records.append((str(identity), confirmed.isoformat(), str(amount(cells[columns.index(VALUE_FIELD)]))))
    return records


@st.cache_data(ttl=300, show_spinner=False, max_entries=4)
def fetch_sales(user, password, token, today_iso):
    sf = Salesforce(user, password, token)
    metadata = sf.read(f"analytics/reports/{REPORT}/describe")["reportMetadata"]
    # Preserve official report filters, scope and Boolean expression. Never save
    # changes to the report. Split months/days to avoid Salesforce's row limit.
    today = date.fromisoformat(today_iso)
    records = {}
    for month in range(9, 13):
        first = date(2026, month, 1)
        last = min(date(2026, month, monthrange(2026, month)[1]), today)
        if first > last:
            continue
        for identity, confirmed, value in extract_period(sf, metadata, first, last):
            record = (confirmed, value)
            if identity in records and records[identity] != record:
                raise DataError("Uma oportunidade apareceu com datas ou valores divergentes.")
            records[identity] = record
    return list(records.values()), datetime.now(SAO_PAULO).strftime("%d/%m/%Y %H:%M")


def totals(records, today):
    realized = sum((Decimal(value) for confirmed, value in records
                    if START <= date.fromisoformat(confirmed) < DEADLINE
                    and date.fromisoformat(confirmed) <= today), Decimal("0"))
    remaining = max(Decimal("0"), GOAL - realized)
    days = max(0, (DEADLINE - max(today, START)).days)
    daily = remaining / days if days else (Decimal("0") if not remaining else None)
    return realized, remaining, days, daily


def money(value):
    return "R$ " + f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def main():
    assets = Path(__file__).parent / "assets"
    st.set_page_config(page_title="Direcional | Meta 200 milhões", page_icon=str(assets / "simbolo_direcional.png"), layout="wide")
    background = base64.b64encode((assets / "fundo_residencial.jpg").read_bytes()).decode()
    st.markdown(f'<style>.stApp::before{{background-image:url("data:image/jpeg;base64,{background}")}}</style>', unsafe_allow_html=True)
    st.markdown("""<style>
    .stApp{background:#fff;color:#12345a;color-scheme:light}
    .stApp::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.34;
    background-repeat:no-repeat;background-size:cover;background-position:center;z-index:0}
    [data-testid="stAppViewContainer"]{position:relative;z-index:1;background:transparent}
    [data-testid="stHeader"],header[data-testid="stHeader"]{background:transparent!important;box-shadow:none!important}
    [data-testid="stToolbar"]{background:transparent!important}
    [data-testid="stAppViewContainer"]>.main{background:transparent}
    .block-container{background:rgba(255,255,255,.64);border-radius:24px;margin-top:12px}
    .block-container{max-width:1400px;padding-top:2rem;padding-bottom:2rem}
    .brand{display:flex;justify-content:center;margin-bottom:28px}
    .brand img{width:min(310px,70vw);height:auto;background:transparent;padding:0;box-sizing:border-box}
    .remaining{background:linear-gradient(120deg,rgba(7,53,104,.96),rgba(8,69,137,.96) 75%,rgba(22,92,164,.96));
    border-radius:20px;padding:32px 38px;margin:20px 0 24px;
    box-shadow:0 14px 36px #07356822;border-left:7px solid #cf0035;color:white}
    .remaining-label{font-size:1rem;letter-spacing:.08em;text-transform:uppercase;color:#d9e8fa}
    .remaining-value{font-size:clamp(2.2rem,5vw,4.2rem);font-weight:750;line-height:1.3;margin-top:8px}
    [data-testid="stMetric"]{background:rgba(255,255,255,.72);border:1px solid rgba(176,195,217,.6);
    border-radius:14px;padding:22px;min-height:135px;box-shadow:0 4px 16px #07356808}
    [data-testid="stMetricValue"]{color:#084589;font-size:clamp(1.4rem,2.4vw,2rem)}
    [data-testid="stMetricLabel"]{color:#334a65}
    [data-testid="stText"]{color:#aebbd0}
    .pace{display:flex;justify-content:space-between;align-items:baseline;gap:16px;
    margin:0 0 28px;font-size:clamp(.85rem,1.7vw,1.1rem);color:#334a65}
    .pace strong{color:#084589;margin-left:8px;white-space:nowrap}
    .goal-progress-label{text-align:right;color:#084589;font-weight:650;margin-bottom:8px}
    .goal-track{height:18px;border-radius:100px;background:rgba(211,222,235,.8);overflow:hidden;margin-bottom:24px}
    .goal-fill{height:100%;border-radius:100px;background:linear-gradient(90deg,#084589 0%,#cf0035 100%)}
    .stButton button{border-radius:12px;background:#084589;border:0;min-height:34px;height:34px;padding:3px 12px;font-weight:650}
    .stButton button:hover{background:#073568;color:white}
    @media(max-width:640px){.remaining{padding:25px 20px}.block-container{padding-top:1rem}}
    </style>""", unsafe_allow_html=True)
    logo = base64.b64encode((assets / "logo_direcional.png").read_bytes()).decode()
    st.markdown(f'<div class="brand"><img src="data:image/png;base64,{logo}" alt="Direcional"></div>', unsafe_allow_html=True)
    try:
        credentials = st.secrets["salesforce"]
        user, password, token = (str(credentials.get(key, "")) for key in ("USER", "PASSWORD", "TOKEN"))
        if not all((user, password, token)):
            raise DataError("Preencha USER, PASSWORD e TOKEN na seção [salesforce] dos secrets do Streamlit.")
    except (FileNotFoundError, KeyError, DataError):
        st.error("Configure os secrets do Streamlit: seção [salesforce] com USER, PASSWORD e TOKEN.")
        st.stop()
    today = datetime.now(SAO_PAULO).date()
    try:
        with st.spinner("Consultando vendas confirmadas…"):
            records, updated = fetch_sales(user, password, token, today.isoformat())
            realized, remaining, days, daily = totals(records, today)
    except DataError as exc:
        st.error(str(exc))
        st.stop()
    except Exception:
        st.error("Não foi possível validar o retorno do Salesforce. Tente atualizar novamente.")
        st.stop()
    row = st.columns(2)
    row[0].metric("Meta", money(GOAL))
    row[1].metric("Realizado", money(realized))
    st.markdown(f'<div class="remaining"><div class="remaining-label">Meta a Atingir</div>'
                f'<div class="remaining-value">{money(remaining)}</div></div>', unsafe_allow_html=True)
    daily_label = money(daily) if daily is not None else "Prazo encerrado"
    st.markdown(f'<div class="pace"><span>Dias para 2027<strong>{days}</strong></span>'
                f'<span>Meta Diária<strong>{daily_label}</strong></span></div>', unsafe_allow_html=True)
    progress = float(realized / GOAL)
    percent = f"{progress:.2%}".replace(".", ",")
    width = min(100.0, max(0.0, progress * 100))
    st.markdown(f'<div class="goal-progress-label">{percent} da meta</div>'
                f'<div class="goal-track" role="progressbar" aria-label="Percentual da meta atingido" '
                f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{width:.2f}">'
                f'<div class="goal-fill" style="width:{width:.4f}%"></div></div>', unsafe_allow_html=True)
    st.button("Atualizar Salesforce", type="primary", use_container_width=True,
              on_click=fetch_sales.clear)


if __name__ == "__main__":
    main()


