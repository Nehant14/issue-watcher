# GitHub Issue Watcher → iPhone Notifications

Polls a list of GitHub repos for open issues with specific labels
(e.g. `good first issue`, `gsoc`) and pushes an instant notification
to your iPhone via [ntfy](https://ntfy.sh) when a new one shows up.

## Setup

### 1. Install ntfy on your iPhone
- Get the free **ntfy** app from the App Store.
- Open it and subscribe to a topic — pick something private and hard to
  guess, e.g. `nehant-gsoc-8f3k2`. This is your personal channel; anyone
  who knows the topic name can send to it, so don't use something obvious.
- (Optional but recommended) In the app, set this topic's notifications
  to **high priority** so they break through Focus/Do Not Disturb.

### 2. Create a GitHub personal access token
- Go to **GitHub → Settings → Developer settings → Personal access tokens
  → Fine-grained tokens**.
- Create one with **read-only access to public repositories**. You don't
  need write access to the target repos — you're only reading issue data.
- This raises your API rate limit from 60/hr (unauthenticated) to
  5,000/hr, which comfortably covers polling many repos every 5 minutes.

### 3. Create your own watcher repo
- Make a new repo under your account (can be private), e.g. `issue-watcher`.
- Copy these files into it:
  - `check_issues.py`
  - `config.json`
  - `.github/workflows/check-issues.yml`

### 4. Edit `config.json`
List the repos and labels you actually want to track:
```json
{
  "repos": [
    {
      "repo": "owner/repo-one",
      "labels": ["good first issue", "help wanted"]
    }
  ]
}
```
Add as many repo entries as you like — this is the only file you'll
need to touch when you want to add/remove a repo later.

**`"labels": []` means every open issue** — new ones and any updated
ones — with no filter at all. That's the default in the example above.
If you later want to narrow a specific repo down to just certain labels
(e.g. `good first issue`), just fill that repo's `"labels"` array in;
leave it `[]` for repos where you want everything.

### 5. Add repo secrets
In your watcher repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add:
- `GH_TOKEN` — the personal access token from step 2
- `NTFY_TOPIC` — the topic name from step 1

### 6. Push and enable Actions
- Commit and push all the files.
- Go to the **Actions** tab in your repo and enable workflows if prompted.
- Manually trigger the workflow once (**Run workflow** button) to make
  sure it runs cleanly before waiting for the schedule.

### 7. Test end-to-end
- Check the Actions run logs — you should see either "Notified: ..." lines
  or nothing (if there's nothing new right now), with no errors.
- Wait for a real issue matching your labels to appear, or ask a friend
  to open a test issue with a matching label on a repo you control, and
  confirm the notification lands on your phone.

## How it works
- Runs every 5 minutes via GitHub Actions' cron schedule.
- For each repo + label pair, asks GitHub for open issues updated since
  the last check (with a 15-minute overlap buffer, so nothing slips
  through a gap between runs).
- Keeps a `state.json` file (auto-committed back to the repo) tracking
  which issues have already been notified, so you never get duplicates.
- Sends a push via `ntfy.sh/<your-topic>` with the issue title and a
  tap-to-open link straight to the issue.

## Notes
- The issues API returns pull requests too; the script filters those out.
- GitHub Actions' cron can lag a few minutes under load — 5 min is
  about as tight as it reliably gets without paying for a dedicated
  server or serverless cron.
- If you want a faster/more reliable trigger later, a small serverless
  function (e.g. on Cloudflare Workers or a free Render cron job) that
  runs `check_issues.py` on its own schedule works the same way.
