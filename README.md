# GitHub Issue Watcher → Telegram Notifications

Polls a list of GitHub repos for open issues (optionally filtered by label) and sends a Telegram message, which appears as a push notification on your phone, when a new one shows up.

Telegram is used instead of ntfy because its iOS push delivery is far more reliable.

---

## How it works

```
cron-job.org  ──(POST, every 15 min)──▶  GitHub API "workflow dispatch"
                                              │
                                              ▼
                                   GitHub Actions: check-issues.yml
                                              │
                                              ▼
                                   check_issues.py reads config.json + state.json
                                              │
                          ┌───────────────────┴──────────────────┐
                          ▼                                      ▼
              GitHub Issues API (uses GH_TOKEN)        Telegram Bot API (uses bot token)
                          │                                      │
                          └──────────── new issues ─────────────▶ your phone
                                              │
                                              ▼
                                  state.json auto-committed back
```

1. **cron-job.org** (or GitHub's own cron schedule) triggers the workflow on a schedule.
2. The workflow runs `check_issues.py`.
3. For each repo in `config.json`, the script asks GitHub for open issues updated since that repo's `last_checked` time (with a 15-minute overlap buffer so nothing slips between runs).
4. Pull requests are filtered out (the issues API returns them too).
5. Any issue not already in the `notified` list triggers a Telegram message with an "Open Issue" button.
6. `state.json` is committed back to the repo so the next run knows what has been handled.

---

## Files

| File | Purpose |
|---|---|
| `check_issues.py` | The watcher script |
| `config.json` | Which repos (and labels) to watch |
| `state.json` | Auto-managed memory: last check times, notified issues (do not edit casually) |
| `.github/workflows/check-issues.yml` | The GitHub Actions workflow |

### `config.json`

```json
{
  "repos": [
    { "repo": "owner/repo-one", "labels": [] },
    { "repo": "owner/repo-two", "labels": ["good first issue"] }
  ]
}
```

- `"labels": []` means every open issue (can be noisy on busy repos).
- Listing labels filters to issues with those labels.

### `state.json` fields

Based on the structure the script writes:

| Field | Meaning |
|---|---|
| `repos.<owner/repo>.last_checked` | Time the repo was last polled. The next run asks GitHub for issues updated since this time. |
| `notified` | Issue keys (`owner/repo#number`) already sent to Telegram. Prevents duplicates. |
| `sent_messages` | Log of Telegram messages sent (message ID, issue key, time). |
| `telegram_update_offset` | Position in the Telegram updates feed, so old bot updates are not re-read. |

---

## Tokens and secrets (what each one is for)

There are four credentials in total. Keep them separate so a leak of one can't do the job of another.

| Credential | Where it lives | Purpose | Permissions needed | Expires? |
|---|---|---|---|---|
| **Cron-job token** (GitHub fine-grained PAT) | cron-job.org, in the `Authorization` header | Lets cron-job.org trigger the workflow | Repository access: only `issue-watcher`; **Actions: Read and write** | Yes |
| **`GH_TOKEN`** (GitHub fine-grained PAT) | Repo secret | Lets the script read issues from other repos (raises API limit from 60/hr to 5,000/hr) | **Public repositories (read-only)**, no extra permissions | Yes |
| **`TELEGRAM_BOT_TOKEN`** | Repo secret | The bot's password from @BotFather; used to send messages | n/a | No |
| **`TELEGRAM_CHAT_ID`** | Repo secret | Your numeric Telegram ID; tells the bot who to message | n/a | No |

> **Fine-grained tokens always expire** (maximum 1 year). Expired tokens are the number one cause of "it silently stopped working." Put a calendar reminder about 2 weeks before each expiry date.

---

## Setup

### 1. Create a Telegram bot

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Choose a name and a username ending in `bot`.
3. Save the **bot token** it gives you (looks like `123456789:AAExample...`). This is `TELEGRAM_BOT_TOKEN`.

### 2. Get your chat ID

1. Open **@userinfobot** in Telegram. It replies with your numeric ID. This is `TELEGRAM_CHAT_ID`.
2. Open a chat with **your own bot** and send it any message (e.g. "hi"). Bots cannot message you until you have messaged them once.

### 3. Create the `GH_TOKEN` (read-only)

1. GitHub → profile picture → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Name: `issue-watcher-GH-TOKEN`. Expiration: 1 year. Resource owner: your account.
3. **Repository access: Public repositories.**
4. Leave permissions empty.
5. Generate, and copy the token immediately (it is shown only once).

### 4. Add repo secrets

In your watcher repo: **Settings → Secrets and variables → Actions → New repository secret**. Add:

- `GH_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

To change one later, click the pencil icon next to it and paste the new value.

### 5. Add files and enable Actions

1. Commit `check_issues.py`, `config.json` and `.github/workflows/check-issues.yml`.
2. Open the **Actions** tab and enable workflows if prompted.
3. Click **Run workflow** once to confirm it runs cleanly.

The workflow file must include a manual/API trigger:

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: "*/15 * * * *"   # optional backup if you also want GitHub's own schedule
```

Without `workflow_dispatch:`, the cron-job API call fails with HTTP 422.

### 6. Set up the cron-job.org job

Create a job with these settings:

| Setting | Value |
|---|---|
| URL | `https://api.github.com/repos/YOUR_USER/issue-watcher/actions/workflows/check-issues.yml/dispatches` |
| Request method | `POST` |
| Request body | `{"ref": "main"}` |
| Headers | `Accept: application/vnd.github+json` |
| | `Authorization: Bearer YOUR_CRONJOB_TOKEN` |
| | `Content-Type: application/json` |
| | `X-GitHub-Api-Version: 2022-11-28` (recommended) |
| Schedule | Every 15 minutes (or 5) |
| Enable job | On |
| Save responses in job history | On (essential for debugging) |
| Requires HTTP authentication | Off (the token is already in the header) |

The cron-job token is a separate fine-grained PAT:

- Repository access: **Only select repositories → `issue-watcher`**
- Permissions: **Actions → Read and write**

A successful call returns **HTTP 204** with an empty body. That is correct.

---

## Renewing an expired token (step by step)

### Expired `GH_TOKEN`

1. Create a new read-only token (Setup step 3).
2. Repo **Settings → Secrets and variables → Actions → GH_TOKEN → pencil → paste → Update secret**.
3. Follow "Catching up after downtime" below.
4. Run the workflow manually and check the logs.

### Expired cron-job token

1. Create a new token scoped to `issue-watcher` with **Actions: Read and write**.
2. In cron-job.org, edit the job and replace the token in the `Authorization` header (`Bearer ` followed by the token, one space, no trailing whitespace).
3. Save, click **Test run**, and check job history for HTTP 204.
4. Make sure **Enable job** is on. Updating the token does not re-enable a deactivated job.

---

## Catching up after downtime

While the watcher is broken, `last_checked` may still move forward, which means issues opened during the outage would be skipped. To make the script rescan the gap:

1. Edit `state.json` on GitHub (pencil icon), when no workflow run is in progress, because the workflow commits to this same file.
2. For each repo under `"repos"`, set `last_checked` to a time just before the outage started, for example:

   ```json
   "zulip/zulip": { "last_checked": "2026-09-26T00:00:00+00:00" }
   ```

3. Commit, then run the workflow manually.

Duplicates are prevented by the `notified` list, so you only receive messages for issues you have not been told about. Setting the time earlier than needed is safe; it just causes a slightly larger scan.

Avoid adding a brand-new repo to `config.json` with a very old `last_checked` and `"labels": []`, or you may receive a flood of messages for every open issue.

---

## Troubleshooting

### Reading cron-job.org failures

Open the job → **History** → latest entry, and read the status code and response body.

| Status | Meaning | Fix |
|---|---|---|
| **204** | Success | Nothing to do |
| **401** "Bad credentials" | Token expired, revoked, mistyped, or has a stray space/newline | Create a new token; re-paste carefully; header must be `Bearer <token>` |
| **403** "Resource not accessible by personal access token" | Token lacks permission | Grant **Actions: Read and write**, scoped to `issue-watcher` |
| **404** Not Found | Token can't see the repo, wrong repo or workflow filename, or workflow disabled | Check token repo access; confirm `.github/workflows/check-issues.yml` exists; re-enable the workflow in the Actions tab |
| **422** | Workflow has no `workflow_dispatch:` trigger, or `ref` branch doesn't exist | Add `workflow_dispatch:` to the YAML; confirm the branch is `main` |
| Timeout / no status | Network or GitHub outage | Retry; check githubstatus.com |
| Job shows **Inactive** | cron-job.org disables jobs after repeated failures | Fix the cause, test run, then turn **Enable job** back on |

### Test the dispatch outside cron-job.org

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/YOUR_USER/issue-watcher/actions/workflows/check-issues.yml/dispatches \
  -d '{"ref":"main"}'
```

`HTTP/2 204` means the token and workflow are fine.

### Cron job succeeds but no Telegram messages

1. **Check the workflow run.** Open **Actions → latest "Check issues" run → the script step**.
   - **Red X or `401` / "Bad credentials":** `GH_TOKEN` has expired. Renew it and catch up.
   - **Errors from `api.telegram.org`:** the bot token or chat ID is wrong, or you never messaged the bot first.
   - **Green, with no "Notified:" lines:** there are simply no new issues.
2. **Check `state.json`.**
   - `last_checked` advancing with nothing added to `notified` for days, in repos you know are active, points to a token problem being swallowed silently.
   - Compare the newest `sent_messages` timestamp with today's date to see when the last notification went out.
3. **Test Telegram directly:**

   ```bash
   curl "https://api.telegram.org/botYOUR_BOT_TOKEN/sendMessage?chat_id=YOUR_CHAT_ID&text=test"
   ```

   If "test" arrives, Telegram is fine. If not, verify the token and chat ID, and confirm you have messaged the bot at least once.
4. **Check the phone.** Make sure the bot chat is not muted and that Telegram notifications are allowed in system settings.

### A repo is never checked

Compare repos in `state.json` with `config.json`. A repo whose `last_checked` is weeks old was probably removed from `config.json` or is failing to fetch. Re-add it or fix the name (`owner/repo`, exact spelling).

### Too many notifications

Add labels in `config.json`, for example `"labels": ["good first issue"]`, instead of `[]`.

### Rate limit errors (403 or 429 from the issues API)

Confirm `GH_TOKEN` is actually being passed to the script. Without it, the limit is 60 requests per hour. With it, 5,000 per hour.

### Workflow stopped running entirely

- GitHub disables scheduled workflows in repos with **60 days of no activity**. The auto-commits to `state.json` normally count as activity, but check the Actions tab for a "disabled" banner and re-enable if needed.
- If you rely only on cron-job.org, this doesn't apply, but a disabled workflow returns 404 or 422 on dispatch.

---

## Security notes

- Never paste tokens into chats, screenshots, issues or commits. If one leaks, revoke it immediately at **Settings → Developer settings → Fine-grained tokens** and create a replacement.
- Use the minimum scope for each token: read-only public for `GH_TOKEN`, and Actions read/write on this one repo for the cron-job token.
- Repo secrets are encrypted and not exposed to workflows triggered by forks.

---

## Maintenance checklist

- [ ] Calendar reminder 2 weeks before `GH_TOKEN` expiry
- [ ] Calendar reminder 2 weeks before cron-job token expiry
- [ ] Occasionally check cron-job.org for **Inactive** or failed jobs
- [ ] Occasionally check the newest `sent_messages` date in `state.json`
- [ ] Keep `config.json` in sync with the repos you actually care about

---

## Notes

- GitHub Actions' own cron can lag a few minutes under load; an external cron service like cron-job.org is more reliable.
- The GitHub issues API returns pull requests as well; the script filters them out.
- `"labels": []` is fine for quiet repos but can be noisy on very active ones (e.g. Oppia, Rocket.Chat).
