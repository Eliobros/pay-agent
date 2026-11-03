# PayAgent

**An open-source AI sales agent that any online store can plug in with one line of code.**
Customers chat to find a product, the agent builds a PayPal checkout, and the store owner
stays in control: large purchases and refunds need a human approval.

Built for the PayPal AI Hackathon (PayPal Sandbox + an AI model with tool calling).

## The problem

Small stores lose sales because shoppers get stuck, and they cannot afford a support team.
A chatbot that only answers questions does not fix that, and an agent that can move money
on its own is risky. PayAgent sells, charges and refunds through PayPal, with limits the
owner sets.

## What it does

- **Chat to checkout:** the agent searches the catalog, confirms the item and quantity, and creates a PayPal order. The customer pays on PayPal and returns to a confirmation page.
- **Prices never come from the AI:** the model only picks a product id and a quantity. The server sets the amount, so the model cannot invent or change a price.
- **Human approval:** purchases above `MAX_ORDER_USD` wait in the **Owner panel** (`/owner`). The customer sees a waiting message and the pay button appears as soon as the owner approves.
- **Owner authentication:** the first visit to `/owner` creates the owner account (Argon2id password, HttpOnly session cookie). Sensitive endpoints are closed to everyone else.
- **Payment credentials in the dashboard:** PayPal sandbox/live keys are saved from `/owner`, encrypted with AES-256-GCM (master key in `CREDENTIAL_ENCRYPTION_KEY`, never in the database) and validated against PayPal before they are used. Only the last four characters of the secret are shown again.
- **Refunds by the agent:** the customer asks for a refund in the chat. Small ones (up to `MAX_AUTO_REFUND_USD`) are done automatically, bigger ones need approval. Each order can be refunded only once.
- **Drop-in widget:** one `<script>` tag adds a chat bubble to any website.
- **Persistent:** orders, approvals and chat turns are stored in a local SQLite file.
- **Abuse protection:** per-visitor rate limit, message length and history caps.

## How it uses PayPal

| PayPal API | Used for |
|---|---|
| Orders v2: create | The agent creates the checkout with the amount computed on the server |
| Orders v2: show details | The agent checks the status of an order |
| Orders v2: capture | Charges the buyer after approval on PayPal |
| Payments v2: refund | Full refunds of captured payments, by the agent or after owner approval |
| Notifications: verify signature | Every webhook is checked with PayPal before it may change a payment status |

All calls go through the **PayPal Sandbox**: no real money moves.

## Owner dashboard

`/owner` is a single page with seven sections, all scoped to the signed-in owner's store:

1. **Overview** — total orders, successful payments, total sales, net of refunds, pending
   approvals, pending refund requests and failed transactions.
2. **Orders** — the latest orders with item, customer, amount, status and date.
3. **Refund requests** — evidence review, approve, reject or ask for more evidence.
4. **Payment providers** — PayPal sandbox/live credentials, encrypted and validated.
5. **AI usage** — conversations, stored messages, last 7 days and tool calls per tool.
6. **Store settings** — store name and the two financial limits, enforced by the backend.
7. **Security & account** — change password (revokes other sessions) and list active sessions.

## Conversations that survive restarts

Every chat turn is written to SQLite (`conversations`, `messages`) keyed by store and visitor, with
its role, timestamp and the order id of any tool call. The next message rebuilds the context from
the database, so a restart or a second worker does not lose the conversation.

- History is capped at `MAX_HISTORY` messages, always cutting at a user turn, and a window that
would start with a tool result is dropped: the model never sees a `tool_call_id` without its call.
- A conversation expires after `CONVERSATION_TTL_HOURS` of inactivity; the next message starts clean
while the expired row is removed at startup or by `POST /owner/conversations/purge`.
- Everything is filtered by `store_id`: the same visitor id in two stores is two conversations.

## Refunds with private evidence

A damage claim is never decided by the AI. The agent opens a request, the customer attaches
photos or a short video in the chat, and only the signed-in owner can approve or reject it.

- Accepted evidence: JPEG, PNG, WebP, MP4 and WebM. The type is read from the bytes, never from
the file name; videos must be at most 60 seconds and every file is capped by `MAX_EVIDENCE_MB`.
- Files go to `EVIDENCE_DIR` (`./evidence`), which is not served as static content. The database
keeps only a relative path plus size, hash and duration, and the bytes leave the backend only
through an owner-authenticated endpoint that answers `Cache-Control: no-store`.
- Statuses: `REQUESTED → WAITING_EVIDENCE → UNDER_REVIEW → (MORE_EVIDENCE_REQUIRED) → APPROVED →
PROCESSING → REFUNDED`, or `REJECTED` / `FAILED` with a reason.
- Approval needs at least one evidence file: the owner has to look at the proof, and the backend
refuses the decision otherwise (the assistant cannot bypass it).
- A request is marked `REFUNDED` only after PayPal confirms the refund; a failed call leaves it
`FAILED`, records the reason and does not touch the order.
- Evidence of closed requests is deleted after `EVIDENCE_RETENTION_DAYS` (30) days, on demand from
`POST /owner/refund-requests/purge`.

