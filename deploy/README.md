# Deploy do backend na VPS

Pré-requisito: registro DNS `A` de `payagent.eliobrostech.site` → IP da VPS,
e no `.env` do projeto:

```bash
FRONTEND_ORIGIN=https://seu-app.vercel.app   # para onde o PayPal devolve o comprador
FRONTEND_ORIGINS=https://seu-app.vercel.app  # origens autorizadas a enviar cookies
```

`SESSION_COOKIE_SAMESITE` pode ficar vazio: em HTTPS ele passa a `none`, que é o
necessário quando o painel está noutro domínio (com `lax` o navegador guarda o cookie
e nunca o envia, e o dono aparece desligado a cada pedido).

```bash
# 1. Backend como serviço
sudo cp deploy/payagent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now payagent
systemctl status payagent

# 2. Nginx como reverse proxy
sudo cp deploy/nginx-payagent.conf /etc/nginx/sites-available/payagent
sudo ln -s /etc/nginx/sites-available/payagent /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 3. HTTPS (obrigatório: o frontend na Vercel é HTTPS)
sudo certbot --nginx -d payagent.eliobrostech.site

# 4. Conferir
curl https://payagent.eliobrostech.site/health
```

Depois: na Vercel defina `NEXT_PUBLIC_API_URL=https://payagent.eliobrostech.site`,
atualize o webhook no painel do PayPal para
`https://payagent.eliobrostech.site/webhooks/paypal` (e `PAYPAL_WEBHOOK_ID` no `.env`),
e use `https://SEU-APP.vercel.app/widget.js` nas lojas — o widget é servido pela
aplicação que tem o `/embed`, não pela API.

Conferência rápida:

```bash
curl -i https://payagent.eliobrostech.site/health
# e, com uma origem diferente da configurada, deve responder 403
curl -i -H "Origin: https://evil.example" https://payagent.eliobrostech.site/health
```
