-- 02 · Network policy for the bot user, then its PAT.
-- Run AFTER the Lambda exists and you know its fixed egress IP (scripts/deploy_lambda.sh egress).
--   snow sql -c <conn> -f 02_network_policy.sql \
--     -D bot_user=CAWA_BOT_SVC -D rule_db=MY_DB -D rule_schema=CAWA_STATE -D egress_ip=203.0.113.10
-- A TYPE=SERVICE user can only create or use a PAT while it is subject to a network policy, so this must
-- run before the PAT is generated. A user-level policy overrides any account-level policy for this user only.
-- Idempotent.

CREATE OR REPLACE NETWORK RULE <% rule_db %>.<% rule_schema %>.CAWA_GATEWAY_EGRESS
  MODE = INGRESS
  TYPE = IPV4
  VALUE_LIST = ('<% egress_ip %>')
  COMMENT = 'Fixed public egress IP of the Cortex Agent gateway Lambda.';

CREATE NETWORK POLICY IF NOT EXISTS CAWA_GATEWAY_NP
  ALLOWED_NETWORK_RULE_LIST = ('<% rule_db %>.<% rule_schema %>.CAWA_GATEWAY_EGRESS')
  COMMENT = 'Only the gateway Lambda egress IP may authenticate as the bot service user.';
ALTER NETWORK POLICY CAWA_GATEWAY_NP SET ALLOWED_NETWORK_RULE_LIST = ('<% rule_db %>.<% rule_schema %>.CAWA_GATEWAY_EGRESS');

ALTER USER <% bot_user %> SET NETWORK_POLICY = CAWA_GATEWAY_NP;
