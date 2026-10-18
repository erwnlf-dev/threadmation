#!/usr/bin/env node
// Jembatan threadmation (Python) -> @opencoredev/social-sdk.
//
//   node bridge.mjs platforms            -> {"platforms": ["x", "bluesky", ...]}
//   echo '{...}' | node bridge.mjs publish
//   echo '{"platform": "x", "query": "Claude for Startups", "limit": 50}' | node bridge.mjs search
//
// Input publish (stdin JSON):
//   {"platforms": ["x", "bluesky"], "text": "...", "media": [{"kind": "image", "url": "https://..."}],
//    "link": "https://...", "idempotency_key": "..."}
// Output: satu objek JSON di stdout. Kredensial hanya dibaca dari environment.

import { connectedAccountRef, createSocial } from "@opencoredev/social-sdk";
import { bluesky } from "@opencoredev/social-sdk/bluesky";
import { linkedin } from "@opencoredev/social-sdk/linkedin";
import { x } from "@opencoredev/social-sdk/x";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";

const env = process.env;

async function blueskySession() {
  const service = (env.SOCIAL_BLUESKY_SERVICE || "https://bsky.social").replace(/\/+$/, "");
  const resp = await fetch(`${service}/xrpc/com.atproto.server.createSession`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ identifier: env.SOCIAL_BLUESKY_HANDLE, password: env.SOCIAL_BLUESKY_APP_PASSWORD }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(`Bluesky login gagal (HTTP ${resp.status}): ${data.message || data.error || ""}`);
  const pds = data.didDoc?.service?.find((s) => String(s.id).endsWith("#atproto_pds"))?.serviceEndpoint;
  return { service: pds || service, did: data.did, accessJwt: data.accessJwt, handle: data.handle };
}

// Setiap platform: kapan aktif, cara membuat adapter, dan accountId-nya.
const PLATFORMS = {
  x: {
    enabled: () => Boolean(env.SOCIAL_X_USER_ID && env.SOCIAL_X_ACCESS_TOKEN),
    build: async () => {
      const auth = { userId: env.SOCIAL_X_USER_ID, accessToken: env.SOCIAL_X_ACCESS_TOKEN };
      return { adapter: x({ auth }), platform: "x", accountId: auth.userId };
    },
  },
  bluesky: {
    enabled: () => Boolean(env.SOCIAL_BLUESKY_HANDLE && env.SOCIAL_BLUESKY_APP_PASSWORD),
    build: async () => {
      const auth = await blueskySession();
      return { adapter: bluesky({ auth }), platform: "bluesky", accountId: auth.did };
    },
  },
  linkedin: {
    enabled: () => Boolean(env.SOCIAL_LINKEDIN_ACCESS_TOKEN && env.SOCIAL_LINKEDIN_AUTHOR),
    build: async () => {
      const auth = { accessToken: env.SOCIAL_LINKEDIN_ACCESS_TOKEN, author: env.SOCIAL_LINKEDIN_AUTHOR };
      const apiVersion = env.SOCIAL_LINKEDIN_API_VERSION || "202609";
      return { adapter: linkedin({ auth, apiVersion }), platform: "linkedin", accountId: auth.author };
    },
  },
  // Backend tiruan dari SDK: tanpa kredensial & tanpa network, untuk test / coba-coba.
  mock: {
    enabled: () => ["1", "true", "yes"].includes(String(env.SOCIAL_SDK_MOCK || "").toLowerCase()),
    build: async () => {
      const { mockBackend } = await import("@opencoredev/social-sdk/testing");
      const scenario = env.SOCIAL_SDK_MOCK_SCENARIO || "immediate-text-success";
      return { adapter: mockBackend({ scenario }), platform: "x", accountId: "mock-account-1" };
    },
  },
};

const enabledPlatforms = () => Object.keys(PLATFORMS).filter((name) => PLATFORMS[name].enabled());

function summarize(name, outcome) {
  const out = { platform: name, state: outcome?.state ?? "unknown" };
  if (outcome?.post) out.post_id = outcome.post.postId;
  if (outcome?.url) out.url = outcome.url;
  if (outcome?.code) out.code = outcome.code;
  if (outcome?.message) out.message = outcome.message;
  if (outcome?.reason) out.reason = outcome.reason;
  if (outcome?.issues?.length) out.issues = outcome.issues.map((i) => i.message || i.code || String(i));
  return out;
}

