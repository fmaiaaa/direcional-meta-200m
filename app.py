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
    html,body,.stApp,[data-testid="stAppViewContainer"],
    [data-testid="stMain"]{height:100%;overflow:hidden!important}
    .stApp{background:#fff;color:#12345a;color-scheme:light}
    .stApp::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.34;
    background-repeat:no-repeat;background-size:cover;background-position:center;z-index:0}
    [data-testid="stAppViewContainer"]{position:relative;z-index:1;background:transparent}
    [data-testid="stHeader"],[data-testid="stToolbar"]{background:transparent!important;box-shadow:none!important}
    [data-testid="stHeader"]{pointer-events:none;opacity:0}
    .block-container{max-width:1400px;width:90vw;height:100dvh;min-height:0;
    padding:3vh 2vw!important;margin:0 auto!important;background:transparent;
    border:0;border-radius:0;box-shadow:none}
    [data-testid="stVerticalBlock"]{gap:0!important}
    [data-testid="stElementContainer"]:has(style){display:none}
    .tv-dashboard{height:calc(94dvh - 40px);display:grid;min-height:0;
    grid-template-rows:1fr 1.1fr 1.9fr .8fr .65fr;gap:1vh}
    .brand{display:flex;justify-content:center;align-items:center;min-height:0}
    .brand img{max-width:30vw;max-height:9vh;width:auto;height:auto;object-fit:contain}
    .totals{display:grid;grid-template-columns:1fr 1fr;align-items:center;gap:3vw;text-align:center}
    .stat-label{font-size:clamp(14px,2.3vh,28px);color:#334a65;line-height:1.2}
    .stat-value{font-size:clamp(22px,4.3vh,52px);color:#084589;font-weight:650;white-space:nowrap}
    .remaining{display:flex;flex-direction:column;justify-content:center;text-align:center;
    background:transparent;border:0;box-shadow:none;padding:0;min-height:0}
    .remaining-label{font-size:clamp(16px,2.6vh,32px);letter-spacing:.08em;text-transform:uppercase;color:#084589}
    .remaining-value{font-size:clamp(32px,8vh,100px);font-weight:750;line-height:1.2;
    color:#084589;white-space:nowrap;text-shadow:0 2px 20px #ffffffb0}
    .pace{display:flex;justify-content:space-between;align-items:center;gap:2vw;
    font-size:clamp(12px,2.5vh,30px);color:#334a65}
    .pace strong{color:#084589;margin-left:10px;white-space:nowrap}
    .goal-progress-label{text-align:right;color:#084589;font-weight:650;
    font-size:clamp(12px,2vh,24px);margin-bottom:.8vh}
    .goal-track{height:1.8vh;min-height:8px;border-radius:100px;background:rgba(211,222,235,.8);overflow:hidden}
    .goal-fill{position:relative;height:100%;border-radius:100px;
    background:linear-gradient(90deg,#084589 0%,#cf0035 100%);overflow:hidden}
    .goal-fill::after{content:"";position:absolute;inset:0;
    background:linear-gradient(100deg,transparent 25%,#ffffff38 50%,transparent 75%);
    transform:translateX(-100%);animation:progress-shimmer 12s ease-in-out infinite}
    @keyframes progress-shimmer{0%,65%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
    @media(prefers-reduced-motion:reduce){.goal-fill::after{animation:none;display:none}}
    .stButton button{border-radius:9px;background:#084589;color:white;border:0;
    min-height:30px;height:30px;padding:2px 12px;font-weight:650}
    .stButton button:hover{background:#073568;color:white}
    @media(max-width:640px){.pace{font-size:2.2vw}.stat-value{font-size:4.2vw}
    .remaining-value{font-size:7vw}.brand img{max-width:50vw}}
    </style>""", unsafe_allow_html=True)
    logo = base64.b64encode((assets / "logo_direcional.png").read_bytes()).decode()
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
    daily_label = money(daily) if daily is not None else "Prazo encerrado"
    progress = float(realized / GOAL)
    percent = f"{progress:.2%}".replace(".", ",")
    width = min(100.0, max(0.0, progress * 100))
    st.markdown(
        f'<div class="tv-dashboard">'
        f'<div class="brand"><img src="data:image/png;base64,{logo}" alt="Direcional"></div>'
        f'<div class="totals"><div><div class="stat-label">Meta</div>'
        f'<div class="stat-value">{money(GOAL)}</div></div>'
        f'<div><div class="stat-label">Realizado</div>'
        f'<div class="stat-value">{money(realized)}</div></div></div>'
        f'<div class="remaining"><div class="remaining-label">Meta a Atingir</div>'
        f'<div class="remaining-value">{money(remaining)}</div></div>'
        f'<div class="pace"><span>Dias para 2027<strong>{days}</strong></span>'
        f'<span>Meta Diária<strong>{daily_label}</strong></span></div>'
        f'<div><div class="goal-progress-label">{percent} da meta</div>'
        f'<div class="goal-track" role="progressbar" aria-label="Percentual da meta atingido" '
        f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{width:.2f}">'
        f'<div class="goal-fill" style="width:{width:.4f}%"></div></div></div></div>',
        unsafe_allow_html=True,
    )
    st.button("Atualizar Salesforce", type="primary", use_container_width=True,
              on_click=fetch_sales.clear)


if __name__ == "__main__":
    main()


