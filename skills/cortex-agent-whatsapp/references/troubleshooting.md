# Troubleshooting

Start with the two sources of truth:

```sql
SELECT * FROM <STATE_DB>.<STATE_SCHEMA>.WA_EVENTS ORDER BY TS DESC LIMIT 20;           -- inbound, outbound, status, worker_error
SELECT * FROM <STATE_DB>.<STATE_SCHEMA>.CONVERSATION_MESSAGES ORDER BY TS DESC LIMIT 20;
```

```bash
aws logs tail /aws/lambda/<FUNCTION_NAME> --since 15m --profile <p> --region <r>
```

| Symptom | Likely cause | Fix |
|---|---|---|
| Function URL returns 403 `Forbidden` for every request | Public URL lacks one of the two required permissions | Re-run `deploy_lambda.sh up` (adds `InvokeFunctionUrl` and `InvokeFunction` with `InvokedViaFunctionUrl`) |
| Meta "callback URL couldn't be validated" | Verify token mismatch, or URL missing `/whatsapp` | `curl "<url>/whatsapp?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=123"` must return `123` |
| Webhook POST returns 401 | `META_APP_SECRET` is wrong (signature check fails) | Fix the secret, `deploy_lambda.sh secret` |
| Nothing in `WA_EVENTS` after messaging the number | Webhook not subscribed, or WABA not subscribed to the app | `meta.py register <url>`; check the app's webhook shows `messages` subscribed |
| `inbound` row but no `outbound` | Worker failed: see `worker_error` rows and Lambda logs | Usually Snowflake auth or grants (below) |
| `status=failed`, error 131030 | Recipient not in the test number's allowed list | Add it in **API Setup → Manage phone number list** |
| `status=failed`, error 131047 | 24-hour window closed (only for proactive sends) | The user must message first, or use an approved template |
| Graph API error 190 | `WA_SYSTEM_TOKEN` expired or revoked | Generate a new system user token, `deploy_lambda.sh secret` |
| Snowflake 390422 / "IP not allowed" | Lambda egress IP is not the one in the network rule (no VPC, or a different NAT) | `deploy_lambda.sh egress 3` must print the same IP every time; update `02_network_policy.sql` |
| Cannot create or use the PAT | Service user has no network policy | Run `02_network_policy.sql` before generating the PAT |
| PAT_INVALID / 401 from Snowflake | PAT expired, revoked, or role restriction no longer granted | Rotate: `ALTER USER <u> ROTATE PAT <name>` and push the new secret |
| Agent API 403/404 "does not exist or not authorized" | Bot role lacks `USAGE` on the agent, or on a tool's objects | `03_agent_grants.sql`; check each tool in `DESCRIBE AGENT` |
| Agent answers "I can't access that data" | Tool-level grants missing (semantic view, base tables, search service, procedure, warehouse) | Grant them to the bot role; test with `USE ROLE <bot_role>` |
| Replies take > 60 s or the web chat times out | Agent orchestration is slow (large budget, many tools) | Lower `orchestration.budget.seconds`, simplify tools, warm with a test question |
| Duplicate replies | Meta retried the webhook; dedup uses `WA_EVENTS.MSG_ID` | Make sure the bot role can `SELECT` on `WA_EVENTS` |
| Bot replies "I can only read text messages" | User sent audio, image or a sticker | Expected: only text and interactive replies are handled |
