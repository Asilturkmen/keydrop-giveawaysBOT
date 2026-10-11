/**
 * Keydrop Bot - anonymous usage stats (Cloudflare Worker + D1).
 *
 *   POST /ping      the app says "I'm running": {id, v, lang, joins}
 *   GET  /stats     the public numbers as JSON (README badges and the app read this)
 *   GET  /card.svg  the stats card shown in the README (?theme=dark|light)
 *
 * What is stored: see schema.sql. No IP addresses, no Steam / Keydrop data.
 */

const REPO = "Asilturkmen/keydrop-giveawaysBOT";
const DAY = 86400;
const STATS_TTL = 600;        // recompute the public numbers at most every 10 minutes
const DOWNLOADS_TTL = 3600;   // ask GitHub for the download count at most once an hour
const MAX_JOINS = 50;         // per ping; the app sends the rest with its next ping
const KEEP_DAYS = 60;         // the daily activity rows are deleted after this

export default {
  async fetch(request, env) {
    const { pathname, searchParams } = new URL(request.url);
    try {
      if (pathname === "/ping" && request.method === "POST") return await ping(request, env);
      if (request.method === "GET" || request.method === "HEAD") {
        if (pathname === "/stats") return json(await getStats(env), 300);
        if (pathname === "/card.svg") return svg(card(await getStats(env), searchParams.get("theme")));
        if (pathname === "/") return Response.redirect(`https://github.com/${REPO}`, 302);
      }
      return new Response("Not found", { status: 404 });
    } catch {
      return new Response("Server error", { status: 500 });
    }
  },
};

// ------------------------------------------------------------------ ping

async function ping(request, env) {
  if (Number(request.headers.get("content-length") || 0) > 1024) return badRequest();
  let body;
  try {
    body = await request.json();
  } catch {
    return badRequest();
  }
  const id = String(body?.id ?? "");
  if (!/^[0-9a-f]{32}$/.test(id)) return badRequest();
  const version = /^\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(body.v) ? body.v : null;
  const lang = ["en", "tr"].includes(body.lang) ? body.lang : null;
  const joins = Number.isInteger(body.joins) ? Math.min(Math.max(body.joins, 0), MAX_JOINS) : 0;
  const country = /^[A-Z]{2}$/.test(request.cf?.country ?? "") ? request.cf.country : null;

  const now = nowSec();
  const writes = [
    env.DB.prepare(
      `INSERT INTO installs (id, first_seen, last_seen, version, lang, country)
       VALUES (?1, ?2, ?2, ?3, ?4, ?5)
       ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen, version = excluded.version,
         lang = excluded.lang, country = excluded.country`,
    ).bind(id, now, version, lang, country),
    env.DB.prepare("INSERT OR IGNORE INTO daily (day, id) VALUES (?1, ?2)").bind(isoDay(now), id),
  ];
  if (joins > 0) {
    writes.push(env.DB.prepare(
      `INSERT INTO meta (key, num) VALUES ('joins', ?1)
       ON CONFLICT (key) DO UPDATE SET num = num + ?1`,
    ).bind(joins));
  }
  await env.DB.batch(writes);
  // `joins` = how many were counted, so the app keeps only the rest for later.
  return json({ ok: true, joins, stats: await getStats(env) }, 0);
}

// ------------------------------------------------------------------ stats

async function getStats(env) {
  const now = nowSec();
  const cached = await env.DB.prepare("SELECT text, updated FROM meta WHERE key = 'stats'").first();
  if (cached?.text && now - cached.updated < STATS_TTL) return JSON.parse(cached.text);
  const stats = await computeStats(env, now);
  await env.DB.prepare(
    `INSERT INTO meta (key, text, updated) VALUES ('stats', ?1, ?2)
     ON CONFLICT (key) DO UPDATE SET text = excluded.text, updated = excluded.updated`,
  ).bind(JSON.stringify(stats), now).run();
  return stats;
}

async function computeStats(env, now) {
  const today = isoDay(now);
  const from = isoDay(now - 30 * DAY);
  const [installs, active24h, active7d, joins, daily] = await env.DB.batch([
    env.DB.prepare("SELECT COUNT(*) AS n FROM installs"),
    env.DB.prepare("SELECT COUNT(*) AS n FROM installs WHERE last_seen > ?1").bind(now - DAY),
    env.DB.prepare("SELECT COUNT(*) AS n FROM installs WHERE last_seen > ?1").bind(now - 7 * DAY),
    env.DB.prepare("SELECT num FROM meta WHERE key = 'joins'"),
    env.DB.prepare(
      "SELECT day, COUNT(*) AS n FROM daily WHERE day >= ?1 AND day < ?2 GROUP BY day ORDER BY day",
    ).bind(from, today),
    env.DB.prepare("DELETE FROM daily WHERE day < ?1").bind(isoDay(now - KEEP_DAYS * DAY)),
  ]);
  const stats = {
    downloads: await getDownloads(env, now),
    installs: installs.results[0].n,
    active_24h: active24h.results[0].n,
    active_7d: active7d.results[0].n,
    joins: joins.results[0]?.num ?? 0,
    trend: fillDays(daily.results, today),   // daily users, finished days only (oldest first)
    updated: now,
  };
  // Ready-made texts for the README badges (shields.io shows values as they are).
  stats.text = {
    downloads: stats.downloads == null ? "—" : compact(stats.downloads),
    installs: compact(stats.installs),
    active_24h: compact(stats.active_24h),
    active_7d: compact(stats.active_7d),
    joins: compact(stats.joins),
  };
  return stats;
}

