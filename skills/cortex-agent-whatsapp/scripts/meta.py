"""Meta / WhatsApp helper for the Cortex Agent gateway. Never prints secret values.

Reads the gateway secret JSON (SECRET_FILE, default ~/.cortex-agent-whatsapp/secret.json).

  python3 meta.py check                         validate token, phone number id and WABA id
  python3 meta.py send <to_phone>               send a free-text test message (needs an open 24h window:
                                                the recipient must have messaged the business number first)
  python3 meta.py register <gateway_url>        set <gateway_url>/whatsapp as the app's webhook + subscribe the WABA
  python3 meta.py e2e <gateway_url> <from_phone> [text] [--conn <snow connection>] [--state <DB.SCHEMA>]
                                                send a correctly signed fake inbound message to the gateway, then
                                                (with --conn/--state) poll Snowflake until the reply is delivered
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v26.0"
SECRET_FILE = os.path.expanduser(os.environ.get("SECRET_FILE", "~/.cortex-agent-whatsapp/secret.json"))
REQUIRED = ["META_APP_ID", "META_APP_SECRET", "WA_PHONE_NUMBER_ID", "WA_WABA_ID", "WA_SYSTEM_TOKEN", "WA_VERIFY_TOKEN"]


def load():
    s = json.load(open(SECRET_FILE))
    missing = [k for k in REQUIRED if not s.get(k) or str(s[k]).startswith("<")]
    if missing:
        sys.exit(f"Missing values in {SECRET_FILE}: {', '.join(missing)}")
    return s


def call(method, path, token=None, params=None, body=None, form=None):
    url = f"{GRAPH}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(s):
    tok, app_token, ok = s["WA_SYSTEM_TOKEN"], f"{s['META_APP_ID']}|{s['META_APP_SECRET']}", True
    st, d = call("GET", "debug_token", params={"input_token": tok, "access_token": app_token})
    data = d.get("data", {})
    exp = data.get("expires_at")
    print("1. token valid:", data.get("is_valid"), "| type:", data.get("type"),
          "| expires:", "never" if exp == 0 else time.strftime("%Y-%m-%d", time.gmtime(exp)) if exp else exp,
          "| scopes:", data.get("scopes"))
    if st != 200 or not data.get("is_valid"):
        ok = False
        print("   ->", d.get("error", d))
    if data.get("app_id") and data["app_id"] != s["META_APP_ID"]:
        ok = False
        print("   -> token belongs to a different app than META_APP_ID")
    missing = {"whatsapp_business_messaging", "whatsapp_business_management"} - set(data.get("scopes") or [])
    if missing:
        ok = False
        print("   -> token lacks scopes:", sorted(missing))
    st, d = call("GET", s["WA_PHONE_NUMBER_ID"], tok, params={"fields": "display_phone_number,verified_name,quality_rating"})
    print("2. phone number:", st, {k: d.get(k) for k in ("display_phone_number", "verified_name", "quality_rating")}
          if st == 200 else d.get("error", d))
    ok &= st == 200
    st, d = call("GET", f"{s['WA_WABA_ID']}/phone_numbers", tok, params={"fields": "id,display_phone_number"})
    ids = [p.get("id") for p in d.get("data", [])]
    print("3. WABA phone numbers:", st, ids if st == 200 else d.get("error", d))
    if st == 200 and s["WA_PHONE_NUMBER_ID"] not in ids:
        ok = False
        print("   -> WA_PHONE_NUMBER_ID is not under WA_WABA_ID")
    ok &= st == 200
    print("\nRESULT:", "ALL OK" if ok else "SOMETHING FAILED (see above)")
    return ok


def send(s, to):
    st, d = call("POST", f"{s['WA_PHONE_NUMBER_ID']}/messages", s["WA_SYSTEM_TOKEN"], body={
        "messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text",
        "text": {"body": "Test message from the Cortex Agent gateway setup."}})
    print("send:", st, d.get("messages", d.get("error", d)))
    if st == 200:
        print("Accepted by Meta. If it never arrives: the 24h window is closed (message the number first) "
              "or, on a test number, the recipient is not in the allowed list (error 131030 in WA_EVENTS).")


def register(s, url):
    callback = url.rstrip("/") + "/whatsapp"
    app_token = f"{s['META_APP_ID']}|{s['META_APP_SECRET']}"
    st, d = call("POST", f"{s['META_APP_ID']}/subscriptions", form={
        "object": "whatsapp_business_account", "callback_url": callback, "verify_token": s["WA_VERIFY_TOKEN"],
        "fields": "messages", "include_values": "true", "access_token": app_token})
    print("1. app webhook subscription:", st, d)
    st, d = call("POST", f"{s['WA_WABA_ID']}/subscribed_apps", s["WA_SYSTEM_TOKEN"])
    print("2. WABA subscribed_apps:", st, d)
    st, d = call("GET", f"{s['META_APP_ID']}/subscriptions", params={"access_token": app_token})
    for x in d.get("data", []):
        print("3. active:", x.get("object"), x.get("callback_url"), [f.get("name") for f in x.get("fields", [])],
              "active=", x.get("active"))


def e2e(s, url, phone, text, conn=None, state=None):
    phone = phone.lstrip("+")
    if not phone.isdigit():
        sys.exit("from_phone must be digits with country code, e.g. 15551234567")
    if state and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*", state):
        sys.exit("--state must be DB.SCHEMA")
    msg_id = f"wamid.E2ETEST{int(time.time())}"
    payload = {"object": "whatsapp_business_account", "entry": [{"id": s["WA_WABA_ID"], "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": s["WA_PHONE_NUMBER_ID"]},
        "contacts": [{"profile": {"name": "E2E Test"}, "wa_id": phone}],
        "messages": [{"from": phone, "id": msg_id, "timestamp": str(int(time.time())), "type": "text", "text": {"body": text}}]}}]}]}
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(s["META_APP_SECRET"].encode(), body, hashlib.sha256).hexdigest()
    t0 = time.time()
    req = urllib.request.Request(url.rstrip("/") + "/whatsapp", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"webhook ack: HTTP {r.status} in {time.time() - t0:.2f}s (must be fast; the reply is async)")
    if not (conn and state):
        print("Check your phone for the reply (add --conn and --state to poll Snowflake).")
        return

    def q(sql):
        out = subprocess.run(["snow", "sql", "-c", conn, "--format", "json", "-q", sql], capture_output=True, text=True)
        return json.loads(out.stdout or "[]")

    for _ in range(36):  # up to ~3 minutes
        time.sleep(5)
        rows = q(f"""WITH inbound AS (SELECT MIN(TS) AS T0 FROM {state}.WA_EVENTS WHERE EVENT_TYPE = 'inbound' AND MSG_ID = '{msg_id}')
                    SELECT e.EVENT_TYPE, e.MSG_ID, e.STATUS, e.ERROR FROM {state}.WA_EVENTS e, inbound
                     WHERE e.TS >= inbound.T0 AND e.PHONE = '{phone}' ORDER BY e.TS""")
        rejected = [r for r in rows if r["EVENT_TYPE"] == "outbound" and r["STATUS"] != "accepted"]
        if rejected:
            print(f"agent answered, but Meta rejected the send after {time.time() - t0:.1f}s:", rejected[-1]["ERROR"])
            return
        out_ids = {r["MSG_ID"] for r in rows if r["EVENT_TYPE"] == "outbound" and r["MSG_ID"]}
        statuses = [(r["STATUS"], r["ERROR"]) for r in rows if r["EVENT_TYPE"] == "status" and r["MSG_ID"] in out_ids]
        errors = [r["ERROR"] for r in rows if r["EVENT_TYPE"] == "worker_error"]
        if errors:
            print("WORKER ERROR:", errors[-1])
            return
        if any(x[0] in ("delivered", "read") for x in statuses):
            print(f"delivered to the phone after {time.time() - t0:.1f}s: {statuses}")
            return
        if any(x[0] == "failed" for x in statuses):
            print("DELIVERY FAILED:", statuses)
            return
    print("timeout: no delivery status after 3 minutes (check WA_EVENTS and the Lambda logs)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in ("check", "send", "register", "e2e"):
        sys.exit(__doc__)
    secret = load()
    opt = {k: a[a.index(k) + 1] for k in ("--conn", "--state") if k in a}
    pos = [x for i, x in enumerate(a) if not x.startswith("--") and (i == 0 or not a[i - 1].startswith("--"))]
    if pos[0] == "check":
        sys.exit(0 if check(secret) else 1)
    if pos[0] == "send":
        send(secret, pos[1])
    if pos[0] == "register":
        register(secret, pos[1])
    if pos[0] == "e2e":
        e2e(secret, pos[1], pos[2], pos[3] if len(pos) > 3 else "Hi! What can you help me with?",
            opt.get("--conn"), opt.get("--state"))