## Webhooks and idempotency

`POST /webhooks/paypal` accepts a delivery only when PayPal itself confirms its signature
(`/v1/notifications/verify-webhook-signature`, using `PAYPAL_WEBHOOK_ID`). Each PayPal event id
is stored once, so a repeated delivery is answered with `duplicate` and never applies a second
financial change. Subscribe the webhook to `PAYMENT.CAPTURE.COMPLETED`,
`PAYMENT.CAPTURE.REFUNDED` and `CHECKOUT.ORDER.APPROVED`.

Order creation, capture and refund claim an idempotency key before calling PayPal and then send
a deterministic `PayPal-Request-Id`. A retried request replays the stored result, a concurrent
duplicate gets `409`, and a failed attempt releases the key so the retry can run. Refunds can
never exceed the captured amount, and only verified PayPal responses or verified webhooks change
a payment status — a frontend confirmation proves nothing.

## Architecture

```
Store website ──(widget.js)──▶ Next.js app (/embed, /, /owner, /success)
                                      │
                                      ▼
                          FastAPI backend (app.py)
                           ├─ AI model (tool calling loop)
                           ├─ PayPal REST API (Sandbox)
                           └─ SQLite (orders, approvals)
```

Agent tools: `search_products`, `create_checkout`, `check_order_status`, `list_my_orders`, `refund_order`.

## Run it locally

Requirements: Python 3.10+, Node.js 18+, a PayPal Sandbox REST app, and an API key for an OpenAI-compatible model API (default: Meta Model API, `muse-spark-1.3`).

```bash
# 1. Backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the keys
# generate the master key used to encrypt payment credentials
python -c "from payagent.security import generate_key; print(generate_key())"
# paste the output into CREDENTIAL_ENCRYPTION_KEY in .env

# 2. Frontend
cd frontend
echo "NEXT_PUBLIC_API_URL=http://localhost:3004" > .env.local
npm install
cd ..

# 3. Start both (two terminals)
uvicorn app:app --reload --port 3004
cd frontend && npm run dev
```

Open http://localhost:3000.

### PayPal Sandbox setup

1. Go to https://developer.paypal.com and switch to **Sandbox**.
2. Apps & Credentials → Create App (type **Merchant**). Copy the Client ID and Secret into `.env`.
3. Testing Tools → Sandbox Accounts: use a **Personal** account as the buyer when paying.

## Try this flow (about 2 minutes)

1. Ask: *"I need a keyboard for my desk"*, then *"Yes, buy 1"*. Pay with the sandbox buyer account.
2. Ask: *"I want a refund, it arrived broken"*. The agent refunds it on its own.
3. Open `/owner` in another tab and create the owner account (first visit only). Ask for the **4K monitor** ($329, above the limit): the chat waits, you approve in `/owner`, and the pay button appears.
4. Open `/demo-store.html` to see the widget inside a sample store.

## Add it to your store

1. Edit `products.json` with your products (`id`, `name`, `price`, `tags`).
2. Set `STORE_NAME`, the limits and your PayPal credentials in `.env`.
3. Paste this in your site:

```html
<script src="https://YOUR-FRONTEND-HOST/widget.js" defer></script>
```

`YOUR-FRONTEND-HOST` is the address that serves `/embed` (the Next.js app), not the API:
the widget derives the chat frame from where the script itself is loaded. Optional:
`data-color="#e11d48"` changes the bubble color. Serve the app over HTTPS when your
store uses HTTPS.

## Deploying (Vercel + your own server)

The backend runs on your server (systemd + nginx, see `deploy/`) and the Next.js app
on Vercel. Three values have to agree, and they are the ones that break silently when
they do not:

```bash
# on the server, in .env
FRONTEND_ORIGIN=https://your-app.vercel.app   # PayPal returns the buyer here
FRONTEND_ORIGINS=https://your-app.vercel.app  # origins allowed to send cookies
# SESSION_COOKIE_SAMESITE can stay empty: over HTTPS it becomes `none`, which
# is what a dashboard on another domain needs (with `lax` the browser keeps the
# cookie but never sends it, and the owner looks logged out on every request).
```

```bash
# on Vercel, environment variables
NEXT_PUBLIC_API_URL=https://your-api.example.com
```

