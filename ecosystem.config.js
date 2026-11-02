module.exports = {
  apps: [
    {
      name: "payagent",
      cwd: "/home/vpcuser/pay-agent",
      script: "/home/vpcuser/pay-agent/.venv/bin/uvicorn",
      args: "app:app --host 127.0.0.1 --port 3004",
      interpreter: "none",
      autorestart: true,
      watch: false,
      max_memory_restart: "500M",
      // O .env da pasta do projeto é lido automaticamente (load_dotenv).
      // Garanta FRONTEND_ORIGIN=https://seu-app.vercel.app no .env de produção.
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
