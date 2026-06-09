import os
import re

from flask import Flask, request, jsonify, send_file, render_template, abort

import store

app = Flask(__name__)

MAX_FILE_MB = int(os.environ.get("SEND_MAX_FILE_MB", "4096"))
MAX_TOTAL_MB = int(os.environ.get("SEND_MAX_TOTAL_MB", "20480"))
MAX_READS_CAP = int(os.environ.get("SEND_MAX_READS", "20"))

MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_TOTAL_BYTES = MAX_TOTAL_MB * 1024 * 1024

app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_BYTES

# label -> seconds. The UI only ever sends one of these keys.
EXPIRY_OPTIONS = {
    "10m": 10 * 60,
    "1h": 60 * 60,
    "1d": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
}
DEFAULT_EXPIRY = "1h"

store.init_db()
store.start_reaper()


def safe_filename(name):
    """Keep something human-readable but strip paths and control characters."""
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name or "file"


@app.route("/")
def index():
    return render_template(
        "index.html",
        expiry_options=list(EXPIRY_OPTIONS.keys()),
        default_expiry=DEFAULT_EXPIRY,
        max_reads_cap=MAX_READS_CAP,
        max_file_mb=MAX_FILE_MB,
    )


@app.route("/api/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "no file provided"}), 400

    expiry = request.form.get("expiry", DEFAULT_EXPIRY)
    if expiry not in EXPIRY_OPTIONS:
        return jsonify({"error": "invalid expiry"}), 400

    try:
        max_reads = int(request.form.get("max_reads", "1"))
    except (TypeError, ValueError):
        max_reads = 1
    max_reads = max(1, min(max_reads, MAX_READS_CAP))

    password = (request.form.get("password") or "").strip() or None

    if store.total_bytes() >= MAX_TOTAL_BYTES:
        return jsonify({"error": "storage full, try again later"}), 507

    filename = safe_filename(file.filename)
    mime = file.mimetype or "application/octet-stream"

    try:
        drop = store.create(
            file_storage=file,
            filename=filename,
            mime=mime,
            expiry_seconds=EXPIRY_OPTIONS[expiry],
            max_reads=max_reads,
            password=password,
        )
    except OSError:
        return jsonify({"error": "could not store file — out of disk space"}), 507

    drop["url"] = request.host_url.rstrip("/") + "/d/" + drop["token"]
    return jsonify(drop)


@app.route("/d/<token>")
def drop_page(token):
    return render_template("drop.html", token=token, meta=store.meta(token))


@app.route("/api/meta/<token>")
def get_meta(token):
    m = store.meta(token)
    if m is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(m)


@app.route("/api/file/<token>", methods=["GET", "POST"])
def get_file(token):
    password = (
        request.headers.get("X-Send-Password")
        or request.form.get("password")
        or request.args.get("pw")
    )
    result = store.consume(token, password)

    if result["status"] == "unauthorized":
        return jsonify({"error": "password required or incorrect"}), 401
    if result["status"] != "ok":
        return jsonify({"error": "not found"}), 404

    # Stream from disk (never load into memory) and always serve as an
    # attachment with a generic type, so uploaded HTML/SVG can't render inline
    # on this origin.
    response = send_file(
        result["path"],
        as_attachment=True,
        download_name=result["filename"],
        mimetype="application/octet-stream",
    )
    if result["burn"]:
        # The row is already gone; drop the blob once this response is done.
        response.call_on_close(lambda: store.remove_blob(token))
    return response


@app.route("/api/burn/<token>", methods=["POST"])
def burn(token):
    store.burn(token)
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": f"file exceeds {MAX_FILE_MB}MB limit"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5010, threaded=True, debug=False)
