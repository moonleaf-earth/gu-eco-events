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
- `notify`: Plans and sends Discord notifications and updates state. Supports `--mode` (`dry-run`, `send`, `record-only`).
  - `dry-run`: Prints plan, does not send or save state.
  - `send`: Delivers via webhook, records in state.
  - `record-only`: Does not deliver, but records in state (useful when secret is missing).
- `run`: Runs build and notify in one go.
- `validate`: Parses an `.ics` file to ensure RFC 5545 validity.
- `check-leaks`: Scans for webhook URLs or secret values in files.

## Extraction Rules & Source Details

- **Source**: Fetched from `gu.se` event search ("Hållbarhet & miljö"). The scraper is considerate: it identifies itself with a custom User-Agent, fetches sequentially without parallel requests, respects robots guidance implicitly by limiting fetch frequency to a weekly schedule, and uses stable canonical URLs.
- **Location Rule**: Events must take place in Göteborg/Gothenburg or online. Events solely outside these areas are rejected.
- **Default Duration**: If an event has a start time but no end time, a default duration of 1 hour is applied.
- **State Projection**: Notification and cancellation status is persisted in `data/state.json`.

## Setup Instructions

### 1. GitHub Pages
The feed is published using GitHub Pages.
1. Go to repository **Settings** -> **Pages**.
2. Set the **Source** to **GitHub Actions**.

### 2. Discord Webhook
1. In Discord, go to the `#eco-events` channel settings.
2. Select **Integrations** -> **Webhooks** -> **New Webhook**.
3. Copy the Webhook URL. Ensure this webhook is channel-scoped and has no other permissions.
4. Go to this repository's **Settings** -> **Secrets and variables** -> **Actions**.
5. Click **New repository secret**.
6. Name it `DISCORD_WEBHOOK_URL` and paste the URL.

### 3. Workflow Monitoring
GitHub may automatically disable scheduled workflows in inactive repositories. To check and reactivate:
1. Go to the repository's **Actions** tab.
2. If there is a banner stating the scheduled workflow was disabled, click the button to **Enable workflow**.
