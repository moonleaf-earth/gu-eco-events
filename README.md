# GU Eco Events

This project builds a weekly calendar feed (`eco-events.ics`) and sends Discord notifications (events requiring registration) for sustainability events from the University of Gothenburg (GU).

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
- `notify`: Plans and sends Discord notifications and updates state. Requires `--mode` (`dry-run`, `send`, `record-only`); there is no default.
  - `dry-run`: Prints plan and records messages as sent, updating the provided state file. To avoid suppressing real notifications, operators running `dry-run` manually should use a separate or temporary state file.
  - `send`: Delivers via each channel's webhook and marks each message as sent only after that service accepts it. If a channel's secret is missing or invalid, its notices are kept pending. If delivery fails part-way (HTTP error such as a revoked webhook, connection reset, TLS or read error), delivered messages stay marked, the rest are retried next run, and the command exits with code 4. The summary line lists the failing channel names in `failed_channels`.
  - `record-only`: Does not deliver and marks no message as sent; only the event snapshot and `last_success` are updated.
- `run`: Runs build and notify in one go. `--mode` defaults to `dry-run`.
- `validate`: Parses an `.ics` file to ensure RFC 5545 validity.
- `check-leaks`: Scans for webhook URLs or secret values in files.

## Extraction Rules & Source Details

- **Source**: Fetched from `gu.se` event search ("Hållbarhet & miljö"). The scraper is considerate: it identifies itself with a custom User-Agent, fetches sequentially without parallel requests, and respects `robots.txt` explicitly via `urllib.robotparser`. It runs on a weekly schedule and uses stable canonical URLs.
- **Location Rule**: Events must take place in Göteborg/Gothenburg or online. Events solely outside these areas are rejected.
- **Default Duration**: If an event has a start time but no end time, a default duration of 1 hour is applied.
- **State Projection**: Discord notification and cancellation status is persisted in `data/state.json`. Older state without event `cost` is read with additive defaults.


## Routing Rules
Past events and events whose registration deadline has passed get no new/update notice; a previously announced event that is cancelled gets one cancellation notice.

- **Discord**: an event is announced iff `registration_required == true`. Open/drop-in/no-registration events are never sent.
- **Cost**: parsed only from the structured GU event field `Kostnad` (or `Cost`); body prose is never used. The exact GU text (e.g. `950 kr plus moms`) is kept and shown in the Discord message.
  - *free*: `Free`, `Gratis`, `Kostnadsfri(tt)`, `Avgiftsfri(tt)`, or a zero price (`0`, `0 kr`, `0,00 SEK`, `0:-`, `SEK 0`).
  - *paid*: a non-zero amount with a currency (`950 kr plus moms`, `SEK 500`, `500:-`) or a bare non-zero number, with no free marker in the same value.
  - *unknown*: missing, blank, or ambiguous values (`Se hemsidan`, `Gratis för studenter, 200 kr för övriga`).
- **Idempotency**: Discord stores `notified_hash` (title/time/place/cost if paid), so a price correction produces exactly one update.
- Discord messages are sent with `allowed_mentions` disabled.

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


### 3. Workflow Monitoring
GitHub may automatically disable scheduled workflows in inactive repositories. To check and reactivate:
1. Go to the repository's **Actions** tab.
2. If there is a banner stating the scheduled workflow was disabled, click the button to **Enable workflow**.

If a notification channel fails, the feed and `data/state.json` are still published, then the deploy job's **Fail if notify failed** step fails the run and names the failing channel(s) (e.g. `Notification delivery failed for: slack`); the same line appears in the build job's step summary. Webhook values are never printed. Undelivered notices for that channel are retried on the next run.
