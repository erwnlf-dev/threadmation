"""Command line:  threadmation <perintah> --help"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time

from .client import ThreadsAPIError


def _bot():
    from .bot import Bot

    return Bot()


def _print(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def cmd_whoami(args):
    b = _bot()
    _print(b.client.me())


def cmd_post(args):
    b = _bot()
    carousel = [{"image_url": u} for u in args.carousel] if args.carousel else None
    text = sys.stdin.read().strip() if args.text == "-" else args.text
    opts = {
        "image_url": args.image,
        "video_url": args.video,
        "carousel": carousel,
        "reply_control": args.reply_control,
        "topic_tag": args.topic_tag,
        "link_attachment": args.link,
        "quote_post_id": args.quote,
    }
    opts = {k: v for k, v in opts.items() if v}
    crosspost = [] if args.no_crosspost else args.also
    if args.dry_run:
        _print({"dry_run": True, "text": text, **opts, "crosspost": crosspost or b.settings.crosspost})
        return
    _print(b.post(text=text, crosspost=crosspost, **opts))


def cmd_reply(args):
    b = _bot()
    if args.dry_run:
        _print({"dry_run": True, "reply_to_id": args.media_id, "text": args.text})
        return
    _print(b.reply(args.media_id, args.text, image_url=args.image))


def cmd_delete(args):
    _print(_bot().client.delete(args.media_id))


def cmd_hide(args):
    _print(_bot().client.hide_reply(args.reply_id, not args.unhide))


def cmd_search(args):
    b = _bot()
    rows = b.client.keyword_search(
        args.query, search_type=args.type, search_mode=args.mode, limit=args.limit,
        author_username=args.author,
    )
    _print(rows)


def cmd_replies(args):
    b = _bot()
    _print(b.client.conversation(args.media_id) if args.all else b.client.replies(args.media_id))


def cmd_mentions(args):
    _print(_bot().client.mentions(limit=args.limit))


def cmd_crawl(args):
    b = _bot()
    r = b.crawl(search=not args.no_search, mentions=not args.no_mentions, replies=not args.no_replies)
    print(f"Discan: {r.scanned} post, lead baru: {len(r.new_leads)}")
    for lead in r.new_leads:
        print(f"  [{lead['source']}] @{lead['username']} skor={lead['score']:.1f} {lead['permalink']}")
        print(f"      {(lead['text'] or '')[:140]!r}")
    for err in r.errors:
        print(f"  ERROR {err}", file=sys.stderr)


def _print_respond(r):
    mode = "DRY-RUN (tidak ada yang dikirim)" if r.dry_run else "LIVE"
    print(f"Responder: {mode}, {len(r.actions)} aksi")
    for a in r.actions:
        flag = "OK " if a.ok else "ERR"
        print(f"  {flag} {a.kind:5} @{a.username} ({a.rule}): {a.detail[:120]!r}" + (f" -> {a.error}" if a.error else ""))
    if r.stopped_reason:
        print(f"  Berhenti: {r.stopped_reason}")


def cmd_respond(args):
    _print_respond(_bot().respond(dry_run=not args.live))


def cmd_run(args):
    b = _bot()
    while True:
        try:
            crawl, resp = b.cycle(dry_run=not args.live)
            print(f"[{time.strftime('%H:%M:%S')}] lead baru: {len(crawl.new_leads)}, scan: {crawl.scanned}")
            _print_respond(resp)
        except ThreadsAPIError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
        if args.once:
            return
        time.sleep(args.interval)


def cmd_leads(args):
    b = _bot()
    if args.set_status:
        lead_id, status = args.set_status
        if not b.store.set_lead_status(lead_id, status):
            sys.exit(f"Lead {lead_id} tidak ditemukan")
        print("OK")
        return
    rows = b.store.list_leads(status=args.status, source=args.source, limit=args.limit)
    if args.csv:
        _write_csv(args.csv, rows)
        print(f"{len(rows)} lead diekspor ke {args.csv}")
    else:
        _print(rows)


def cmd_outbox(args):
    b = _bot()
    if args.action == "list":
        _print(b.store.list_outbox(status=args.status or None))
    elif args.action == "export":
        rows = b.store.list_outbox(status=args.status or None)
        for r in rows:
            r["profile_url"] = f"https://www.threads.com/@{r['username']}"
        _write_csv(args.file, rows)
        print(f"{len(rows)} item diekspor ke {args.file}")
    elif args.action in ("sent", "skip"):
        if not args.ids:
            sys.exit("Sebutkan ID outbox, mis: threadmation outbox sent 3 4 5")
        status = "sent" if args.action == "sent" else "skipped"
        for i in args.ids:
            b.store.set_outbox_status(int(i), status)
        print(f"{len(args.ids)} item ditandai {status}")


def cmd_social(args):
    from .social import SocialBridgeError

    b = _bot()
    try:
        if args.action == "platforms":
            _print({"configured": b.social.platforms(), "default_crosspost": b.settings.crosspost})
            return
        if not args.text:
            sys.exit("Isi teks: threadmation social post \"teks\" --to x bluesky")
        text = sys.stdin.read().strip() if args.text == "-" else args.text
        targets = args.to or b.settings.crosspost
        if not targets:
            sys.exit("Sebutkan platform: --to x bluesky linkedin (atau isi THREADMATION_CROSSPOST)")
        media = [{"kind": "image", "url": u} for u in args.image or []]
        if args.video:
            media.append({"kind": "video", "url": args.video})
        if args.dry_run:
            _print({"dry_run": True, "platforms": targets, "text": text, "media": media, "link": args.link})
            return
        result = b.social.publish(targets, text, media=media, link=args.link)
        b.notifier.emit("social.published", {"text": text, **result})
        _print(result)
    except SocialBridgeError as exc:
        sys.exit(f"social-sdk: {exc}")


def cmd_quota(args):
    _print(_bot().client.publishing_limit())


def cmd_refresh_token(args):
    info = _bot().maybe_refresh_token(force=True)
    print("Token diperpanjang dan disimpan di database lokal.")
    _print(info)


def cmd_serve(args):
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)


def _write_csv(path: str, rows: list[dict]) -> None:
    keys = list(rows[0].keys()) if rows else ["id"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in r.items()})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="threadmation", description="Bot Threads: post, reply, crawl leads, outbox DM.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="cek token & profil akun").set_defaults(fn=cmd_whoami)

    s = sub.add_parser("post", help="publish post baru")
    s.add_argument("text", nargs="?", help="isi post (pakai '-' untuk baca dari stdin)")
    s.add_argument("--image", help="URL gambar publik")
    s.add_argument("--video", help="URL video publik")
    s.add_argument("--carousel", nargs="+", metavar="IMAGE_URL", help="2-20 URL gambar")
    s.add_argument("--reply-control", choices=["everyone", "accounts_you_follow", "mentioned_only", "parent_post_author_only", "followers_only"])
    s.add_argument("--topic-tag")
    s.add_argument("--link", help="link attachment (post teks)")
    s.add_argument("--quote", help="media id post yang di-quote")
    s.add_argument("--also", nargs="+", metavar="PLATFORM", help="cross-post via social-sdk (x, bluesky, linkedin)")
    s.add_argument("--no-crosspost", action="store_true", help="abaikan THREADMATION_CROSSPOST untuk post ini")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_post)

    s = sub.add_parser("reply", help="balas sebuah post/reply")
    s.add_argument("media_id")
    s.add_argument("text")
    s.add_argument("--image")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_reply)

    s = sub.add_parser("delete", help="hapus post")
    s.add_argument("media_id")
    s.set_defaults(fn=cmd_delete)

    s = sub.add_parser("hide", help="sembunyikan reply di post Anda")
    s.add_argument("reply_id")
    s.add_argument("--unhide", action="store_true")
    s.set_defaults(fn=cmd_hide)

    s = sub.add_parser("search", help="keyword search post publik")
    s.add_argument("query")
    s.add_argument("--type", default="RECENT", choices=["TOP", "RECENT"])
    s.add_argument("--mode", default="KEYWORD", choices=["KEYWORD", "TAG"])
    s.add_argument("--author", help="filter username penulis")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("replies", help="lihat balasan sebuah post")
    s.add_argument("media_id")
    s.add_argument("--all", action="store_true", help="semua level (conversation)")
    s.set_defaults(fn=cmd_replies)

    s = sub.add_parser("mentions", help="post yang me-mention akun Anda")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(fn=cmd_mentions)

    s = sub.add_parser("crawl", help="crawl leads (search + mention + reply) sesuai rules")
    s.add_argument("--no-search", action="store_true")
    s.add_argument("--no-mentions", action="store_true")
    s.add_argument("--no-replies", action="store_true")
    s.set_defaults(fn=cmd_crawl)

    s = sub.add_parser("respond", help="proses lead baru dengan rules (default dry-run)")
    s.add_argument("--live", action="store_true", help="benar-benar kirim balasan")
    s.set_defaults(fn=cmd_respond)

    s = sub.add_parser("run", help="loop: crawl + respond tiap interval")
    s.add_argument("--interval", type=int, default=900, help="detik (default 900)")
    s.add_argument("--live", action="store_true")
    s.add_argument("--once", action="store_true", help="sekali jalan (untuk cron)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("leads", help="lihat/ekspor/update lead")
    s.add_argument("--status")
    s.add_argument("--source", choices=["search", "mention", "reply"])
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--csv", metavar="FILE")
    s.add_argument("--set-status", nargs=2, metavar=("LEAD_ID", "STATUS"))
    s.set_defaults(fn=cmd_leads)

    s = sub.add_parser("outbox", help="antrean DM (kirim manual / via pipeline)")
    s.add_argument("action", choices=["list", "export", "sent", "skip"])
    s.add_argument("ids", nargs="*")
    s.add_argument("--status", default="pending")
    s.add_argument("--file", default="outbox.csv")
    s.set_defaults(fn=cmd_outbox)

    s = sub.add_parser("social", help="post ke X/Bluesky/LinkedIn via social-sdk (tanpa Threads)")
    s.add_argument("action", choices=["platforms", "post"])
    s.add_argument("text", nargs="?", help="isi post (pakai '-' untuk baca dari stdin)")
    s.add_argument("--to", nargs="+", metavar="PLATFORM")
    s.add_argument("--image", nargs="+", metavar="IMAGE_URL")
    s.add_argument("--video", metavar="VIDEO_URL")
    s.add_argument("--link")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_social)

    sub.add_parser("quota", help="sisa kuota post & reply 24 jam").set_defaults(fn=cmd_quota)
    sub.add_parser("refresh-token", help="perpanjang long-lived token 60 hari").set_defaults(fn=cmd_refresh_token)

    s = sub.add_parser("serve", help="jalankan HTTP API + webhook receiver")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        args.fn(args)
    except ThreadsAPIError as exc:
        sys.exit(f"Threads API error: {exc}")
    except ValueError as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
