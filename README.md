# GU Eco Events

This project builds a weekly calendar feed (`eco-events.ics`) and sends Discord notifications for sustainability events from the University of Gothenburg (GU).

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
  - `send`: Delivers via webhook and marks each message as sent only after Discord accepts it. If delivery fails part-way (HTTP error, connection reset, TLS or read error), messages already delivered stay marked in state, the rest are retried next run, and the command exits with code 4.
  - `record-only`: Does not deliver and marks no message as sent; only the event snapshot and `last_success` are updated. Used automatically when the secret is missing, so installing `ECO_EVENTS_DISCORD_WEBHOOK_URL` later still announces the pending events.
- `run`: Runs build and notify in one go. `--mode` defaults to `dry-run`.
- `validate`: Parses an `.ics` file to ensure RFC 5545 validity.
- `check-leaks`: Scans for webhook URLs or secret values in files.

## Extraction Rules & Source Details

- **Source**: Fetched from `gu.se` event search ("Hållbarhet & miljö"). The scraper is considerate: it identifies itself with a custom User-Agent, fetches sequentially without parallel requests, and respects `robots.txt` explicitly via `urllib.robotparser`. It runs on a weekly schedule and uses stable canonical URLs.
- **Location Rule**: Events must take place in Göteborg/Gothenburg or online. Events solely outside these areas are rejected.
- **Default Duration**: If an event has a start time but no end time, a default duration of 1 hour is applied.
- **State Projection**: Notification and cancellation status is persisted in `data/state.json`.


## Routing Rules
- **Discord**: All events requiring pre-registration are announced to Discord.
- **Slack**: Only a strict subset is sent to Slack: events requiring pre-registration where the structured cost field is confidently non-free (e.g., has a numerical cost like "950 kr plus moms"). Free, zero-price, and ambiguous cost fields are treated as free/unknown and not sent to Slack. Open events (no registration required) are never sent to either channel.

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

#### Slack Webhook
1. In your Slack workspace, go to Apps & Integrations and configure a new Incoming Webhook.
2. Select the destination channel for paid events and copy the Webhook URL (starts with `https://hooks.slack.com/services/...`).
3. Ensure this webhook is channel-scoped.
4. Go to this repository's **Settings** -> **Secrets and variables** -> **Actions**.
5. Click **New repository secret**.
6. Name it `ECO_EVENTS_SLACK_WEBHOOK_URL` and paste the URL.


### 3. Workflow Monitoring
GitHub may automatically disable scheduled workflows in inactive repositories. To check and reactivate:
1. Go to the repository's **Actions** tab.
2. If there is a banner stating the scheduled workflow was disabled, click the button to **Enable workflow**.
