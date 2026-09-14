"""Lightdocs backend — Notes → docs with optional charts + multi-format output.

Flow: accept job → OCR → AIVM → parse optional ```lightdocs JSON → build
docx/md/xlsx/pptx → short-lived download. No secrets in responses.
Env: AIVM_RELAY, JOB_TTL_SECONDS, CORS_ORIGINS, LIGHTDOCS_DATA, PORT.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

APP_NAME = "lightdocs"
VERSION = "0.4.0"

VALID_MODES = frozenset(
    {
        "notes-word",
        "notes-to-word",
        "meeting",
        "resume",
        "invoice",
        "sop",
        "study",
        "sheet",
        "deck",
        "explain",
        "rewrite",
        "condition",
        "dao",
        "litepaper",
        "announce",
    }
)

AIVM_RELAY = os.environ.get(
    "AIVM_RELAY", "https://web-production-aaaba.up.railway.app"
).rstrip("/")
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "3600"))
DATA_DIR = Path(os.environ.get("LIGHTDOCS_DATA", "/tmp/lightdocs-jobs"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
_META_DIR = DATA_DIR / "meta"
_META_DIR.mkdir(parents=True, exist_ok=True)

CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ORIGINS",
        "https://keiko-dev-lcai.github.io,http://localhost:5500,http://127.0.0.1:5500,http://localhost:8080",
    ).split(",")
    if o.strip()
]

BRAND_PURPLE = "#5B4BFF"
BRAND_MAGENTA = "#DD00AC"
BRAND_COLORS = [BRAND_PURPLE, BRAND_MAGENTA, "#7B6CFF", "#EE11FB", "#4A3CE0", "#CCCEEF"]

app = Flask(__name__)
CORS(app, origins=CORS_ORIGINS, supports_credentials=False)

_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _job_meta_path(job_id: str) -> Path:
    return _META_DIR / f"{job_id}.json"


def _save_job(job_id: str, job: dict[str, Any]) -> None:
    try:
        _job_meta_path(job_id).write_text(json.dumps(job), encoding="utf-8")
    except Exception as e:
        print(f"[job] meta save failed: {e}")


def _load_job(job_id: str) -> Optional[dict[str, Any]]:
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


def _cleanup_expired() -> None:
    now = time.time()
    with _jobs_lock:
        dead = [
            jid
            for jid, j in _jobs.items()
            if now - float(j.get("created", now)) > JOB_TTL_SECONDS
        ]
        for jid in dead:
            for key in ("docx_path", "file_path"):
                path = _jobs[jid].get(key)
                if path and Path(path).is_file():
                    try:
                        Path(path).unlink()
                    except OSError:
                        pass
            # chart pngs
            for png in DATA_DIR.glob(f"{jid}-chart-*.png"):
                try:
                    png.unlink()
                except OSError:
                    pass
            try:
                _job_meta_path(jid).unlink(missing_ok=True)
            except OSError:
                pass
            _jobs.pop(jid, None)
    for p in _META_DIR.glob("*.json"):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
            if now - float(job.get("created", 0)) > JOB_TTL_SECONDS:
                for key in ("docx_path", "file_path"):
                    doc = job.get(key)
                    if doc and Path(doc).is_file():
                        Path(doc).unlink(missing_ok=True)
                jid = job.get("id") or p.stem
                for png in DATA_DIR.glob(f"{jid}-chart-*.png"):
                    png.unlink(missing_ok=True)
                p.unlink(missing_ok=True)
        except Exception:
            pass


def ocr_image_bytes(data: bytes) -> str:
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
        return "\n".join(line[1] for line in result if line and len(line) > 1).strip()
    except Exception as e:
        print(f"[ocr] failed: {e}")
        return ""


def aivm_infer(prompt: str, timeout: int = 240) -> str:
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


_TELEMETRY_RE = re.compile(
    r"\{[^{}]*\"(?:promptTokens|evalTokens|tokensPerSecond|totalMs)\"[^{}]*\}"
)


def _strip_aivm_noise(text: str) -> str:
    """Drop AIVM chatter/telemetry that is not document content."""
    t = _TELEMETRY_RE.sub("", text or "")
    # common wrapper lines
    drop_prefixes = (
        "here is the output",
        "here's the output",
        "i hope this helps",
        "let me know if",
        "sure,",
        "of course,",
    )
    lines = []
    for ln in t.splitlines():
        low = ln.strip().lower()
        if any(low.startswith(p) for p in drop_prefixes):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def parse_lightdocs_payload(raw: str) -> tuple[str, dict[str, Any]]:
    """Split AIVM output into markdown + optional ```lightdocs JSON block."""
    meta: dict[str, Any] = {}
    md = raw
    m = re.search(r"```lightdocs\s*([\s\S]*?)```", raw, re.I)
    if m:
        md = (raw[: m.start()] + raw[m.end() :]).strip()
        try:
            meta = json.loads(m.group(1).strip())
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
    # also accept bare JSON object with charts/sheets/slides anywhere
    if not meta:
        m2 = re.search(
            r"(\{\s*\"(?:charts|sheets|slides)\"\s*:\s*\[[\s\S]*?\]\s*\})", raw
        )
        if m2:
            try:
                meta = json.loads(m2.group(1))
                md = (raw[: m2.start()] + raw[m2.end() :]).strip()
            except Exception:
                pass
    md = _strip_aivm_noise(md)
    return md, meta


MODE_TASKS: dict[str, str] = {
    "notes-word": (
        "Turn messy notes into a clean document: short H1 title, headings, bullets, "
        "and short paragraphs. Preserve facts from INPUT only."
    ),
    "notes-to-word": (
        "Turn messy notes into a clean document: short H1 title, headings, bullets, "
        "and short paragraphs. Preserve facts from INPUT only."
    ),
    "meeting": (
        "Turn messy meeting notes into: (1) Meeting title + meta if present, "
        "(2) Concise summary, (3) Decisions, (4) Action items as a checklist with "
        "Owner — Task — Due when present (use [TO FILL] if owner/due missing). "
        "Do not invent attendees or commitments."
    ),
    "resume": (
        "Build a structured resume/CV OR a cover letter from INPUT. "
        "If INPUT looks like a role + notes, prefer a cover letter; otherwise a resume "
        "with Contact, Summary, Experience, Education, Skills. Use [TO FILL] for missing "
        "contact details — never invent employers, dates, or credentials."
    ),
    "invoice": (
        "Draft an invoice or quote: seller/buyer placeholders, invoice/quote number "
        "[TO FILL], date, line items (description, qty, unit price, line total), "
        "subtotal, tax if mentioned, total. Currency from INPUT or mark [TO FILL]. "
        "Do not invent prices."
    ),
    "sop": (
        "Turn rough steps into a clear SOP or checklist: purpose, prerequisites, "
        "numbered procedure steps, and a final checkbox verification list. "
        "Keep steps actionable; mark unknowns as [TO FILL]."
    ),
    "study": (
        "Build a study guide / lesson outline from INPUT: learning objectives, "
        "outline of topics, key terms with short definitions, and review questions. "
        "Stay on the subject in INPUT."
    ),
    "sheet": (
        "Shape INPUT as spreadsheet data: infer sensible columns and rows from the notes. "
        "Prefer real numbers/labels from INPUT; do not invent metrics."
    ),
    "deck": (
        "Turn INPUT into a short presentation outline: 5–10 slides with titles and "
        "tight bullets. One idea per slide; preserve facts from INPUT only."
    ),
    "explain": (
        "Scan & explain: give a plain-language explanation of what the INPUT document "
        "or notes mean. Structure as Overview, Key points, What to do next. "
        "If quoting, keep short and clearly marked. Do not invent content not in INPUT."
    ),
    "rewrite": (
        "Rewrite / simplify / translate the INPUT text in the requested voice. "
        "Preserve meaning and facts; improve clarity. If INPUT asks for a language, "
        "translate into that language; otherwise rewrite in the voice style."
    ),
    "condition": (
        "Create a property condition / move-in report from notes/photo OCR: "
        "Property/unit [TO FILL], date, rooms/areas with condition notes, issues found, "
        "photos referenced by filename if present, and a signature/ack placeholder. "
        "Do not invent damage that is not in INPUT."
    ),
    "dao": (
        "Draft a Lightchain LCAI DUNA governor proposal from rough notes. Match real "
        "Lightchain proposal structure EXACTLY with these sections in order:\n"
        "1) Title — [Verb] [Subject], plain and specific (no hype)\n"
        "2) Summary — 1–2 sentences of what is authorized, including headline numbers\n"
        "3) Key terms — bullets for amounts, rates, counterparties, duration\n"
        "4) Why it matters / rationale — short paragraph\n"
        "5) Scope & limitations — what this does NOT authorize\n"
        "6) What this means / effect if passed — bullets\n"
        "7) Governance / execution footer — voting-window note + placeholders for "
        "on-chain record and dao.lightchain.ai link\n"
        "House style: sober, transparent, DUNA framing (LCAI DUNA; Administrator "
        "Quantum Counsel LLC when relevant). NEVER invent tx hashes, proposal IDs, "
        "treasury balances, or vote results — use [TO FILL]."
    ),
    "litepaper": (
        "Write a Lightchain-oriented litepaper / one-pager from INPUT: Title, Hook, "
        "Problem, Solution, How it works, Token/utility if mentioned, Roadmap, "
        "Call to action. Keep it one-page dense. No invented tokenomics numbers."
    ),
    "announce": (
        "Write a Forum / Discord announcement in Lightchain community style: short, "
        "scannable, with emoji section markers (e.g. 🚀 🚨 🧠 ⏳ 🗳️) where natural, "
        "clear CTA, and link placeholders as [TO FILL]. Do not invent proposal IDs "
        "or tx hashes."
    ),
}


def _format_instructions(output: str, mode: str, want_chart: bool = False) -> str:
    """How the model should shape the reply for the chosen download format."""
    chart_hint = ""
    if want_chart:
        chart_hint = (
            " Include a sibling \"charts\" array when INPUT has real numeric series "
            "(never invent numbers)."
        )
    if output == "xlsx":
        return (
            "Return ONLY a fenced block (nothing else):\n"
            "```lightdocs\n"
            '{ "sheets": [ { "name": "Sheet1", "columns": ["A","B"], '
            '"rows": [["x",1],["y",2]] } ] }\n'
            "```\n"
            + (
                "Also include a \"charts\" array in the same JSON for a native Excel chart "
                "when INPUT has real numbers — never invent values.\n"
                if want_chart
                else "Do not add a charts array unless the user data clearly needs one.\n"
            )
            + "If INPUT is not tabular, one Notes column listing the points."
            + chart_hint
        )
    if output == "pptx":
        return (
            "Return ONLY a fenced block (nothing else):\n"
            "```lightdocs\n"
            '{ "slides": [ { "title": "Overview", "bullets": ["Point A","Point B"], '
            '"notes": "" } ] }\n'
            "```\n"
            + (
                "Also include a \"charts\" array in the same JSON when INPUT has real "
                "numbers for a chart slide — never invent values."
                if want_chart
                else "Do not add a charts array unless clearly needed."
            )
        )
    base = "Output clean Markdown only (no preamble). "
    if mode == "sheet":
        base = "Output clean Markdown with a readable table of the data (no preamble). "
    elif mode == "deck":
        base = "Output clean Markdown: each slide as ## Title plus bullets (no preamble). "
    if want_chart:
        return base + (
            "The user requested a chart — see the chart instruction below."
        )
    return base + "Do not emit a charts block unless the input is clearly numeric series data."



