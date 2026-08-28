# GitHub Issue Watcher → Telegram Notifications

Polls a list of GitHub repos for open issues (optionally filtered by
label) and pushes an instant message to your Telegram (which shows up
as a push notification on your iPhone) when a new one shows up.

Telegram is used instead of ntfy because its iOS push delivery is far
more reliable — no Firebase relay chain, no delayed/dropped notifications.

## Setup

### 1. Create a Telegram bot
- In Telegram, search for **@BotFather** and start a chat with it.
- Send `/newbot`, give it a name and a username (must end in `bot`,
  e.g. `nehant_issue_watcher_bot`).
- BotFather will reply with a **bot token** — looks like
  `123456789:AAExampleTokenString`. Save this, you'll need it as
  `TELEGRAM_BOT_TOKEN`.

### 2. Get your personal chat ID
- Search for **@userinfobot** in Telegram, start a chat with it, and it
  will immediately reply with your numeric **chat ID** (e.g. `987654321`).
  Save this as `TELEGRAM_CHAT_ID`.
- Then open a chat with **your own bot** (search its username) and send
  it any message, e.g. "hi" — this is required so the bot is allowed to
  message you first (Telegram bots can't message you until you've
  messaged them at least once).

### 3. Create a GitHub personal access token
- Go to **GitHub → Settings → Developer settings → Personal access tokens
  → Fine-grained tokens**.
- Create one with **read-only access to public repositories**.
- This raises your API rate limit from 60/hr (unauthenticated) to
  5,000/hr, which comfortably covers polling many repos every 5 minutes.

### 4. Create your own watcher repo
- Make a new repo under your account (can be private), e.g. `issue-watcher`.
- Copy these files into it:
  - `check_issues.py`
  - `config.json`
  - `.github/workflows/check-issues.yml`

### 5. Edit `config.json`
List the repos you want to track. Use `"labels": []` to get every open
issue, or list specific labels to filter:
```json
{
  "repos": [
    { "repo": "owner/repo-one", "labels": [] },
    { "repo": "owner/repo-two", "labels": ["good first issue"] }
  ]
}
```

### 6. Add repo secrets
In your watcher repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add:
- `GH_TOKEN` — the personal access token from step 3
- `TELEGRAM_BOT_TOKEN` — from step 1
- `TELEGRAM_CHAT_ID` — from step 2

### 7. Push and enable Actions
- Commit and push all the files.
- Go to the **Actions** tab in your repo and enable workflows if prompted.
- Manually trigger the workflow once (**Run workflow** button) to make
  sure it runs cleanly before waiting for the schedule.

### 8. Test end-to-end
- Check the Actions run logs — you should see "Notified: ..." lines
  or nothing (if there's nothing new right now), with no errors.
- Confirm messages actually arrived in your chat with the bot on Telegram,
  and that push notifications for that chat are on (Telegram's push
  reliability is generally excellent by default, but double check
  Settings → Notifications in the Telegram app if nothing arrives).

## How it works
- Runs every 5 minutes via GitHub Actions' cron schedule.
- For each repo (+ label, if any), asks GitHub for open issues updated
  since the last check (with a 15-minute overlap buffer, so nothing
  slips through a gap between runs).
- Keeps a `state.json` file (auto-committed back to the repo) tracking
  which issues have already been notified, so you never get duplicates.
- Sends a Telegram message with the issue title and an "Open Issue"
  button that links straight to it.

## Notes
- The issues API returns pull requests too; the script filters those out.
- GitHub Actions' cron can lag a few minutes under load — 5 min is
  about as tight as it reliably gets without paying for a dedicated
  server or serverless cron.
- `"labels": []` for a repo means every open issue with no filtering —
  fine for quiet repos, can get noisy on very active ones (e.g. Oppia).

