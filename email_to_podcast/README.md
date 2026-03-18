# Heatmap Email → Daily Podcast

Automatically converts the Heatmap daily briefing email to an MP3 and
delivers it to your phone via Telegram every weekday morning.

## How it works

```
Gmail (IMAP) → extract text → OpenAI TTS → MP3 → Telegram bot → your phone
```

GitHub Actions runs the script on a schedule. No server needed.

---

## One-time setup

### 1. Gmail — create an App Password

You need a Google **App Password** (not your regular password).

1. Go to your Google Account → **Security**
2. Enable **2-Step Verification** (required)
3. Go to **Security → App passwords**
4. Create a new app password (name it "Heatmap Podcast")
5. Copy the 16-character password

> If you use Gmail with Google Workspace, your admin may need to allow app passwords.

### 2. OpenAI API key

1. Sign in at [platform.openai.com](https://platform.openai.com)
2. Go to **API keys** → **Create new secret key**
3. Copy it

**Cost estimate:** A typical Heatmap briefing is ~3,000–5,000 characters.
With `tts-1-hd` at $0.030/1K chars that's **~$0.09–0.15 per episode**
(~$2–3/month for weekday delivery).

### 3. Telegram bot

#### a) Create the bot
1. Open Telegram, search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** (format: `123456:ABC-DEF...`)

#### b) Get your chat ID
1. Start a conversation with your new bot (send it any message)
2. Open this URL in your browser (replace `TOKEN`):
   ```
   https://api.telegram.org/botTOKEN/getUpdates
   ```
3. Find `"chat": {"id": 123456789}` — that number is your chat ID

### 4. Add GitHub Secrets

In your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the 16-char app password |
| `OPENAI_API_KEY` | your OpenAI key |
| `TELEGRAM_BOT_TOKEN` | your bot token |
| `TELEGRAM_CHAT_ID` | your numeric chat ID |

### 5. Enable the workflow

Push this code to your repo. GitHub Actions will automatically pick up
`.github/workflows/daily_podcast.yml` and run at **8 AM ET on weekdays**.

You can also trigger it manually: **Actions tab → "Heatmap Daily Podcast" → Run workflow**.

---

## Customization

| Env var | Default | Options |
|---|---|---|
| `HEATMAP_SENDER` | `heatmap` | Full email address or domain fragment |
| `TTS_MODEL` | `tts-1-hd` | `tts-1` (cheaper/faster) or `tts-1-hd` |
| `TTS_VOICE` | `alloy` | `alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer` |

Set these as additional GitHub Secrets or edit the workflow YAML directly.

---

## Running locally

```bash
cd email_to_podcast
pip install -r requirements.txt

export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export OPENAI_API_KEY="sk-..."
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"

python main.py
```

---

## Troubleshooting

**"No email from 'heatmap' found today"**
- Check your Gmail inbox — the email may not have arrived yet
- Set `HEATMAP_SENDER` to the exact sender address (e.g. `briefing@heatmap.news`)
- IMAP access must be enabled in Gmail settings (Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP)

**Telegram sends but audio won't play**
- Telegram's inline player works for files under ~20 MB. A ~5-minute briefing at tts-1-hd is typically 5–8 MB.

**GitHub Actions not running**
- Check the Actions tab for errors
- Make sure all 5 secrets are set
- GitHub disables scheduled workflows on repos with no activity for 60 days — trigger manually to re-enable
