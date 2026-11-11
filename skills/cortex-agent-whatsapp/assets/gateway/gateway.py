"""Cortex Agent gateway: WhatsApp (Meta Cloud API) + web chat -> Snowflake Cortex Agent REST API.

One AWS Lambda behind a public Function URL.

Routes
  GET  /             web chat page (index.html), only if ENABLE_WEB=true
  GET  /whatsapp     Meta webhook verification (hub.challenge handshake)
  POST /whatsapp     Meta webhook events, HMAC-verified; replies are produced asynchronously
  POST /api/chat     one web chat turn (synchronous), only if ENABLE_WEB=true
  GET  /api/health   liveness

Internal jobs (Lambda invoke payloads, never reachable from the Function URL)
  {"job": "wa_reply", ...}   async WhatsApp worker (the function re-invokes itself)
  {"job": "egress_ip"}       returns the public IP Snowflake will see (for the network rule)

Configuration
  Environment (non-secret): SNOWFLAKE_HOST, AGENT_FQN, BOT_ROLE, BOT_WAREHOUSE, STATE_SCHEMA,
                            TIMEZONE, BOT_LANG, ENABLE_WEB, HISTORY_TURNS, CONTEXT_LINE
  Secret (Secrets Manager JSON at SECRET_ID, or a local JSON file at SECRET_FILE for dev):
                            SNOWFLAKE_PAT, META_APP_SECRET, WA_PHONE_NUMBER_ID, WA_SYSTEM_TOKEN, WA_VERIFY_TOKEN
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v26.0"
MAX_USER_CHARS = 1000
WA_CHUNK = 3500
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

TEXTS = {
    "en": {
        "reset_words": {"reset", "restart", "start over", "new conversation"},
        "reset_ok": "Done, let's start over. How can I help?",
        "empty": "Sorry, I couldn't process that. Could you rephrase it?",
        "error": "Sorry, I'm having a technical problem. Please try again in a moment.",
        "text_only": "For now I can only read text messages. Could you type your question?",
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    },
    "es": {
        "reset_words": {"reiniciar", "reset", "empezar de nuevo", "nueva conversación", "nueva conversacion"},
        "reset_ok": "Listo, empezamos de nuevo. ¿En qué te puedo ayudar?",
        "empty": "Perdona, no he podido procesar tu consulta. ¿Puedes reformularla?",
        "error": "Perdona, estoy teniendo un problema técnico. Inténtalo de nuevo en un momento.",
        "text_only": "Por ahora solo puedo leer mensajes de texto. ¿Me escribes tu consulta?",
        "days": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
    },
}

_CFG = None


# ---------------------------------------------------------------- config
def _env_flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _fqn(value, parts, name):
    items = value.split(".")
    if len(items) != parts or not all(IDENT.match(p) for p in items):
        raise RuntimeError(f"{name} must be {parts} unquoted identifiers separated by dots, got {value!r}")
    return [p.upper() for p in items]


def cfg():
    """Merge environment config with secrets (Secrets Manager in Lambda, JSON file locally)."""
    global _CFG
    if _CFG is not None:
        return _CFG
    if os.environ.get("SECRET_ID"):
        import boto3  # bundled with the Lambda Python runtime
        c = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SECRET_ID"])["SecretString"])
    else:
        c = json.load(open(os.path.expanduser(os.environ.get("SECRET_FILE", "~/.cortex-agent-whatsapp/secret.json"))))
    db, sch, agent = _fqn(os.environ["AGENT_FQN"], 3, "AGENT_FQN")
    state_db, state_sch = _fqn(os.environ["STATE_SCHEMA"], 2, "STATE_SCHEMA")
    lang = os.environ.get("BOT_LANG", "en")
    c.update({
        "SNOWFLAKE_HOST": os.environ["SNOWFLAKE_HOST"],
        "AGENT_PATH": "/api/v2/databases/{}/schemas/{}/agents/{}:run".format(*map(urllib.parse.quote, (db, sch, agent))),
        "BOT_ROLE": os.environ["BOT_ROLE"],
        "BOT_WAREHOUSE": os.environ["BOT_WAREHOUSE"],
        "STATE_DB": state_db,
        "STATE_SCHEMA": state_sch,
        "TZ": os.environ.get("TIMEZONE", "UTC"),
        "T": TEXTS.get(lang, TEXTS["en"]),
        "ENABLE_WEB": _env_flag("ENABLE_WEB", "true"),
        "CONTEXT_LINE": _env_flag("CONTEXT_LINE", "true"),
        "HISTORY_TURNS": int(os.environ.get("HISTORY_TURNS", "12")),
    })
    _CFG = c
    return _CFG


def now_local():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(cfg()["TZ"]))
    except Exception:  # noqa: BLE001
        return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------- http helpers
def _http(url, method="GET", headers=None, body=None, timeout=30):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _sf_headers():
    return {
        "Authorization": f"Bearer {cfg()['SNOWFLAKE_PAT']}",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------- snowflake
def sql(statement, *params, timeout=45):
    """Run one statement through the SQL API with positional binds. Returns rows (list of lists)."""
    c = cfg()
    bindings = {}
    for i, p in enumerate(params, 1):
        if p is None:
            bindings[str(i)] = {"type": "TEXT", "value": None}
        elif isinstance(p, int) and not isinstance(p, bool):
            bindings[str(i)] = {"type": "FIXED", "value": str(p)}
        else:
            bindings[str(i)] = {"type": "TEXT", "value": str(p)}
    body = {"statement": statement, "timeout": timeout, "warehouse": c["BOT_WAREHOUSE"], "role": c["BOT_ROLE"],
            "database": c["STATE_DB"], "schema": c["STATE_SCHEMA"], "bindings": bindings}
    st, raw = _http(f"https://{c['SNOWFLAKE_HOST']}/api/v2/statements", "POST", _sf_headers(), body, timeout + 15)
    d = json.loads(raw or b"{}")
    if st != 200:
        raise RuntimeError(f"SQL API {st}: {d.get('message', raw[:300])}")
    return d.get("data", [])


def run_agent(messages):
    """Call the Cortex Agent (non-streaming). Returns the concatenated text answer."""
    c = cfg()
    st, raw = _http(f"https://{c['SNOWFLAKE_HOST']}{c['AGENT_PATH']}", "POST", _sf_headers(),
                    {"messages": messages, "stream": False}, timeout=110)
    d = json.loads(raw or b"{}")
    if st != 200:
        raise RuntimeError(f"Agent API {st}: {d.get('message', raw[:300])}")
    return "\n\n".join(x.get("text", "") for x in d.get("content", []) if x.get("type") == "text").strip()


def load_history(channel, key):
    rows = sql(
        """SELECT MSG_ROLE, MSG_TEXT FROM (
             SELECT TS, MSG_ROLE, MSG_TEXT FROM CONVERSATION_MESSAGES
              WHERE CHANNEL = ? AND SESSION_KEY = ? AND TS > DATEADD(hour, -24, CURRENT_TIMESTAMP())
                AND TS > COALESCE((SELECT MAX(TS) FROM CONVERSATION_MESSAGES
                                    WHERE CHANNEL = ? AND SESSION_KEY = ? AND MSG_ROLE = 'reset'), '1970-01-01'::TIMESTAMP_LTZ)
                AND MSG_ROLE IN ('user', 'assistant')
              ORDER BY TS DESC LIMIT ?)
           ORDER BY TS""", channel, key, channel, key, cfg()["HISTORY_TURNS"])
    return [{"role": r[0], "content": [{"type": "text", "text": r[1]}]} for r in rows]


def log_msg(channel, key, role, text, msg_id=None, latency_ms=None):
    sql("INSERT INTO CONVERSATION_MESSAGES (CHANNEL, SESSION_KEY, MSG_ROLE, MSG_TEXT, MSG_ID, LATENCY_MS) "
        "SELECT ?, ?, ?, ?, ?, ?", channel, key, role, text, msg_id, latency_ms)


def context_line(channel, phone=None):
    """Prefix telling the agent the channel, the user's phone (WhatsApp) and today's date."""
    now = now_local()
    parts = [f"channel={channel}"]
    if phone:
        parts.append(f"user_phone=+{phone.lstrip('+')}")
    parts.append(f"today={now:%Y-%m-%d} ({cfg()['T']['days'][now.weekday()]})")
    return "[" + "; ".join(parts) + "]"


def converse(channel, key, user_text, phone=None, msg_id=None):
    """One conversation turn. History lives in Snowflake, keyed by channel + session key."""
    t = cfg()["T"]
    user_text = user_text.strip()[:MAX_USER_CHARS]
    if user_text.lower().strip(" .!¡?¿") in t["reset_words"]:
        log_msg(channel, key, "reset", "")
        return t["reset_ok"], 0
    prompt = f"{context_line(channel, phone)}\n{user_text}" if cfg()["CONTEXT_LINE"] else user_text
    messages = load_history(channel, key) + [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    t0 = time.time()
    reply = run_agent(messages) or t["empty"]
    latency = int((time.time() - t0) * 1000)
    log_msg(channel, key, "user", user_text, msg_id)
    log_msg(channel, key, "assistant", reply, None, latency)
    return reply, latency


# ---------------------------------------------------------------- whatsapp
def wa_signature_ok(raw_body: bytes, header: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(cfg()["META_APP_SECRET"].encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def wa_api(payload):
    c = cfg()
    st, raw = _http(f"{GRAPH}/{c['WA_PHONE_NUMBER_ID']}/messages", "POST",
                    {"Authorization": f"Bearer {c['WA_SYSTEM_TOKEN']}", "Content-Type": "application/json"},
                    payload, timeout=20)
    return st, json.loads(raw or b"{}")


def to_whatsapp_format(text):
    """Markdown -> WhatsApp formatting: **b** -> *b*, headings and tables flattened, links inlined."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if re.fullmatch(r"\|?\s*:?-{2,}.*", s):
            continue
        if s.startswith("|") and s.endswith("|"):
            cells = [x.strip() for x in s.strip("|").split("|") if x.strip()]
            line = "• " + " · ".join(cells)
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*]\s+", "• ", line)
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"_\1_", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_chunks(text, size=WA_CHUNK):
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > size and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return [c[i:i + size] for c in chunks for i in range(0, len(c), size)]