/** Total downloads of all release files on GitHub (the KeydropBot.zip of every release). */
async function getDownloads(env, now) {
  const saved = await env.DB.prepare("SELECT num, updated FROM meta WHERE key = 'downloads'").first();
  if (saved && now - saved.updated < DOWNLOADS_TTL) return saved.num;
  try {
    const r = await fetch(`https://api.github.com/repos/${REPO}/releases?per_page=100`, {
      headers: { "User-Agent": "keydrop-stats", Accept: "application/vnd.github+json" },
    });
    if (!r.ok) throw new Error(`GitHub ${r.status}`);
    const releases = await r.json();
    const total = releases.reduce(
      (sum, rel) => sum + rel.assets.reduce((s, a) => s + (a.download_count || 0), 0), 0);
    await env.DB.prepare(
      `INSERT INTO meta (key, num, updated) VALUES ('downloads', ?1, ?2)
       ON CONFLICT (key) DO UPDATE SET num = excluded.num, updated = excluded.updated`,
    ).bind(total, now).run();
    return total;
  } catch {
    return saved ? saved.num : null;   // GitHub busy: keep the last known number
  }
}

/** [{day, n}] -> counts from the first day with data up to yesterday, gaps as 0. */
function fillDays(rows, today) {
  if (!rows.length) return [];
  const byDay = new Map(rows.map((r) => [r.day, r.n]));
  const out = [];
  for (let d = new Date(`${rows[0].day}T00:00:00Z`); isoDay(d / 1000) < today; d.setUTCDate(d.getUTCDate() + 1)) {
    out.push(byDay.get(isoDay(d / 1000)) ?? 0);
  }
  return out;
}

// ------------------------------------------------------------------ card

const THEMES = {
  dark: { bg: "#0f1115", tile: "#181b22", border: "#2a2f3a", text: "#f2f4f8", muted: "#8b93a7",
          accent: "#c8f25f", live: "#3ddc84" },
  light: { bg: "#ffffff", tile: "#f6f8fa", border: "#d0d7de", text: "#1f2328", muted: "#59636e",
           accent: "#4d7c0f", live: "#1a7f37" },
};

// 20x20 line icons
const ICONS = {
  download: "M10 3.5v9M6 9l4 4 4-4M4 16.5h12",
  installs: "M4.5 3.5h11a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2zM7 17h6M10 13.5V17",
  active: "M2 10.5h3.5l2.2-5.5 4.6 11 2.2-5.5H18",
  gift: "M3.5 9.5h13V17a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1zM2.5 6.5h15v3h-15zM10 6.5V18"
      + "M10 6.5C8.6 3.4 5.4 3.6 6.1 5.6c.4 1 2.2.9 3.9.9zM10 6.5c1.4-3.1 4.6-2.9 3.9-.9-.4 1-2.2.9-3.9.9z",
};

const W = 840;
const H = 250;