def build_prompt(
    raw: str,
    style: str,
    output: str,
    mode: str = "notes-word",
    extras: Optional[dict[str, Any]] = None,
) -> str:
    extras = extras or {}
    mode = mode if mode in VALID_MODES else "notes-word"
    if mode == "notes-to-word":
        mode = "notes-word"
    voice = STYLE_PROMPTS.get(style, STYLE_PROMPTS["plain"])
    lightchain_ok = mode in ("dao", "litepaper", "announce")
    ground = (
        "CRITICAL: Use ONLY the INPUT below. Do not invent facts. "
        "No preamble, no apologies, no 'here is the output', no token stats. "
    )
    if lightchain_ok:
        ground += "Lightchain/DAO framing is appropriate for this mode. "
    else:
        ground += (
            "Do not write about Lightchain SDKs, APIs, or unrelated platform topics. "
        )

    task = MODE_TASKS.get(mode, MODE_TASKS["notes-word"])
    extra_bits = []
    if mode == "dao":
        ptype = str(extras.get("prop_type") or "general")
        extra_bits.append(f"Proposal type hint: {ptype}.")
        if extras.get("forum_wrap"):
            extra_bits.append(
                "AFTER the seven proposal sections, also append a Forum/Discord "
                "announcement wrapper of the same content with emoji section headers "
                "and a Review-and-vote CTA + [TO FILL] link (default wrapper is OFF "
                "unless requested — it is requested now)."
            )
        else:
            extra_bits.append(
                "Do NOT wrap as a forum announcement; emit the proposal body only."
            )

    want_chart = bool(extras.get("want_chart"))
    chart_type = str(extras.get("chart_type") or "auto").lower()
    if chart_type not in ("auto", "bar", "line", "pie"):
        chart_type = "auto"
    if want_chart:
        type_line = (
            "Pick the best of bar, line, or pie for the data."
            if chart_type == "auto"
            else f'Use chart type "{chart_type}" only.'
        )
        extra_bits.append(
            "CHART REQUESTED BY USER: Extract a numeric series from INPUT (labels + values) "
            "and emit a ```lightdocs chart spec. "
            + type_line
            + " Example:\n"
            "```lightdocs\n"
            '{"charts":[{"type":"bar","title":"...","labels":["A","B"],"values":[1,2],'
            '"x_label":"","y_label":""}]}\n'
            "```\n"
            "If INPUT has no clear chartable numbers, omit the charts array entirely — "
            "NEVER invent numbers to satisfy this request. Still produce the normal document."
        )

    fmt = _format_instructions(output, mode, want_chart=want_chart)
    extra = ("\n".join(extra_bits) + "\n") if extra_bits else ""

    return f"""You are Lightdocs, a document formatter.

Mode: {mode}
{voice}
{ground}

Task:
{task}
{extra}
Output shaping:
{fmt}

INPUT:
{raw[:12000]}
"""