def wa_reply_job(job):
    """Async worker: read receipt + typing indicator, agent turn, send the reply."""
    phone, msg_id, text = job["from"], job["msg_id"], job["text"]
    wa_api({"messaging_product": "whatsapp", "status": "read", "message_id": msg_id,
            "typing_indicator": {"type": "text"}})
    try:
        reply, latency = converse("whatsapp", phone, text, phone=phone, msg_id=msg_id)
    except Exception as e:  # noqa: BLE001
        sql("INSERT INTO WA_EVENTS (EVENT_TYPE, MSG_ID, PHONE, ERROR) SELECT 'worker_error', ?, ?, ?",
            msg_id, phone, repr(e)[:2000])
        reply, latency = cfg()["T"]["error"], None
    results = []
    for chunk in split_chunks(to_whatsapp_format(reply)):
        st, d = wa_api({"messaging_product": "whatsapp", "to": phone, "type": "text",
                        "text": {"body": chunk, "preview_url": False}})
        out_id = (d.get("messages") or [{}])[0].get("id")
        sql("INSERT INTO WA_EVENTS (EVENT_TYPE, MSG_ID, STATUS, PHONE, ERROR) SELECT 'outbound', ?, ?, ?, ?",
            out_id, "accepted" if st == 200 else f"http_{st}", phone, None if st == 200 else json.dumps(d)[:2000])
        results.append(st)
    return {"sent": results, "latency_ms": latency}


