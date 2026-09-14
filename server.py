"""Lightdocs v1 backend — Notes → Word hero loop.

Accepts text and/or images, OCR → AIVM (server-side) → real .docx → short-lived download.
No secrets in responses. Env: AIVM_RELAY, JOB_TTL_SECONDS, CORS_ORIGINS, PORT.
"""
from __future__ import annotations

import io
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from docx import Document
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

APP_NAME = "lightdocs"
VERSION = "0.1.0"

AIVM_RELAY = os.environ.get(
    "AIVM_RELAY", "https://web-production-aaaba.up.railway.app"
).rstrip("/")
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "3600"))  # 1h default
DATA_DIR = Path(os.environ.get("LIGHTDOCS_DATA", "/tmp/lightdocs-jobs"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ORIGINS",
        "https://keiko-dev-lcai.github.io,http://localhost:5500,http://127.0.0.1:5500,http://localhost:8080",
    ).split(",")
    if o.strip()
]

app = Flask(__name__)
CORS(app, origins=CORS_ORIGINS, supports_credentials=False)

_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_META_DIR = DATA_DIR / "meta"
_META_DIR.mkdir(parents=True, exist_ok=True)


def _job_meta_path(job_id: str) -> Path:
    return _META_DIR / f"{job_id}.json"


def _save_job(job_id: str, job: dict[str, Any]) -> None:
    """Persist job meta to disk so Railway restarts don't lose in-flight/done jobs."""
    try:
        import json

        _job_meta_path(job_id).write_text(json.dumps(job), encoding="utf-8")
    except Exception as e:
        print(f"[job] meta save failed: {e}")


def _load_job(job_id: str) -> Optional[dict[str, Any]]:
    import json

    with _jobs_lock:
        if job_id in _jobs:
            return _jobs[job_id]
    p = _job_meta_path(job_id)
    if not p.is_file():
        return None
    try:
        job = json.loads(p.read_text(encoding="utf-8"))
        with _jobs_lock:
            _jobs[job_id] = job
        return job
    except Exception:
        return None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cleanup_expired() -> None:
    now = time.time()
    with _jobs_lock:
        dead = [jid for jid, j in _jobs.items() if now - j.get("created", now) > JOB_TTL_SECONDS]
        for jid in dead:
            path = _jobs[jid].get("docx_path")
            if path and Path(path).is_file():
                try:
                    Path(path).unlink()
                except OSError:
                    pass
            try:
                _job_meta_path(jid).unlink(missing_ok=True)
            except OSError:
                pass
            _jobs.pop(jid, None)
    # also sweep meta dir
    for p in _META_DIR.glob("*.json"):
        try:
            import json

            job = json.loads(p.read_text(encoding="utf-8"))
            if now - float(job.get("created", 0)) > JOB_TTL_SECONDS:
                doc = job.get("docx_path")
                if doc and Path(doc).is_file():
                    Path(doc).unlink(missing_ok=True)
                p.unlink(missing_ok=True)
        except Exception:
            pass


def ocr_image_bytes(data: bytes) -> str:
    """OCR image bytes → text. RapidOCR if available."""
    try:
        from rapidocr_onnxruntime import RapidOCR
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        ocr = RapidOCR()
        result, _ = ocr(buf.getvalue())
        if not result:
            return ""
        lines = [line[1] for line in result if line and len(line) > 1]
        return "\n".join(lines).strip()
    except Exception as e:
        print(f"[ocr] failed: {e}")
        return ""


def aivm_infer(prompt: str, timeout: int = 240) -> str:
    """Server-side AIVM via fleet relay (same pattern as Binai)."""
    start = requests.post(
        f"{AIVM_RELAY}/api/chat",
        json={"message": prompt, "mode": "chat"},
        timeout=30,
    )
    if not start.ok:
        raise RuntimeError(f"AIVM start failed: {start.status_code} {start.text[:200]}")
    data = start.json()
    job_id = data.get("job_id")
    if not job_id:
        return (data.get("reply") or data.get("message") or "").strip()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        poll = requests.get(
            f"{AIVM_RELAY}/api/chat/status",
            params={"job_id": job_id},
            timeout=20,
        )
        if not poll.ok:
            raise RuntimeError(f"AIVM poll failed: {poll.status_code}")
        pd = poll.json()
        if pd.get("status") == "done":
            return (pd.get("reply") or "").strip()
        if pd.get("status") == "error":
            raise RuntimeError(pd.get("error") or "AIVM job failed")
    raise RuntimeError("AIVM timed out — try again")


STYLE_PROMPTS = {
    "formal": "Write in a formal, professional tone.",
    "plain": "Write in clear plain English for everyday readers.",
    "simple": "Write simply (ELI5) — short sentences, no jargon.",
    "nerd": "Write in a detailed, nerdy, precise tone.",
    "tech": "Write as a technical expert — precise terminology OK.",
    "friendly": "Write in a friendly, casual tone.",
}


def build_notes_prompt(raw: str, style: str) -> str:
    voice = STYLE_PROMPTS.get(style, STYLE_PROMPTS["plain"])
    return f"""You are Lightdocs on Lightchain. Turn messy notes into clean meeting-style notes.

{voice}

Rules:
- Output clean Markdown only (no preamble about being an AI).
- Use headings, bullets, and short paragraphs.
- Preserve facts from the input; do not invent names, dates, or numbers.
- If input is sparse, organize what exists and note gaps briefly.
- Title the doc with a short H1.

INPUT NOTES:
{raw[:12000]}
"""


