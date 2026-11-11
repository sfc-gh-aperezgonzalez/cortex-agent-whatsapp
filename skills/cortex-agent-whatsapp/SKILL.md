---
name: cortex-agent-whatsapp
description: Deploy a Snowflake Cortex Agent to WhatsApp (Meta Cloud API) and a public web chat through an AWS Lambda gateway. Wizard that picks an existing Cortex Agent or creates a new one over a semantic view, creates a least-privilege service user with a role-restricted PAT and a network policy, deploys the Lambda (Function URL, Secrets Manager, fixed egress IP), guides the Meta developer app setup (test number, system user token, webhook) and runs an end-to-end test. Triggers: cortex agent whatsapp, whatsapp bot snowflake, cortex agent on whatsapp, expose cortex agent publicly, cortex agent web chat, meta cloud api webhook snowflake, whatsapp gateway lambda.
---

# Cortex Agent on WhatsApp + web chat

Deploys this architecture into the user's own Snowflake and AWS accounts:

```
WhatsApp user ─► Meta Cloud API ─► (webhook, HMAC) ─┐
                                                    ├─► AWS Lambda gateway ─► Cortex Agent REST API (PAT)
Web visitor ─────────────► (HTTPS /api/chat) ───────┘        │                      └─► tools (Analyst, Search, procedures)
                                                             └─► SQL API: conversation history + webhook events
Lambda ─► Meta Graph API /messages (reply)
```

Why a relay: Meta webhooks cannot send a Snowflake auth header, and Snowflake ingress (SPCS, App Runtime,
REST APIs) always requires Snowflake authentication. The Lambda verifies Meta's HMAC signature, then calls
Snowflake outbound with a PAT.

Files used by this skill (paths relative to this SKILL.md):

| Path | Purpose |
|---|---|
| `assets/gateway/gateway.py`, `assets/gateway/index.html` | Lambda code and the web chat page |
| `assets/sql/01_bot_identity_and_state.sql` | Role, service user, state schema and tables |
| `assets/sql/02_network_policy.sql` | Network rule + user-level network policy (fixed Lambda egress IP) |
| `assets/sql/03_agent_grants.sql` | Grants for the agent and its tools |
| `assets/sql/04_new_agent_template.sql` | Optional new agent over a semantic view |
| `scripts/deploy_lambda.sh` + `deploy.env.example` | Idempotent AWS deployment |
| `scripts/meta.py` | Validate Meta values, register the webhook, signed E2E test |
| `references/meta-setup.md` | Click-by-click Meta app setup |
| `references/network-egress.md` | Fixed egress IP options |
| `references/troubleshooting.md` | Error codes and fixes |

## Rules for the agent running this skill

- **Secrets never enter the chat.** Ask the user to write Meta values into the local secret file and confirm.
  Capture the PAT straight into that file (step 6). Never `cat` the secret file or print token values.
- **Stop and confirm** before: creating the Snowflake role/user/network policy, `CREATE OR REPLACE AGENT` on a
  name that exists, creating AWS resources, creating a NAT gateway (it costs money), and sending any WhatsApp
  message. Show exactly what will be created.
- Use `snow sql -c <connection> -f <file> -D key=value` for the SQL templates (`<% key %>` placeholders).
- Run independent checks in parallel; keep the user informed with one-line updates.
- Verify every step against the real system (query, invoke, curl) before moving on. Do not report success
  on a step you did not verify.

## Step 0 · Prerequisites

Check in parallel and report one table:

1. Snowflake: `snow connection list`; ask which connection to use. Run `SELECT CURRENT_ACCOUNT_NAME(),
   CURRENT_ORGANIZATION_NAME(), CURRENT_ROLE(), CURRENT_REGION()`. The role must be able to create roles,
   users, network rules/policies and grant privileges (typically ACCOUNTADMIN for a demo).
   `SNOWFLAKE_HOST` = `<org>-<account>.snowflakecomputing.com`, lowercase, with any `_` in the account name
   replaced by `-`.
