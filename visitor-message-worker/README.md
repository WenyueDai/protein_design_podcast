# Visitor Message Worker

This Cloudflare Worker accepts website contact-form submissions, writes each one into a Notion database, and can also track daily visits / unique visitors.

Flow:

`website form -> Cloudflare Worker -> Notion database`

`website page load -> Cloudflare Worker /visit -> Cloudflare KV`

## Why this setup

- The website stays static on GitHub Pages.
- The Notion token stays server-side inside Cloudflare secrets.
- Visitors can submit directly from the page without opening email.

## What the Worker expects

The website sends JSON like this:

```json
{
  "name": "Ada",
  "message": "Love the site.",
  "submitted_at": "2026-03-03T21:12:00.000Z",
  "site": "https://wenyuedai.github.io/protein_design_podcast/",
  "page_title": "Protein Design Podcast"
}
```

## Notion setup

1. Create a Notion integration.
2. Create a database to collect visitor messages.
3. Add one title property named `Name`.
4. Share that database with the integration.
5. Copy the integration token and the database ID.

The Worker writes:

- the title property as `Website message: <name>`
- the actual message as page content blocks
- submission timestamp and page URL as page content blocks

If your database uses a different title property name, set `NOTION_TITLE_PROPERTY`.

## Cloudflare setup

From this directory:

```bash
npm install
```

Set the secrets:

```bash
npx wrangler secret put NOTION_TOKEN
npx wrangler secret put NOTION_DATABASE_ID
```

Optional variables in `wrangler.toml`:

- `ALLOWED_ORIGIN`: the only browser origin allowed to submit
- `NOTION_TITLE_PROPERTY`: the title property name in your Notion database

For visit tracking, add a KV namespace binding named `VISIT_STATS`.
Example `wrangler.toml` snippet:

```toml
[[kv_namespaces]]
binding = "VISIT_STATS"
id = "<your-production-kv-id>"
preview_id = "<your-preview-kv-id>"
```

Optional secret for protecting stats reads:

```bash
npx wrangler secret put STATS_TOKEN
```

Deploy:

```bash
npx wrangler deploy
```

After deploy, Cloudflare gives you a Worker URL such as:

`https://visitor-message-worker.<your-subdomain>.workers.dev`

## Connect the website

Rebuild the site with the Worker URL:

```bash
VISITOR_MESSAGE_ENDPOINT="https://visitor-message-worker.<your-subdomain>.workers.dev" \
python3 openclaw-knowledge-radio/tools/build_site.py
```

That bakes the Worker URL into the static site form.

## Security notes

- Do not put `NOTION_TOKEN` in the website code.
- Keep `ALLOWED_ORIGIN` limited to your real site origin.
- This Worker accepts:
  - `POST /` for visitor messages
  - `POST /visit` for visit tracking
  - `GET /visit-stats` for the public lifetime visitor count
  - `GET /stats?days=7&token=...` for owner stats reads

## Visit tracking

If `VISIT_STATS` is configured and the site is built with `VISITOR_MESSAGE_ENDPOINT`,
the page will send one `POST /visit` per browser per day using an anonymous ID stored
in `localStorage`.

This gives you approximate:

- daily total visits
- daily unique visitors (unique browsers/devices)
- lifetime unique visitors since tracking was enabled

To read the owner stats:

```bash
curl "https://visitor-message-worker.<your-subdomain>.workers.dev/stats?days=7&token=<your-stats-token>"
```

Response shape:

```json
{
  "ok": true,
  "lifetime_unique": 143,
  "days": [
    { "day": "2026-03-04", "total": 12, "unique": 9 }
  ]
}
```

To read only the public lifetime total:

```bash
curl "https://visitor-message-worker.<your-subdomain>.workers.dev/visit-stats"
```

These are browser-level unique visitors, not perfect person-level identity.