def handle_wa_webhook(raw_body: bytes, signature: str, context):
    if not wa_signature_ok(raw_body, signature):
        return _resp(401, {"error": "bad signature"})
    payload = json.loads(raw_body or b"{}")
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            for st in v.get("statuses", []):
                err = st.get("errors")
                sql("INSERT INTO WA_EVENTS (EVENT_TYPE, MSG_ID, STATUS, PHONE, ERROR, PAYLOAD) "
                    "SELECT 'status', ?, ?, ?, ?, PARSE_JSON(?)",
                    st.get("id"), st.get("status"), st.get("recipient_id"), json.dumps(err) if err else None, json.dumps(st))
            for m in v.get("messages", []):
                mid, frm, mtype = m.get("id"), m.get("from"), m.get("type")
                seen = sql("SELECT COUNT(*) FROM WA_EVENTS WHERE EVENT_TYPE = 'inbound' AND MSG_ID = ?", mid)
                if seen and int(seen[0][0]) > 0:
                    continue  # Meta retry of a message already accepted
                sql("INSERT INTO WA_EVENTS (EVENT_TYPE, MSG_ID, STATUS, PHONE, PAYLOAD) SELECT 'inbound', ?, ?, ?, PARSE_JSON(?)",
                    mid, mtype, frm, json.dumps(m))
                if mtype == "text":
                    text = m.get("text", {}).get("body", "")
                elif mtype == "interactive":
                    i = m.get("interactive", {})
                    text = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                else:
                    text = ""
                if not text:
                    wa_api({"messaging_product": "whatsapp", "to": frm, "type": "text",
                            "text": {"body": cfg()["T"]["text_only"]}})
                    continue
                job = {"job": "wa_reply", "from": frm, "msg_id": mid, "text": text}
                if context is not None:
                    import boto3
                    boto3.client("lambda").invoke(FunctionName=context.invoked_function_arn,
                                                  InvocationType="Event", Payload=json.dumps(job).encode())
                else:
                    wa_reply_job(job)  # local dev: synchronous
    return _resp(200, {"ok": True})