def markdown_to_docx(md: str, path: Path) -> None:
    """Minimal Markdown → docx (headings, bullets, paragraphs)."""
    doc = Document()
    for raw_line in md.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif re.match(r"^[-*]\s+", line):
            doc.add_paragraph(re.sub(r"^[-*]\s+", "", line), style="List Bullet")
        elif re.match(r"^\d+\.\s+", line):
            doc.add_paragraph(re.sub(r"^\d+\.\s+", "", line), style="List Number")
        else:
            # strip simple bold markers
            clean = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
            doc.add_paragraph(clean)
    doc.save(str(path))


def process_job(job_id: str, text: str, images: list[bytes], style: str) -> None:
    def set_step(status: str, step: str, **extra: Any) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id) or {}
            job.update({"status": status, "step": step, **extra})
            _jobs[job_id] = job
            _save_job(job_id, job)

    try:
        set_step("ocr", "Reading images (OCR)…")
        ocr_parts = []
        for i, blob in enumerate(images[:5]):
            t = ocr_image_bytes(blob)
            if t:
                ocr_parts.append(t)
            set_step("ocr", f"OCR image {i + 1}/{min(len(images), 5)}…")

        combined = (text or "").strip()
        if ocr_parts:
            combined = (combined + "\n\n" + "\n\n".join(ocr_parts)).strip()
        if not combined:
            raise RuntimeError("No text found — paste notes or use a clearer photo.")

        set_step("aivm", "AIVM drafting notes (may take 1–2 min)…")
        prompt = build_notes_prompt(combined, style)
        out = aivm_infer(prompt)
        if not out or len(out) < 8:
            raise RuntimeError("AIVM returned empty output — try again.")

        set_step("building", "Building Word file…")
        fname = f"lightdocs-notes-{job_id[:8]}.docx"
        path = DATA_DIR / fname
        markdown_to_docx(out, path)

        set_step(
            "done",
            "Done — download ready. We don’t keep your docs long.",
            text_out=out,
            download_name=fname,
            docx_path=str(path),
            error=None,
        )
    except Exception as e:
        print(f"[job {job_id}] error: {e}")
        set_step("error", "Failed", error=str(e)[:400])


@app.get("/api/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "app": APP_NAME,
            "version": VERSION,
            "aivm_relay_configured": bool(AIVM_RELAY),
            "retention_seconds": JOB_TTL_SECONDS,
            "privacy": "Uploads processed for the job only; files auto-expire. We don’t keep your docs.",
        }
    )


@app.post("/api/jobs")
def create_job():
    """Create notes-to-word job. multipart: text, style, images[]; or JSON."""
    _cleanup_expired()
    style = "plain"
    text = ""
    images: list[bytes] = []

    if request.content_type and "multipart/form-data" in request.content_type:
        text = (request.form.get("text") or "").strip()
        style = (request.form.get("style") or "plain").strip()
        mode = (request.form.get("mode") or "notes-word").strip()
        for key in ("images", "image", "files"):
            for f in request.files.getlist(key):
                if f and f.filename:
                    data = f.read()
                    if data and len(data) < 12_000_000:
                        images.append(data)
    else:
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        style = (body.get("style") or "plain").strip()
        mode = (body.get("mode") or "notes-word").strip()
        for b64 in body.get("images") or []:
            # optional base64 data URLs
            try:
                import base64

                raw = b64.split(",", 1)[-1]
                images.append(base64.b64decode(raw))
            except Exception:
                pass

    if mode not in ("notes-word", "notes-to-word", "meeting"):
        # v1 hero only — accept meeting as alias, ignore others
        mode = "notes-word"

    if not text and not images:
        return jsonify({"error": "Provide text and/or at least one image"}), 400

    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "step": "Queued",
        "created": time.time(),
        "error": None,
        "text_out": None,
        "download_name": None,
        "docx_path": None,
        "mode": mode,
        "style": style,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        _save_job(job_id, job)

    t = threading.Thread(
        target=process_job, args=(job_id, text, images, style), daemon=True
    )
    t.start()
    return jsonify(
        {
            "job_id": job_id,
            "status": "queued",
            "poll_url": f"/api/jobs/{job_id}",
            "retention_seconds": JOB_TTL_SECONDS,
        }
    ), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    _cleanup_expired()
    job = _load_job(job_id)
    if not job:
        return jsonify({"error": "Job not found or expired"}), 404
    out = {
        "job_id": job_id,
        "status": job["status"],
        "step": job["step"],
        "error": job.get("error"),
        "text": job.get("text_out") if job["status"] == "done" else None,
        "download_url": (
            f"/api/jobs/{job_id}/download" if job["status"] == "done" else None
        ),
        "download_name": job.get("download_name"),
        "privacy": "We don’t keep your docs — downloads expire automatically.",
    }
    return jsonify(out)


@app.get("/api/jobs/<job_id>/download")
def download_job(job_id: str):
    _cleanup_expired()
    job = _load_job(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready or expired"}), 404
    path = job.get("docx_path")
    if not path or not Path(path).is_file():
        return jsonify({"error": "File gone — please generate again"}), 410
    return send_file(
        path,
        as_attachment=True,
        download_name=job.get("download_name") or "lightdocs-notes.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8099"))
    app.run(host="0.0.0.0", port=port, debug=False)
