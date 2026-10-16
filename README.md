# Threadmation

Bot untuk akun **Threads** (Meta) yang bisa:

| Fitur | Cara kerja |
|---|---|
| **Update / posting** | Teks, gambar, video, carousel, topic tag, link attachment, quote post. Bisa dipanggil dari CLI, HTTP API, atau kode Python. |
| **Reply** | Balas post/reply mana pun, plus auto-reply berbasis rules untuk mention dan balasan di post Anda. Reply spam bisa otomatis di-hide. |
| **Crawl leads** | Keyword/tag search post publik + mention + balasan di post Anda, diberi skor dari keyword, lalu disimpan di SQLite (tanpa duplikat). |
| **DM** | ⚠️ Threads API **tidak menyediakan endpoint DM**. Bot membuat **outbox DM** berisi pesan siap kirim + link profil. Kirim manual, ekspor CSV, atau teruskan ke pipeline Anda lewat event webhook. |
| **Integrasi pipeline** | HTTP API (`/v1/posts`, `/v1/replies`, `/v1/leads`, ...) dan event webhook keluar (`lead.created`, `dm.queued`, `post.published`, `reply.published`). |

## 1. Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[server]"          # tanpa [server] kalau tidak butuh HTTP API

cp .env.example .env                 # isi THREADS_ACCESS_TOKEN, dll
cp config/rules.example.yaml config/rules.yaml   # sesuaikan keyword & template

threadmation whoami                  # cek token -> harus muncul username Anda
```

`.env` dan `config/rules.yaml` sudah di-`.gitignore`. **Jangan pernah commit token.**

### Permission Meta App

Aktifkan use case Threads API dan izinkan scope berikut saat generate token:

| Scope | Untuk |
|---|---|
| `threads_basic` | wajib (profil, baca post) |
| `threads_content_publish` | post & reply |
| `threads_read_replies` | baca balasan di post Anda |
| `threads_manage_replies` | hide reply, reply control |
| `threads_manage_mentions` | baca mention |
| `threads_keyword_search` | crawl leads via search |
| `threads_delete` | hapus post |
| `threads_profile_discovery` | (opsional) lihat profil publik akun lain |

> **Penting untuk crawl leads:** sebelum app Anda lolos App Review untuk `threads_keyword_search`,
> keyword search **hanya mengembalikan post milik akun Anda sendiri**. Ajukan Advanced Access agar
> search mencakup post publik semua orang. Batasnya 2.200 query per 24 jam.

### Token

- Long-lived token berlaku 60 hari. Jalankan `threadmation refresh-token` (atau biarkan
  `threadmation run --live` memperpanjang otomatis tiap 7 hari). Token hasil refresh disimpan
  di database SQLite lokal dan dipakai selama token di `.env` tidak Anda ganti.
- Token bisa diperpanjang hanya jika umurnya ≥ 24 jam.

## 2. Pemakaian CLI

```bash
# Posting
threadmation post "Update produk terbaru kami 🚀" --topic-tag cloud
threadmation post "Foto event" --image https://contoh.com/foto.jpg
threadmation post "Album" --carousel https://a.jpg https://b.jpg https://c.jpg
echo "teks panjang dari pipeline" | threadmation post -
threadmation post "draft" --dry-run          # tampilkan saja, tidak dikirim

# Reply & moderasi
threadmation reply <MEDIA_ID> "Terima kasih!"
threadmation replies <MEDIA_ID> --all
threadmation hide <REPLY_ID>
threadmation mentions

# Leads
threadmation search "butuh vendor IT" --type RECENT
threadmation crawl                           # search + mention + reply sesuai rules
threadmation leads --status new
threadmation leads --csv leads.csv
threadmation leads --set-status <LEAD_ID> contacted

# Auto-respond (default DRY-RUN, tambah --live untuk benar-benar kirim)
threadmation respond
threadmation respond --live

# Outbox DM
threadmation outbox list
threadmation outbox export --file dm.csv     # ada kolom profile_url
threadmation outbox sent 3 4 5               # tandai sudah dikirim

