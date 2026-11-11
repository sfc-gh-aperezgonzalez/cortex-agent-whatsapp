-- 03 · Grants so the bot role can run the agent and every tool it calls.
-- The agent runs with the CALLER's privileges: the bot role needs USAGE on the agent and access to
-- everything each tool touches. Fill the sections that match the agent's tools (see DESCRIBE AGENT)
-- and delete the rest. Template variables:
--   bot_role, agent_db, agent_schema, agent_name
-- Example:
--   snow sql -c <conn> -f 03_agent_grants.sql -D bot_role=CAWA_BOT_ROLE \
--     -D agent_db=SALES -D agent_schema=AGENTS -D agent_name=SALES_ASSISTANT

GRANT USAGE ON DATABASE <% agent_db %> TO ROLE <% bot_role %>;
GRANT USAGE ON SCHEMA <% agent_db %>.<% agent_schema %> TO ROLE <% bot_role %>;
GRANT USAGE ON AGENT <% agent_db %>.<% agent_schema %>.<% agent_name %> TO ROLE <% bot_role %>;

-- Cortex Analyst tool over a semantic view: SELECT + REFERENCES on the view, SELECT on its base tables.
-- GRANT USAGE ON DATABASE <SV_DB> TO ROLE <% bot_role %>;
-- GRANT USAGE ON SCHEMA <SV_DB>.<SV_SCHEMA> TO ROLE <% bot_role %>;
-- GRANT SELECT, REFERENCES ON SEMANTIC VIEW <SV_DB>.<SV_SCHEMA>.<SV_NAME> TO ROLE <% bot_role %>;
-- GRANT SELECT ON ALL TABLES IN SCHEMA <SV_DB>.<SV_SCHEMA> TO ROLE <% bot_role %>;   -- or list the base tables

-- Cortex Search tool:
-- GRANT USAGE ON CORTEX SEARCH SERVICE <DB>.<SCHEMA>.<SERVICE> TO ROLE <% bot_role %>;

-- Custom tool (stored procedure / UDF). Use the full argument signature:
-- GRANT USAGE ON PROCEDURE <DB>.<SCHEMA>.<PROC>(<ARG TYPES>) TO ROLE <% bot_role %>;

-- Warehouse used by tools (Analyst execution_environment, procedures):
-- GRANT USAGE ON WAREHOUSE <TOOL_WAREHOUSE> TO ROLE <% bot_role %>;
