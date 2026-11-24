"""Run: streamlit run app.py

Configure Streamlit secrets (never commit the populated file):
[salesforce]
USER = ""
PASSWORD = ""
TOKEN = ""

Only Streamlit and Python's standard library are required. The official report's
business filters and date scope are translated to paginated SOQL, avoiding the
report execution quota. Period date = confirmation 2.0, falling back to sale date
when confirmation is empty, matching the source sheet's Data Corrigida formula.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import base64
from pathlib import Path
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import streamlit as st

REPORT = "00OU600000MquCjMAJ"
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
    def __init__(self, status, code="", quota=False):
        self.status, self.code, self.quota = status, code, quota
        if quota:
            message = "O Salesforce atingiu o limite de consultas. Aguarde a renovação da cota e atualize."
        elif code == "INVALID_SESSION_ID" or status == 401:
            message = "A sessão do Salesforce expirou. Tente atualizar novamente."
        elif status == 403:
            message = "O usuário Salesforce não tem permissão para esta consulta ou campo. Confira o perfil de acesso à API."
        else:
            message = "O Salesforce não concluiu a consulta. Tente novamente."
        super().__init__(message)


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
            try:
                details = json.loads(raw)
                code = details[0].get("errorCode", "") if isinstance(details, list) and details else ""
            except (ValueError, AttributeError):
                code = ""
            quota = code in {"REQUEST_LIMIT_EXCEEDED", "REPORT_LIMIT_EXCEEDED"} or (
                "500" in raw and "60" in raw
            )
            if exc.code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise ApiError(exc.code, code, quota) from None
        except (URLError, socket.timeout, TimeoutError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise DataError("Sem conexão com o Salesforce. Confira a rede e tente novamente.") from None
        except (ValueError, ElementTree.ParseError):
            raise DataError("O Salesforce retornou uma resposta inválida.") from None


class Salesforce:
    def __init__(self, user, password, token):
        self._credentials = (user, password, token)
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
        try:
            return transport(self.base + path, self.headers, payload)
        except ApiError as exc:
            if exc.code != "INVALID_SESSION_ID":
                raise
            self.__init__(*self._credentials)
            return transport(self.base + path, self.headers, payload)

    def query(self, soql):
        page = self.read("query?q=" + quote(soql, safe=""))
        records = []
        expected = page.get("totalSize")
        seen_pages = set()
        while True:
            records.extend(page.get("records", []))
            if page.get("done") is True:
                break
            next_url = page.get("nextRecordsUrl", "")
            prefix = f"/services/data/v{VERSION}/query/"
            if not next_url.startswith(prefix) or next_url in seen_pages:
                raise DataError("O Salesforce retornou uma paginação inválida.")
            seen_pages.add(next_url)
            page = self.read(next_url.split(f"/services/data/v{VERSION}/", 1)[1])
        if expected != len(records):
            raise DataError("A consulta retornou dados incompletos. O total não será exibido parcialmente.")
        return records


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


def sql_value(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def report_where(metadata):
    """Fail closed on report features without a verified SOQL equivalent."""
    blocks = metadata.get("blocks")
    if blocks:
        if len(blocks) != 1:
            raise DataError("O relatório mudou para vários blocos. Revise os filtros antes de somar.")
        definition = blocks[0]
    else:
        definition = metadata
    if (definition.get("scope") != "organization" or metadata.get("crossFilters") or
            definition.get("reportBooleanFilter") or definition.get("standardFilters")):
        raise DataError("O relatório passou a usar filtros adicionais. Revise a equivalência SOQL.")
    allowed = {"Opportunity.StageName": "StageName", "Opportunity.Regional__c": "Regional__c",
               "Opportunity.Name": "Name", "Contract.Status": "Contract.Status",
               "Opportunity.Unidade_De_Neg_cio__c": "Unidade_De_Neg_cio__c"}
    clauses = []
    for rule in definition.get("reportFilters", []):
        field = allowed.get(rule.get("column"))
        operator, value = rule.get("operator"), rule.get("value")
        if not field or value is None:
            raise DataError("O relatório contém um filtro novo. Revise a equivalência SOQL.")
        values = [part.strip() for part in value.split(",")]
        if operator == "equals":
            clauses.append(f"{field} IN ({','.join(sql_value(v) for v in values)})")
        elif operator == "notEqual":
            # The report uses an outer join: absent contracts remain eligible.
            condition = f"{field} NOT IN ({','.join(sql_value(v) for v in values)})"
            clauses.append(f"({field} = null OR {condition})")
        elif operator == "notContain" and field == "Name":
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(f"(NOT {field} LIKE {sql_value('%' + escaped + '%')})")
        else:
            raise DataError("O relatório contém um operador novo. Revise a equivalência SOQL.")
    dates = definition.get("standardDateFilter", {})
    if dates.get("column") != "Opportunity.ContratoGeradoEm__c" or not clauses:
        raise DataError("O escopo de datas do relatório mudou. Revise a consulta.")
    try:
        first, last = (date.fromisoformat(dates[key]) for key in ("startDate", "endDate"))
    except (KeyError, ValueError, TypeError):
        raise DataError("O relatório não informou um intervalo de datas válido.") from None
    clauses.extend([f"ContratoGeradoEm__c >= {first.isoformat()}",
                    f"ContratoGeradoEm__c <= {last.isoformat()}"])
    return " AND ".join(clauses)


def sales_query(metadata, today):
    last = min(today, DEADLINE - timedelta(days=1))
    if last < START:
        return None
    # Source sheet BF = IF(AM = blank; I; AM): confirmation, otherwise sale date.
    period = (f"((DataConfirmacaoVenda2__c >= {START} AND DataConfirmacaoVenda2__c <= {last}) OR "
              f"(DataConfirmacaoVenda2__c = null AND DataVenda__c >= {START} AND DataVenda__c <= {last}))")
    return ("SELECT Id, IDOportunidade__c, DataConfirmacaoVenda2__c, DataVenda__c, "
            "Valor_Real_de_Venda__c FROM Opportunity WHERE " + report_where(metadata) +
            " AND " + period + " ORDER BY Id ASC")


@st.cache_data(ttl=900, show_spinner=False, max_entries=4)
def fetch_sales(user, password, token, today_iso):
    sf = Salesforce(user, password, token)
    metadata = sf.read(f"analytics/reports/{REPORT}/describe")["reportMetadata"]
    today = date.fromisoformat(today_iso)
    query = sales_query(metadata, today)
    records = {}
    for row in sf.query(query) if query else []:
        raw_date = row.get("DataConfirmacaoVenda2__c") or row.get("DataVenda__c")
        try:
            confirmed = date.fromisoformat(raw_date)
        except (TypeError, ValueError):
            raise DataError("Há oportunidades sem uma data válida de confirmação ou venda.") from None
        if not START <= confirmed < DEADLINE or confirmed > today:
            raise DataError("A consulta retornou uma venda fora do período esperado.")
        identity = row.get("Id")
        if not identity:
            raise DataError("Há oportunidades sem identificação única.")
        record = (confirmed.isoformat(), str(amount({"value": row.get("Valor_Real_de_Venda__c")})))
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
    .totals>div{background:transparent;border:0;border-radius:0;
    padding:.8vh 1vw;box-shadow:none}
    .stat-label{font-size:clamp(14px,2.3vh,28px);color:#334a65;line-height:1.2}
    .stat-value{font-size:clamp(22px,4.3vh,52px);color:#084589;font-weight:650;white-space:nowrap;
    background:transparent;border:0;padding:0;margin-top:.5vh;box-shadow:none}
    .remaining{display:flex;flex-direction:column;justify-content:center;text-align:center;
    background:linear-gradient(120deg,rgba(7,53,104,.92),rgba(8,69,137,.86));border:0;border-radius:16px;
    box-shadow:0 8px 28px rgba(7,53,104,.22);padding:1vh 2vw;min-height:0}
    .remaining-label{font-size:clamp(16px,2.6vh,32px);letter-spacing:.08em;text-transform:uppercase;color:#e4efff}
    .remaining-value{font-size:clamp(32px,8vh,100px);font-weight:750;line-height:1.2;
    color:#fff;white-space:nowrap;text-shadow:none;
    background:transparent;border:0;padding:0;margin-top:.6vh;box-shadow:none}
    .pace{display:flex;justify-content:space-between;align-items:center;gap:2vw;
    font-size:clamp(12px,2.5vh,30px);color:#334a65}
    .pace>span{background:transparent;border:0;border-radius:0;
    padding:.8vh 1vw;flex:1;text-align:center;box-shadow:none}
    .pace strong{display:inline-block;color:#084589;margin-left:10px;white-space:nowrap;
    background:transparent;border:0;padding:0;box-shadow:none}
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