# Loop otomatis (crawl + respond tiap 15 menit)
threadmation run --live --interval 900
threadmation quota                           # sisa kuota 250 post / 1000 reply per 24 jam
```

Untuk cron cukup: `*/15 * * * * cd /path/threadmation && .venv/bin/threadmation run --once --live`

## 3. Rules (`config/rules.yaml`)

- `leads.queries` — keyword/tag yang dicari.
- `leads.include` — keyword → bobot skor; post hasil search disimpan jika skor ≥ `min_score`.
- `leads.exclude` — kata yang membuat post dibuang (mis. `loker`, `giveaway`).
- Mention dan balasan di post Anda selalu disimpan sebagai lead (kecuali kena `exclude`).
- `responder.rules` — dicek berurutan, rule pertama yang cocok dipakai. Aksi: `reply` (balasan publik),
  `dm` (masuk outbox), `hide` (sembunyikan reply). Placeholder: `{username} {text} {permalink} {query} {source}`.

Default contoh: lead dari **search tidak dibalas publik**, hanya masuk outbox DM. Membalas orang asing
secara massal berisiko dianggap spam oleh Meta dan bisa membuat akun dibatasi.

Pengaman bawaan: dry-run default, tidak pernah membalas post yang sama dua kali, batas balasan per run,
jeda acak antar balasan, cek kuota reply harian, dan publish tidak di-retry saat error 5xx (supaya tidak double-post).

## 4. Integrasi pipeline

### HTTP API

```bash
threadmation serve --port 8080
```

Semua endpoint `/v1/*` butuh header `X-API-Key: $THREADMATION_API_KEY`.

| Method | Endpoint | Body / query |
|---|---|---|
| POST | `/v1/posts` | `{"text", "image_url", "video_url", "carousel":[{"image_url"}], "topic_tag", "link_attachment", "reply_control", "quote_post_id"}` |
| POST | `/v1/replies` | `{"reply_to_id", "text"}` |
| DELETE | `/v1/posts/{id}` | |
| POST | `/v1/replies/{id}/hide?hide=true` | |
| GET | `/v1/search?q=...&search_type=RECENT` | |
| POST | `/v1/crawl` | `{"search":true, "respond":true, "dry_run":false}` |
| GET | `/v1/leads?status=new&source=search` | |
| PATCH | `/v1/leads/{id}` | `{"status":"contacted"}` |
| GET | `/v1/outbox?status=pending` | |
| PATCH | `/v1/outbox/{id}` | `{"status":"sent"}` |
| GET | `/v1/me`, `/v1/quota`, `/healthz` | |

Contoh dari pipeline lain (mis. setelah produk selesai di-build):

```bash
curl -X POST http://localhost:8080/v1/posts \
  -H "X-API-Key: $THREADMATION_API_KEY" -H "Content-Type: application/json" \
  -d '{"text": "Rilis v2.3 sudah live! Cek changelog: https://contoh.com/changelog", "topic_tag": "release"}'
```

### Dari kode Python

```python
from threadmation.bot import Bot

bot = Bot()                                   # baca .env
bot.post("Laporan mingguan sudah terbit 📊", link_attachment="https://contoh.com/report")
for lead in bot.store.list_leads(status="new"):
    print(lead["username"], lead["text"])
```

### Event webhook keluar

Isi `THREADMATION_EVENT_WEBHOOK_URL` (n8n, Make, Zapier, CRM, atau service Anda). Bot akan POST:

```json
{"event": "dm.queued", "at": "2026-10-16T08:00:00+00:00",
 "data": {"username": "budi", "profile_url": "https://www.threads.com/@budi",
          "message": "Halo budi, ...", "lead_text": "...", "permalink": "..."}}
```

Event: `lead.created`, `dm.queued`, `reply.published`, `post.published`.

### Webhook masuk dari Meta (real-time mention & reply)

1. Jalankan `threadmation serve` di server dengan HTTPS publik.
2. Di Meta App → Webhooks → Threads: callback `https://domain-anda/webhooks/threads`, verify token =
   `THREADS_WEBHOOK_VERIFY_TOKEN`, subscribe field `mentions` dan `replies`.
3. Isi `THREADS_APP_SECRET` agar signature `X-Hub-Signature-256` diverifikasi.
4. `THREADMATION_WEBHOOK_AUTO_REPLY=true` jika ingin auto-reply langsung saat event masuk.

## 5. Development

```bash
pip install -e ".[dev]"
pytest
```

Struktur: `client.py` (wrapper Graph API) · `leads.py` (crawler + scoring) · `responder.py` (auto-reply/DM outbox) ·
`store.py` (SQLite) · `bot.py` (wiring + token + webhook) · `server.py` (FastAPI) · `cli.py`.
