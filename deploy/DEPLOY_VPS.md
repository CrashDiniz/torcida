# Deploy no VPS Contabo — runbook (15 min)

> Pré-requisito: VPS Ubuntu 24.04 provisionado + IP em mãos. Tudo abaixo roda
> do WSL, na pasta `worldcup-hackathon/app`.

## 1. Setup da máquina (1 comando)
```bash
ssh root@<IP> 'bash -s' < deploy/setup_vps.sh app.torcida.app
```
Instala tudo (python, node, caddy, ffmpeg), clona o repo, cria os services
systemd (`torcida-web`, `torcida-bot`), firewall e backup diário do sqlite.
Idempotente — pode rodar de novo.

## 2. Segredos (o setup não toca em segredo)
```bash
scp .env root@<IP>:/home/torcida/app/.env
ssh root@<IP> 'mkdir -p /home/torcida/.keys'
scp ../.keys/devnet-wallet.json root@<IP>:/home/torcida/.keys/
ssh root@<IP> 'chown -R torcida:torcida /home/torcida/app/.env /home/torcida/.keys'
```
Depois, NO VPS, editar o `.env`:
- `WEBAPP_URL=https://app.torcida.app`
- adicionar `WALLET_PATH=/home/torcida/.keys/devnet-wallet.json`

## 3. DNS (Spaceship)
`app.torcida.app` → **A record** pro IP do VPS (TTL mínimo). O Caddy emite o
TLS sozinho quando o DNS propagar (1-5 min).

## 4. Ligar (ORDEM IMPORTA — 1 bot só!)
```bash
# no WSL: derrubar o bot local ANTES (senão TelegramConflict)
pkill -f 'src.bot.main'
# no VPS:
ssh root@<IP> 'systemctl restart torcida-web torcida-bot && systemctl status torcida-web torcida-bot --no-pager'
```

## 5. Verificar
```bash
curl -s -o /dev/null -w '%{http_code}\n' https://app.torcida.app/        # 200
curl -s -o /dev/null -w '%{http_code}\n' https://app.torcida.app/demo    # 200
```
Telegram: `@BotFather` → `/myapps` → editar a Mini App pra apontar pro
domínio novo (ou conferir que o bot usa WEBAPP_URL). Testar /jogos no grupo.

## Rollback rápido
O túnel local continua sendo o plano B: religar bot+web local (CONTINUAR.md)
e voltar WEBAPP_URL pro trycloudflare.

## Logs no VPS
```bash
journalctl -u torcida-bot -f
journalctl -u torcida-web -f
```
