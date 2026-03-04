export default {
  async fetch(request, env) {
    return handleRequest(request, env);
  },
};

const DEFAULT_ALLOWED_ORIGIN = "https://wenyuedai.github.io";
const DEFAULT_TITLE_PROPERTY = "Name";
const DEFAULT_SOURCE_LABEL = "Website message";
const VISIT_NS = "VISIT_STATS";

async function handleRequest(request, env) {
  const url = new URL(request.url);
  const allowedOrigin = (env.ALLOWED_ORIGIN || DEFAULT_ALLOWED_ORIGIN).trim();
  const corsHeaders = buildCorsHeaders(allowedOrigin);

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }

  const origin = request.headers.get("Origin") || "";
  if (origin && origin !== allowedOrigin) {
    return jsonResponse({ error: "Forbidden origin" }, 403, corsHeaders);
  }

  if (url.pathname === "/visit") {
    if (request.method !== "POST") {
      return jsonResponse({ error: "Method not allowed" }, 405, corsHeaders);
    }
    return handleVisit(request, env, corsHeaders);
  }

  if (url.pathname === "/visit-stats") {
    if (request.method !== "GET") {
      return jsonResponse({ error: "Method not allowed" }, 405, corsHeaders);
    }
    return handleVisitStats(env, corsHeaders);
  }

  if (url.pathname === "/stats") {
    if (request.method !== "GET") {
      return jsonResponse({ error: "Method not allowed" }, 405, corsHeaders);
    }
    return handleStats(url, env, corsHeaders);
  }

  if (request.method !== "POST") {
    return jsonResponse({ error: "Method not allowed" }, 405, corsHeaders);
  }

  return handleMessage(request, env, corsHeaders);
}