2. AWS: `aws sts get-caller-identity --profile <p>` and the region. If the keys are expired, ask the user to
   refresh them.
3. Tools: `python3 --version` (3.9+), `zip`, `aws --version` (must support `add-permission --invoked-via-function-url`).
4. Meta: does the user already have a Meta developer app with WhatsApp? If not, step 4 covers it.

## Step 1 · Choose the agent

Ask: **use an existing Cortex Agent** or **create a new one**?

**Existing agent**
1. `SHOW AGENTS IN ACCOUNT;` and let the user pick. Then `DESCRIBE AGENT <db>.<schema>.<name>;` and read the
   specification: list every tool with its resource (semantic view, search service, procedure, warehouse).
2. Check it suits public users: the instructions should keep it scoped to the business (Meta's WhatsApp
   Business Messaging policy does not allow general-purpose AI chatbots since 2026-01-15), answer briefly,
   and avoid tables and long Markdown on WhatsApp. Offer to add short channel-aware instructions (see the
   `response` text in `04_new_agent_template.sql`) only with the user's approval: it changes their agent.
3. The gateway prefixes each message with `[channel=..; user_phone=..; today=..]`. If the agent should not
   see it, set `CONTEXT_LINE=false`; otherwise suggest adding one instruction line saying it is internal
   context (as in the template).

**New agent**
1. Find semantic views: `SHOW SEMANTIC VIEWS IN ACCOUNT;`. If none fits, stop and suggest building one first
   with the semantic view skill (`agent-studio`), then return here.
2. Ask for: business name, scope sentence ("orders, deliveries and returns for Acme customers"), agent
   location (`db.schema.name`) and tool warehouse. Edit the `TOOL_DESCRIPTION` and sample questions in a copy
   of `04_new_agent_template.sql` from the semantic view's description, show the diff, confirm, run it.
3. Confirm `DESCRIBE AGENT <db>.<schema>.<name>;` succeeds. The real test runs in step 7 with the bot PAT.

## Step 2 · Bot identity and state tables

Propose names (defaults: `CAWA_BOT_ROLE`, `CAWA_BOT_SVC`, state schema `<agent_db>.CAWA_STATE`), confirm,
then run `assets/sql/01_bot_identity_and_state.sql` with `-D bot_role= -D bot_user= -D warehouse=
-D state_db= -D state_schema=`. Verify with `SHOW GRANTS TO ROLE <bot_role>;` and `DESCRIBE USER <bot_user>;`.

## Step 3 · Grants for the agent and its tools

Copy `assets/sql/03_agent_grants.sql`, uncomment and fill one block per tool found in step 1, show it, run it.
Verify with `SHOW GRANTS TO ROLE <bot_role>;`: every object named in the agent's `tool_resources` must appear.
A missing grant shows up in step 7 as an agent answer like "I can't access that data".

## Step 4 · Meta app (user in the browser)

Walk the user through `references/meta-setup.md` steps 1 to 5, one at a time. Create the secret file first:

```bash
mkdir -p ~/.cortex-agent-whatsapp && chmod 700 ~/.cortex-agent-whatsapp
cp scripts/secret.json.example ~/.cortex-agent-whatsapp/secret.json && chmod 600 ~/.cortex-agent-whatsapp/secret.json
```

Generate `WA_VERIFY_TOKEN` yourself and write it into the file with a small Python one-liner (do not echo it).
Leave `SNOWFLAKE_PAT` as the placeholder for now. When the user says the Meta values are in, run
`python3 scripts/meta.py check` and fix anything that fails before continuing.

Explain the free test-number limits now (max 5 allowed recipients, each verified by code; 24-hour window; test
number shown as a +1 555 number; system-user token expiry). Production needs a real number (see README).

## Step 5 · Deploy the Lambda (placeholder PAT)

1. Fixed egress: follow `references/network-egress.md`. Find two private subnets with a NAT or transit
   gateway route and a security group with outbound HTTPS. If none exists, explain the NAT option and its
   cost; create it only with explicit approval.
