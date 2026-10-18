// node --test  (dijalankan dari folder social-bridge)
import assert from "node:assert/strict";
import test from "node:test";

import { search } from "./bridge.mjs";

const page = (ids, next) => ({
  data: ids.map((id) => ({ id, text: `post ${id}`, author_id: "u1", created_at: "2026-10-17T00:00:00Z", edit_history_tweet_ids: [id] })),
  includes: { users: [{ id: "u1", username: "budi_dev", name: "Budi", description: "Founder @acme, building with Claude", public_metrics: { followers_count: 850, following_count: 300, tweet_count: 1200 }, created_at: "2019-01-01T00:00:00Z" }] },
  meta: next ? { next_token: next } : {},
});

test("search X: map post + profil penulis dan ikuti pagination", async () => {
  process.env.SOCIAL_X_BEARER_TOKEN = "app-token";
  delete process.env.SOCIAL_X_ACCESS_TOKEN;
  const calls = [];
  const responses = [page(["1", "2"], "n2"), page(["3"])];
  const fakeFetch = async (url, init) => {
    calls.push({ url: String(url), auth: new Headers(init?.headers).get("authorization") });
    return new Response(JSON.stringify(responses.shift()), { status: 200, headers: { "content-type": "application/json" } });
  };
  const out = await search({ platform: "x", query: "Claude for Startups -is:retweet", limit: 3 }, fakeFetch);
  assert.deepEqual(out.items.map((i) => i.id), ["x_1", "x_2", "x_3"]);
  const first = out.items[0];
  assert.equal(first.username, "budi_dev");
  assert.equal(first.permalink, "https://x.com/budi_dev/status/1");
  assert.equal(first.author.bio, "Founder @acme, building with Claude");
  assert.equal(first.author.followers, 850);
  assert.equal(calls.length, 2);
  assert.match(calls[0].url, /\/2\/tweets\/search\/recent\?/);
  assert.match(calls[0].url, /expansions=author_id/);
  assert.match(calls[1].url, /next_token=n2/);
  assert.equal(calls[0].auth, "Bearer app-token");
});

test("search X tanpa kredensial ditolak", async () => {
  delete process.env.SOCIAL_X_BEARER_TOKEN;
  delete process.env.SOCIAL_X_ACCESS_TOKEN;
  await assert.rejects(search({ platform: "x", query: "claude" }), /SOCIAL_X_BEARER_TOKEN/);
});