def render_chart_png(spec: dict[str, Any], out_path: Path) -> bool:
    """Render one chart spec with matplotlib (Agg). Return True on success."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        ctype = (spec.get("type") or "bar").lower()
        labels = list(spec.get("labels") or [])
        values = [float(v) for v in (spec.get("values") or [])]
        if not labels or not values or len(labels) != len(values):
            return False
        title = str(spec.get("title") or "Chart")
        fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=140)
        fig.patch.set_facecolor("#ffffff")
        ax.set_facecolor("#f7f7fb")
        colors = (BRAND_COLORS * ((len(values) // len(BRAND_COLORS)) + 1))[: len(values)]
        xs = list(range(len(labels)))
        if ctype == "line":
            ax.plot(xs, values, color=BRAND_PURPLE, marker="o", linewidth=2.2)
            ax.fill_between(xs, values, alpha=0.12, color=BRAND_PURPLE)
            ax.set_xticks(xs)
            ax.set_xticklabels(labels, rotation=20, ha="right")
        elif ctype == "pie":
            ax.pie(values, labels=labels, colors=colors, autopct="%1.0f%%", startangle=90)
            ax.axis("equal")
        else:
            ax.bar(xs, values, color=colors, edgecolor="white", linewidth=0.6)
            ax.set_xticks(xs)
            ax.set_xticklabels(labels, rotation=20, ha="right")
        if ctype != "pie":
            if spec.get("x_label"):
                ax.set_xlabel(str(spec["x_label"]))
            if spec.get("y_label"):
                ax.set_ylabel(str(spec["y_label"]))
            ax.grid(axis="y", linestyle="--", alpha=0.35)
            for spine in ("top", "right"):
                ax.spines[spine].set_visible(False)
        ax.set_title(title, fontsize=13, fontweight="bold", color="#14152C", pad=12)
        fig.tight_layout()
        fig.savefig(str(out_path), bbox_inches="tight")
        plt.close(fig)
        return out_path.is_file()
    except Exception as e:
        print(f"[chart] render failed: {e}")
        return False


def markdown_to_docx(md: str, path: Path, chart_pngs: list[tuple[str, Path]]) -> None:
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
            clean = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
            doc.add_paragraph(clean)
    for title, png in chart_pngs:
        if not png.is_file():
            continue
        doc.add_heading(title or "Chart", level=2)
        doc.add_picture(str(png), width=Inches(5.8))
        cap = doc.add_paragraph(title or "Chart")
        if cap.runs:
            cap.runs[0].font.size = Pt(10)
            cap.runs[0].font.color.rgb = RGBColor(0x5B, 0x4B, 0xFF)
    doc.save(str(path))


def build_xlsx(meta: dict[str, Any], fallback_md: str, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference

    wb = Workbook()
    sheets = meta.get("sheets") if isinstance(meta.get("sheets"), list) else None
    if not sheets:
        ws = wb.active
        ws.title = "Notes"
        ws.append(["Notes"])
        for line in fallback_md.splitlines():
            if line.strip():
                ws.append([line.strip()])
        ws.column_dimensions["A"].width = 60
    else:
        first = True
        for spec in sheets[:8]:
            name = str(spec.get("name") or "Sheet")[:31] or "Sheet"
            cols = list(spec.get("columns") or [])
            rows = list(spec.get("rows") or [])
            if first:
                ws = wb.active
                ws.title = name
                first = False
            else:
                ws = wb.create_sheet(name)
            if cols:
                ws.append(cols)
                for cell in ws[1]:
                    cell.font = Font(bold=True, color="5B4BFF")
            for row in rows:
                if isinstance(row, (list, tuple)):
                    ws.append(list(row))
                else:
                    ws.append([row])
            for i, _ in enumerate(cols or [0], start=1):
                ws.column_dimensions[chr(64 + min(i, 26))].width = 16
        # optional native charts on first sheet if chart specs exist
        charts = meta.get("charts") if isinstance(meta.get("charts"), list) else []
        if charts and sheets:
            ws = wb[wb.sheetnames[0]]
            # simple bar from first chart if values align with rows
            try:
                c0 = charts[0]
                labels = list(c0.get("labels") or [])
                values = list(c0.get("values") or [])
                if labels and values and len(labels) == len(values):
                    tmp = wb.create_sheet("_chart_data")
                    tmp.append(["Label", "Value"])
                    for lab, val in zip(labels, values):
                        tmp.append([lab, float(val)])
                    ctype = (c0.get("type") or "bar").lower()
                    chart = (
                        PieChart()
                        if ctype == "pie"
                        else LineChart()
                        if ctype == "line"
                        else BarChart()
                    )
                    chart.title = str(c0.get("title") or "Chart")
                    data = Reference(tmp, min_col=2, min_row=1, max_row=1 + len(values))
                    cats = Reference(tmp, min_col=1, min_row=2, max_row=1 + len(values))
                    chart.add_data(data, titles_from_data=True)
                    chart.set_categories(cats)
                    ws.add_chart(chart, "E2")
            except Exception as e:
                print(f"[xlsx chart] skip: {e}")
    wb.save(str(path))


def build_pptx(meta: dict[str, Any], fallback_md: str, path: Path) -> None:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor as PptRGB
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches as PptInches, Pt as PptPt

    prs = Presentation()
    slides = meta.get("slides") if isinstance(meta.get("slides"), list) else None
    if not slides:
        # fallback: split markdown headings into slides
        chunks = re.split(r"\n(?=# )", fallback_md.strip()) or [fallback_md]
        slides = []
        for ch in chunks[:12]:
            lines = [ln.strip() for ln in ch.splitlines() if ln.strip()]
            title = lines[0].lstrip("# ").strip() if lines else "Notes"
            bullets = [re.sub(r"^[-*#\d.\s]+", "", ln) for ln in lines[1:8]]
            slides.append({"title": title, "bullets": [b for b in bullets if b], "notes": ""})
    for spec in slides[:16]:
        layout = prs.slide_layouts[1]  # title + content
        slide = prs.slides.add_slide(layout)
        title = str(spec.get("title") or "Slide")
        slide.shapes.title.text = title
        body = slide.placeholders[1].text_frame
        body.clear()
        bullets = list(spec.get("bullets") or [])
        if not bullets:
            p = body.paragraphs[0]
            p.text = ""
        else:
            for i, b in enumerate(bullets[:10]):
                p = body.paragraphs[0] if i == 0 else body.add_paragraph()
                p.text = str(b)
                p.level = 0
                p.font.size = PptPt(20)
                p.font.color.rgb = PptRGB(0x14, 0x15, 0x2C)
        notes = str(spec.get("notes") or "").strip()
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

    # optional native chart slide when AIVM returned a chart spec
    charts = meta.get("charts") if isinstance(meta.get("charts"), list) else []
    for c0 in charts[:2]:
        if not isinstance(c0, dict):
            continue
        labels = list(c0.get("labels") or [])
        values = list(c0.get("values") or [])
        if not labels or not values or len(labels) != len(values):
            continue
        try:
            nums = [float(v) for v in values]
            ctype = (c0.get("type") or "bar").lower()
            chart_data = CategoryChartData()
            chart_data.categories = [str(x) for x in labels]
            chart_data.add_series(str(c0.get("y_label") or "Value"), nums)
            xl_type = (
                XL_CHART_TYPE.PIE
                if ctype == "pie"
                else XL_CHART_TYPE.LINE_MARKERS
                if ctype == "line"
                else XL_CHART_TYPE.COLUMN_CLUSTERED
            )
            blank = prs.slide_layouts[5]  # title only
            slide = prs.slides.add_slide(blank)
            slide.shapes.title.text = str(c0.get("title") or "Chart")
            slide.shapes.add_chart(
                xl_type, PptInches(1.0), PptInches(1.6), PptInches(8.0), PptInches(4.8), chart_data
            )
        except Exception as e:
            print(f"[pptx chart] skip: {e}")
    prs.save(str(path))


def process_job(
    job_id: str,
    text: str,
    images: list[bytes],
    style: str,
    output: str,
    mode: str = "notes-word",
    extras: Optional[dict[str, Any]] = None,
) -> None:
    def set_step(status: str, step: str, **extra: Any) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id) or {}
            job.update({"status": status, "step": step, **extra})
            _jobs[job_id] = job
            _save_job(job_id, job)

    extras = extras or {}
    chart_pngs: list[tuple[str, Path]] = []
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

        set_step("aivm", f"AIVM drafting ({mode})…")
        raw_out = aivm_infer(build_prompt(combined, style, output, mode, extras))
        if not raw_out or len(raw_out) < 4:
            raise RuntimeError("AIVM returned empty output — try again.")
        md, meta = parse_lightdocs_payload(raw_out)
        if not md.strip() and output in ("docx", "md"):
            md = _strip_aivm_noise(raw_out)

        # mode defaults: sheet→xlsx builder path if sheets present even when md empty
        if mode == "sheet" and output not in ("xlsx", "pptx") and meta.get("sheets"):
            # keep user's output; markdown_to_docx/md will use md or a simple dump
            if not md.strip():
                md = json.dumps(meta.get("sheets"), indent=2)
        if mode == "deck" and output not in ("pptx",) and meta.get("slides") and not md.strip():
            md = json.dumps(meta.get("slides"), indent=2)

        set_step("building", f"Building {output} file…")
        # charts for docx
        charts = meta.get("charts") if isinstance(meta.get("charts"), list) else []
        if output == "docx" and charts:
            for i, spec in enumerate(charts[:4]):
                if not isinstance(spec, dict):
                    continue
                png = DATA_DIR / f"{job_id}-chart-{i}.png"
                if render_chart_png(spec, png):
                    chart_pngs.append((str(spec.get("title") or f"Chart {i+1}"), png))

        chart_note = ""
        if extras.get("want_chart"):
            has_chart = bool(chart_pngs) or bool(charts)
            if not has_chart:
                chart_note = "No clear numbers to chart — added the text only."
                if md.strip():
                    md = md.strip() + "\n\n_" + chart_note + "_\n"
                else:
                    md = "_" + chart_note + "_\n"

        if output == "md":
            fname = f"lightdocs-{job_id[:8]}.md"
            path = DATA_DIR / fname
            path.write_text(md.strip() + "\n", encoding="utf-8")
            mime = "text/markdown; charset=utf-8"
            text_out = md.strip()
        elif output == "xlsx":
            fname = f"lightdocs-{job_id[:8]}.xlsx"
            path = DATA_DIR / fname
            build_xlsx(meta, md, path)
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            text_out = (
                json.dumps(meta.get("sheets") or [], indent=2)
                if meta.get("sheets")
                else md.strip()
            )
            if chart_note:
                text_out = (text_out + "\n\n" + chart_note).strip()
        elif output == "pptx":
            fname = f"lightdocs-{job_id[:8]}.pptx"
            path = DATA_DIR / fname
            build_pptx(meta, md, path)
            mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            text_out = (
                json.dumps(meta.get("slides") or [], indent=2)
                if meta.get("slides")
                else md.strip()
            )
            if chart_note:
                text_out = (text_out + "\n\n" + chart_note).strip()
        else:
            output = "docx"
            fname = f"lightdocs-{job_id[:8]}.docx"
            path = DATA_DIR / fname
            markdown_to_docx(md, path, chart_pngs)
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            text_out = md.strip()

        set_step(
            "done",
            "Done — download ready. We don’t keep your docs long.",
            text_out=text_out,
            download_name=fname,
            file_path=str(path),
            docx_path=str(path),  # backward compat
            mime=mime,
            output=output,
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
            "formats": ["docx", "md", "xlsx", "pptx"],
            "modes": sorted(m for m in VALID_MODES if m != "notes-to-word"),
            "privacy": "Uploads processed for the job only; files auto-expire. We don’t keep your docs. Drafts stay on your device only.",
        }
    )


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


@app.post("/api/jobs")
def create_job():
    _cleanup_expired()
    style = "plain"
    text = ""
    output = "docx"
    mode = "notes-word"
    prop_type = "general"
    forum_wrap = False
    want_chart = False
    chart_type = "auto"
    images: list[bytes] = []

    if request.content_type and "multipart/form-data" in request.content_type:
        text = (request.form.get("text") or "").strip()
        style = (request.form.get("style") or "plain").strip()
        output = (request.form.get("output") or request.form.get("outfmt") or "docx").strip()
        mode = (request.form.get("mode") or "notes-word").strip()
        prop_type = (request.form.get("prop_type") or request.form.get("propType") or "general").strip()
        forum_wrap = _truthy(request.form.get("forum_wrap") or request.form.get("forumWrap"))
        want_chart = _truthy(request.form.get("want_chart") or request.form.get("wantChart"))
        chart_type = (request.form.get("chart_type") or request.form.get("chartType") or "auto").strip()
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
        output = (body.get("output") or body.get("outfmt") or "docx").strip()
        mode = (body.get("mode") or "notes-word").strip()
        prop_type = str(body.get("prop_type") or body.get("propType") or "general").strip()
        forum_wrap = _truthy(body.get("forum_wrap") or body.get("forumWrap"))
        want_chart = _truthy(body.get("want_chart") or body.get("wantChart"))
        chart_type = str(body.get("chart_type") or body.get("chartType") or "auto").strip()
        for b64 in body.get("images") or []:
            try:
                import base64

                raw = b64.split(",", 1)[-1]
                images.append(base64.b64decode(raw))
            except Exception:
                pass

    if output not in ("docx", "md", "xlsx", "pptx"):
        output = "docx"
    if mode not in VALID_MODES:
        mode = "notes-word"
    if mode == "notes-to-word":
        mode = "notes-word"
    if prop_type not in ("general", "treasury", "param", "signal", "grant"):
        prop_type = "general"
    chart_type = chart_type.lower()
    if chart_type not in ("auto", "bar", "line", "pie"):
        chart_type = "auto"

    if not text and not images:
        return jsonify({"error": "Provide text and/or at least one image"}), 400

    extras = {
        "prop_type": prop_type,
        "forum_wrap": forum_wrap,
        "want_chart": want_chart,
        "chart_type": chart_type,
    }
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "step": "Queued",
        "created": time.time(),
        "error": None,
        "text_out": None,
        "download_name": None,
        "file_path": None,
        "docx_path": None,
        "mime": None,
        "mode": mode,
        "style": style,
        "output": output,
        "extras": extras,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        _save_job(job_id, job)

    threading.Thread(
        target=process_job,
        args=(job_id, text, images, style, output, mode, extras),
        daemon=True,
    ).start()
    return jsonify(
        {
            "job_id": job_id,
            "status": "queued",
            "poll_url": f"/api/jobs/{job_id}",
            "retention_seconds": JOB_TTL_SECONDS,
            "output": output,
            "mode": mode,
        }
    ), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    _cleanup_expired()
    job = _load_job(job_id)
    if not job:
        return jsonify({"error": "Job not found or expired"}), 404
    return jsonify(
        {
            "job_id": job_id,
            "status": job["status"],
            "step": job["step"],
            "error": job.get("error"),
            "text": job.get("text_out") if job["status"] == "done" else None,
            "download_url": (
                f"/api/jobs/{job_id}/download" if job["status"] == "done" else None
            ),
            "download_name": job.get("download_name"),
            "output": job.get("output"),
            "mode": job.get("mode"),
            "privacy": "We don’t keep your docs — downloads expire automatically. Drafts stay on your device only.",
        }
    )


@app.get("/api/jobs/<job_id>/download")
def download_job(job_id: str):
    _cleanup_expired()
    job = _load_job(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready or expired"}), 404
    path = job.get("file_path") or job.get("docx_path")
    if not path or not Path(path).is_file():
        return jsonify({"error": "File gone — please generate again"}), 410
    return send_file(
        path,
        as_attachment=True,
        download_name=job.get("download_name") or "lightdocs-out.bin",
        mimetype=job.get("mime") or "application/octet-stream",
    )


def _decode_image_src(src: str) -> Optional[bytes]:
    if not src or not isinstance(src, str):
        return None
    try:
        if src.startswith("data:"):
            import base64

            raw = src.split(",", 1)[-1]
            return base64.b64decode(raw)
        if src.startswith("http://") or src.startswith("https://"):
            r = requests.get(src, timeout=30)
            if r.ok:
                return r.content
        # relative API path
        if src.startswith("/"):
            # not fetchable from here without host — skip
            return None
    except Exception as e:
        print(f"[export] image decode failed: {e}")
    return None


def _add_picture_wrapped(doc: Document, img_bytes: bytes, width_in: float, wrap: str) -> None:
    """Embed PNG/JPEG with approximate wrap: full/inline block, left/right via 2-col table."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches as DocInches

    width_in = max(0.8, min(float(width_in or 4.5), 6.5))
    wrap = (wrap or "full").lower()
    bio = io.BytesIO(img_bytes)
    if wrap in ("left", "right"):
        table = doc.add_table(rows=1, cols=2)
        table.autofit = True
        cell_img = table.rows[0].cells[0 if wrap == "left" else 1]
        cell_txt = table.rows[0].cells[1 if wrap == "left" else 0]
        p = cell_img.paragraphs[0]
        run = p.add_run()
        bio.seek(0)
        run.add_picture(bio, width=DocInches(width_in))
        cell_txt.paragraphs[0].add_run("")  # placeholder for surrounding text flow
        return
    p = doc.add_paragraph()
    if wrap == "full":
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    bio.seek(0)
    run.add_picture(bio, width=DocInches(width_in))