2. Write `scripts/deploy.env` from `deploy.env.example` with the user's values (git-ignored). Show it.
3. Confirm, then `DEPLOY_ENV=scripts/deploy.env bash scripts/deploy_lambda.sh up`. It creates the IAM role
   (basic + VPC execution, inline policy for the secret and self-invoke), the secret, the function (python3.12,
   120 s, 512 MB, reserved concurrency), the public Function URL and its two conditioned permissions.
4. Verify: `curl -s <url>/api/health` returns `{"ok": true}`, and
   `curl -s "<url>/whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1"` returns 403.
5. Measure egress: `bash scripts/deploy_lambda.sh egress 3`. All three IPs must be identical. If not, the
   Lambda is not behind a fixed NAT: stop and fix step 5.1.

## Step 6 · Network policy, then the PAT

1. Run `assets/sql/02_network_policy.sql` with `-D bot_user= -D rule_db= -D rule_schema= -D egress_ip=<measured>`.
2. Generate the PAT and write it into the secret file **without printing it**:

```bash
snow sql -c <conn> --format json -q "ALTER USER <bot_user> ADD PROGRAMMATIC ACCESS TOKEN CAWA_GATEWAY_PAT ROLE_RESTRICTION = '<bot_role>' DAYS_TO_EXPIRY = 30" \
 | python3 -c "import json,os,sys; p=os.path.expanduser('~/.cortex-agent-whatsapp/secret.json'); s=json.load(open(p)); r=json.load(sys.stdin); r=r[0] if isinstance(r,list) else r; s['SNOWFLAKE_PAT']=r['token_secret']; json.dump(s,open(p,'w'),indent=1); print('PAT stored, name', r['token_name'])"
```

   If the token name exists, rotate instead (`ALTER USER <u> ROTATE PROGRAMMATIC ACCESS TOKEN CAWA_GATEWAY_PAT`
   gives a new `token_secret` the same way).
3. `bash scripts/deploy_lambda.sh secret` (pushes the secret and recycles execution environments).
4. Prove the block works: a request from the user's laptop with the PAT must fail with a network policy error,
   while the Lambda succeeds (next step).

## Step 7 · Connect WhatsApp and test end to end

1. `python3 scripts/meta.py register <url>`: both calls must return success and the subscription must show
   `messages`, `active=True`.
2. Web channel (if `ENABLE_WEB=true`): POST a question to `<url>/api/chat` with a Python client (shell `echo`
   can mangle JSON); expect a `reply` and a `session_id`. Open `<url>/` in the browser.
3. Ask the user to send "hi" from an allowed phone to the test number, then wait for the reply (typically
   10 to 30 s, the typing indicator shows meanwhile).
4. Automated check: `python3 scripts/meta.py e2e <url> <allowed_phone> "a real question" --conn <conn>
   --state <state_db>.<state_schema>` sends a signed inbound event and waits for Meta's `delivered` status.
5. Show the user `WA_EVENTS` and `CONVERSATION_MESSAGES` rows as proof, and the measured latency.

## Step 8 · Hand-off

Summarize: URL, agent, bot user/role, network rule IP, PAT expiry date, Meta token expiry date, and these
operations:

- Reset a conversation: the user sends `reset` (or `reiniciar` with `BOT_LANG=es`).
- Rotate the PAT before it expires (step 6.2 with ROTATE, then `deploy_lambda.sh secret`).
- Update code: `deploy_lambda.sh code`; settings: edit `deploy.env`, then `deploy_lambda.sh config`.
- WhatsApp only: `ENABLE_WEB=false`. The web chat is public and anonymous: anyone with the URL spends credits.
- Teardown: delete the Function URL, function, IAM role and secret; `DROP USER <bot_user>; DROP ROLE <bot_role>;
  DROP NETWORK POLICY CAWA_GATEWAY_NP; DROP NETWORK RULE ...; DROP SCHEMA <state>`; remove the webhook in Meta.

If anything fails, use `references/troubleshooting.md`.
