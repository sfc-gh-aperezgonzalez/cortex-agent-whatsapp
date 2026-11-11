# Meta (WhatsApp Cloud API) setup

The user does these steps in the Meta UI; the agent validates every value with `scripts/meta.py check`.
Collect each value into the local secret file (`~/.cortex-agent-whatsapp/secret.json`, mode 600). Never
ask the user to paste secrets into the chat: ask them to write them into the file and confirm.

## 1. Create the app

1. Go to <https://developers.facebook.com/apps> → **Create app**.
2. Use case: **Connect with customers through WhatsApp**. Pick (or create) a business portfolio.
3. The app opens in **Development** mode. That is enough for the free test number: no app review or
   business verification is needed for a demo.

## 2. Test number, IDs and allowed recipients

In the app: **WhatsApp → API Setup**.

- Meta creates a **test business number** automatically (a +1 555 number). Copy:
  - **Phone number ID** → `WA_PHONE_NUMBER_ID`
  - **WhatsApp Business Account ID** → `WA_WABA_ID`
- **To → Manage phone number list → Add phone number**: add every phone that will chat with the bot
  (maximum **5**). Meta sends each number a verification code on WhatsApp. The test number only
  delivers to numbers on this list (other senders reach the webhook, but replies fail with error 131030).

## 3. App ID and App secret

**App settings → Basic**: copy **App ID** → `META_APP_ID` and **App secret** → `META_APP_SECRET`.
The app secret signs every webhook (`X-Hub-Signature-256`); the gateway rejects unsigned calls.

## 4. Long-lived token (system user)

The temporary token on the API Setup page expires in about 24 hours. Use a system user instead:

1. <https://business.facebook.com/settings> → **Users → System users → Add** (role **Admin**).
2. **Assign assets**: the app (full control) and the WhatsApp account (full control).
3. **Generate token**: select the app, expiry (60 days or never), permissions
   `whatsapp_business_messaging` and `whatsapp_business_management`.
4. Copy it → `WA_SYSTEM_TOKEN`. Note the expiry date: the bot stops replying when it expires.

## 5. Verify token

Any random string, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(24))"` →
`WA_VERIFY_TOKEN`. Meta sends it back during the webhook handshake.

## 6. Validate

```bash
python3 scripts/meta.py check
```

All three checks must pass (token valid with both scopes, phone number readable, phone number under the WABA).

## 7. Webhook (after the Lambda is deployed)

```bash
python3 scripts/meta.py register https://<function-url>
```

This sets `<url>/whatsapp` as the app's webhook for `whatsapp_business_account` with field `messages`, and
subscribes the WABA to the app. Equivalent UI: **WhatsApp → Configuration → Webhook** (callback URL + verify
token, then subscribe to `messages`).

## 8. Open the 24-hour window

From each allowed phone, send any message (e.g. "hi") to the test number. Businesses can only send free-text
messages within 24 hours of the user's last message; outside that window only approved templates can be sent.
Because the bot always answers a user message, this is only an issue for the very first test message
(`meta.py send`) or for proactive notifications.