async function publish(input) {
  const requested = [...new Set(input.platforms || [])];
  if (!requested.length) throw new Error("platforms kosong");
  if (!input.text && !(input.media || []).length) throw new Error("text atau media wajib diisi");

  const results = [];
  const backends = {};
  const targets = [];
  for (const name of requested) {
    const def = PLATFORMS[name];
    if (!def) {
      results.push({ platform: name, state: "not-submitted", reason: "unknown-platform" });
      continue;
    }
    if (!def.enabled()) {
      results.push({ platform: name, state: "not-submitted", reason: "not-configured" });
      continue;
    }
    try {
      const built = await def.build();
      backends[name] = built.adapter;
      targets.push({
        name,
        account: connectedAccountRef({ backend: name, platform: built.platform, accountId: built.accountId }),
      });
    } catch (err) {
      results.push({ platform: name, state: "failed", code: "auth", message: String(err.message || err) });
    }
  }

  if (targets.length) {
    const content = {};
    if (input.text) content.text = input.text;
    if (input.link) content.link = { url: input.link };
    if ((input.media || []).length) {
      content.media = input.media.map((m) => ({
        kind: m.kind || "image",
        source: { kind: "https-url", url: m.url },
        ...(m.alt_text ? { altText: m.alt_text } : {}),
      }));
    }
    const social = createSocial({ backends });
    const result = await social.posts.publish({
      targets: targets.map((t) => ({ account: t.account })),
      content,
      ...(input.idempotency_key ? { idempotencyKey: String(input.idempotency_key) } : {}),
    });
    // Outcome diurutkan sesuai targets.
    targets.forEach((t, i) => results.push(summarize(t.name, result.outcomes[i])));
  }

  const ok = results.filter((r) => r.state === "published" || r.state === "accepted" || r.state === "processing");
  const status = ok.length === results.length ? "complete" : ok.length ? "partial" : "failed";
  return { status, results };
}

// Cari post X (7 hari terakhir) beserta profil penulisnya, dalam format post threadmation.
// Cukup SOCIAL_X_BEARER_TOKEN (app-only) atau user token SOCIAL_X_ACCESS_TOKEN.
export async function search(input, fetchImpl = undefined) {
  if (input.platform !== "x") throw new Error(`search belum didukung untuk platform: ${input.platform}`);
  if (!input.query) throw new Error("query wajib diisi");
  const accessToken = env.SOCIAL_X_ACCESS_TOKEN || undefined;
  const appBearerToken = env.SOCIAL_X_BEARER_TOKEN || undefined;
  if (!accessToken && !appBearerToken) throw new Error("X belum dikonfigurasi: isi SOCIAL_X_BEARER_TOKEN");
  const userId = env.SOCIAL_X_USER_ID || "app";
  const adapter = x({ auth: { userId, accessToken }, appBearerToken, ...(fetchImpl ? { fetch: fetchImpl } : {}) });
  const account = connectedAccountRef({ backend: "x", platform: "x", accountId: userId });

  const limit = Math.max(1, Number(input.limit) || 25);
  const items = [];
  let cursor = input.cursor;
  do {
    const context = { backendInstance: "x", correlationId: randomUUID(), retryBudget: { maxAttempts: 1, maxElapsedMs: 30_000 } };
    const page = await adapter.native.searchRecentPosts({
      account,
      context,
      search: {
        query: input.query,
        limit: Math.min(100, Math.max(10, limit - items.length)),
        ...(cursor ? { cursor } : {}),
        sortOrder: input.sort || "recency",
        expansions: ["author_id"],
        userFields: ["username", "name", "description", "public_metrics", "created_at"],
      },
    });
    const users = new Map((page.metadata?.includes?.users || []).map((u) => [u.id, u]));
    for (const tweet of page.items) {
      const user = users.get(tweet.author_id) || {};
      const metrics = user.public_metrics || {};
      items.push({
        id: `x_${tweet.id}`,
        platform: "x",
        text: tweet.text,
        username: user.username,
        permalink: `https://x.com/${user.username || "i"}/status/${tweet.id}`,
        timestamp: tweet.created_at,
        lang: tweet.lang,
        author: {
          id: tweet.author_id,
          name: user.name,
          bio: user.description,
          followers: metrics.followers_count,
          following: metrics.following_count,
          posts: metrics.tweet_count,
          created_at: user.created_at,
        },
      });
      if (items.length >= limit) break;
    }
    cursor = page.nextCursor;
  } while (cursor && items.length < limit);
  return { items, next_cursor: cursor || null };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  return raw ? JSON.parse(raw) : {};
}

async function main() {
  const action = process.argv[2];
  if (action === "platforms") return { platforms: enabledPlatforms(), supported: Object.keys(PLATFORMS) };
  if (action === "publish") return publish(await readStdin());
  if (action === "search") return search(await readStdin());
  throw new Error(`aksi tidak dikenal: ${action ?? "(kosong)"} (pakai: platforms | publish | search)`);
}

// Hanya jalan sebagai CLI; saat di-import (test) cukup ekspor fungsi.
if (import.meta.url === pathToFileURL(process.argv[1] || "").href) main().then(
  (out) => process.stdout.write(`${JSON.stringify(out)}\n`),
  (err) => {
    process.stdout.write(`${JSON.stringify({ error: String(err?.message || err) })}\n`);
    process.exitCode = 1;
  },
);
