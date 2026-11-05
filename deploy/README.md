# Deploy do backend na VPS

Pré-requisito: registro DNS `A` de `payagent.eliobrostech.site` → IP da VPS,
e no `.env` do projeto:

```bash
FRONTEND_ORIGIN=https://seu-app.vercel.app   # para onde o PayPal devolve o comprador
FRONTEND_ORIGINS=https://seu-app.vercel.app  # origens autorizadas a enviar cookies
```

`SESSION_COOKIE_SAMESITE` pode ficar vazio: em HTTPS ele passa a `none`. Como o
navegador fala com a API pelo proxy `/api/backend` do próprio app (veja abaixo), o
cookie de sessão é de primeira parte e `lax` também funciona — mais restrito e
recomendado se você quiser fixá-lo.

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

Depois, na Vercel, defina a variável **de build** que o proxy usa:

```bash
NEXT_PUBLIC_API_URL=https://payagent.eliobrostech.site
# API_ORIGIN também funciona e tem prioridade se as duas existirem
```

## O proxy `/api/backend`

O navegador não chama a API diretamente: as páginas pedem `/api/backend/...` no
próprio domínio do app e o servidor do Next.js repassa para `NEXT_PUBLIC_API_URL`
(ou `API_ORIGIN`), conforme `frontend/next.config.js`. Isso mantém o cookie de sessão
do painel como cookie de primeira parte — cookie de outro site é descartado pelo
navegador, e o dono volta ao login logo depois de entrar.

A variável é lida no *build*: mudou o valor, precisa de um novo deploy na Vercel,
senão `/api/backend` continua apontando para a API antiga (ou para
`http://localhost:8000` quando nenhuma das duas está definida).

Também: atualize o webhook no painel do PayPal para
`https://payagent.eliobrostech.site/webhooks/paypal` (e `PAYPAL_WEBHOOK_ID` no `.env`),
e use `https://SEU-APP.vercel.app/widget.js` nas lojas — o widget é servido pela
aplicação que tem o `/embed`, não pela API.

Conferência rápida:

```bash
curl -i https://payagent.eliobrostech.site/health
# o mesmo health através do proxy do app: deve responder {"ok":true}
curl -i https://SEU-APP.vercel.app/api/backend/health
# e, com uma origem diferente da configurada, a API deve responder 403
curl -i -H "Origin: https://evil.example" https://payagent.eliobrostech.site/health
```