And two addresses outside the code:

- **PayPal webhook**: `https://your-api.example.com/webhooks/paypal` in the app's
  Webhooks page (plus `PAYPAL_WEBHOOK_ID`), otherwise payments are only confirmed by
  the browser redirect.
- **Widget on your store**: `https://your-app.vercel.app/widget.js`.

Checklist when something does not work: the API answers `curl https://your-api.example.com/health`,
the dashboard keeps the session after a reload (that is the `SameSite` value), and
`FRONTEND_ORIGINS` contains the exact origin the browser shows in the address bar
(no trailing slash, scheme included).

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `MAX_ORDER_USD` | Purchases above this need owner approval | 200 |
| `MAX_AUTO_REFUND_USD` | Refunds above this need owner approval | 100 |
| `RATE_LIMIT_PER_MIN` | Messages per visitor per minute | 20 |
| `MAX_HISTORY` | Messages kept per visitor | 30 |
| `CONVERSATION_TTL_HOURS` | Inactivity after which a chat starts fresh | 24 |
| `STORE_NAME` | Name shown in the chat and the agent prompt | Demo Store |
| `DEFAULT_STORE_ID` | Id of the demo store created on first boot | store_demo |
| `FRONTEND_ORIGINS` | Origins allowed to call the API with credentials | `FRONTEND_ORIGIN` |
| `SESSION_TTL_HOURS` | How long an owner session stays valid | 12 |
| `SESSION_COOKIE_SECURE` | Send the session cookie over HTTPS only | true on HTTPS |
| `SESSION_COOKIE_SAMESITE` | Cookie scope; `none` is required across sites | derived |
| `ALLOW_OWNER_REGISTRATION` | Allow more owner accounts after the first one | false |
| `OWNER_SIGNUP_CODE` | Optional shared code required to register | — |
| `LOGIN_RATE_LIMIT_PER_MIN` | Login attempts per minute | 8 |
| `CREDENTIAL_ENCRYPTION_KEY` | AES-256-GCM master key for provider credentials | — |
| `PAYPAL_BASE_URL` | Fallback PayPal endpoint when no dashboard credentials exist | sandbox |
| `PAYPAL_WEBHOOK_ID` | Webhook id used to verify notifications (required for `/webhooks/paypal`) | — |
| `EVIDENCE_DIR` | Private folder for refund evidence | ./evidence |
| `EVIDENCE_STORAGE` | Evidence driver (`local`) | local |
| `MAX_EVIDENCE_MB` | Maximum size of one evidence file | 15 |
| `MAX_VIDEO_SECONDS` | Maximum refund video duration | 60 |
| `EVIDENCE_RETENTION_DAYS` | Days evidence is kept after a request closes | 30 |

Credentials saved in `/owner` (Payment providers) take precedence over `PAYPAL_CLIENT_ID`/`PAYPAL_CLIENT_SECRET`; the environment variables are only a fallback. Saving credentials requires `CREDENTIAL_ENCRYPTION_KEY`, otherwise the API answers 503.

## Run the tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests use a temporary SQLite file and never call PayPal or the model API.

## Notes and limits

- Owner accounts: the first account bootstraps the installation; later registration only works with `ALLOW_OWNER_REGISTRATION=true` (optionally guarded by `OWNER_SIGNUP_CODE`).
- Currency is USD in this version.
- Chat history is persistent per store and visitor, capped by `MAX_HISTORY` and expiring after `CONVERSATION_TTL_HOURS`; orders, approvals, owners and sessions are persistent too.
- Every order, approval and conversation row carries a `store_id`, and the API filters by the store of the signed-in owner.

## Roadmap
* **SaaS Multi-Tenant Platform:** Transform PayAgent into a subscription-based platform where multiple store owners can register accounts, manage independent catalogs, and configure custom system prompts.
* **CDN Script Injection:** Provide a drop-in `<script>` tag pointing to our global CDN, uniquely identified by a secure store token (`?id=store_uuid&token=jwt_hash`), enabling plug-and-play installation on any custom website.
* **AI Quota Management:** Implement a tiered monetization model based on monthly consumption thresholds and message usage limits rather than lifetime access.
* **Local Mobile Money (Moçambique):** Expand the existing payment provider registry to natively support local gateways like M-Pesa (Vodacom) and e-Mola (Movitel), allowing customers in African markets to buy via USSD/STK push notifications during chat sessions.
* **E-commerce Integrations:** Build native webhooks and sync connectors for popular platforms like Shopify, WooCommerce, and Wix.


## What was built during the hackathon

The whole project was created from scratch during the submission period.

## License

MIT. See [LICENSE](LICENSE).
