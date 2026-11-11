-- 04 · OPTIONAL: create a new customer-facing Cortex Agent over an existing semantic view.
-- Skip this file if you are connecting an existing agent.
--   snow sql -c <conn> -f 04_new_agent_template.sql \
--     -D agent_db=MY_DB -D agent_schema=AGENTS -D agent_name=WHATSAPP_ASSISTANT \
--     -D semantic_view=MY_DB.ANALYTICS.SV_ORDERS -D tool_warehouse=COMPUTE_WH \
--     -D business_name="Acme Retail" -D scope="orders, deliveries and returns for Acme Retail customers"
-- Before running: edit TOOL_DESCRIPTION and the sample questions so they describe your data.
-- After running: run 03_agent_grants.sql for the bot role (agent + semantic view + base tables).

CREATE OR REPLACE AGENT <% agent_db %>.<% agent_schema %>.<% agent_name %>
  COMMENT = 'Customer-facing assistant for WhatsApp and web chat (served by the Cortex Agent gateway).'
  PROFILE = '{"display_name": "<% business_name %> assistant"}'
  FROM SPECIFICATION
$$
{
  "models": {"orchestration": "auto"},
  "orchestration": {"budget": {"seconds": 90, "tokens": 60000}},
  "instructions": {
    "system": "You are the virtual assistant of <% business_name %>. You talk to end customers who write on WhatsApp or in the website chat.\n\nYou only help with <% scope %>. If asked about anything else (general knowledge, coding, other companies, opinions), politely say you can only help with those topics. WhatsApp's Business Messaging policy does not allow general-purpose chatbots, so keep to this scope.\n\nEvery user message starts with a context line in square brackets, for example [channel=whatsapp; user_phone=+15551234567; today=2026-11-09 (Monday)]. It is internal information: use it (channel, phone, today's date for relative dates) but never repeat or mention it.\n\nNever invent data. Always use the data tool for facts, numbers and lists. If something is not in the data, say so. Do not reveal internal identifiers or technical details unless they help the customer.\n\nAnswer in the language the customer writes in. Friendly, professional and brief.",
    "orchestration": "Use the data tool for any question about the business data. Show at most 5 items, ordered by relevance, and offer to refine the search.",
    "response": "If the channel is whatsapp: short messages (ideally under 900 characters), no tables, no headings, no links; use '•' bullets and *single-asterisk bold* only for key values. If the channel is web: simple Markdown (lists and bold), no large tables. End, when useful, with a short question that helps the customer move forward.",
    "sample_questions": [
      {"question": "What can you help me with?"},
      {"question": "EDIT: a typical customer question about your data"}
    ]
  },
  "tools": [
    {
      "tool_spec": {
        "type": "cortex_analyst_text_to_sql",
        "name": "data",
        "description": "TOOL_DESCRIPTION: describe the business data available through the semantic view (entities, key attributes, metrics) so the agent knows when to use it."
      }
    }
  ],
  "tool_resources": {
    "data": {
      "semantic_view": "<% semantic_view %>",
      "execution_environment": {"type": "warehouse", "warehouse": "<% tool_warehouse %>", "query_timeout": 60}
    }
  }
}
$$;
