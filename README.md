# send

A minimal self-hosted ephemeral file relay. Drop a file, get a link that
self-destructs after one download or one timer — whichever comes first.

Part of the same family as [grab](../grab): same terminal-console design
language, different signature colour (cyan / "secure relay").

## Features

- Drag-and-drop upload → instant share link
- **Burn after read** (default 1 download) and/or **time-to-live** (10m / 1h / 1d / 7d)
- Optional password protection
- Live countdown + "burn now" on the sender's ticket
- A dedicated recipient page that collapses to `// channel closed` once the drop is gone
- Files always served as attachments (uploaded HTML/SVG can never render inline)

## Stack

- **Backend**: Python / Flask
- **Metadata**: SQLite (survives restarts)
- **Blobs**: flat files under a Docker volume
- **Container**: Docker — no media dependencies, the lightest box in the family

## How it works

1. Sender uploads → server stores the blob, writes a row, returns a short token URL (`/d/<token>`).
2. Recipient opens the link → sees filename, size, countdown, and a download button.
3. Each download atomically claims one read. When `reads_left` hits zero, the bytes
   are read into memory, served once, and the blob + row are deleted immediately.
4. A background reaper sweeps anything expired or exhausted every 60s.

## Setup

### Running

```bash
git clone https://github.com/elaw142/send.git
cd send
# Optional: copy .env.example to .env and tune the limits
docker compose up -d --build
```

The app runs on port `5010` by default. Uploaded files live in the `send_data`
Docker volume.

### Caddy (reverse proxy)

```
send.yourdomain.com {
    reverse_proxy send:5010
}
```

### Local (no Docker)

```bash
pip install -r requirements.txt
python app.py
```

## Configuration

| Variable             | Default | Meaning                                              |
| -------------------- | ------- | ---------------------------------------------------- |
| `SEND_MAX_FILE_MB`   | `4096`  | Max size of a single upload (4GB)                    |
| `SEND_MAX_TOTAL_MB`  | `20480` | Max total bytes on disk across all live drops (20GB) |
| `SEND_MAX_READS`     | `20`    | Hard ceiling on the `max_reads` a sender can request |
| `SEND_DATA_DIR`      | `/data` | Where the SQLite DB and blobs live                   |

Keep `SEND_MAX_TOTAL_MB` comfortably below your server's free disk (`df -h`) —
it's the cap that stops uploads from filling the drive. Files are streamed both
in and out, so neither the server nor the recipient's browser ever holds a whole
file in memory.

### Large transfers (e.g. a Minecraft world)

For big files, prefer **`max_reads` ≥ 3** (or a longer TTL) over burn-after-1.
A burn-after-1 download that drops halfway is gone; a few allowed reads let a
friend retry. Unprotected links stream straight to disk; password-protected
downloads are buffered in the recipient's browser, so leave huge files
unprotected and let the unguessable link be the secret.

## Security notes

- Tokens are high-entropy (`secrets.token_urlsafe`).
- Downloads are always `Content-Disposition: attachment` with a generic MIME type.
- Missing, expired, and burned tokens all return a uniform `404`.
- Passwords are hashed with `werkzeug.security` (never stored in plaintext).

## Roadmap

- **Zero-knowledge mode**: encrypt in the browser with WebCrypto and keep the key
  in the URL `#fragment` so the server only ever stores ciphertext.
- QR code on the sender ticket for phone-to-phone handoff.
- Rate limiting on `/api/upload`.

## Deployment

Pushes to `main` deploy via GitHub Actions: the workflow SSHs into the server,
pulls latest, and rebuilds the container.