async function handleMessage(request, env, corsHeaders) {
  if (!env.NOTION_TOKEN || !env.NOTION_DATABASE_ID) {
    return jsonResponse({ error: "Worker is not configured" }, 500, corsHeaders);
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return jsonResponse({ error: "Invalid JSON" }, 400, corsHeaders);
  }

  const message = cleanText(payload.message, 4000);
  if (!message) {
    return jsonResponse({ error: "Message is required" }, 400, corsHeaders);
  }

  const name = cleanText(payload.name, 200) || "Website Visitor";
  const sourcePage = cleanText(payload.site, 500) || "";
  const pageTitle = cleanText(payload.page_title, 300) || "";
  const submittedAt = parseSubmittedAt(payload.submitted_at);
  const titleProperty = (env.NOTION_TITLE_PROPERTY || DEFAULT_TITLE_PROPERTY).trim();

  const notionBody = {
    parent: { database_id: env.NOTION_DATABASE_ID },
    properties: {
      [titleProperty]: {
        title: [
          {
            text: {
              content: `${DEFAULT_SOURCE_LABEL}: ${name}`.slice(0, 2000),
            },
          },
        ],
      },
    },
    children: buildMessageBlocks({
      name,
      message,
      sourcePage,
      pageTitle,
      submittedAt,
    }),
  };

  const notionRes = await fetch("https://api.notion.com/v1/pages", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.NOTION_TOKEN}`,
      "Content-Type": "application/json",
      "Notion-Version": "2022-06-28",
    },
    body: JSON.stringify(notionBody),
  });

  if (!notionRes.ok) {
    const errText = await notionRes.text();
    return jsonResponse(
      {
        error: "Notion request failed",
        status: notionRes.status,
        details: errText.slice(0, 1000),
      },
      502,
      corsHeaders,
    );
  }

  return jsonResponse({ ok: true }, 200, corsHeaders);
}

function buildCorsHeaders(allowedOrigin) {
  return {
    "Access-Control-Allow-Origin": allowedOrigin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function jsonResponse(body, status, extraHeaders) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...extraHeaders,
    },
  });
}

function cleanText(value, maxLen) {
  if (typeof value !== "string") return "";
  return value.trim().replace(/\0/g, "").slice(0, maxLen);
}

function parseSubmittedAt(value) {
  if (typeof value !== "string") return new Date().toISOString();
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return new Date().toISOString();
  return parsed.toISOString();
}

function buildMessageBlocks({ name, message, sourcePage, pageTitle, submittedAt }) {
  const lines = [
    `From: ${name}`,
    `Submitted: ${submittedAt}`,
  ];

  if (pageTitle) lines.push(`Page title: ${pageTitle}`);
  if (sourcePage) lines.push(`Page URL: ${sourcePage}`);

  const blocks = [
    paragraphBlock(lines.join("\n")),
  ];

  for (const chunk of splitIntoChunks(message, 1800)) {
    blocks.push(paragraphBlock(chunk));
  }

  return blocks;
}

function paragraphBlock(text) {
  return {
    object: "block",
    type: "paragraph",
    paragraph: {
      rich_text: [
        {
          type: "text",
          text: {
            content: text,
          },
        },
      ],
    },
  };
}

function splitIntoChunks(text, maxLen) {
  const chunks = [];
  let remaining = text;

  while (remaining.length > maxLen) {
    let splitAt = remaining.lastIndexOf("\n", maxLen);
    if (splitAt < maxLen * 0.5) splitAt = remaining.lastIndexOf(" ", maxLen);
    if (splitAt < maxLen * 0.5) splitAt = maxLen;
    chunks.push(remaining.slice(0, splitAt).trim());
    remaining = remaining.slice(splitAt).trim();
  }

  if (remaining) chunks.push(remaining);
  return chunks;
}

async function handleVisit(request, env, corsHeaders) {
  const store = env[VISIT_NS];
  if (!store) {
    return jsonResponse({ error: "Visit tracking is not configured" }, 500, corsHeaders);
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return jsonResponse({ error: "Invalid JSON" }, 400, corsHeaders);
  }

  const visitorId = cleanText(payload.visitor_id, 128);
  const day = cleanDay(payload.day);
  if (!visitorId) {
    return jsonResponse({ error: "visitor_id is required" }, 400, corsHeaders);
  }

  const totalKey = `visits:${day}:total`;
  const uniqueCountKey = `visits:${day}:unique_count`;
  const uniqueSeenKey = `visits:${day}:visitor:${visitorId}`;
  const lifetimeUniqueKey = "visits:lifetime:unique_count";
  const lifetimeSeenKey = `visits:lifetime:visitor:${visitorId}`;

  const currentTotal = parseInt((await store.get(totalKey)) || "0", 10) || 0;
  await store.put(totalKey, String(currentTotal + 1));

  let unique = false;
  const seen = await store.get(uniqueSeenKey);
  if (!seen) {
    unique = true;
    await store.put(uniqueSeenKey, "1", { expirationTtl: 60 * 60 * 24 * 45 });
    const currentUnique = parseInt((await store.get(uniqueCountKey)) || "0", 10) || 0;
    await store.put(uniqueCountKey, String(currentUnique + 1));
  }

  let lifetimeUnique = parseInt((await store.get(lifetimeUniqueKey)) || "0", 10) || 0;
  const seenLifetime = await store.get(lifetimeSeenKey);
  if (!seenLifetime) {
    await store.put(lifetimeSeenKey, "1");
    lifetimeUnique += 1;
    await store.put(lifetimeUniqueKey, String(lifetimeUnique));
  }

  const totals = await readVisitCounts(store, day);
  totals.lifetime_unique = lifetimeUnique;
  return jsonResponse({ ok: true, day, unique, ...totals }, 200, corsHeaders);
}

async function handleStats(url, env, corsHeaders) {
  const store = env[VISIT_NS];
  if (!store) {
    return jsonResponse({ error: "Visit tracking is not configured" }, 500, corsHeaders);
  }

  const expectedToken = cleanText(env.STATS_TOKEN || "", 200);
  const suppliedToken = cleanText(url.searchParams.get("token") || "", 200);
  if (expectedToken && suppliedToken !== expectedToken) {
    return jsonResponse({ error: "Forbidden" }, 403, corsHeaders);
  }

  const days = Math.max(1, Math.min(31, parseInt(url.searchParams.get("days") || "7", 10) || 7));
  const rows = [];
  for (let offset = 0; offset < days; offset += 1) {
    const day = isoDay(offset);
    rows.push({
      day,
      ...(await readVisitCounts(store, day)),
    });
  }

  const lifetime_unique = parseInt((await store.get("visits:lifetime:unique_count")) || "0", 10) || 0;
  return jsonResponse({ ok: true, lifetime_unique, days: rows }, 200, corsHeaders);
}

async function handleVisitStats(env, corsHeaders) {
  const store = env[VISIT_NS];
  if (!store) {
    return jsonResponse({ error: "Visit tracking is not configured" }, 500, corsHeaders);
  }

  const lifetime_unique = parseInt((await store.get("visits:lifetime:unique_count")) || "0", 10) || 0;
  return jsonResponse({ ok: true, lifetime_unique }, 200, corsHeaders);
}

async function readVisitCounts(store, day) {
  const total = parseInt((await store.get(`visits:${day}:total`)) || "0", 10) || 0;
  const unique = parseInt((await store.get(`visits:${day}:unique_count`)) || "0", 10) || 0;
  return { total, unique };
}

function cleanDay(value) {
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return value;
  }
  return isoDay(0);
}

function isoDay(daysAgo) {
  const d = new Date();
  d.setUTCHours(0, 0, 0, 0);
  d.setUTCDate(d.getUTCDate() - daysAgo);
  return d.toISOString().slice(0, 10);
}