def build_docx_from_blocks(blocks: list[Any], path: Path) -> None:
    """Build a .docx from the editor document model (text + positioned images)."""
    doc = Document()
    for block in blocks[:400]:
        if not isinstance(block, dict):
            continue
        btype = (block.get("type") or "paragraph").lower()
        if btype == "heading":
            level = int(block.get("level") or 1)
            level = 1 if level < 1 else 3 if level > 3 else level
            doc.add_heading(str(block.get("text") or "").strip() or " ", level=level)
        elif btype in ("bullet", "list_item", "list"):
            doc.add_paragraph(str(block.get("text") or "").strip(), style="List Bullet")
        elif btype in ("number", "ordered"):
            doc.add_paragraph(str(block.get("text") or "").strip(), style="List Number")
        elif btype in ("image", "chart"):
            src = block.get("src") or block.get("dataUrl") or ""
            blob = _decode_image_src(str(src))
            if not blob:
                continue
            width_in = float(block.get("width_in") or block.get("widthIn") or 4.5)
            wrap = str(block.get("wrap") or "full")
            try:
                _add_picture_wrapped(doc, blob, width_in, wrap)
            except Exception as e:
                print(f"[export] picture skip: {e}")
                cap = doc.add_paragraph(str(block.get("alt") or "[image]"))
                if cap.runs:
                    cap.runs[0].font.size = Pt(10)
        else:
            text = str(block.get("text") or "")
            if text.strip():
                doc.add_paragraph(text)
            elif btype == "paragraph":
                doc.add_paragraph("")
    doc.save(str(path))