function card(stats, theme) {
  const c = THEMES[theme] || THEMES.dark;
  const tiles = [
    ["download", "Downloads", stats.text.downloads],
    ["installs", "Installs", stats.text.installs],
    ["active", "Active this week", stats.text.active_7d],
    ["gift", "Giveaways joined", stats.text.joins],
  ];
  const tileW = (W - 56 - 36) / 4;
  const tilesSvg = tiles.map(([icon, label, value], i) => {
    const x = 28 + i * (tileW + 12);
    const hero = i === tiles.length - 1;
    return `
  <g class="tile" style="animation-delay:${(0.08 * i).toFixed(2)}s">
    <rect x="${x}" y="88" width="${tileW}" height="96" rx="14" fill="${c.tile}"
          stroke="${hero ? c.accent : c.border}" stroke-opacity="${hero ? 0.55 : 1}"/>
    <rect x="${x + 16}" y="119" width="34" height="34" rx="10" fill="${c.accent}" fill-opacity="0.13"/>
    <path transform="translate(${x + 23} 126)" d="${ICONS[icon]}" fill="none" stroke="${c.accent}"
          stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <text x="${x + 62}" y="137" class="value" fill="${c.text}">${value}</text>
    <text x="${x + 62}" y="158" class="label" fill="${c.muted}">${label}</text>
  </g>`;
  }).join("");

  const updated = new Date(stats.updated * 1000).toISOString().slice(11, 16);
  const desc = `${stats.text.downloads} downloads, ${stats.text.installs} installs, `
    + `${stats.text.active_7d} active this week, ${stats.text.joins} giveaways joined.`;

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"
     role="img" aria-labelledby="title desc">
  <title id="title">Keydrop Bot live stats</title>
  <desc id="desc">${desc}</desc>
  <style>
    text { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; }
    .title { font-size: 20px; font-weight: 700; }
    .sub, .label, .foot { font-size: 12.5px; font-weight: 500; }
    .value { font-size: 28px; font-weight: 700; }
    .pill { font-size: 11px; font-weight: 700; letter-spacing: 1.5px; }
    .tile { animation: rise .6s ease-out backwards; }
    .ring { transform-box: fill-box; transform-origin: center; animation: ring 1.8s ease-out infinite; }
    .spark { stroke-dasharray: 1200; animation: draw 1.4s ease-out backwards .3s; }
    @keyframes rise { from { opacity: 0; transform: translateY(6px); } }
    @keyframes ring { from { transform: scale(1); opacity: .7; } to { transform: scale(2.8); opacity: 0; } }
    @keyframes draw { from { stroke-dashoffset: 1200; } to { stroke-dashoffset: 0; } }
    @media (prefers-reduced-motion: reduce) { .tile, .spark { animation: none; } .ring { display: none; } }
  </style>
  <rect x="0.5" y="0.5" width="${W - 1}" height="${H - 1}" rx="18" fill="${c.bg}" stroke="${c.border}"/>

  <g transform="translate(28 24)">
    <rect width="40" height="40" rx="11" fill="#c8f25f"/>
    <circle cx="15.5" cy="11.2" r="3.3" fill="none" stroke="#11150a" stroke-width="2.6"/>
    <circle cx="24.5" cy="11.2" r="3.3" fill="none" stroke="#11150a" stroke-width="2.6"/>
    <rect x="8" y="14.5" width="24" height="6" rx="1.6" fill="#11150a"/>
    <rect x="10" y="21.5" width="20" height="11.5" rx="1.6" fill="#11150a"/>
    <rect x="19" y="14.5" width="2" height="18.5" fill="#c8f25f"/>
  </g>
  <text x="82" y="42" class="title" fill="${c.text}">Keydrop Bot</text>
  <text x="82" y="61" class="sub" fill="${c.muted}">Live community stats</text>

  <rect x="${W - 106}" y="30" width="78" height="28" rx="14" fill="${c.tile}" stroke="${c.border}"/>
  <circle class="ring" cx="${W - 89}" cy="44" r="4" fill="${c.live}"/>
  <circle cx="${W - 89}" cy="44" r="4" fill="${c.live}"/>
  <text x="${W - 78}" y="48" class="pill" fill="${c.text}">LIVE</text>
${tilesSvg}
${trendSvg(stats.trend, c)}
  <text x="${W - 28}" y="222" class="foot" fill="${c.muted}" text-anchor="end">Updated ${updated} UTC</text>
</svg>`;
}

/** "Daily users" sparkline over the finished days (needs at least two of them). */
function trendSvg(trend, c) {
  const label = `<text x="28" y="222" class="foot" fill="${c.muted}">Daily users · last 30 days</text>`;
  if (!trend || trend.length < 2) {
    return `  ${label}
  <text x="214" y="222" class="foot" fill="${c.muted}" fill-opacity="0.75">The trend appears after a couple of days</text>`;
  }
  const x0 = 214, x1 = 600, top = 202, bottom = 230;
  const max = Math.max(...trend, 1);
  const pts = trend.map((n, i) => [
    x0 + (i * (x1 - x0)) / (trend.length - 1),
    bottom - (n / max) * (bottom - top),
  ]);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join("");
  const [lx, ly] = pts[pts.length - 1];
  return `  ${label}
  <defs>
    <linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${c.accent}" stop-opacity="0.28"/>
      <stop offset="1" stop-color="${c.accent}" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <path d="${line}L${x1} ${bottom}L${x0} ${bottom}Z" fill="url(#fade)"/>
  <path class="spark" d="${line}" fill="none" stroke="${c.accent}" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="3.5" fill="${c.accent}" stroke="${c.bg}" stroke-width="2"/>
  <text x="${(lx + 10).toFixed(1)}" y="${(ly + 4.5).toFixed(1)}" class="foot" fill="${c.text}">${compact(trend[trend.length - 1])}</text>`;
}

// ------------------------------------------------------------------ helpers

function compact(n) {
  if (n < 10000) return n.toLocaleString("en-US");
  const short = (x) => (x >= 100 ? String(Math.round(x)) : x.toFixed(1).replace(/\.0$/, ""));
  return n < 1e6 ? `${short(n / 1e3)}K` : `${short(n / 1e6)}M`;
}

const nowSec = () => Math.floor(Date.now() / 1000);
const isoDay = (sec) => new Date(sec * 1000).toISOString().slice(0, 10);

function json(data, maxAge) {
  return new Response(JSON.stringify(data), {
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": maxAge ? `public, max-age=${maxAge}` : "no-store",
    },
  });
}

function svg(body) {
  return new Response(body, {
    headers: { "Content-Type": "image/svg+xml; charset=utf-8", "Cache-Control": "public, max-age=600" },
  });
}

function badRequest() {
  return new Response("Bad request", { status: 400 });
}
