# Bitomat Kyiv USDT Monitor — Railway

## Telegram variables

Create these variables in Railway:

- `TELEGRAM_BOT_TOKEN` — token from @BotFather
- `TELEGRAM_CHAT_ID` — your Telegram numeric chat ID
- `STATE_FILE_PATH` — `/data/bitomat_state.json`

## Persistent state

In Railway, attach a Volume to the service and mount it at:

`/data`

This keeps the last notified rates/reserves across redeployments and restarts.

## What it monitors

Kyiv Bitomat locations configured in `LOCATIONS`:
- Lesia Kurbasa Ave, 19A
- Liatoshynskoho St, 14
- Antonovycha St, 176 (Ocean Plaza)

Thresholds:
- USDT rate: 0.20%
- cash reserve: 1000 UAH
- calculated max: 25 USDT

Check interval:
- 60 seconds

## Railway deployment

1. Put these files in a GitHub repository.
2. Railway → New Project → Deploy from GitHub repo.
3. Choose the repository.
4. Railway will detect the `Dockerfile`.
5. Add the variables above.
6. Add a Volume mounted at `/data`.
7. Redeploy if required.
8. Open Logs and confirm `[OK]` entries appear.