# ---------------------------------------------------------------- web
def handle_chat(body):
    msg = (body.get("message") or "").strip()
    if not msg:
        return _resp(400, {"error": "empty message"})
    sid = body.get("session_id") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", sid):
        sid = secrets.token_urlsafe(24)  # server-issued, unguessable session key
    reply, latency = converse("web", sid, msg)
    return _resp(200, {"session_id": sid, "reply": reply, "latency_ms": latency})


# ---------------------------------------------------------------- lambda entry
def _resp(status, body, content_type="application/json"):
    return {"statusCode": status, "headers": {"Content-Type": content_type, "Cache-Control": "no-store"},
            "body": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)}


def handler(event, context):
    job = event.get("job")
    if job == "wa_reply":
        return wa_reply_job(event)
    if job == "egress_ip":
        return {"egress_ip": _http("https://checkip.amazonaws.com", timeout=10)[1].decode().strip()}

    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", "GET"), event.get("rawPath", "/")
    raw = event.get("body") or ""
    raw_bytes = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    try:
        if path == "/whatsapp" and method == "GET":
            q = event.get("queryStringParameters") or {}
            if q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), cfg()["WA_VERIFY_TOKEN"]):
                return {"statusCode": 200, "headers": {"Content-Type": "text/plain"}, "body": q.get("hub.challenge", "")}
            return _resp(403, {"error": "verification failed"})
        if path == "/whatsapp" and method == "POST":
            return handle_wa_webhook(raw_bytes, headers.get("x-hub-signature-256", ""), context)
        if path == "/api/health":
            return _resp(200, {"ok": True, "ts": now_local().isoformat()})
        if cfg()["ENABLE_WEB"]:
            if path == "/api/chat" and method == "POST":
                return handle_chat(json.loads(raw_bytes or b"{}"))
            if path in ("/", "/index.html") and method == "GET":
                html = open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()
                return _resp(200, html, "text/html; charset=utf-8")
        return _resp(404, {"error": "not found"})
    except Exception as e:  # noqa: BLE001
        print("ERROR", path, repr(e))
        return _resp(500, {"error": "internal error"})