@app.post("/api/render-chart")
def render_chart_api():
    """Render a chart spec to PNG (matplotlib). Editor positions it; does not draw charts."""
    body = request.get_json(silent=True) or {}
    spec = body.get("chart") if isinstance(body.get("chart"), dict) else body
    if not isinstance(spec, dict):
        return jsonify({"error": "Provide a chart spec object"}), 400
    job_id = uuid.uuid4().hex[:12]
    out = DATA_DIR / f"chart-render-{job_id}.png"
    if not render_chart_png(spec, out):
        return jsonify({"error": "Could not render chart — check labels/values"}), 400
    return send_file(
        out,
        mimetype="image/png",
        as_attachment=False,
        download_name=f"chart-{job_id}.png",
    )


@app.post("/api/export/docx")
def export_docx():
    """Rebuild a .docx from the edited document model (client-side editor source of truth)."""
    body = request.get_json(silent=True) or {}
    blocks = body.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return jsonify({"error": "Provide blocks[] document model"}), 400
    fname = f"lightdocs-edit-{uuid.uuid4().hex[:8]}.docx"
    path = DATA_DIR / fname
    try:
        build_docx_from_blocks(blocks, path)
    except Exception as e:
        print(f"[export] failed: {e}")
        return jsonify({"error": f"Export failed: {e}"}), 500
    # short-lived: register a mini job meta so cleanup can find it
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "done",
        "step": "Edited export ready",
        "created": time.time(),
        "file_path": str(path),
        "docx_path": str(path),
        "download_name": fname,
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "output": "docx",
        "text_out": None,
        "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        _save_job(job_id, job)
    return jsonify(
        {
            "job_id": job_id,
            "download_url": f"/api/jobs/{job_id}/download",
            "download_name": fname,
            "output": "docx",
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8099"))
    app.run(host="0.0.0.0", port=port, debug=False)
