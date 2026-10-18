# Setup: email, Google Sheets, Slack and GitHub Actions

Secrets live in `.env` locally, or in repository secrets on GitHub Actions. They never go in
`config.yaml`: the tracker refuses to load a config that contains a password, token or
webhook. Run `uv run tracker check-config` after each step; it lists anything missing.

## Email through Gmail (about 5 minutes)

Use a separate Gmail account for sending, because an app password gives full access to its
mailbox.

1. Turn on **2-Step Verification**: https://myaccount.google.com/security
2. Create an **app password** at https://myaccount.google.com/apppasswords. Name it
   `price-tracker` and copy the 16-character code, because Google shows it only once.
3. Add to `.env`:

   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=yourname.tracker@gmail.com
   SMTP_PASSWORD=abcdefghijklmnop
   SMTP_FROM=Price Tracker <yourname.tracker@gmail.com>
   ```

4. In `config.yaml`, set `alerts.email.enabled: true` and `to: [you@example.com]`.

Any SMTP provider works the same way. Port 465 uses SSL automatically; anything else uses
STARTTLS. For a local test relay, set `SMTP_SECURITY=none`.

| Problem | Fix |
|---|---|
| `login failed … Gmail needs an app password` | Use the app password, not your normal one; check 2-Step Verification is on |
| `CERTIFICATE_VERIFY_FAILED` | Already handled: the tracker trusts certifi's certificates. Update if you see it on an old version |
| Sent but nothing arrives | Check Spam and mark it "Not spam" |

## Google Sheets (about 15 minutes)

1. At https://console.cloud.google.com/, create a project, for example `price-tracker`.
2. Enable the **Google Sheets API** and the **Google Drive API** for it.
3. Under **IAM & Admin → Service accounts**, create a service account. It needs no roles.
4. On the service account's **Keys** tab, choose **Add key → JSON**. Move the file out of
   Downloads and keep it outside the repo:

   ```sh
   mkdir -p ~/.config/price-tracker && mv ~/Downloads/KEYFILE.json ~/.config/price-tracker/service-account.json && chmod 600 ~/.config/price-tracker/service-account.json
   ```

5. Add the key's path to `.env`:

   ```
   GOOGLE_SERVICE_ACCOUNT_JSON=/Users/you/.config/price-tracker/service-account.json
   ```

6. Create a sheet and **Share** it, as **Editor**, with the service account's
   `client_email`. You can find it with
   `grep client_email ~/.config/price-tracker/service-account.json`.
7. In `config.yaml`, set `sheets.enabled: true` and `spreadsheet_id` to the sheet's URL or
   ID.

The tracker creates an **Observations** tab (appended every run) and a **Latest** tab (one
row per product). `SpreadsheetNotFound` means the sheet isn't shared with the service
account.

## Slack (optional)

Create an incoming webhook (https://api.slack.com/messaging/webhooks) and add
`SLACK_WEBHOOK_URL=https://hooks.slack.com/services/…` to `.env`. Then set
`alerts.slack.enabled: true` in the config.

## GitHub Actions

The daily workflow reads the same values from repository secrets. With the
[GitHub CLI](https://cli.github.com/) logged in (`gh auth login --scopes workflow`), run
these from the repo folder. They send values straight from your files and never print them.

```sh
gh secret set -f <(grep -E '^SMTP_(HOST|PORT|USER|PASSWORD|FROM)=' .env)
printf '%s' "you@example.com" | gh secret set DIGEST_EMAIL_TO
```

These are optional, for Sheets on Actions:

```sh
gh secret set GOOGLE_SERVICE_ACCOUNT_KEY < ~/.config/price-tracker/service-account.json
printf '%s' "https://docs.google.com/spreadsheets/d/…" | gh secret set SHEETS_SPREADSHEET_ID
```

Then click **Actions → Daily run → Run workflow**, or run `gh workflow run daily.yml`. The
first run starts with empty history and creates the `data` branch, so it never emails: there
is nothing to compare against yet. Later runs restore the history from `data` and save it
back, even when sending fails.

To track your own products instead of the demo books, commit a config without secrets, for
example `clients/acme.yaml`, and change `CONFIG:` at the top of
`.github/workflows/daily.yml`.
