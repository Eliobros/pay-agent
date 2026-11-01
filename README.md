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
- **Refunds by the agent:** the customer asks for a refund in the chat. Small ones (up to `MAX_AUTO_REFUND_USD`) are done automatically, bigger ones need approval. Each order can be refunded only once.
- **Drop-in widget:** one `<script>` tag adds a chat bubble to any website.
- **Persistent:** orders and approvals are stored in a local SQLite file.
- **Abuse protection:** per-visitor rate limit, message length and history caps.

## How it uses PayPal

| PayPal API | Used for |
|---|---|
| Orders v2: create | The agent creates the checkout with the amount computed on the server |
| Orders v2: show details | The agent checks the status of an order |
| Orders v2: capture | Charges the buyer after approval on PayPal |
| Payments v2: refund | Full refunds of captured payments, by the agent or after owner approval |

All calls go through the **PayPal Sandbox**: no real money moves.

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
3. Open `/owner` in another tab (token = `ADMIN_TOKEN`). Ask for the **4K monitor** ($329, above the limit): the chat waits, you approve in `/owner`, and the pay button appears.
4. Open `/demo-store.html` to see the widget inside a sample store.

## Add it to your store

1. Edit `products.json` with your products (`id`, `name`, `price`, `tags`).
2. Set `STORE_NAME`, the limits and your PayPal credentials in `.env`.
3. Paste this in your site:

```html
<script src="https://YOUR-HOST/widget.js" defer></script>
```

Optional: `data-color="#e11d48"` changes the bubble color. Serve the app over HTTPS when your store uses HTTPS.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `MAX_ORDER_USD` | Purchases above this need owner approval | 200 |
| `MAX_AUTO_REFUND_USD` | Refunds above this need owner approval | 100 |
| `RATE_LIMIT_PER_MIN` | Messages per visitor per minute | 20 |
| `MAX_HISTORY` | Messages kept per visitor | 30 |
| `ADMIN_TOKEN` | Password for the Owner panel | demo |
| `STORE_NAME` | Name shown in the chat and the agent prompt | Demo Store |

## Notes and limits

- The Owner panel token is a simple shared secret meant for the demo. A production version needs real authentication.
- Currency is USD in this version.
- Chat history is kept in memory; orders and approvals are persistent.

## Roadmap

Shopify and WooCommerce connectors, several stores per installation, PayPal webhooks, sales summaries for the owner with the Transaction Search API.

## What was built during the hackathon

The whole project was created from scratch during the submission period.

## License

MIT. See [LICENSE](LICENSE).
