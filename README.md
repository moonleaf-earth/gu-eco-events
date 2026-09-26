# GU Eco Events

This project builds a weekly calendar feed (`eco-events.ics`) and sends Discord notifications for sustainability events from the University of Gothenburg (GU).

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

### 3. Usage
The pipeline runs automatically every week (configured in `.github/workflows/publish.yml`). It can also be triggered manually using **workflow_dispatch**.

Note: Subscription updates for clients like Apple Calendar or Google Calendar might lag behind the source check due to their own caching policies. If the GitHub Actions schedule is paused due to inactivity, a repository maintainer will need to manually re-enable it.
