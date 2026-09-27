# GU Eco Events

This project builds a weekly calendar feed (`eco-events.ics`) and sends Discord notifications (events requiring registration) and Slack notifications (the paid subset, with cost) for sustainability events from the University of Gothenburg (GU).

## Feed Subscription

- **ICS Feed URL**: `https://moonleaf-earth.github.io/gu-eco-events/eco-events.ics`
- **Webcal**: `webcal://moonleaf-earth.github.io/gu-eco-events/eco-events.ics`

Note: Calendar clients choose their own refresh interval, so subscription updates may lag behind the weekly source check.

## Testing & Development

Run the documented test command against checked-in HTML fixtures:

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. pytest
```

### CLI Modes
The CLI (`python -m gu_eco_events`) supports several modes:
- `build`: Fetches events, runs safety guards, and writes feed, but leaves state untouched.
- `notify`: Plans and sends Discord and Slack notifications and updates state. Requires `--mode` (`dry-run`, `send`, `record-only`); there is no default.
  - `dry-run`: Prints plan and records messages as sent, updating the provided state file. To avoid suppressing real notifications, operators running `dry-run` manually should use a separate or temporary state file.
  - `send`: Delivers via each channel's webhook and marks each message as sent only after that service accepts it. Discord and Slack are tracked independently: a channel whose secret is missing or invalid keeps its notices pending (the other channel still delivers), and if one channel fails part-way (HTTP error such as a revoked webhook, connection reset, TLS or read error) only that channel stops; its delivered messages stay marked, the rest are retried next run, the other channel keeps delivering, and the command exits with code 4. The summary line lists the failing channel names in `failed_channels`.
  - `record-only`: Does not deliver and marks no message as sent; only the event snapshot and `last_success` are updated.
- `run`: Runs build and notify in one go. `--mode` defaults to `dry-run`.
- `validate`: Parses an `.ics` file to ensure RFC 5545 validity.
- `check-leaks`: Scans for webhook URLs or secret values in files.

## Extraction Rules & Source Details

- **Source**: Fetched from `gu.se` event search ("Hållbarhet & miljö"). The scraper is considerate: it identifies itself with a custom User-Agent, fetches sequentially without parallel requests, and respects `robots.txt` explicitly via `urllib.robotparser`. It runs on a weekly schedule and uses stable canonical URLs.
- **Location Rule**: Events must take place in Göteborg/Gothenburg or online. Events solely outside these areas are rejected.
- **Default Duration**: If an event has a start time but no end time, a default duration of 1 hour is applied.
- **State Projection**: Discord and Slack notification and cancellation status is persisted independently in `data/state.json`. Older state without Slack markers or event `cost` is read with additive defaults.


## Routing Rules
Both channels share the same date/cancellation rules: past events and events whose registration deadline has passed get no new/update notice; a previously announced event that is cancelled gets one cancellation notice on the channel(s) that announced it.

- **Discord** (unchanged): an event is announced iff `registration_required == true`. Open/drop-in/no-registration events are never sent.
- **Slack** (paid events): the strict subset `registration_required == true AND cost is paid`. Open events are never sent, even when they have a cost.
- **Cost**: parsed only from the structured GU event field `Kostnad` (or `Cost`); body prose is never used. The exact GU text (e.g. `950 kr plus moms`) is kept and shown in the Slack message.
  - *free*: `Free`, `Gratis`, `Kostnadsfri(tt)`, `Avgiftsfri(tt)`, or a zero price (`0`, `0 kr`, `0,00 SEK`, `0:-`, `SEK 0`).
  - *paid*: a non-zero amount with a currency (`950 kr plus moms`, `SEK 500`, `500:-`) or a bare non-zero number, with no free marker in the same value.
  - *unknown* (never Slack): missing, blank, or ambiguous values (`Se hemsidan`, `Gratis för studenter, 200 kr för övriga`).
- **Idempotency**: Discord stores `notified_hash` (title/time/place); Slack stores `slack_notified_hash` (title/time/place/cost), so a price correction produces exactly one Slack update and no Discord update. Events announced to Discord before Slack existed still get their first Slack announcement.
- Slack messages escape `&`, `<` and `>` in all scraped text, so GU content cannot trigger `@channel`/`@here`/user mentions; Discord messages are sent with `allowed_mentions` disabled.

## Setup Instructions

### 1. GitHub Pages
The feed is published using GitHub Pages.
1. Make the repository **public** (**Settings** -> **General** -> **Danger Zone** -> **Change repository visibility**). On the GitHub Free plan (the `moonleaf-earth` personal account) GitHub Pages is not available for private repositories, so the stable feed URL above only exists once the repo is public. This is safe: no secrets are committed, the Discord webhook lives only in the `ECO_EVENTS_DISCORD_WEBHOOK_URL` Actions secret (never logged, and `check-leaks` guards tracked files and the published artifact), and `data/state.json` contains only public GU event data (titles, times, places, URLs) plus content hashes and notification flags.
2. Go to repository **Settings** -> **Pages**.
3. Set the **Source** to **GitHub Actions**.

### 2. Discord and Slack Webhooks
1. In Discord, go to the `#eco-events` channel settings.
2. Select **Integrations** -> **Webhooks** -> **New Webhook**.
3. Copy the Webhook URL. Ensure this webhook is channel-scoped and has no other permissions.
4. Go to this repository's **Settings** -> **Secrets and variables** -> **Actions**.
5. Click **New repository secret**.
6. Name it `ECO_EVENTS_DISCORD_WEBHOOK_URL` and paste the URL.

#### Slack Webhook (`ECO_EVENTS_SLACK_WEBHOOK_URL`)
Use a webhook that belongs to this repository only; do not reuse Kodama Harness's Slack credentials.
1. Go to <https://api.slack.com/apps> -> **Create New App** -> **From scratch**, and pick the target workspace.
2. Under **Features** -> **Incoming Webhooks**, turn **Activate Incoming Webhooks** on.
3. Click **Add New Webhook to Workspace**, choose the channel that should receive paid events, and **Allow**. The webhook is scoped to that single channel; the webhook itself decides the target channel.
4. Copy the Webhook URL (`https://hooks.slack.com/services/...`). Treat it as a secret: never paste it into issues, commits, or logs.
5. Go to this repository's **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**.
6. Name it `ECO_EVENTS_SLACK_WEBHOOK_URL` and paste the URL.

Until the secret exists, eligible Slack notices stay pending in `data/state.json` and are delivered on the first run after it is added; feed publication and Discord are unaffected. `check-leaks` fails the run if the secret value or any `hooks.slack.com/services/...` URL appears in tracked files or the published artifact.

### 3. Workflow Monitoring
GitHub may automatically disable scheduled workflows in inactive repositories. To check and reactivate:
1. Go to the repository's **Actions** tab.
2. If there is a banner stating the scheduled workflow was disabled, click the button to **Enable workflow**.

If a notification channel fails, the feed and `data/state.json` are still published, then the deploy job's **Fail if notify failed** step fails the run and names the failing channel(s) (e.g. `Notification delivery failed for: slack`); the same line appears in the build job's step summary. Webhook values are never printed. Undelivered notices for that channel are retried on the next run.
