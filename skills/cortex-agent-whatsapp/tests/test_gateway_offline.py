"""Offline behaviour test for gateway.py: routes, HMAC, dedup, formatting, config validation.

Snowflake and Meta are mocked; no network. Run: python3 tests/test_gateway_offline.py
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "assets", "gateway"))

secret = {"SNOWFLAKE_PAT": "pat", "META_APP_SECRET": "appsecret", "WA_PHONE_NUMBER_ID": "123",
          "WA_SYSTEM_TOKEN": "tok", "WA_VERIFY_TOKEN": "verify-me"}
tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump(secret, tmp)
tmp.close()
os.environ.update({"SECRET_FILE": tmp.name, "SNOWFLAKE_HOST": "org-acct.snowflakecomputing.com",
                   "AGENT_FQN": "MY_DB.AGENTS.BOT", "BOT_ROLE": "R", "BOT_WAREHOUSE": "WH",
                   "STATE_SCHEMA": "MY_DB.STATE", "TIMEZONE": "Europe/Madrid", "BOT_LANG": "en"})
import gateway as g  # noqa: E402

calls = {"sql": [], "agent": [], "wa": []}
inbound_seen = set()


def fake_sql(statement, *params, timeout=45):
    calls["sql"].append((statement.split()[0], params))
    if "COUNT(*) FROM WA_EVENTS" in statement:
        return [[1 if params[0] in inbound_seen else 0]]
    if "EVENT_TYPE" in statement and "'inbound'" in statement and statement.startswith("INSERT"):
        inbound_seen.add(params[0])
    if statement.lstrip().startswith("SELECT MSG_ROLE"):
        return [["user", "earlier question"], ["assistant", "earlier answer"]]
    return []


def fake_agent(messages):
    calls["agent"].append(messages)
    return "## Result\n**Bold** item\n- one\n- two\n| a | b |\n|---|---|\n| 1 | 2 |"


def fake_wa(payload):
    calls["wa"].append(payload)
    return 200, {"messages": [{"id": "wamid.out"}]}


g.sql, g.run_agent, g.wa_api = fake_sql, fake_agent, fake_wa


def ev(method, path, body=b"", headers=None, query=None):
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "headers": headers or {},
            "queryStringParameters": query, "isBase64Encoded": True, "body": base64.b64encode(body).decode()}


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.failed = True


check.failed = False

c = g.cfg()
check("agent path built from AGENT_FQN", c["AGENT_PATH"] == "/api/v2/databases/MY_DB/schemas/AGENTS/agents/BOT:run")

r = g.handler(ev("GET", "/whatsapp", query={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"}), None)
check("verify handshake echoes challenge", r["statusCode"] == 200 and r["body"] == "42")
r = g.handler(ev("GET", "/whatsapp", query={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"}), None)
check("wrong verify token -> 403", r["statusCode"] == 403)

msg = {"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.in1", "from": "15550001111", "type": "text",
                                                         "text": {"body": "hello"}}]}}]}]}
raw = json.dumps(msg).encode()
r = g.handler(ev("POST", "/whatsapp", raw, {"X-Hub-Signature-256": "sha256=deadbeef"}), None)
check("bad signature -> 401", r["statusCode"] == 401 and not calls["agent"])

sig = "sha256=" + hmac.new(b"appsecret", raw, hashlib.sha256).hexdigest()
r = g.handler(ev("POST", "/whatsapp", raw, {"X-Hub-Signature-256": sig}), None)  # context None -> synchronous worker
check("signed webhook -> 200", r["statusCode"] == 200)
check("agent called once with history + context line", len(calls["agent"]) == 1 and len(calls["agent"][0]) == 3
      and calls["agent"][0][-1]["content"][0]["text"].startswith("[channel=whatsapp; user_phone=+15550001111; today="))
sent = [p for p in calls["wa"] if p.get("type") == "text"]
body = sent[-1]["text"]["body"] if sent else ""
check("reply converted to WhatsApp format", "*Bold*" in body and "• one" in body and "## " not in body and "---" not in body
      and "• 1 · 2" in body)
check("typing indicator sent", any(p.get("typing_indicator") for p in calls["wa"]))

g.handler(ev("POST", "/whatsapp", raw, {"X-Hub-Signature-256": sig}), None)
check("Meta retry deduplicated (agent not called again)", len(calls["agent"]) == 1)

r = g.handler(ev("POST", "/api/chat", json.dumps({"message": "hi", "session_id": "attacker-chosen"}).encode()), None)
d = json.loads(r["body"])
check("web chat ok and server issues its own session id", r["statusCode"] == 200 and d["session_id"] != "attacker-chosen"
      and len(d["session_id"]) >= 20)

r = g.handler(ev("POST", "/api/chat", json.dumps({"message": "reset", "session_id": d["session_id"]}).encode()), None)
check("reset word handled without calling the agent", json.loads(r["body"])["reply"].startswith("Done") and len(calls["agent"]) == 2)

r = g.handler(ev("GET", "/"), None)
check("web page served", r["statusCode"] == 200 and "<html" in r["body"])
r = g.handler(ev("GET", "/nope"), None)
check("unknown route -> 404", r["statusCode"] == 404)

os.environ["ENABLE_WEB"] = "false"
g._CFG = None
r = g.handler(ev("POST", "/api/chat", json.dumps({"message": "hi"}).encode()), None)
check("ENABLE_WEB=false hides /api/chat", r["statusCode"] == 404)

os.environ["AGENT_FQN"] = "MY_DB.AGENTS.BOT;DROP"
g._CFG = None
try:
    g.cfg()
    check("invalid AGENT_FQN rejected", False)
except RuntimeError:
    check("invalid AGENT_FQN rejected", True)

check("long text split under 3500 chars", all(len(x) <= 3500 for x in g.split_chunks("x" * 9000)))
sys.exit(1 if check.failed else 0)
