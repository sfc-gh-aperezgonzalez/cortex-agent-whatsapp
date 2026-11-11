-- 01 · Bot identity: role, service user, state tables.
-- Run with:  snow sql -c <conn> -f 01_bot_identity_and_state.sql \
--              -D bot_role=CAWA_BOT_ROLE -D bot_user=CAWA_BOT_SVC -D warehouse=COMPUTE_WH \
--              -D state_db=MY_DB -D state_schema=CAWA_STATE
-- Needs a role that can create roles, users and schemas (e.g. ACCOUNTADMIN, or SECURITYADMIN + the DB owner).
-- Idempotent.

CREATE ROLE IF NOT EXISTS <% bot_role %>
  COMMENT = 'Cortex Agent WhatsApp/web gateway: least-privilege role used by the gateway service user.';
GRANT USAGE ON WAREHOUSE <% warehouse %> TO ROLE <% bot_role %>;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE <% bot_role %>;

CREATE USER IF NOT EXISTS <% bot_user %>
  TYPE = SERVICE
  DEFAULT_ROLE = <% bot_role %>
  DEFAULT_WAREHOUSE = <% warehouse %>
  COMMENT = 'Cortex Agent WhatsApp/web gateway (AWS Lambda). Authenticates with a role-restricted PAT.';
GRANT ROLE <% bot_role %> TO USER <% bot_user %>;

-- Conversation state written by the gateway. The gateway owns history (no Cortex threads),
-- keyed by channel + session key (WhatsApp phone number or a server-issued web session id).
CREATE SCHEMA IF NOT EXISTS <% state_db %>.<% state_schema %>
  COMMENT = 'Cortex Agent gateway: conversation history and WhatsApp webhook events.';

CREATE TABLE IF NOT EXISTS <% state_db %>.<% state_schema %>.CONVERSATION_MESSAGES (
  TS           TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  CHANNEL      STRING,          -- 'whatsapp' | 'web'
  SESSION_KEY  STRING,          -- phone number (WhatsApp) or web session id
  MSG_ROLE     STRING,          -- 'user' | 'assistant' | 'reset'
  MSG_TEXT     STRING,
  MSG_ID       STRING,
  LATENCY_MS   NUMBER
) COMMENT = 'One row per conversation turn written by the gateway.';

CREATE TABLE IF NOT EXISTS <% state_db %>.<% state_schema %>.WA_EVENTS (
  TS          TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  EVENT_TYPE  STRING,           -- 'inbound' | 'outbound' | 'status' | 'worker_error'
  MSG_ID      STRING,
  STATUS      STRING,
  PHONE       STRING,
  ERROR       STRING,
  PAYLOAD     VARIANT
) COMMENT = 'Raw WhatsApp webhook events and send results (used for dedup and troubleshooting).';

GRANT USAGE ON DATABASE <% state_db %> TO ROLE <% bot_role %>;
GRANT USAGE ON SCHEMA <% state_db %>.<% state_schema %> TO ROLE <% bot_role %>;
GRANT SELECT, INSERT ON TABLE <% state_db %>.<% state_schema %>.CONVERSATION_MESSAGES TO ROLE <% bot_role %>;
GRANT SELECT, INSERT ON TABLE <% state_db %>.<% state_schema %>.WA_EVENTS TO ROLE <% bot_role %>;
