# Setup and deployment

About 30–40 minutes, one time. You need a Google account and Telegram.

## 1. Create the Telegram bot (2 min)

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Pick a name and a username ending in `bot`.
3. Copy the token it gives you → `TELEGRAM_BOT_TOKEN`.

## 2. Get a free Gemini API key (2 min)

1. Go to <https://aistudio.google.com/apikey> and click **Create API key**.
2. Copy it → `GEMINI_API_KEY`.

> Privacy note: on Gemini's **free** tier, Google may use what you send to
> improve its products. Your notes include driver names and amounts. If that
> matters, turn on billing for the Gemini project later. Flash models cost
> only cents per day at one person's volume, and paid-tier data isn't used
> for training.

Optional backup: create a free key at <https://console.groq.com/keys> → `GROQ_API_KEY`.

## 3. Let the bot edit your Google Sheet (10 min)

The bot signs in as a **service account** (a robot Google user).

1. Open <https://console.cloud.google.com/>, create a project (e.g. `onecargobot`).
2. **APIs & Services → Library**: enable **Google Sheets API** and **Google Drive API**.
3. **APIs & Services → Credentials → Create credentials → Service account**.
   Name it `onecargobot`, click through and finish.
4. Open the service account → **Keys → Add key → Create new key → JSON**.
   A file downloads. Rename it to `service_account.json`.
5. Open the JSON and copy the `client_email` value
   (looks like `onecargobot@your-project.iam.gserviceaccount.com`).
6. Open your sheet → **Share** → paste that email → **Editor** → Send.

On first start the bot builds the layout itself: **Log**, **Today** and
**Settings** tabs, headers, dropdowns, and colors. It reuses the empty
default tab if it's the only one.

## 4. Run it on a free server (15 min)

Google Cloud's **Always Free** tier includes one small VM (e2-micro) in
some US regions. It's plenty for this bot, and it's the same Google account
you already used. The bot uses long polling, so you don't need a domain,
HTTPS, or open ports.

1. In the same Google Cloud project: **Compute Engine → VM instances → Create**.
   - Region: `us-east1`, `us-central1` or `us-west1` (free-tier regions)
   - Machine type: **e2-micro**
   - Boot disk: Debian 12 or Ubuntu 24.04, **Standard persistent disk**, 30 GB max
   - Create. (Google asks for a card to verify billing; e2-micro in those
     regions stays within the free tier. Check the current free-tier terms on
     Google's site and set a $1 budget alert to be safe.)
2. Click **SSH** on the VM and run:

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo useradd -r -m -d /opt/onecargobot onecargo
sudo -u onecargo git clone https://github.com/usershoislom/onecargobot.git /opt/onecargobot
cd /opt/onecargobot
sudo -u onecargo python3 -m venv .venv
sudo -u onecargo .venv/bin/pip install -r requirements.txt
sudo -u onecargo cp .env.example .env
sudo -u onecargo nano .env          # paste your token and keys
```

   If the GitHub repo is private, `git clone` will ask for a login: use your
   GitHub username and a [personal access token](https://github.com/settings/tokens)
   (read-only "Contents" access) as the password.

3. Upload `service_account.json`: in the SSH window use the **⚙ → Upload file**
   button, then:

```bash
sudo mv ~/service_account.json /opt/onecargobot/
sudo chown onecargo /opt/onecargobot/service_account.json
sudo chmod 600 /opt/onecargobot/service_account.json /opt/onecargobot/.env
```

4. Find your Telegram user ID: run the bot once in the foreground, send it
   any message, and it replies with your ID:

```bash
sudo -u onecargo .venv/bin/python -m bot.main     # Ctrl+C after you get the ID
sudo -u onecargo nano .env                         # set ALLOWED_USER_ID=<your id>
```

5. Install it as a service so it runs 24/7 and restarts itself:

```bash
sudo cp deploy/onecargobot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now onecargobot
sudo journalctl -u onecargobot -f    # live logs, Ctrl+C to exit
```

### Updating later

```bash
cd /opt/onecargobot && sudo -u onecargo git pull && \
  sudo -u onecargo .venv/bin/pip install -r requirements.txt && \
  sudo systemctl restart onecargobot
```

### Other hosting options

- **Oracle Cloud Always Free**: more generous VM, same steps; sign-up can be picky.
- **Any $4–6/mo VPS** (Hetzner, DigitalOcean): same steps, most reliable.
- **Your own PC**: `python -m bot.main` works, but reminders stop when it's off.

## 5. Run locally (for testing)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in
python -m bot.main
pytest                 # unit tests, no network needed
```
