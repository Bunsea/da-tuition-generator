from __future__ import annotations
import streamlit as st
from google import genai
from google.genai import types
import os
import io
import re
import subprocess
import base64
import shutil
import time
import tempfile
import uuid
import logging
import ast
import operator as _op
from urllib.parse import unquote
from PIL import Image
import hashlib
import json
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback for older Python versions
    import pytz

    def get_sydney_time(utc_string):
        if not utc_string:
            return "N/A"
        clean_ts = utc_string.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(clean_ts)
        dt_sydney = dt_utc.astimezone(pytz.timezone("Australia/Sydney"))
        return dt_sydney.strftime("%d %b %Y, %I:%M %p")
else:

    def get_sydney_time(utc_string):
        if not utc_string:
            return "N/A"
        clean_ts = utc_string.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(clean_ts)
        dt_sydney = dt_utc.astimezone(ZoneInfo("Australia/Sydney"))
        return dt_sydney.strftime("%d %b %Y, %I:%M %p")


import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pypdf import PdfReader

# Supabase Cloud
from supabase import create_client, Client

# Word layout overrider
import docx
from docx.shared import Cm

# Ensure Nix-installed binaries (pdflatex, pandoc, etc.) are on PATH
_NIX_PATHS = [
    "/run/current-system/sw/bin",
    "/nix/var/nix/profiles/default/bin",
]
for _p in _NIX_PATHS:
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(_BASE_DIR, "assets", "logo.png")
SAMPLES_DIR = os.path.join(_BASE_DIR, "samples")

# ── LOGGING (so silent failures are at least visible in the server console) ───
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_logger = logging.getLogger("da_tuition")


def _log_error(context: str, exc: Exception) -> None:
    _logger.warning("[%s] %s", context, exc)


# ── SUPABASE CONNECTION ────────────────────────────────────────────────────────
def _get_supabase_creds():
    """Read Supabase URL and key, always preferring the service_role key to bypass RLS."""
    sb_url = os.environ.get("SUPABASE_URL", "")
    # Prefer service_role key from Streamlit secrets (highest priority)
    sb_key = ""
    try:
        if hasattr(st, "secrets"):
            sb_url = st.secrets.get("SUPABASE_URL", sb_url) or sb_url
            sb_key = (
                st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")
                or st.secrets.get("SUPABASE_SERVICE_KEY", "")
                or st.secrets.get("SUPABASE_KEY", "")
            )
    except Exception:
        pass
    # Fall back to environment variables
    if not sb_key:
        sb_key = (
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
            or os.environ.get("SUPABASE_SERVICE_KEY", "")
            or os.environ.get("SUPABASE_KEY", "")
        )
    return sb_url, sb_key


@st.cache_resource
def init_supabase() -> Client | None:
    sb_url, sb_key = _get_supabase_creds()
    if sb_url and sb_key:
        return create_client(sb_url, sb_key)
    return None


supabase_client = init_supabase()



def get_next_set_number(subject, year, diff, clean_topic):
    if not supabase_client:
        return 1
    try:
        found_sets = set()

        # 1. Direct match on clean_topic prefix with year scoping
        query = (
            supabase_client.table("saved_exams")
            .select("topic")
            .eq("subject", subject)
        )
        if year:
            query = query.eq("year_group", year)
        res = query.ilike("topic", f"{clean_topic}%Set%").execute()
        for row in res.data or []:
            t = row.get("topic", "")
            m = re.search(r"\bSet\s*(\d+)\b", t, re.IGNORECASE)
            if m:
                found_sets.add(int(m.group(1)))

        # 2. Match on school/course stem if no direct match found
        if not found_sets:
            words = clean_topic.split()
            if len(words) >= 4:
                prefix = " ".join(words[:4])
                query2 = (
                    supabase_client.table("saved_exams")
                    .select("topic")
                    .eq("subject", subject)
                )
                if year:
                    query2 = query2.eq("year_group", year)
                res2 = query2.ilike("topic", f"{prefix}%Set%").execute()
                for row in res2.data or []:
                    t = row.get("topic", "")
                    m = re.search(r"\bSet\s*(\d+)\b", t, re.IGNORECASE)
                    if m:
                        found_sets.add(int(m.group(1)))

        # Find the lowest available positive integer (re-uses deleted/missing set slots starting at Set 1)
        k = 1
        while k in found_sets:
            k += 1
        return k
    except Exception as e:
        _log_error("get_next_set_number", e)
        return 1


def save_to_supabase(
    clean_topic,
    subject,
    year,
    diff,
    set_num,
    pdf_bytes,
    word_bytes,
    num_mc=0,
    num_easy=0,
    num_med=0,
    num_hard=0,
    num_xh=0,
    existing_id=None,
    cost=None,
    model=None,
    extra_instructions="",
    created_by="",
):
    if not supabase_client:
        return False, "Supabase is not connected. Missing URL or Key."

    dist_parts = []
    if num_mc:
        dist_parts.append(f"{num_mc} MC")
    if num_easy:
        dist_parts.append(f"{num_easy} Easy")
    if num_med:
        dist_parts.append(f"{num_med} Medium")
    if num_hard:
        dist_parts.append(f"{num_hard} Hard")
    if num_xh:
        dist_parts.append(f"{num_xh} Ext. Hard")
    dist_str = f" ({', '.join(dist_parts)})" if dist_parts else ""

    yr_short = year.replace("Year ", "Yr")
    db_topic = f"{clean_topic} ({yr_short}) Set {set_num}{dist_str}"

    level_map = {
        "Advanced": "Adv",
        "Extension": "Ext1",
        "Extension 2": "Ext2",
        "Standard": "Std",
    }

    short_diff = level_map.get(diff, diff) if diff else ""
    lvl_text = f" {short_diff}" if short_diff else ""
    safe_topic = clean_topic.replace("/", "-").replace("\\", "-")
    file_base = f"{safe_topic} Set {set_num}{dist_str} - {yr_short} {subject}{lvl_text}"
    pdf_path = f"{subject}/{year}/{file_base}.pdf"
    docx_path = f"{subject}/{year}/{file_base}.docx"
    instr_path = f"{subject}/{year}/{file_base}_instructions.txt"

    try:
        supabase_client.storage.from_("exam-files").upload(
            pdf_path,
            pdf_bytes,
            {"content-type": "application/pdf", "upsert": "true"},
        )
        base_pdf_url = supabase_client.storage.from_("exam-files").get_public_url(pdf_path)
        pdf_url = f"{base_pdf_url}?t={int(time.time())}"

        docx_url = ""
        if word_bytes:
            supabase_client.storage.from_("exam-files").upload(
                docx_path,
                word_bytes,
                {
                    "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "upsert": "true",
                },
            )
            base_docx_url = supabase_client.storage.from_("exam-files").get_public_url(docx_path)
            docx_url = f"{base_docx_url}?t={int(time.time())}"

        clean_instr = str(extra_instructions).strip() if extra_instructions else ""
        if clean_instr:
            try:
                supabase_client.storage.from_("exam-files").upload(
                    instr_path,
                    clean_instr.encode("utf-8"),
                    {"content-type": "text/plain; charset=utf-8", "upsert": "true"},
                )
            except Exception as e:
                _log_error("save_instructions_storage", e)

        data = {
            "subject": subject,
            "year_group": year,
            "difficulty": diff,
            "topic": db_topic,
            "pdf_url": pdf_url,
            "docx_url": docx_url,
        }

        # Prepare payload with all possible optional columns
        data_to_save = dict(data)
        if cost is not None:
            try:
                data_to_save["cost"] = round(float(cost), 5)
                if model:
                    data_to_save["model"] = str(model)
            except (ValueError, TypeError):
                pass

        if clean_instr:
            data_to_save["extra_instructions"] = clean_instr
            data_to_save["instructions"] = clean_instr

        if created_by:
            data_to_save["created_by"] = str(created_by).strip()

        def _execute_update(row_id):
            for payload in [
                data_to_save,
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions", "created_by")},
                data,
            ]:
                try:
                    supabase_client.table("saved_exams").update(payload).eq("id", row_id).execute()
                    return True
                except Exception:
                    continue
            return False

        def _execute_insert():
            for payload in [
                data_to_save,
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions", "created_by")},
                data,
            ]:
                try:
                    res = supabase_client.table("saved_exams").insert(payload).execute()
                    saved_id = None
                    if res.data and len(res.data) > 0 and "id" in res.data[0]:
                        saved_id = res.data[0]["id"]
                    return True, saved_id
                except Exception:
                    continue
            return False, "Failed to insert exam into database."

        if existing_id:
            if _execute_update(existing_id):
                try:
                    get_exam_instructions.clear()
                except Exception:
                    pass
                return True, existing_id
            return False, "Failed to update existing exam in database."
        else:
            try:
                existing_match = (
                    supabase_client.table("saved_exams")
                    .select("id")
                    .eq("subject", subject)
                    .eq("year_group", year)
                    .eq("difficulty", diff)
                    .eq("topic", db_topic)
                    .execute()
                )
                if existing_match.data and len(existing_match.data) > 0:
                    matched_id = existing_match.data[0]["id"]
                    if _execute_update(matched_id):
                        try:
                            get_exam_instructions.clear()
                        except Exception:
                            pass
                        return True, matched_id
            except Exception as e:
                _log_error("check_existing_match", e)

            success, saved_id = _execute_insert()
            try:
                get_exam_instructions.clear()
            except Exception:
                pass
            return success, saved_id
    except Exception as e:
        return False, str(e)


# ── DYNAMIC FILE ENGINE ─────────────────────────────────────────────────────────
def get_parent_path(year: str, subject: str, difficulty: str) -> str:
    if difficulty:
        diff_path = os.path.join(SAMPLES_DIR, year, subject, difficulty)
        if os.path.exists(diff_path):
            return diff_path
    sub_path = os.path.join(SAMPLES_DIR, year, subject)
    return sub_path


def get_available_topics(parent_path: str) -> list:
    if not os.path.exists(parent_path):
        return []
    topics = [d for d in os.listdir(parent_path) if os.path.isdir(os.path.join(parent_path, d))]
    return sorted(topics)


def _read_files_in_dir(directory: str) -> list:
    if not os.path.exists(directory):
        return []
    samples = []
    for fname in sorted(os.listdir(directory)):
        fpath = os.path.join(directory, fname)
        if os.path.isfile(fpath):
            if fname.endswith((".txt", ".md")):
                try:
                    with open(fpath, "r", errors="replace") as f:
                        samples.append(f.read())
                except Exception as e:
                    _log_error(f"read_sample:{fpath}", e)
            elif fname.endswith(".pdf"):
                try:
                    reader = PdfReader(fpath)
                    pdf_text = "".join([page.extract_text() or "" for page in reader.pages])
                    if pdf_text.strip():
                        samples.append(pdf_text)
                except Exception as e:
                    _log_error(f"read_sample_pdf:{fpath}", e)
    return samples


@st.cache_data
def load_style_samples(year: str, subject: str, difficulty: str, topic: str) -> tuple:
    parent_path = get_parent_path(year, subject, difficulty)
    topic_path = os.path.join(parent_path, topic) if topic else ""

    samples = []
    source_msg = ""

    if topic_path and os.path.exists(topic_path):
        samples = _read_files_in_dir(topic_path)
        if samples:
            source_msg = f"✅ Using {len(samples)} sample file(s) from Topic folder."

    if not samples and os.path.exists(parent_path):
        samples = _read_files_in_dir(parent_path)
        if samples:
            source_msg = f"⚠️ Topic folder empty. Using {len(samples)} syllabus/reference file(s) from Subject folder."
        else:
            source_msg = "❌ No samples or syllabus files found."

    return "\n\n---\n\n".join(samples), source_msg


# ── SAFE MATH EXPRESSION EVALUATOR ─────────────────────────────────────────────
# Evaluates AI-generated graph expressions (e.g. "sin(x) + 2") WITHOUT calling
# Python's raw eval() on untrusted text. This walks the parsed AST and only
# permits a fixed whitelist of operators, numeric constants, and named math
# functions through. There is no way to reach attribute access, subscripting,
# imports, or builtins via this path — unlike eval() with a stripped
# __builtins__, which is a known-incomplete sandbox.
class UnsafeExpressionError(Exception):
    pass


_ALLOWED_BINOPS = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.Pow: _op.pow,
    ast.Mod: _op.mod,
    ast.FloorDiv: _op.floordiv,
}
_ALLOWED_UNARYOPS = {ast.UAdd: _op.pos, ast.USub: _op.neg}
_ALLOWED_FUNCS = {
    "sin": np.sin, "cos": np.cos, "tan": np.tan,
    "arcsin": np.arcsin, "arccos": np.arccos, "arctan": np.arctan,
    "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
    "exp": np.exp, "log": np.log, "log10": np.log10, "log2": np.log2,
    "sqrt": np.sqrt, "abs": np.abs, "floor": np.floor, "ceil": np.ceil,
}


def _safe_eval_node(node, names):
    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body, names)
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](
            _safe_eval_node(node.left, names), _safe_eval_node(node.right, names)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval_node(node.operand, names))
    if isinstance(node, ast.Call):
        if node.keywords or not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            fname = getattr(node.func, "id", "?")
            raise UnsafeExpressionError(f"Function '{fname}' is not allowed.")
        args = [_safe_eval_node(a, names) for a in node.args]
        return _ALLOWED_FUNCS[node.func.id](*args)
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise UnsafeExpressionError(f"Unknown variable '{node.id}'.")
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    raise UnsafeExpressionError(f"Disallowed expression element: {type(node).__name__}")


def safe_eval_math(expr_str: str, names: dict):
    """Evaluate a simple math expression string against an AST whitelist."""
    tree = ast.parse(expr_str, mode="eval")
    return _safe_eval_node(tree, names)


# ── PYTHON GRAPHING ENGINE ──────────────────────────────────────────────────────
def parse_graph_spec(block: str) -> dict | None:
    data = {}
    for line in block.strip().split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        data[key.strip().lower()] = val.strip()

    def _f(k, default):
        try:
            return float(data[k])
        except Exception:
            return default

    g_type = data.get("type", "function")
    spec = {
        "type": g_type,
        "xmin": _f("xmin", -5.0),
        "xmax": _f("xmax", 5.0),
        "ymin": data.get("ymin"),
        "ymax": data.get("ymax"),
        "xlabel": data.get("xlabel", "x"),
        "ylabel": data.get("ylabel", "y"),
    }

    if g_type in ["function", "slope_field"]:
        if "expr" not in data:
            return None
        spec["expr"] = data["expr"]
    elif g_type == "normal":
        spec["mean"] = _f("mean", 0.0)
        spec["std"] = _f("std", 1.0)
        spec["shade_min"] = data.get("shade_min")
        spec["shade_max"] = data.get("shade_max")

    return spec


def draw_function_graph(spec: dict) -> bytes | None:
    if not spec:
        return None
    g_type = spec["type"]
    DA = "#1A3A8A"
    fig, ax = plt.subplots(figsize=(6, 4))

    xmin, xmax = float(spec["xmin"]), float(spec["xmax"])
    auto_ymin, auto_ymax = (-5.0, 5.0)

    try:
        if g_type == "function":
            raw = spec["expr"].strip()
            raw = re.sub(r"^[yYfF]\s*[\(x\)]?\s*=\s*", "", raw)
            py_expr = raw.replace("^", "**")
            py_expr = re.sub(r"(\d)\s*\(", r"\1*(", py_expr)
            py_expr = re.sub(r"(\d)([a-df-wyzA-DF-WYZ])", r"\1*\2", py_expr)

            x = np.linspace(xmin, xmax, 1000)
            with np.errstate(divide="ignore", invalid="ignore"):
                y = safe_eval_math(py_expr, {"x": x, "pi": np.pi, "e": np.e})
            y = np.asarray(y, dtype=float)

            clip_val = max(abs(xmax - xmin) * 20, 200)
            with np.errstate(invalid="ignore"):
                y = np.where(np.abs(y) > clip_val, np.nan, y)
            ax.plot(x, y, color=DA, linewidth=2)

            y_valid = y[np.isfinite(y)]
            if len(y_valid) > 0:
                pad = max((y_valid.max() - y_valid.min()) * 0.1, 0.5)
                auto_ymin, auto_ymax = y_valid.min() - pad, y_valid.max() + pad

        elif g_type == "slope_field":
            ymin = float(spec["ymin"]) if spec.get("ymin") else -5.0
            ymax = float(spec["ymax"]) if spec.get("ymax") else 5.0
            auto_ymin, auto_ymax = ymin, ymax

            raw = spec["expr"].strip()
            py_expr = raw.replace("^", "**")

            x_vals = np.linspace(xmin, xmax, 20)
            y_vals = np.linspace(ymin, ymax, 20)
            X, Y = np.meshgrid(x_vals, y_vals)

            with np.errstate(divide="ignore", invalid="ignore"):
                dy = safe_eval_math(py_expr, {"x": X, "y": Y, "pi": np.pi, "e": np.e})

            dx = np.ones_like(dy)
            norm = np.sqrt(dx**2 + dy**2)
            dx = dx / norm
            dy = dy / norm

            ax.quiver(X, Y, dx, dy, color=DA, headwidth=1, headlength=0, pivot="middle", scale=30)

        elif g_type == "normal":
            mean = float(spec["mean"])
            std = float(spec["std"])
            x = np.linspace(mean - 4 * std, mean + 4 * std, 1000)
            y = (1 / (std * np.sqrt(2 * np.pi))) * np.exp(-0.5 * ((x - mean) / std) ** 2)
            ax.plot(x, y, color=DA, linewidth=2)

            if spec.get("shade_min") and spec.get("shade_max"):
                s_min = float(spec["shade_min"])
                s_max = float(spec["shade_max"])
                sx = np.linspace(s_min, s_max, 100)
                sy = (1 / (std * np.sqrt(2 * np.pi))) * np.exp(-0.5 * ((sx - mean) / std) ** 2)
                ax.fill_between(sx, sy, alpha=0.3, color=DA)

            xmin, xmax = mean - 4 * std, mean + 4 * std
            auto_ymin, auto_ymax = 0, y.max() * 1.2
            ax.set_xticks(
                [mean - 3 * std, mean - 2 * std, mean - std, mean, mean + std, mean + 2 * std, mean + 3 * std]
            )

        ymin = float(spec["ymin"]) if spec.get("ymin") else auto_ymin
        ymax = float(spec["ymax"]) if spec.get("ymax") else auto_ymax

        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.spines["left"].set_position("zero")
        ax.spines["bottom"].set_position("zero")
        ax.spines["right"].set_visible(False)
        ax.spines["top"].set_visible(False)
        ax.set_xlabel(spec["xlabel"], loc="right")
        ax.set_ylabel(spec["ylabel"], loc="top")
        ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.7)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        plt.close(fig)
        _log_error("draw_function_graph", e)
        return None


def inject_python_graphs(text: str, work_dir: str) -> str:
    """Renders any GRAPH_START...GRAPH_END blocks into PNGs inside work_dir and
    swaps them for \\includegraphics. Uses a random filename per graph so
    repeated calls (content / answers / solutions) never collide."""
    if not text:
        return text
    segments = re.split(r"(GRAPH_START.*?GRAPH_END)", text, flags=re.DOTALL)
    out_text = ""
    for seg in segments:
        if seg.startswith("GRAPH_START"):
            gspec = parse_graph_spec(seg.replace("GRAPH_START", "").replace("GRAPH_END", "").strip())
            if gspec and (png_bytes := draw_function_graph(gspec)):
                img_path = os.path.join(work_dir, f"graph_{uuid.uuid4().hex[:10]}.png")
                with open(img_path, "wb") as f:
                    f.write(png_bytes)
                img_url_path = img_path.replace(chr(92), "/")
                out_text += f"\\begin{{center}}\\includegraphics[width=0.6\\textwidth]{{{img_url_path}}}\\end{{center}}"
            else:
                out_text += "\n\\textit{[Python Graph Generation Failed]}\n"
        else:
            out_text += seg
    return out_text


# ── LATEX SANITIZER ────────────────────────────────────────────────────────────
_SAFE_AMP_ENV_RE = re.compile(
    r"(\\begin\{(?:tabular\*?|align\*?|alignat\*?|array|[pPbBvV]?matrix|cases|tikzpicture|eqnarray\*?|split)\}.*?"
    r"\\end\{(?:tabular\*?|align\*?|alignat\*?|array|[pPbBvV]?matrix|cases|tikzpicture|eqnarray\*?|split)\})",
    re.DOTALL,
)


def _escape_bare_ampersands(text: str) -> str:
    parts = _SAFE_AMP_ENV_RE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(re.sub(r"(?<!\\)&", r"\\&", part))
        else:
            out.append(part)
    return "".join(out)


def sanitize_ai_latex(text: str) -> str:
    if not text:
        return ""

    # 1. Strip preamble commands the AI may have emitted
    text = re.sub(r"(?m)^[ \t]*\\documentclass.*$\n?", "", text)
    text = re.sub(r"(?m)^[ \t]*\\usepackage.*$\n?", "", text)
    text = re.sub(r"(?m)^[ \t]*\\usetikzlibrary.*$\n?", "", text)
    text = re.sub(r"(?m)^[ \t]*\\pgfplotsset.*$\n?", "", text)
    text = re.sub(r"(?m)^[ \t]*\\geometry\{[^}]*\}[ \t]*$\n?", "", text)
    text = re.sub(r"\\begin\{document\}", "", text)
    text = re.sub(r"\\end\{document\}", "", text)
    text = re.sub(r"(?m)^[ \t]*\\pagestyle\{.*?\}[ \t]*$\n?", "", text)

    # 2. Strip whole-line LaTeX comments (e.g. "% Original: y = |x^2-4|...").
    text = re.sub(r"(?m)^[ \t]*%.*\n?", "", text)

    # 3. Escape bare percent signs across the whole text (e.g. 50% -> 50\%, Mass Change (%) -> Mass Change (\%))
    text = re.sub(r"(?<!\\)%", r"\%", text)

    # 4. Escape bare ampersands (protecting matrices, tables, align, tikz)
    text = _escape_bare_ampersands(text)

    # 5. Fix markdown bold **text** -> \textbf{text}
    text = re.sub(r"\*\*(.*?)\*\*", r"\\textbf{\1}", text)

    # 6. Fix command argument typos where '{...>' was written instead of '{...}'
    for _ in range(3):
        text = re.sub(r"(\\[a-zA-Z*]+(?:\[[^\]\n]*\])?(?:\{[^{}\n]*\})*)\{([^{}\n]*?)>", r"\1{\2}", text)

    # 7. Fix \begin[env] / \end[env] or \begin(env) / \end(env)
    text = re.sub(r"\\(begin|end)\[([a-zA-Z*]+)\]", r"\\\1{\2}", text)
    text = re.sub(r"\\(begin|end)\(([a-zA-Z*]+)\)", r"\\\1{\2}", text)

    # 8. Fix \begin{env> / \end{env> / \begin{env) / \end{env)
    text = re.sub(r"\\(begin|end)\{([a-zA-Z*]+)[>\]\)]", r"\\\1{\2}", text)

    # 9. Fix commands where closing brace was omitted at end of line
    text = re.sub(r"(\\(?:vspace\*?|hspace\*?|rule|label|ref|textbf|textit|mathbf|bm|vec|hat|underline))\{([a-zA-Z0-9\.\-\_\s]+)(?=[ \t]*[\n\r]|$)", r"\1{\2}", text)

    # 10. Fix \item[(A)>] or \item[(A)]> or \item[(A)>
    text = re.sub(r"\\item\[\(([A-Za-z0-9]+)\)[>\]\)]*", r"\\item[(\1)]", text)

    # 11. Fix "Missing \item" if \vspace appears immediately after \begin{enumerate} or \begin{itemize}
    text = re.sub(r"(\\begin\{(?:enumerate|itemize)\})\s*\\vspace\*?\{[^}]+\}\s*", r"\1\n", text)

    # 12. Rescue orphaned TikZ / pgfplots commands outside of \begin{tikzpicture}
    _TIKZ_CMD_RE = re.compile(
        r"\\(?:draw|fill|filldraw|path|node|foreach|coordinate|clip|shade|shadedraw|addplot)\b|\\begin\{axis\}"
    )

    def _has_orphaned_tikz(txt):
        stripped = re.sub(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", "", txt, flags=re.DOTALL)
        return bool(_TIKZ_CMD_RE.search(stripped))

    if _has_orphaned_tikz(text):
        parts = re.split(r"(\\begin\{tikzpicture\}.*?\\end\{tikzpicture\})", text, flags=re.DOTALL)
        repaired = []
        for i, part in enumerate(parts):
            if i % 2 == 1:
                repaired.append(part)
            else:
                if _TIKZ_CMD_RE.search(part):
                    lines = part.split("\n")
                    out_lines = []
                    tikz_buf = []
                    in_run = False
                    for line in lines:
                        is_tikz_line = bool(_TIKZ_CMD_RE.search(line))
                        is_continuation = in_run and line.strip() and not re.match(r"\\(?:section|item|begin\{enumerate|begin\{itemize|end\{enumerate|end\{itemize)", line.strip())
                        if is_tikz_line or is_continuation:
                            if not in_run:
                                in_run = True
                                tikz_buf = []
                            tikz_buf.append(line)
                        else:
                            if in_run:
                                out_lines.append("\\begin{tikzpicture}")
                                out_lines.extend(tikz_buf)
                                out_lines.append("\\end{tikzpicture}")
                                in_run = False
                                tikz_buf = []
                            out_lines.append(line)
                    if in_run:
                        out_lines.append("\\begin{tikzpicture}")
                        out_lines.extend(tikz_buf)
                        out_lines.append("\\end{tikzpicture}")
                    repaired.append("\n".join(out_lines))
                else:
                    repaired.append(part)
        text = "".join(repaired)

    # 13. Clean up all TikZ / pgfplots blocks: remove any empty or whitespace-only lines.
    # Blank lines produce \par in TeX, which fatally crashes \begin{axis} ("Paragraph ended before axis was complete").
    def _clean_tikz(match):
        tikz_block = match.group(0)
        lines = [l for l in tikz_block.splitlines() if l.strip()]
        return "\n".join(lines)

    text = re.sub(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", _clean_tikz, text, flags=re.DOTALL)

    # 14. Balance environments (auto-close any unclosed begin{env})
    tracked_envs = ["enumerate", "itemize", "tikzpicture", "axis", "align*", "aligned", "cases", "matrix", "pmatrix", "bmatrix", "center"]
    for env in tracked_envs:
        escaped_env = re.escape(env)
        opens = len(re.findall(rf"\\begin\{{{escaped_env}\}}", text))
        closes = len(re.findall(rf"\\end\{{{escaped_env}\}}", text))
        if opens > closes:
            text += ("\n" + f"\\end{{{env}}}\n" * (opens - closes))

    # 15. Automatically wrap all \begin{tikzpicture}...\end{tikzpicture} with adjustbox so no diagram overflows the page margins
    def _wrap_tikz_adjustbox(match):
        block = match.group(0)
        return "\\adjustbox{max width=\\linewidth}{%\n" + block + "\n}"

    text = re.sub(
        r"(?<!\\adjustbox\{max width=\\linewidth\}\{%\n)(?<!\\resizebox\{\\linewidth\}\{!\}\{%\n)(\\begin\{tikzpicture\}.*?\\end\{tikzpicture\})",
        _wrap_tikz_adjustbox,
        text,
        flags=re.DOTALL,
    )

    return text.strip()


def _contains_tikz(*texts) -> bool:
    return any(t and "\\begin{tikzpicture}" in t for t in texts)


def _format_exam_content(text: str) -> str:
    """
    Optimize question layout so questions and their working spaces stay together.
    Prevents orphaned question text at page bottoms and separated working space on the next page.
    """
    if not text:
        return ""

    # 1. Protect mark allocations from word-breaking across lines/pages: (2 marks) -> \mbox{\textbf{(2~marks)}}
    text = re.sub(
        r"\((\d+)\s+marks?\)",
        r"\\mbox{\\textbf{(\1~marks)}}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\[(\d+)\s+marks?\]",
        r"\\mbox{\\textbf{[\1~marks]}}",
        text,
        flags=re.IGNORECASE,
    )

    # 2. Prevent page breaks between question lines and their \vspace working spaces
    text = re.sub(
        r"(?<!\\nopagebreak)\s*\\vspace(\*?\{[^}]+\})",
        r"\n\\par\\nopagebreak\\vspace\1",
        text,
    )

    # 3. Prevent orphaned section titles at the bottom of pages
    text = re.sub(
        r"(?<!\\Needspace\{6cm\}\n)(\\section\*?\{[^}]+\})",
        r"\n\\Needspace{6cm}\n\1",
        text,
    )

    # 4. Inject \Needspace{4.5cm} for each question item so that if a question + working space
    # cannot fit on the current page, LaTeX automatically starts the question cleanly on the next page.
    text = re.sub(
        r"(^[ \t]*\\item\b(?!\s*\[)(?!\s*\\Needspace))",
        r"\1 \\Needspace{4.5cm}",
        text,
        flags=re.M,
    )

    return text


# ── PANDOC & PDF BUILDERS ──────────────────────────────────────────────────────
def build_word_doc_pandoc(content, answers, solutions, topic, header_title, total_marks, time_allowed, work_dir):
    tex_filename = os.path.join(work_dir, "pandoc.tex")
    docx_filename = os.path.join(work_dir, "exam.docx")

    def format_word(text):
        if not text:
            return ""
        text = re.sub(r"\\Needspace\{[^}]+\}", "", text)
        text = re.sub(r"\\item\[\(([A-D])\)\]\s*", r"\n\n(\1) ", text)
        return text.replace(r"\begin{itemize}", "").replace(r"\end{itemize}", "")

    solutions_section = (
        f"\\newpage\n\\begin{{center}}\\Large \\textbf{{FULLY WORKED SOLUTIONS}}\\end{{center}}\\vspace{{0.2cm}}\\hrule\\vspace{{0.5cm}}\n{format_word(solutions)}"
        if solutions
        else ""
    )

    full_tex = f"""\\documentclass{{article}}
\\usepackage{{amsmath, amssymb, amsfonts, graphicx, booktabs, array, bm, mathtools}}
\\begin{{document}}
\\begin{{center}}
    \\includegraphics[width=1.2in]{{{LOGO_PATH.replace(chr(92), "/")}}} \\\\[0.4cm]
    {{\\LARGE \\textbf{{{header_title}}}}} \\\\[0.2cm]
    {{\\Large \\textbf{{{topic}}}}}
\\end{{center}}
\\vspace{{0.5cm}}
\\noindent
\\begin{{tabular}}{{@{{}} p{{0.6\\textwidth}} p{{0.4\\textwidth}} @{{}}}}
\\textbf{{Student Name:}} \\rule{{5cm}}{{0.4pt}} & \\textbf{{Class:}} \\rule{{3cm}}{{0.4pt}} \\\\[0.5cm]
\\textbf{{Time Allowed:}} {time_allowed} minutes & \\textbf{{Total Marks:}} {total_marks}
\\end{{tabular}}
\\vspace{{0.5cm}}\\hrule\\vspace{{0.5cm}}

{format_word(content)}
\\newpage
\\begin{{center}}\\Large \\textbf{{ANSWERS}}\\end{{center}}\\vspace{{0.2cm}}\\hrule\\vspace{{0.5cm}}
{format_word(answers)}
{solutions_section}
\\end{{document}}"""

    with open(tex_filename, "w", encoding="utf-8") as f:
        f.write(full_tex)

    if not shutil.which("pandoc"):
        return None

    try:
        subprocess.run(["pandoc", tex_filename, "-o", docx_filename], cwd=work_dir, capture_output=True)
        if os.path.exists(docx_filename):
            doc = docx.Document(docx_filename)
            for section in doc.sections:
                section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Cm(1.5)
            doc.save(docx_filename)
            with open(docx_filename, "rb") as f:
                return f.read()
    except Exception as e:
        _log_error("build_word_doc_pandoc", e)
    return None


def build_latex_pdf(display_topic, header_title, content, answers, solutions, total_marks, time_allowed, work_dir):
    filename = os.path.join(work_dir, "exam.tex")
    pdf_filename = os.path.join(work_dir, "exam.pdf")
    if os.path.exists(pdf_filename):
        os.remove(pdf_filename)

    safe_title = header_title.replace("&", r"\&").replace("%", r"\%").replace("$", r"\$").replace("_", r"\_")
    safe_topic = display_topic.replace("&", r"\&").replace("%", r"\%").replace("$", r"\$").replace("_", r"\_")

    formatted_content = _format_exam_content(content)

    solutions_section = (
        f"\\newpage\n\\begin{{center}}\\Large \\textbf{{FULLY WORKED SOLUTIONS}}\\end{{center}}\\vspace{{0.2cm}}\\hrule\\vspace{{0.4cm}}\n{solutions}"
        if solutions
        else ""
    )

    tex_template = f"""\\documentclass[11pt,a4paper]{{article}}
\\usepackage[margin=1.5cm]{{geometry}}
\\usepackage{{lmodern}}
\\usepackage[utf8]{{inputenc}}
\\usepackage[T1]{{fontenc}}
\\usepackage{{amsmath, amssymb, amsfonts, booktabs, array, bm, mathtools}}
\\usepackage{{fancyhdr}}
\\usepackage{{graphicx}}
\\usepackage{{adjustbox}}
\\usepackage{{tikz}}
\\usetikzlibrary{{arrows.meta, positioning, calc, shapes.geometric, 3d, angles, quotes, patterns, patterns.meta, decorations.pathmorphing, decorations.markings, intersections, backgrounds, fit}}
\\usepackage{{pgfplots}}
\\pgfplotsset{{compat=1.18}}
\\usepackage{{needspace}}

\\widowpenalty=10000
\\clubpenalty=10000
\\displaywidowpenalty=10000
\\predisplaypenalty=10000
\\postdisplaypenalty=10000
\\raggedbottom

\\pagestyle{{fancy}}
\\fancyhead[L]{{\\textbf{{{safe_title}}}}}
\\fancyhead[R]{{\\textit{{DA Tuition}}}}
\\fancyfoot[C]{{\\thepage}}
\\setlength{{\\headheight}}{{15pt}}
\\renewcommand{{\\theenumi}}{{\\arabic{{enumi}}}}
\\renewcommand{{\\labelenumi}}{{\\textbf{{\\theenumi.}}}}
\\renewcommand{{\\labelenumii}}{{(\\alph{{enumii}})}}

\\begin{{document}}
\\thispagestyle{{plain}}
\\vspace*{{-1.5cm}}
\\noindent
\\begin{{minipage}}[c]{{0.7\\textwidth}}
    \\LARGE \\textbf{{{safe_title}}} \\\\[0.3cm]
    \\Large \\textbf{{{safe_topic}}}
\\end{{minipage}}
\\hfill
\\begin{{minipage}}[c]{{0.3\\textwidth}}
    \\raggedleft
    \\includegraphics[width=1.2in]{{{LOGO_PATH.replace(chr(92), "/")}}}
\\end{{minipage}}
\\vspace{{0.5cm}}
\\hrule\\vspace{{0.4cm}}
\\noindent\\textbf{{Student Name:}} \\underline{{\\hspace{{7cm}}}} \\hfill \\textbf{{Class:}} \\underline{{\\hspace{{3cm}}}} \\\\[0.4cm]
\\textbf{{Time Allowed:}} {time_allowed} minutes \\hfill \\textbf{{Total Marks:}} {total_marks}
\\vspace{{0.2cm}}\\hrule\\vspace{{0.4cm}}

{formatted_content}
\\newpage
\\begin{{center}}\\Large \\textbf{{ANSWERS}}\\end{{center}}\\vspace{{0.2cm}}\\hrule\\vspace{{0.4cm}}
{answers}
{solutions_section}
\\end{{document}}"""

    with open(filename, "w", encoding="utf-8") as f:
        f.write(tex_template)
    tex_bytes = tex_template.encode("utf-8")

    if not shutil.which("pdflatex"):
        return None, tex_bytes, "CRITICAL ERROR: 'pdflatex' not found."

    try:
        proc = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", filename],
            cwd=work_dir, capture_output=True, text=True,
        )
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", filename],
            cwd=work_dir, capture_output=True, text=True,
        )
        if os.path.exists(pdf_filename):
            with open(pdf_filename, "rb") as f:
                return f.read(), tex_bytes, ""
        return None, tex_bytes, proc.stdout
    except Exception as e:
        return None, tex_bytes, str(e)


# ── WORK-DIR ISOLATION (prevents concurrent users overwriting each other) ──────
def _start_new_work_dir() -> str:
    """Clean up any previous temp directory for this session and create a
    fresh, uniquely-named one for this generation request."""
    old = st.session_state.get("work_dir")
    if old and os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
    new_dir = tempfile.mkdtemp(prefix="da_exam_")
    st.session_state.work_dir = new_dir
    return new_dir


def _get_or_create_work_dir() -> str:
    """Reuse the active work dir for this exam if it still exists (Phase 2
    solutions rebuild); otherwise create a fresh one."""
    wd = st.session_state.get("work_dir")
    if wd and os.path.isdir(wd):
        return wd
    return _start_new_work_dir()


def _render_exam_files(work_dir, display_topic, title, q_text, a_text, s_text, total_q):
    """(Re)injects graphs fresh into work_dir and compiles the PDF + Word doc.
    Safe to call more than once against the same work_dir (Phase 1, then again
    once Phase 2 adds worked solutions)."""
    c_final = inject_python_graphs(q_text, work_dir)
    a_final = inject_python_graphs(a_text, work_dir)
    s_final = inject_python_graphs(s_text, work_dir) if s_text else ""

    marks = sum(int(m) for m in re.findall(r"\((\d+)\s*marks?\)", c_final, re.IGNORECASE)) or (total_q * 2)
    time_allowed = int(marks * 1.5)

    pdf_bytes, tex_bytes, log = build_latex_pdf(
        display_topic, title, c_final, a_final, s_final, marks, time_allowed, work_dir
    )

    if _contains_tikz(c_final, a_final, s_final):
        word_bytes, word_reason = None, "diagram"
    elif not shutil.which("pandoc"):
        word_bytes, word_reason = None, "pandoc_missing"
    else:
        word_bytes = build_word_doc_pandoc(c_final, a_final, s_final, display_topic, title, marks, time_allowed, work_dir)
        word_reason = None if word_bytes else "build_failed"

    return pdf_bytes, tex_bytes, word_bytes, log, word_reason


# ── AI CALL HELPERS ─────────────────────────────────────────────────────────────
MAX_OUTPUT_TOKENS = 32768  # Generous budget so larger question sets aren't silently truncated

_RETRYABLE_MARKERS = (
    "503", "500", "429", "404", "400", "unavailable", "overloaded",
    "internal", "resource_exhausted", "not found", "empty text",
    "truncated", "invalid_argument",
)


def _is_retryable(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(marker in s for marker in _RETRYABLE_MARKERS)


def friendly_error_message(exc: Exception) -> str:
    """Translate a raw exception into a short, plain-English message for staff."""
    msg = str(exc)
    low = msg.lower()
    if "api key" in low or "401" in msg or "permission" in low or "403" in msg:
        return "The AI service rejected the request — the API key may be missing or invalid. Please contact tech support."
    if "429" in msg or "resource_exhausted" in low or "quota" in low:
        return "The AI service has temporarily hit its usage limit. Please wait a minute and try again."
    if "503" in msg or "overloaded" in low or "unavailable" in low or "500" in msg or "internal" in low:
        return "Google's AI servers are temporarily busy. Please wait a moment and click the button again."
    if "timeout" in low or "timed out" in low:
        return "The request took too long and timed out. Try reducing the number of questions and generating again."
    if "truncated" in low or "empty text" in low:
        return "The AI's response was incomplete — this usually happens when too many questions are requested at once. Try a smaller batch."
    return "Something went wrong while generating the exam. Please try again, or contact tech support if this keeps happening."


# ══════════════════════════════════════════════════════════════════════════════
# TEACHER ACCOUNTS & AUTHENTICATION MODULE
# ══════════════════════════════════════════════════════════════════════════════
TEACHER_REGISTRY_PATH = "_system/teachers_registry.json"
PASSWORD_SALT = "DA_TUITION_ACADEMY_2026_SECURE_SALT"
SCHOOL_INVITE_CODE = "DA-JUNIOR-2026"


def _hash_password(password: str) -> str:
    return hashlib.sha256((password + PASSWORD_SALT).encode("utf-8")).hexdigest()


def _load_teachers_registry() -> dict:
    """Loads dictionary of junior teachers from cloud storage."""
    if not supabase_client:
        return {}
    try:
        res = supabase_client.storage.from_("exam-files").download(TEACHER_REGISTRY_PATH)
        if res:
            return json.loads(res.decode("utf-8"))
    except Exception:
        pass
    return {}


def _save_teachers_registry(registry: dict) -> bool:
    """Persists dictionary of junior teachers to cloud storage."""
    if not supabase_client:
        return False
    try:
        data = json.dumps(registry, indent=2).encode("utf-8")
        supabase_client.storage.from_("exam-files").upload(
            TEACHER_REGISTRY_PATH,
            data,
            {"content-type": "application/json", "upsert": "true"},
        )
        return True
    except Exception as e:
        _log_error("_save_teachers_registry", e)
        return False


def _register_teacher(username: str, password: str, display_name: str, api_key: str = "", role: str = "junior") -> tuple[bool, str]:
    u = username.strip().lower()
    if not u or not password:
        return False, "Username and password cannot be empty."
    if len(password) < 4:
        return False, "Password must be at least 4 characters long."
    registry = _load_teachers_registry()
    if u in registry or u in ("admin", "senior", "root"):
        return False, f"Username '{u}' is already taken. Please choose another."
    registry[u] = {
        "username": u,
        "display_name": display_name.strip() or u.capitalize(),
        "password_hash": _hash_password(password),
        "role": role,
        "api_key": api_key.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if _save_teachers_registry(registry):
        return True, "Account created successfully!"
    return False, "Failed to save account to cloud storage. Please try again."


def _update_teacher_api_key(username: str, api_key: str) -> bool:
    u = username.strip().lower()
    registry = _load_teachers_registry()
    if u in registry:
        registry[u]["api_key"] = api_key.strip()
        return _save_teachers_registry(registry)
    return False


def _delete_teacher(username: str) -> bool:
    u = username.strip().lower()
    registry = _load_teachers_registry()
    if u in registry:
        del registry[u]
        return _save_teachers_registry(registry)
    return False


def _reset_teacher_password(username: str, new_password: str) -> bool:
    u = username.strip().lower()
    registry = _load_teachers_registry()
    if u in registry:
        registry[u]["password_hash"] = _hash_password(new_password)
        return _save_teachers_registry(registry)
    return False


def _mask_api_key(key: str) -> str:
    if not key:
        return "Not configured"
    if len(key) <= 8:
        return "••••••••"
    return f"{key[:6]}••••••••{key[-4:]}"


def _get_genai_client():
    """Builds the Gemini client using role-specific API keys.
    Junior teachers strictly use their own API key to preserve school credits.
    Senior teachers use the server master API key.
    """
    user_role = st.session_state.get("user_role", "senior")
    if user_role == "junior":
        api_key = (st.session_state.get("user_api_key") or "").strip()
        if not api_key:
            st.error("⚠️ Missing personal Gemini API key. Please connect your API key in the sidebar.")
            st.stop()
    else:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            api_key = (st.session_state.get("user_api_key") or "").strip()
        if not api_key:
            st.error("⚠️ The AI engine isn't configured (missing server API key). Please contact tech support before using the generator.")
            st.stop()
    return genai.Client(api_key=api_key)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG & UI
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="DA Tuition Generator",
    page_icon=LOGO_PATH if os.path.exists(LOGO_PATH) else "📚",
    layout="wide",
)
st.markdown(
    """
    <style>
        .stApp { background-color: #EAF6FF; }
        [data-testid="stHeader"] { background-color: transparent; }
        html, body, p, span, label, input, textarea, button, 
        [data-testid="stWidgetLabel"] p, .stMarkdown p, .stButton button, 
        div[role="listbox"], div[role="combobox"] span, .stSelectbox div, .stMultiSelect div { font-size: 18px !important; }
        h1 { font-size: 2.6rem !important; font-weight: 700 !important; }
        h2 { font-size: 2.2rem !important; font-weight: 700 !important; }
        h3 { font-size: 1.8rem !important; font-weight: 600 !important; }
        h4 { font-size: 1.5rem !important; font-weight: 600 !important; }
        h5 { font-size: 1.3rem !important; font-weight: 600 !important; }
        .stCaption, caption, small { font-size: 14px !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

col1, col2 = st.columns([1, 3], vertical_alignment="center")
with col1:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH)
with col2:
    st.markdown(
        """
        <style>
        .massive-header { font-size: 35px !important; font-weight: 600 !important; color: #31333F !important; line-height: 1.2 !important; margin-bottom: 0px !important; }
        </style>
        <div class="massive-header">DA Tuition | NESA Question Generator</div>
        """,
        unsafe_allow_html=True,
    )

st.markdown("---")

# ──────────────────────────────────────────────────────────────────────────────
# AUTHENTICATION & LOGIN GATE
# ──────────────────────────────────────────────────────────────────────────────
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
if "user_role" not in st.session_state:
    st.session_state["user_role"] = None
if "username" not in st.session_state:
    st.session_state["username"] = None
if "user_display_name" not in st.session_state:
    st.session_state["user_display_name"] = None
if "user_api_key" not in st.session_state:
    st.session_state["user_api_key"] = None

if not st.session_state.get("logged_in"):
    st.markdown("### 🔐 Teacher Login Portal")
    st.caption("Please sign in to access the question generator and cloud exam library.")

    login_tab1, login_tab2, login_tab3 = st.tabs([
        "🧑‍🏫 Junior Teacher Login",
        "👑 Senior / Admin Access",
        "✨ New Junior Teacher Sign Up",
    ])

    with login_tab1:
        st.markdown("##### Sign In with Your Teacher Account")
        with st.form("junior_login_form"):
            login_user = st.text_input("Username", placeholder="e.g. john_doe").strip()
            login_pwd = st.text_input("Password", type="password")
            submit_login = st.form_submit_button("Log In", type="primary", use_container_width=True)

            if submit_login:
                if not login_user or not login_pwd:
                    st.error("Please enter both your username and password.")
                else:
                    registry = _load_teachers_registry()
                    user_record = registry.get(login_user.lower())
                    if not user_record:
                        st.error(f"No account found for username '{login_user}'. Please sign up if you are a new teacher.")
                    elif user_record.get("password_hash") != _hash_password(login_pwd):
                        st.error("Incorrect password. Please try again.")
                    else:
                        st.session_state["logged_in"] = True
                        st.session_state["user_role"] = user_record.get("role", "junior")
                        st.session_state["username"] = user_record.get("username", login_user.lower())
                        st.session_state["user_display_name"] = user_record.get("display_name", login_user.capitalize())
                        st.session_state["user_api_key"] = user_record.get("api_key", "")
                        st.success(f"Welcome back, {st.session_state['user_display_name']}!")
                        time.sleep(0.4)
                        st.rerun()

    with login_tab2:
        st.markdown("##### Senior Teacher & Admin Master Login")
        with st.form("senior_login_form"):
            master_pwd = st.text_input("Master Password or Admin PIN", type="password")
            submit_master = st.form_submit_button("Log In as Senior Teacher", type="primary", use_container_width=True)

            if submit_master:
                if master_pwd in ("DA2026", "DA_ADMIN"):
                    st.session_state["logged_in"] = True
                    st.session_state["user_role"] = "senior"
                    st.session_state["username"] = "admin"
                    st.session_state["user_display_name"] = "Senior Teacher"
                    st.session_state["user_api_key"] = os.environ.get("GEMINI_API_KEY", "")
                    st.session_state["admin_pin"] = "DA_ADMIN"
                    st.success("Senior Teacher access granted!")
                    time.sleep(0.4)
                    st.rerun()
                else:
                    st.error("Incorrect password or PIN.")

    with login_tab3:
        st.markdown("##### Register New Junior Teacher Account")
        with st.form("junior_signup_form"):
            reg_invite = st.text_input(
                "School Registration Code",
                placeholder="e.g. DA-JUNIOR-2026",
                help="Ask your senior administrator for the school registration code.",
            ).strip()
            reg_name = st.text_input("Full Name", placeholder="e.g. Sarah Connor").strip()
            reg_user = st.text_input("Desired Username", placeholder="e.g. sarah_c").strip()
            reg_pwd1 = st.text_input("Password (min 4 characters)", type="password")
            reg_pwd2 = st.text_input("Confirm Password", type="password")
            reg_key = st.text_input(
                "Your Gemini API Key (Optional now — you can also add it after logging in)",
                type="password",
                placeholder="AIzaSy...",
            ).strip()
            submit_reg = st.form_submit_button("Create Account", type="primary", use_container_width=True)

            if submit_reg:
                if reg_invite != SCHOOL_INVITE_CODE:
                    st.error("Invalid School Registration Code. Please check with your senior teacher.")
                elif not reg_name or not reg_user:
                    st.error("Please enter your name and choose a username.")
                elif reg_pwd1 != reg_pwd2:
                    st.error("Passwords do not match.")
                elif len(reg_pwd1) < 4:
                    st.error("Password must be at least 4 characters.")
                else:
                    success, msg = _register_teacher(reg_user, reg_pwd1, reg_name, api_key=reg_key, role="junior")
                    if success:
                        st.session_state["logged_in"] = True
                        st.session_state["user_role"] = "junior"
                        st.session_state["username"] = reg_user.lower()
                        st.session_state["user_display_name"] = reg_name
                        st.session_state["user_api_key"] = reg_key
                        st.success(f"Account created successfully! Welcome, {reg_name}.")
                        time.sleep(0.8)
                        st.rerun()
                    else:
                        st.error(msg)

    with st.sidebar:
        if os.path.exists(LOGO_PATH):
            st.image(LOGO_PATH, width="stretch")
        st.info("👋 Welcome! Please log in on the main screen to continue.")

    st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# FIRST-TIME API KEY ONBOARDING FOR JUNIOR TEACHERS
# ──────────────────────────────────────────────────────────────────────────────
if st.session_state.get("user_role") == "junior" and not st.session_state.get("user_api_key"):
    st.warning(f"👋 Welcome, **{st.session_state['user_display_name']}**! One quick step before you begin.")
    st.markdown("""
    To ensure fair use and protect the school's shared resources, junior teachers generate worksheets using their own personal Google Gemini API key.
    
    **How to get your free Gemini API key in 30 seconds:**
    1. Open [Google AI Studio (Get API Key)](https://aistudio.google.com/app/apikey) in a new tab.
    2. Sign in with your Google account and click **"Create API Key"**.
    3. Copy the key (starts with `AIzaSy...`) and paste it below.
    
    *(Your key is securely saved to your account — you will only ever have to enter this once).*
    """)

    with st.form("connect_key_form"):
        new_k = st.text_input("Paste your Google Gemini API Key:", type="password", placeholder="AIzaSy...").strip()
        submit_k = st.form_submit_button("🔒 Save & Connect API Key", type="primary", use_container_width=True)

        if submit_k:
            if not new_k or not new_k.startswith("AIza"):
                st.error("Please enter a valid Gemini API key (it starts with 'AIzaSy...').")
            else:
                _update_teacher_api_key(st.session_state["username"], new_k)
                st.session_state["user_api_key"] = new_k
                st.success("✅ API key connected successfully! Loading generator...")
                time.sleep(0.8)
                st.rerun()

    with st.sidebar:
        if os.path.exists(LOGO_PATH):
            st.image(LOGO_PATH, width="stretch")
        st.markdown("---")
        st.caption(f"👤 **{st.session_state['user_display_name']}** (Junior Teacher)")
        if st.button("🚪 Log Out", key="logout_btn_no_key"):
            st.session_state.clear()
            st.rerun()

    st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# SIDEBAR LOGGED IN PROFILE & CONTROLS
# ──────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, width="stretch")
    st.markdown("---")

    role_badge = "👑 Senior Teacher" if st.session_state.get("user_role") == "senior" else "🧑‍🏫 Junior Teacher"
    st.markdown(f"👤 **{st.session_state.get('user_display_name', 'Teacher')}**")
    st.caption(f"Role: {role_badge}")

    if st.session_state.get("user_role") == "junior":
        with st.expander("🔑 My Gemini API Key"):
            masked = _mask_api_key(st.session_state.get("user_api_key", ""))
            st.caption(f"**Connected Key:** `{masked}`")
            st.caption("All your worksheet generation strictly uses your personal API credits.")
            new_k_input = st.text_input("Update API Key", type="password", placeholder="AIzaSy...", key="update_key_input").strip()
            if st.button("Save New Key", key="save_new_key_btn", use_container_width=True):
                if new_k_input and new_k_input.startswith("AIza"):
                    _update_teacher_api_key(st.session_state["username"], new_k_input)
                    st.session_state["user_api_key"] = new_k_input
                    st.success("API key updated successfully!")
                    st.rerun()
                else:
                    st.error("Please enter a valid key starting with 'AIza'.")
            st.markdown("[Get a Gemini API Key](https://aistudio.google.com/app/apikey)")

    if st.session_state.get("user_role") == "senior":
        with st.expander("👥 Manage Junior Teachers"):
            registry = _load_teachers_registry()
            if not registry:
                st.caption("No junior teacher accounts registered yet.")
            else:
                st.caption(f"**{len(registry)} Registered Junior Teacher(s):**")
                for u, u_info in list(registry.items()):
                    k_status = "🔑 Connected" if u_info.get("api_key") else "⚠️ No Key"
                    st.markdown(f"• **{u_info.get('display_name', u)}** (`@{u}`) · {k_status}")

                st.markdown("---")
                st.caption("Manage Account:")
                sel_teacher = st.selectbox(
                    "Select Teacher",
                    list(registry.keys()),
                    format_func=lambda x: f"{registry[x].get('display_name', x)} (@{x})",
                    key="sel_admin_teacher",
                )
                c_del, c_rst = st.columns(2)
                with c_del:
                    if st.button("🗑️ Remove", key=f"del_teacher_{sel_teacher}", use_container_width=True):
                        _delete_teacher(sel_teacher)
                        st.toast(f"Removed @{sel_teacher}")
                        st.rerun()
                with c_rst:
                    rst_pwd = st.text_input("New Password", type="password", key=f"rst_pwd_{sel_teacher}", placeholder="New pass")
                    if st.button("🔑 Reset", key=f"btn_rst_{sel_teacher}", use_container_width=True):
                        if rst_pwd and len(rst_pwd) >= 4:
                            _reset_teacher_password(sel_teacher, rst_pwd)
                            st.toast(f"Password reset for @{sel_teacher}")
                            st.rerun()
                        else:
                            st.error("Min 4 characters.")

    if st.button("🚪 Log Out", use_container_width=True):
        st.session_state.clear()
        st.rerun()

    # Quick diagnostic so deployment issues (missing pdflatex/pandoc) are obvious
    _pdflatex_ok = shutil.which("pdflatex") is not None
    _pandoc_ok = shutil.which("pandoc") is not None
    st.caption(f"⚙️ LaTeX engine: {'✅' if _pdflatex_ok else '❌ missing'}  ·  Word export: {'✅' if _pandoc_ok else '❌ missing'}")

    st.markdown("---")
    app_modes = ["✨ Generator", "📚 Exam Library"]
    app_mode = st.radio("App Mode", app_modes, key="main_app_mode")
    st.markdown("---")
    if app_mode == "✨ Generator":
        st.header("⚙️ Advanced Settings")
        use_live_search = st.checkbox("🌍 Enable Live Web Search", value=False)
        extra_instructions = st.text_area(
            "Extra Instructions (Optional)",
            key="extra_instructions_input",
            help="Specific focus or requirements for this exam. Saved with the exam in the library.",
        )

    if st.session_state.get("user_role") == "senior":
        st.markdown("---")
        with st.expander("🔐 Admin PIN"):
            st.text_input("Admin PIN", type="password", key="admin_pin", value="DA_ADMIN")


def _get_exam_cost_info(exam: dict) -> tuple[float, str]:
    """Extract stored cost or compute an accurate estimate using Gemini 3.7 Flash rates."""
    if exam.get("cost") is not None:
        try:
            return float(exam["cost"]), exam.get("model") or "gemini-3.7-flash"
        except (ValueError, TypeError):
            pass

    topic_str = exam.get("topic", "")
    mc_matches = re.findall(r"(\d+)\s*MC", topic_str, re.IGNORECASE)
    fr_matches = re.findall(r"(\d+)\s*(?:Easy|Med|Medium|Hard|Ext)", topic_str, re.IGNORECASE)

    num_mc = sum(int(m) for m in mc_matches) if mc_matches else 0
    num_fr = sum(int(m) for m in fr_matches) if fr_matches else 0
    total_q = num_mc + num_fr

    if total_q == 0:
        total_q = 15
        num_mc = 5
        num_fr = 10

    est_in_tokens = 6500
    est_out_tokens = (num_mc * 120) + (num_fr * 250) + 500

    # Gemini 3.7 Flash current published pricing: $0.75 / 1M in, $3.75 / 1M out
    est_cost = ((est_in_tokens / 1_000_000) * 0.75) + ((est_out_tokens / 1_000_000) * 3.75)
    return round(est_cost, 5), exam.get("model") or "gemini-3.7-flash"


@st.cache_data(ttl=300, show_spinner=False)
def get_exam_instructions(pdf_url: str, db_instructions: str = "") -> str:
    """Retrieve instructions from db field or storage companion file."""
    if db_instructions and str(db_instructions).strip():
        return str(db_instructions).strip()
    if not pdf_url or not supabase_client:
        return ""
    try:
        storage_path = unquote(pdf_url.split("/exam-files/")[-1].split("?")[0])
        if storage_path.endswith(".pdf"):
            instr_path = storage_path[:-4] + "_instructions.txt"
            res = supabase_client.storage.from_("exam-files").download(instr_path)
            if res:
                return res.decode("utf-8").strip()
    except Exception:
        pass
    return ""


def save_exam_instructions(exam_id: str, pdf_url: str, instructions_text: str) -> bool:
    """Persist instructions to storage file and database."""
    if not supabase_client:
        return False
    clean_text = instructions_text.strip()
    if pdf_url:
        try:
            storage_path = unquote(pdf_url.split("/exam-files/")[-1].split("?")[0])
            if storage_path.endswith(".pdf"):
                instr_path = storage_path[:-4] + "_instructions.txt"
                supabase_client.storage.from_("exam-files").upload(
                    instr_path,
                    clean_text.encode("utf-8"),
                    {"content-type": "text/plain; charset=utf-8", "upsert": "true"},
                )
        except Exception as e:
            _log_error("save_exam_instructions_storage", e)

    for col_name in ("extra_instructions", "instructions"):
        try:
            supabase_client.table("saved_exams").update({col_name: clean_text}).eq("id", exam_id).execute()
            break
        except Exception:
            pass

    try:
        get_exam_instructions.clear()
    except Exception:
        pass
    return True

if app_mode == "📚 Exam Library":
    st.header("📚 Exam Library")
    if not supabase_client:
        st.warning("Supabase backend is not connected.")
    else:
        with st.spinner("Loading exams..."):
            response = supabase_client.table("saved_exams").select("*").order("created_at", desc=True).execute()
        if not response.data:
            st.info("No exams found.")
        else:
            all_exams = response.data
            available_subjects = ["All"] + sorted(list(set([ex["subject"] for ex in all_exams if ex.get("subject")])))
            available_years = ["All"] + sorted(list(set([ex["year_group"] for ex in all_exams if ex.get("year_group")])))

            st.markdown("### 🔍 Search, Filter & Sort")
            
            # --- NEW SEARCH BAR ---
            search_query = st.text_input("Search exams by keyword...", placeholder="Type to search topics, subjects, or levels...", label_visibility="collapsed")
            
            f_col1, f_col2, f_col3, f_col4 = st.columns(4)
            with f_col1:
                filter_subject = st.selectbox("Subject", available_subjects)
            with f_col2:
                filter_year = st.selectbox("Year Group", available_years)

            # --- UPDATED FILTER LOGIC ---
            temp_filtered = [
                ex for ex in all_exams
                if (filter_subject == "All" or ex.get("subject") == filter_subject)
                and (filter_year == "All" or ex.get("year_group") == filter_year)
                and (not search_query or search_query.lower() in ex.get("topic", "").lower() or search_query.lower() in ex.get("subject", "").lower())
            ]
            
            available_topics = ["All"] + sorted(list(set([ex.get("topic", "").split(" (")[0] for ex in temp_filtered if ex.get("topic")])))

            with f_col3:
                filter_topic = st.selectbox("Topic", available_topics)
            with f_col4:
                sort_option = st.selectbox("Sort By", ["Date Added (Newest)", "Date Added (Oldest)", "Name (A-Z)", "Name (Z-A)"])
            st.markdown("---")

            filtered_exams = [ex for ex in temp_filtered if filter_topic == "All" or ex.get("topic", "").split(" (")[0] == filter_topic]

            if sort_option == "Date Added (Newest)":
                filtered_exams.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            elif sort_option == "Date Added (Oldest)":
                filtered_exams.sort(key=lambda x: x.get("created_at", ""))
            elif sort_option == "Name (A-Z)":
                filtered_exams.sort(key=lambda x: x.get("topic", "").lower())
            elif sort_option == "Name (Z-A)":
                filtered_exams.sort(key=lambda x: x.get("topic", "").lower(), reverse=True)

            if not filtered_exams:
                st.info("No exams match your current filters.")
            else:
                st.caption(f"Showing {len(filtered_exams)} exam(s)")
                for exam in filtered_exams:
                    lvl_str_lib = f" {exam['difficulty']}" if exam.get("difficulty") else ""
                    cost_val, model_val = _get_exam_cost_info(exam)
                    cost_badge = f"${cost_val:.4f}"
                    with st.expander(f"📝 {exam['topic']} ({exam['subject']} - {exam['year_group']}{lvl_str_lib})  ·  💸 {cost_badge}"):
                        sydney_timestamp = get_sydney_time(exam.get("created_at", ""))
                        teacher_name = exam.get("created_by") or "Senior Teacher"
                        st.caption(f"📅 **Generated:** {sydney_timestamp} &nbsp;&nbsp;|&nbsp;&nbsp; 👤 **Teacher:** {teacher_name} &nbsp;&nbsp;|&nbsp;&nbsp; 💸 **Cost:** {cost_badge} &nbsp;&nbsp;|&nbsp;&nbsp; 🤖 **Engine:** `{model_val}`")

                        e_col1, e_col2, e_col3 = st.columns([2, 2, 1])
                        if exam.get("pdf_url"):
                            e_col1.markdown(f"[📥 Download PDF]({exam['pdf_url']})")
                        if exam.get("docx_url"):
                            e_col2.markdown(f"[📄 Download Word Doc]({exam['docx_url']})")

                        if e_col3.button("🗑️ Delete", key=f"del_{exam['id']}"):
                            with st.spinner("Deleting from Cloud..."):
                                paths_to_delete = []
                                if exam.get("pdf_url"):
                                    p_path = unquote(exam["pdf_url"].split("/exam-files/")[-1].split("?")[0])
                                    paths_to_delete.append(p_path)
                                    if p_path.endswith(".pdf"):
                                        paths_to_delete.append(p_path[:-4] + "_instructions.txt")
                                if exam.get("docx_url"):
                                    paths_to_delete.append(unquote(exam["docx_url"].split("/exam-files/")[-1].split("?")[0]))

                                if paths_to_delete:
                                    try:
                                        supabase_client.storage.from_("exam-files").remove(paths_to_delete)
                                    except Exception as e:
                                        _log_error("delete_storage_files", e)
                                try:
                                    supabase_client.table("saved_exams").delete().eq("id", exam["id"]).execute()
                                    st.rerun()
                                except Exception as e:
                                    st.error("⚠️ Couldn't delete this exam. Please try again or contact tech support.")
                                    with st.expander("🛠️ Technical details"):
                                        st.code(str(e))

                        # --- EXTRA INSTRUCTIONS SECTION ---
                        instr_text = get_exam_instructions(
                            exam.get("pdf_url", ""),
                            exam.get("extra_instructions") or exam.get("instructions") or "",
                        )

                        if instr_text:
                            st.markdown("---")
                            st.markdown("##### 💬 Extra Instructions Used")
                            st.code(instr_text, language="text")
                            c_reuse, _ = st.columns([2, 3])
                            with c_reuse:
                                if st.button("✨ Load Instructions into Generator", key=f"reuse_instr_{exam['id']}", type="primary", use_container_width=True):
                                    st.session_state["extra_instructions_input"] = instr_text
                                    st.session_state["main_app_mode"] = "✨ Generator"
                                    st.rerun()

                        with st.expander("✏️ Edit Instructions" if instr_text else "➕ Add Instructions / Notes"):
                            edit_box = st.text_area("Instructions / Notes", value=instr_text, key=f"edit_box_{exam['id']}", placeholder="e.g. Focus on finding vertex, quadratic formula, word problems...")
                            if st.button("💾 Save Instructions", key=f"save_btn_{exam['id']}"):
                                with st.spinner("Saving instructions..."):
                                    save_exam_instructions(exam["id"], exam.get("pdf_url", ""), edit_box)
                                    st.success("Instructions updated!")
                                    st.rerun()

    st.stop()

# ── GENERATOR MODE ─────────────────────────────────────────────────────────────
st.markdown("### 📝 Exam Details")
c1, c2, c3 = st.columns(3)
with c1:
    year_group = st.multiselect("Year", ["Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"])
with c2:
    subject = st.selectbox("Subject", ["Maths", "English", "Biology", "Business Studies", "Chemistry", "Legal Studies", "Science"])

with c3:
    level_disabled = False
    if subject == "Maths" and "Year 11" in year_group:
        level_opts = ["Standard", "Advanced", "Extension"]
    elif subject == "Maths" and "Year 12" in year_group:
        level_opts = ["Standard", "Advanced", "Extension 1", "Extension 2"]
    elif subject == "English" and ("Year 11" in year_group or "Year 12" in year_group):
        level_opts = ["Standard", "Advanced", "EALD", "Extension"]
    else:
        level_opts = ["N/A"]
        level_disabled = True
    level = st.selectbox("Level", level_opts, disabled=level_disabled)

actual_level = "" if level == "N/A" else level

available_topics = []
for y in year_group:
    p_path = get_parent_path(y, subject, actual_level)
    available_topics.extend(get_available_topics(p_path))

available_topics = sorted(list(set(available_topics)), key=lambda x: [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", x)])

c4, c5, c6 = st.columns([3, 2, 1])
with c4:
    if available_topics:
        topic = st.multiselect("Topic", available_topics)
    else:
        topic = st.text_input("Topic", placeholder="e.g. Algebra, Calculus")
with c5:
    sub_topic = st.text_input("Specific Sub-topic (Optional)", placeholder="e.g. Slope Fields")
with c6:
    set_number_input = st.number_input(
        "Set #",
        min_value=0,
        value=0,
        help="Leave 0 for automatic numbering (starts at 1 or fills missing gaps), or enter a specific set number (e.g. 1).",
    )

exemplar_questions = st.text_area(
    "🧬 Question Cloner (Optional Text Input)",
    placeholder="Paste specific questions here. The AI will generate variations matching their exact style and difficulty!",
)

st.markdown("##### 📸 Or, Upload an Exam Notification / Worksheet Photo(s)")
uploaded_photos = st.file_uploader(
    "Upload picture(s) or PDF pages of a school worksheet or exam notice to automatically extract the context (multiple files supported)",
    type=["png", "jpg", "jpeg", "webp", "pdf"],
    accept_multiple_files=True,
)
st.caption("⚠️ Use your own past worksheets, or describe the style you want — avoid pasting or uploading questions copied verbatim from copyrighted textbooks or commercial exam banks.")

st.markdown("### 📊 Question Distribution")
dist_cols = st.columns(5)
with dist_cols[0]:
    use_mc = st.checkbox("Multiple Choice", value=True)
    num_mc = st.number_input("MC Qty", min_value=0, max_value=100, value=5, label_visibility="collapsed") if use_mc else 0
with dist_cols[1]:
    use_easy = st.checkbox("Easy", value=True)
    num_easy = st.number_input("Easy Qty", min_value=0, max_value=100, value=5, label_visibility="collapsed") if use_easy else 0
with dist_cols[2]:
    use_med = st.checkbox("Medium", value=True)
    num_med = st.number_input("Med Qty", min_value=0, max_value=100, value=5, label_visibility="collapsed") if use_med else 0
with dist_cols[3]:
    use_hard = st.checkbox("Hard", value=False)
    num_hard = st.number_input("Hard Qty", min_value=0, max_value=100, value=5, label_visibility="collapsed") if use_hard else 0
with dist_cols[4]:
    use_xh = st.checkbox("Extremely Hard", value=False)
    num_xh = st.number_input("Ext. Hard Qty", min_value=0, max_value=100, value=5, label_visibility="collapsed") if use_xh else 0

total_q = num_mc + num_easy + num_med + num_hard + num_xh
if total_q > 60:
    st.info(f"ℹ️ Large batch requested ({total_q} questions). The AI may take 30–60 seconds to draft all questions and answers.")

st.markdown("### 🖨️ Format & Spacing")
layout_mode = st.radio("Layout:", ["Worksheet (No working out space)", "Exam (Space for working out)"], horizontal=True, label_visibility="collapsed")

safe_topic = topic[0] if isinstance(topic, list) and len(topic) > 0 else (topic if isinstance(topic, str) else "")

combined_samples = []
msgs = []
for y in year_group:
    s, m = load_style_samples(y, subject, actual_level, safe_topic if available_topics else "")
    if s:
        combined_samples.append(s)
        msgs.append(f"{y}: {m.replace('✅ ', '').replace('⚠️ ', '')}")

style_samples = "\n\n---\n\n".join(combined_samples)[:10000]
source_msg = "✅ " + " | ".join(msgs) if combined_samples else "❌ No samples found."

with st.sidebar:
    st.markdown("---")
    st.markdown("📂 **File Tracker**")
    if "✅" in source_msg:
        st.success(source_msg)
    elif "⚠️" in source_msg:
        st.warning(source_msg)
    else:
        st.error(source_msg)
    st.caption(f"Searching in: {', '.join(year_group) if year_group else 'None'}")

_SS_KEYS = (
    "questions_text", "answers_text", "solutions_text", "pdf_bytes", "tex_bytes", "word_bytes",
    "word_skip_reason", "compiler_log", "meta_topic", "meta_subject", "meta_year", "meta_diff",
    "meta_n", "meta_set", "cloud_saved", "display_topic", "meta_mc", "meta_easy", "meta_med",
    "meta_hard", "meta_xh", "meta_input_tokens", "meta_output_tokens", "meta_model_used",
    "meta_total_cost", "meta_extra_instructions", "used_search", "phase_1_raw", "saved_ai_payload", "work_dir",
    "saved_exam_id", "saved_has_solutions",
)
for _key in _SS_KEYS:
    if _key not in st.session_state:
        st.session_state[_key] = None

st.markdown("---")
btn_col1, btn_col2 = st.columns([5, 1])
with btn_col1:
    generate_btn = st.button("✨ It's go time!", type="primary", use_container_width=True)
with btn_col2:
    if st.button("🗑 Clear", use_container_width=True):
        _old_wd = st.session_state.get("work_dir")
        if _old_wd and os.path.isdir(_old_wd):
            shutil.rmtree(_old_wd, ignore_errors=True)
        for _key in _SS_KEYS:
            st.session_state[_key] = None
        st.rerun()

if generate_btn:
    is_topic_empty = not topic or (isinstance(topic, str) and not topic.strip())
    has_upload = bool(uploaded_photos) if isinstance(uploaded_photos, list) else (uploaded_photos is not None)
    if not year_group:
        st.error("🚨 Action Required: Please select at least one **Year** group from the dropdown above.")
    elif is_topic_empty and not has_upload:
        st.error("🚨 Action Required: Please enter a **Topic**, or upload photo(s) for the AI to extract.")
    elif total_q == 0:
        st.error("🚨 Action Required: Please select at least one question to generate.")
    else:
        client = _get_genai_client()

        if is_topic_empty and has_upload:
            clean_topic = "Topics from Attached Document"
            exam_focus = "the exact topics, syllabus outcomes, and areas assessed in the attached document(s)"
        else:
            topic_list = topic if isinstance(topic, list) else [topic]
            clean_topic = ", ".join([re.sub(r"^\d+[\.\-]\s*", "", t).strip() for t in topic_list])
            if sub_topic:
                safe_sub = sub_topic.replace("/", "-").replace("\\", "-")
                clean_topic = f"{clean_topic} - {safe_sub}"
            exam_focus = f"{clean_topic}" + (f" - specifically focusing on: {sub_topic}" if sub_topic else "")

        grades_string = ", ".join(year_group)
        if set_number_input > 0:
            current_set_number = int(set_number_input)
        else:
            current_set_number = get_next_set_number(subject, grades_string, actual_level, clean_topic)
        display_topic = f"{clean_topic} Set {current_set_number}"

        style_block = f"STYLE & SYLLABUS REFERENCE:\n{style_samples}\n\n" if style_samples else ""
        custom_instructions_block = f"EXTRA TUTOR INSTRUCTIONS:\n{extra_instructions}\n\n" if extra_instructions.strip() else ""

        dist_req = []
        if num_mc > 0:
            dist_req.append(f"{num_mc} Multiple Choice questions")
        total_fr = num_easy + num_med + num_hard + num_xh
        if num_easy > 0:
            dist_req.append(f"{num_easy} Easy Free-Response questions")
        if num_med > 0:
            dist_req.append(f"{num_med} Medium Free-Response questions")
        if num_hard > 0:
            dist_req.append(f"{num_hard} Hard Free-Response questions")
        if num_xh > 0:
            dist_req.append(f"{num_xh} Extremely Hard Free-Response questions")

        has_mc = num_mc > 0
        has_fr = total_fr > 0

        if "Exam" in layout_mode:
            layout_instruction = "CRITICAL SPACING & PAGE-BREAK RULE: Add `\\vspace*{4cm}` (or more) after EVERY Free-Response question and `\\vspace*{2.5cm}` after sub-questions. ALWAYS place the mark indicator (e.g., `\\hfill \\textbf{(2 marks)}`) directly at the end of the question text or equation, followed immediately by the `\\vspace*`. Keep each question and its space together."
        else:
            layout_instruction = "CRITICAL SPACING RULE: Add exactly `\\vspace{0.5cm}` after EVERY question and sub-question."

        level_text = f" {actual_level}" if actual_level else ""

        template_f = ""
        auto_name_rule = ""
        if has_upload:
            template_f = "===FILENAME_START===\nSchool_Subject_Year_Level_AssessmentOrTopic\n===FILENAME_END===\n"
            auto_name_rule = "12. AUTO-NAMING & TOPIC EXTRACTION: You MUST inspect the attached document(s) and extract the actual School Name (e.g. Bonnyrigg High School), Subject (e.g. Maths), Year Group (e.g. Year 9), and official Assessment Title / Term (e.g. Term 3 Exam, Assessment Task 2, Half Yearly). Format EXACTLY like this: School_Subject_Year_Level_AssessmentTitle (e.g. Bonnyrigg_High_School_Maths_Year_9_Acceleration_Term_3_Exam). If no term/exam title is given, use a concise topic summary. Use underscores. Place inside ===FILENAME_START=== and ===FILENAME_END=== tags. Keep this filename identical and consistent across generations for the same document."

        template_c = "===LATEX_CONTENT_START===\n"
        template_a = "===LATEX_ANSWERS_START===\n"

        if has_mc:
            template_c += "\\section*{Section 1}\n\\begin{enumerate}\n    \\item Multiple choice question...\n    \\begin{itemize}\n        \\item[(A)] Option 1\n        \\item[(B)] Option 2\n    \\end{itemize}\n\\end{enumerate}\n\n"
            template_a += "\\section*{Section 1}\n\\begin{enumerate}\n    \\item A\n\\end{enumerate}\n\n"

        if has_fr:
            sec_title = "\\section*{Section 2}" if has_mc else "\\section*{Section 1}"
            template_c += f"{sec_title}\n\\begin{{enumerate}}\n    \\item Single part question... \\hfill \\textbf{{(2 marks)}}\n    \\vspace{{...}}\n\\end{{enumerate}}\n"
            template_a += f"{sec_title}\n\\begin{{enumerate}}\n    \\item $x = 5$\n\\end{{enumerate}}\n\n"

        template_c += "===LATEX_CONTENT_END==="
        template_a += "===LATEX_ANSWERS_END==="

        strict_negatives = ""
        if not has_fr:
            strict_negatives = "CRITICAL: DO NOT GENERATE ANY FREE-RESPONSE QUESTIONS OR A SECTION 2. ONLY GENERATE MULTIPLE CHOICE."
        elif not has_mc:
            strict_negatives = "CRITICAL: DO NOT GENERATE ANY MULTIPLE CHOICE QUESTIONS."

        # ---> UPGRADED: Dynamic Post-2019 NESA Syllabus Guardrails <---
        syllabus_ban = ""
        if subject == "Maths":
            universal_ban = (
                " CRITICAL NESA SYLLABUS BANS (ALL MATHS COURSES): NEVER generate questions on obsolete pre-2019 topics: "
                "(1) t-method / t-formulae (t = tan(x/2) substitutions), "
                "(2) Product-to-Sum or Sum-to-Product trigonometric identities, "
                "(3) Locus / Focus-Directrix parabola geometry (e.g. x^2 = 4ay, chords of contact, parametric tangents/normals), "
                "(4) Euclidean Circle Geometry proofs (alternate segment, cyclic quadrilaterals, intersecting chords), "
                "(5) Division of an interval in a given ratio (internal/external division), "
                "(6) Simpson's Rule."
            )
            
            level_ban = ""
            if actual_level == "Advanced":
                level_ban = (
                    " CRITICAL ADVANCED LEVEL BANS: "
                    "(1) 3D TRIGONOMETRY IS STRICTLY BANNED — Year 11 & 12 Advanced only covers 2D Trigonometry (Sine rule, Cosine rule, Area of triangle, Radians). 3D Trigonometry is exclusively an Extension 1 topic. "
                    "(2) DO NOT include Extension 1/2 topics: Compound/Double Angles (sin(A+B), cos(2A)), Auxiliary angle method (Rcos(x-a)), Inverse Trig functions, Polynomial division / Remainder theorem / Sum and product of roots, Combinatorics / Permutations & Combinations, Vectors, Projectile Motion, Mathematical Induction, Perpendicular Distance formula, Angle between two lines."
                )
            elif actual_level == "Standard":
                level_ban = (
                    " CRITICAL STANDARD LEVEL BANS: 3D Trigonometry is STRICTLY BANNED. NO Calculus, NO Radians, NO Advanced Polynomials, NO Logarithm laws, NO Compound/Double angle trigonometry. Keep all math strictly within the NSW Mathematics Standard syllabus."
                )
            elif actual_level in ["Extension", "Extension 1"]:
                level_ban = (
                    " EXTENSION 1 SYLLABUS RULES: 3D Trigonometry IS ALLOWED & REQUIRED for 3D trig topics (ME-T1). "
                    "REMEMBER: t-formulae, product-to-sum identities, Euclidean circle proofs, and division of an interval are completely obsolete and strictly banned in Extension 1. "
                    "DO NOT include Extension 2 topics (Complex Numbers, Proof by Contradiction/Contrapositive, Integration by Parts, Volumes by Cylindrical Shells, 3D Vectors, Mechanics)."
                )
            elif actual_level in ["Extension 2"]:
                level_ban = (
                    " EXTENSION 2 SYLLABUS RULES: Follow the post-2019 Mathematics Extension 2 syllabus: Proof (Nature of Proof), Vectors (3D Vectors), Complex Numbers, Calculus (Further Integration, Volumes by Slicing & Cylindrical Shells), Mechanics (Resisted Motion, Simple Harmonic Motion). DO NOT include obsolete pre-2019 topics like Conics or Harder Circle Geometry."
                )
            
            yr_ban = ""
            if "Year 11" in year_group and "Year 12" not in year_group:
                if actual_level == "Standard":
                    yr_ban = (
                        " YEAR 11 STANDARD BOUNDARIES: DO NOT generate Year 12 Standard topics (Normal Distribution, Z-scores, Annuities, Depreciation, Critical Path Analysis, Networks, Bivariate Data). DO NOT require manual calculation of standard deviation."
                    )
                elif actual_level == "Advanced":
                    yr_ban = (
                        " YEAR 11 ADVANCED BOUNDARIES: DO NOT generate Year 12 Advanced topics: NO Integration / Area under curves / Volumes of revolution, NO Financial Mathematics (Superannuation, Annuities, Series loan repayments), NO Continuous Random Variables / Normal Distribution, NO Bivariate Data Analysis, NO Calculus applications to exponentials or trigonometrics. Year 11 differentiation is strictly limited to First Principles, Power Rule on polynomials, and basic Tangents/Normals."
                    )
                elif actual_level in ["Extension", "Extension 1"]:
                    yr_ban = (
                        " YEAR 11 EXTENSION 1 BOUNDARIES: DO NOT generate Year 12 Extension 1 topics: NO Proof by Mathematical Induction, NO Projectile Motion (Vectors), NO Trigonometric Equations via Auxiliary Angle Rcos(x-a), NO Differential Equations / Exponential Growth, NO Calculus of Inverse Trig Functions, NO Binomial Distribution / Normal Approximation. Year 11 Ext 1 is strictly limited to Functions & Polynomials, Further Trigonometry (3D Trig, Compound/Double angles), Permutations & Combinations, Vectors in 2D, and Rates of Change."
                    )
                    
            syllabus_ban = f"11. SYLLABUS STRICTNESS (CRITICAL): Strictly adhere to the post-2019 NESA {grades_string} {actual_level} syllabus.{universal_ban}{level_ban}{yr_ban}"
        else:
            syllabus_ban = f"11. SYLLABUS STRICTNESS (CRITICAL): Strictly adhere to the post-2019 NESA {grades_string} {subject} syllabus. Ensure all diagrams, notations, reaction rates, equilibrium graphs, and scientific concepts follow official NSW HSC curriculum standards."

        prompt = f"""You are a NESA Examiner.
Generate an examination on "{exam_focus}" for {grades_string}{level_text} in {subject}.

CRITICAL QUESTION DISTRIBUTION (OBEY EXACTLY): 
- You MUST generate EXACTLY {total_q} questions in total.
- Breakdown: {", ".join(dist_req)}
- {strict_negatives}

{style_block}

EXAMINER RULES:
1. SPELLING: Australian/UK English.
2. MARKS: 1 to 3 marks per free-response. Reserve 3-marks for extreme difficulty. Make questions demanding.
3. SUB-QUESTIONS: Use a nested `\\begin{{enumerate}}` environment so they format as (a), (b), (c).
4. ANSWERS PAGE: Do NOT use display math (`\\[ ... \\]` or `$$...$$`) for the answers. Use inline math (`$...$`) so all answers naturally align left.
5. {layout_instruction}
6. "MISSING \ITEM" CRASH PREVENTION: Immediately after starting a `\\begin{{enumerate}}` or `\\begin{{itemize}}`, the next command MUST be a valid `\\item` (e.g., `\\item` or `\\item[(A)]`). NEVER place `\\vspace`, text, or blank lines before the first item. Place all spacing commands AFTER the item text.
7. SECTION TITLES: Name each section purely as "Section 1", "Section 2", etc. You MUST use the exact standard syntax `\\section*{{Section X}}`. CRITICAL: DO NOT add difficulty labels like "Easy Questions", "Medium Questions", or "Hard" to the section titles.
8. MULTIPLE CHOICE: You MUST heavily randomize the correct answer options (A, B, C, D) across the multiple-choice section. The correct answer must NOT always be 'A'.
9. PYTHON CALCULATOR SANDBOX (CRITICAL): You are equipped with a Python Code Execution tool. You MUST use it to calculate exact final decimal answers for strictly numeric topics like Financial Mathematics, Compound Interest, Annuities, or Statistics. 
CRITICAL EXCEPTIONS: 
- DO NOT use the Python tool for pure algebraic, calculus, or trigonometric topics (e.g., Parametrics, Inverse Functions, Polynomials, Integration). Evaluate symbolic algebra using your own internal reasoning.
10. ATTACHED DOCUMENT & QUESTION CLONING RULE (CRITICAL):
    - ASSESSMENT NOTIFICATION / SCOPE & SEQUENCE: If the attached document is an Assessment Notification or list of topics/outcomes (e.g., lists Probability, Data Analysis, Surface Area, Volume, etc.), you MUST strictly and exclusively generate questions on the exact topics and skills listed in that document. Distribute questions across all listed areas. DO NOT generate questions on unlisted topics (such as Algebra, Indices, or Financial Maths if they are not listed in the notification).
    - QUESTION EXEMPLARS: If sample questions or past worksheet items are provided, reverse-engineer their mathematical mechanics, formatting phrasing, structural complexity, and cognitive depth to generate original parallel practice questions.
11. CRITICAL OUTPUT FORMAT & NO COMMENTS: You must output ONLY the raw content. 
    - Do NOT generate \\documentclass, \\usepackage, \\begin{{document}}, \\end{{document}}, or \\geometry.
    - Provide raw LaTeX code that starts immediately with \\section* or \\begin{{enumerate}}.
    - NEVER use the `%` symbol to write hidden code comments (e.g. `% Vertex` or `% Graph starts here`). The system automatically escapes all `%` symbols into `\\%`, so if you place them inside option brackets or coordinate lists, the `pgfplots` compiler will fatally crash trying to read them as math. Only use `%` for actual mathematical percentages (e.g., 50%).
12. MANDATORY ENVIRONMENT CLOSING & BRACES: Every single `\\begin{{enumerate}}`, `\\begin{{itemize}}`, `\\begin{{align*}}`, or `\\begin{{tikzpicture}}` MUST have a matching `\\end{{...}}` tag with exact curly braces `{{...}}` (NEVER write angle brackets like `\\end{{enumerate>` or omit closing braces). You must meticulously check that no environments are left open or malformed, as unclosed environments will crash the compiler.
{syllabus_ban}
{auto_name_rule}

DIAGRAM RULES (CRITICAL):
You have TWO graphing engines available. You MUST choose the correct one based on the diagram type.

ENGINE 1: THE PYTHON ENGINE
Use this strictly for continuous 2D Cartesian functions, derivatives, Projectile Motion trajectories, Slope Fields, and Normal Distributions.
Syntax:
GRAPH_START
type: slope_field
expr: x + y
xmin: -5
xmax: 5
ymin: -5
ymax: 5
GRAPH_END

ENGINE 2: NATIVE TikZ
Use this strictly for structural geometry: Networks, Critical Paths, 3D Trig diagrams, 3D Vectors, Forces/Inclined Planes, and Box Plots. The compiler has `\\usepackage{{tikz}}` installed (do NOT use pgfplots). Inject your `\\begin{{tikzpicture}}` code directly into the LaTeX output.

- BOX PLOTS & NUMBER LINES (CRITICAL):
  * NEVER draw box plots using raw unscaled data coordinates if they exceed 12cm (e.g., coordinates like `(20,0) -- (100,0)` without scaling create a 100cm wide diagram that runs off the page!).
  * ALWAYS scale your TikZ box plots so the entire number line spans between 10cm and 13cm in total width. Use `xscale` on the tikzpicture:
    - If scores range 0 to 100, use `\\begin{{tikzpicture}}[xscale=0.12]`.
    - If data ranges 10 to 30, use `\\begin{{tikzpicture}}[xscale=0.6]`.
  * For parallel box plots: Place group labels (e.g., 'Greenhouse A', 'Greenhouse B' or 'Method 1', 'Method 2') cleanly above the plots or with `node[left]`, ensuring they do not push the axis past the right page margin.

CRITICAL GRAPHING REQUIREMENT:
Whenever a question asks the student to "sketch" or "draw" a graph, you MUST provide the actual rendered graph in the Answers sections using the LaTeX `pgfplots` package. 
- You must code the visual plot using \\begin{{tikzpicture}} \\begin{{axis}}[...] ... \\end{{axis}} \\end{{tikzpicture}}. 
- NEVER just describe the graph in text. You must mathematically plot the curves, asymptotes, and intercepts using pgfplots.
- NEVER use the setting `trig format plots=none` in your axis options. It does not exist and will crash the compiler. If plotting trigonometric functions, use `trig format plots=rad` or omit the setting entirely.
- PREVENT DIMENSION ERRORS: When plotting rational functions or graphs with vertical asymptotes, you MUST restrict the vertical plotting domain to prevent "Dimension too large" LaTeX crashes. You must include `ymin=-10, ymax=10` (or appropriate limits) inside the \\begin{{axis}}[...] options, AND you must include `restrict y to domain=-15:15` inside the \\addplot[...] options to safely clip the asymptotes.
- MANDATORY SEMICOLONS: Every single drawing command inside the axis environment MUST end with a semicolon (;). Do not forget the semicolon, or the LaTeX compiler will crash.
- TIKZ LABELS AND ANCHORS SECURING: When creating labels or polar positioning elements in TikZ, you MUST use explicit standard syntax (e.g., label=90:{{$P_1$}}). NEVER use shorthand styles like [90:P_1] directly inside bracket options, as this will trigger a fatal pgfkeys compiler crash.
- TIKZ SYNTAX CRASH PREVENTION: NEVER place raw text, descriptions, or unformatted comments directly inside a `\\draw` or `\\addplot` command path. If you need to add text to a diagram, you MUST use a properly formatted `\\node` at a specific coordinate (e.g., `\\node at (2,4) {{Text}};`).
- STRICT COORDINATE FORMATTING (CRITICAL): When listing points in `\\addplot coordinates {{...}};`, you MUST ONLY output the raw coordinate pairs. NEVER add text, labels, or `%` comments next to the points. For example, writing `(0,5) % Y-intercept` or `(2,9) % Vertex` will fatally crash the compiler. Output only the pure coordinates: `(0,5) (2,9) (5,0)`.
- CHEMISTRY & SCIENCE EQUILIBRIUM GRAPHS (CRITICAL):
  * INSTANTANEOUS DISTURBANCES AT $t_1$: In Reaction Rate vs Time and Concentration vs Time graphs, whenever an instantaneous disturbance occurs (e.g. adding or removing a reactant/product, or an instantaneous pressure/volume change), the sudden jump MUST be drawn with a solid vertical line at $t_1$ connecting the pre-disturbance baseline directly to the new instantaneous peak value (e.g., `\\draw[thick] (t1, base_y) -- (t1, jump_y);`). NEVER leave a floating gap or disconnected curve starting in mid-air.
  * GRADUAL RESPONSES: For gradual changes (e.g. reverse rate responding over time, temperature changes, or concentrations shifting toward a new equilibrium between $t_1$ and $t_2$), use smooth continuous curves.
  * NO TEXT-LINE OVERLAPPING: Never let plot lines pass directly through text labels (such as 'Forward rate', 'Reverse rate', or chemical formulas). Always position text labels cleanly above or below curves using `node[above right]`, `node[above]`, `node[below]`, or `fill=white, inner sep=1.5pt`.

{custom_instructions_block}
When instructed, your final combined output must follow this template structure exactly:
{template_f}

{template_c}

{template_a}
"""

        logo_html = ""
        if os.path.exists(LOGO_PATH):
            try:
                logo_b64 = base64.b64encode(open(LOGO_PATH, "rb").read()).decode()
                logo_html = f'<img src="data:image/png;base64,{logo_b64}" style="animation: pulse 1.5s infinite ease-in-out; width: 120px; height: auto;" />'
            except Exception:
                logo_html = '<h1 style="font-size: 80px;">✨</h1>'
        else:
            logo_html = '<h1 style="font-size: 80px;">✨</h1>'

        loading_placeholder = st.empty()
        with loading_placeholder.container():
            st.markdown(
                f"""
                <div style="text-align: center; padding: 40px 20px;">
                    {logo_html}
                    <h4 style="color: #1A3A8A; margin-top: 20px; font-weight: bold;">✨ Drafting Worksheet...</h4>
                    <p style="color: #666; font-size: 15px;">Fast-compiling questions and short answers for {display_topic}.</p>
                </div>
                <style>
                    [data-testid="stStatusWidget"] {{ display: none !important; }}
                    @keyframes pulse {{
                        0% {{ transform: scale(0.95); opacity: 0.6; }}
                        50% {{ transform: scale(1.05); opacity: 1; }}
                        100% {{ transform: scale(0.95); opacity: 0.6; }}
                    }}
                </style>
                """,
                unsafe_allow_html=True,
            )

        try:
            max_retries = 3
            out = None
            for attempt in range(max_retries):
                try:
                    live_search_subjects = ["Business Studies", "Legal Studies", "Science"]

                    active_tools = [types.Tool(code_execution=types.ToolCodeExecution())]
                    if subject in live_search_subjects and use_live_search:
                        active_tools.append(types.Tool(google_search=types.GoogleSearch()))

                    gen_config = types.GenerateContentConfig(
                        max_output_tokens=MAX_OUTPUT_TOKENS,
                        tools=active_tools,
                        safety_settings=[
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                        ],
                    )

                    ai_payload = [prompt]
                    if exemplar_questions.strip():
                        ai_payload.append(f"\n\n[CLONE EXEMPLARS - TEXT INPUT]:\n{exemplar_questions}")
                    if has_upload:
                        photos_list = uploaded_photos if isinstance(uploaded_photos, list) else [uploaded_photos]
                        for photo in photos_list:
                            doc_bytes = photo.getvalue()
                            fname = photo.name.lower()
                            is_pdf = fname.endswith(".pdf") or (getattr(photo, "type", "") == "application/pdf")

                            if is_pdf:
                                # 1. Convert PDF pages to JPEG images (compatible with Gemini code_execution)
                                rendered_any = False
                                try:
                                    import pypdfium2 as pdfium
                                    pdf = pdfium.PdfDocument(doc_bytes)
                                    for page in pdf:
                                        pil_image = page.render(scale=2.0).to_pil()
                                        buf = io.BytesIO()
                                        pil_image.save(buf, format="JPEG", quality=85)
                                        ai_payload.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"))
                                    rendered_any = True
                                except Exception:
                                    pass

                                if not rendered_any:
                                    try:
                                        reader = PdfReader(io.BytesIO(doc_bytes))
                                        for page in reader.pages:
                                            for img_file in page.images:
                                                img_name = img_file.name.lower()
                                                img_mime = "image/png" if img_name.endswith(".png") else "image/jpeg"
                                                ai_payload.append(types.Part.from_bytes(data=img_file.data, mime_type=img_mime))
                                                rendered_any = True
                                    except Exception:
                                        pass

                                # 2. Extract text if available from PDF
                                try:
                                    reader = PdfReader(io.BytesIO(doc_bytes))
                                    pdf_text = "\n".join([page.extract_text() or "" for page in reader.pages]).strip()
                                    if pdf_text:
                                        ai_payload.append(f"\n\n[EXTRACTED PDF TEXT FROM {photo.name}]:\n{pdf_text}")
                                except Exception:
                                    pass
                            else:
                                # Direct image upload (PNG, JPEG, WEBP)
                                mime_type = "image/png" if fname.endswith(".png") else ("image/webp" if fname.endswith(".webp") else "image/jpeg")
                                ai_payload.append(types.Part.from_bytes(data=doc_bytes, mime_type=mime_type))

                        ai_payload.append(
                            "\n\n[ATTACHED DOCUMENTS INSTRUCTION - HIGHEST PRIORITY]:\n"
                            "Carefully analyze ALL attached documents/pages:\n"
                            "1. IF THE ATTACHMENTS ARE AN ASSESSMENT NOTIFICATION, EXAM NOTICE, OR TOPIC LIST (e.g. lists topics, areas assessed, outcomes, syllabus dot points across one or more pages):\n"
                            "   - You MUST inspect ALL attached pages/images and extract EVERY topic, sub-topic, and skill specified in the notification.\n"
                            "   - Your generated exam MUST STRICTLY AND EXCLUSIVELY focus on the topics, sub-topics, and syllabus outcomes listed across all uploaded pages. Distribute the requested number of questions across all assessed areas.\n"
                            "   - DO NOT generate questions on unlisted topics (for example, if the notification covers Probability, Statistics, and Volume, DO NOT generate Algebra, Indices, or Financial Mathematics).\n"
                            "   - Extract the School Name, Subject, Year Group, and Topic Summary to format the filename in ===FILENAME_START=== tags (e.g. Bonnyrigg_High_School_Maths_Year_9_Probability_Data_Analysis_Volume).\n"
                            "2. IF THE ATTACHMENTS CONTAIN SAMPLE / EXEMPLAR QUESTIONS OR A WORKSHEET:\n"
                            "   - Reverse-engineer the mechanics, phrasing, difficulty, and diagram styles of those exact questions, and generate original parallel practice questions testing the same competency."
                        )

                    models_to_try = ["gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
                    last_error = None

                    for model_name in models_to_try:
                        try:
                            st.toast(f"🏃 {model_name}: Drafting Questions & Answers...")
                            p1_payload = ai_payload + [
                                "\n\nPHASE 1 INSTRUCTION: You must generate the exam questions AND the answer key. First, generate the questions between ===LATEX_CONTENT_START=== and ===LATEX_CONTENT_END===. Then, immediately generate the short answers between ===LATEX_ANSWERS_START=== and ===LATEX_ANSWERS_END===. Do NOT generate fully worked solutions."
                            ]
                            res1 = client.models.generate_content(model=model_name, contents=p1_payload, config=gen_config)

                            if not res1 or not res1.text or "LATEX_ANSWERS_END" not in res1.text:
                                raise ValueError("Phase 1 returned truncated or empty text. Forcing retry...")

                            st.toast(f"✅ Preview Ready! Engine used: {model_name}")
                            st.session_state.meta_model_used = model_name

                            out = res1.text.replace("```latex", "").replace("```", "")

                            st.session_state.phase_1_raw = out
                            st.session_state.saved_ai_payload = ai_payload

                            if res1.usage_metadata:
                                st.session_state.meta_input_tokens = getattr(res1.usage_metadata, "prompt_token_count", 0)
                                st.session_state.meta_output_tokens = getattr(res1.usage_metadata, "candidates_token_count", 0)
                            else:
                                st.session_state.meta_input_tokens = 0
                                st.session_state.meta_output_tokens = 0

                            st.session_state.used_search = (subject in live_search_subjects and use_live_search)
                            break
                        except Exception as e:
                            last_error = e
                            if _is_retryable(e):
                                st.toast(f"🚦 {model_name} unavailable. Pivoting to next model...")
                                continue
                            else:
                                raise e

                    if not out:
                        raise last_error
                    break
                except Exception as e:
                    if _is_retryable(e) and attempt < max_retries - 1:
                        st.toast(f"AI Hiccup. Retrying automatically... (Attempt {attempt + 1}/{max_retries})")
                        time.sleep(3)
                        continue
                    else:
                        raise e

            if not out:
                raise Exception("Failed to generate content after multiple attempts.")

            fn_m = re.search(r"===?\s*FILENAME_START\s*===?(.*?)===?\s*FILENAME_END\s*===?", out, re.DOTALL | re.IGNORECASE)
            if fn_m and fn_m.group(1).strip():
                ai_generated_name = re.sub(r"[^A-Za-z0-9_\-\(\) ]", "_", fn_m.group(1).strip())
                clean_topic = ai_generated_name.replace("_", " ")
                if set_number_input > 0:
                    current_set_number = int(set_number_input)
                else:
                    current_set_number = get_next_set_number(subject, grades_string, actual_level, clean_topic)
                display_topic = f"{clean_topic} Set {current_set_number}"
            else:
                if set_number_input > 0:
                    current_set_number = int(set_number_input)
                else:
                    current_set_number = get_next_set_number(subject, grades_string, actual_level, clean_topic)
                display_topic = f"{clean_topic} Set {current_set_number}"

            c_m = re.search(r"===?\s*LATEX_CONTENT_START\s*===?(.*?)(?:===?\s*LATEX_CONTENT_END\s*===?|===?\s*LATEX_ANSWERS_START|$)", out, re.DOTALL | re.IGNORECASE)
            a_m = re.search(r"===?\s*LATEX_ANSWERS_START\s*===?(.*?)(?:===?\s*LATEX_ANSWERS_END\s*===?|$)", out, re.DOTALL | re.IGNORECASE)

            q_sanitized = sanitize_ai_latex(c_m.group(1).strip() if c_m else "Failed.")
            a_sanitized = sanitize_ai_latex(a_m.group(1).strip() if a_m else "Failed.")

            title = f"{grades_string} {subject}{level_text}".strip()
            work_dir = _start_new_work_dir()

            p_bytes, t_bytes, w_bytes, log, word_reason = _render_exam_files(
                work_dir, display_topic, title, q_sanitized, a_sanitized, "", total_q
            )

            st.session_state.update({
                "questions_text": q_sanitized,
                "answers_text": a_sanitized,
                "solutions_text": "",
                "pdf_bytes": p_bytes,
                "tex_bytes": t_bytes,
                "word_bytes": w_bytes,
                "word_skip_reason": word_reason,
                "compiler_log": log,
                "meta_topic": clean_topic,
                "meta_subject": subject,
                "meta_year": grades_string,
                "meta_diff": actual_level,
                "meta_n": total_q,
                "meta_set": current_set_number,
                "display_topic": display_topic,
                "cloud_saved": False,
                "saved_exam_id": None,
                "saved_has_solutions": False,
                "meta_mc": num_mc,
                "meta_easy": num_easy,
                "meta_med": num_med,
                "meta_hard": num_hard,
                "meta_xh": num_xh,
                "meta_extra_instructions": extra_instructions.strip() if extra_instructions else "",
            })

            loading_placeholder.empty()

        except Exception as e:
            loading_placeholder.empty()
            st.error(f"⚠️ {friendly_error_message(e)}")
            with st.expander("🛠️ Technical details (for tech support)"):
                st.code(str(e))

if st.session_state.questions_text:
    _t, _s, _y, _d, _set, _disp = (
        st.session_state.meta_topic,
        st.session_state.meta_subject,
        st.session_state.meta_year,
        st.session_state.meta_diff,
        st.session_state.meta_set,
        st.session_state.display_topic,
    )

    st.markdown("---")
    lvl_str = f" {_d}" if _d else ""

    p_col1, p_col2 = st.columns([4, 1], vertical_alignment="center")
    with p_col1:
        st.markdown(f"### {_disp} ({_y}{lvl_str})")
        if st.session_state.get("meta_extra_instructions"):
            st.caption(f"💬 **Extra Instructions:** {st.session_state.meta_extra_instructions}")
    with p_col2:
        edit_set = st.number_input(
            "Set #",
            min_value=1,
            value=int(_set or 1),
            key="preview_edit_set",
            help="Change the set number before saving to Cloud Library if needed.",
        )
        if edit_set != _set:
            st.session_state.meta_set = edit_set
            st.session_state.display_topic = f"{_t} Set {edit_set}"
            title = f"{_y} {_s}{lvl_str}".strip()
            work_dir = _get_or_create_work_dir()
            p_bytes, t_bytes, w_bytes, log, word_reason = _render_exam_files(
                work_dir,
                st.session_state.display_topic,
                title,
                st.session_state.questions_text,
                st.session_state.answers_text,
                st.session_state.solutions_text or "",
                st.session_state.meta_n,
            )
            st.session_state.pdf_bytes = p_bytes
            st.session_state.tex_bytes = t_bytes
            st.session_state.word_bytes = w_bytes
            st.session_state.cloud_saved = False
            st.rerun()

    in_tok = st.session_state.meta_input_tokens or 0
    out_tok = st.session_state.meta_output_tokens or 0
    model_used = st.session_state.meta_model_used or "Unknown"

    model_lower = model_used.lower()
    if "3.7-flash" in model_lower or "3.8-flash" in model_lower:
        in_cost = (in_tok / 1_000_000) * 0.75
        out_cost = (out_tok / 1_000_000) * 3.75
    elif "flash-lite" in model_lower:
        in_cost = (in_tok / 1_000_000) * 0.10
        out_cost = (out_tok / 1_000_000) * 0.40
    elif "2.5-flash" in model_lower or "3.5-flash" in model_lower or "flash" in model_lower:
        in_cost = (in_tok / 1_000_000) * 0.30
        out_cost = (out_tok / 1_000_000) * 2.50
    elif "pro" in model_lower:
        in_cost = (in_tok / 1_000_000) * 1.50
        out_cost = (out_tok / 1_000_000) * 6.00
    else:
        in_cost = (in_tok / 1_000_000) * 0.75
        out_cost = (out_tok / 1_000_000) * 3.75

    search_cost = 0.014 if st.session_state.used_search else 0.00
    total_cost = in_cost + out_cost + search_cost
    st.session_state["meta_total_cost"] = total_cost

    if st.session_state.get("admin_pin") == "DA_ADMIN":
        search_badge = " &nbsp;&nbsp;|&nbsp;&nbsp; 🌍 *Live Web Search*" if st.session_state.used_search else ""
        st.caption(f"**💸 Generation Cost:** ${total_cost:.5f} &nbsp;&nbsp;|&nbsp;&nbsp; **Engine:** `{model_used}` &nbsp;&nbsp;|&nbsp;&nbsp; **Tokens:** {in_tok:,} In / {out_tok:,} Out{search_badge}")
        st.caption("ℹ️ Per-token pricing above may drift from Google's current published rates — treat this as an estimate.")

    if not st.session_state.solutions_text:
        st.info("💡 **Preview Ready!** The Worksheet and Answer Key have been drafted. You can download them now, or generate the step-by-step solutions to append to the document.")
        if st.button("🧠 Generate Fully Worked Solutions", type="primary", use_container_width=True):
            with st.spinner("🤖 Calculating deep step-by-step mathematical solutions..."):
                try:
                    client = _get_genai_client()

                    active_tools = [types.Tool(code_execution=types.ToolCodeExecution())]
                    if st.session_state.used_search:
                        active_tools.append(types.Tool(google_search=types.GoogleSearch()))

                    gen_config = types.GenerateContentConfig(
                        max_output_tokens=MAX_OUTPUT_TOKENS,
                        tools=active_tools,
                        safety_settings=[
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                        ],
                    )

                    p2_payload = st.session_state.saved_ai_payload + [
                        st.session_state.phase_1_raw,
                        "\n\nPHASE 2 INSTRUCTION: Excellent. Now, generate the step-by-step fully worked solutions for these EXACT questions. You must place your entire output between the tags ===LATEX_SOLUTIONS_START=== and ===LATEX_SOLUTIONS_END===. Group trivial algebra. Show all key mathematical steps.",
                    ]

                    models_to_try = ["gemini-3.7-flash", "gemini-3.5-flash", "gemini-2.5-flash"]
                    s_out = None
                    last_error = None
                    for model_name in models_to_try:
                        try:
                            st.toast(f"🧠 {model_name}: Solving equations...")
                            res2 = client.models.generate_content(model=model_name, contents=p2_payload, config=gen_config)

                            if not res2 or not res2.text or "LATEX_SOLUTIONS_END" not in res2.text:
                                raise ValueError("Solutions truncated. Retrying...")

                            s_out = res2.text.replace("```latex", "").replace("```", "")

                            if res2.usage_metadata:
                                st.session_state.meta_input_tokens += getattr(res2.usage_metadata, "prompt_token_count", 0)
                                st.session_state.meta_output_tokens += getattr(res2.usage_metadata, "candidates_token_count", 0)
                            break
                        except Exception as e:
                            last_error = e
                            if _is_retryable(e):
                                st.toast(f"🚦 {model_name} busy. Pivoting...")
                                continue
                            else:
                                raise e

                    if s_out:
                        s_m = re.search(r"===?\s*LATEX_SOLUTIONS_START\s*===?(.*?)(?:===?\s*LATEX_SOLUTIONS_END\s*===?|$)", s_out, re.DOTALL | re.IGNORECASE)
                        s_sanitized = sanitize_ai_latex(s_m.group(1).strip() if s_m else "Failed.")
                        st.session_state.solutions_text = s_sanitized

                        title = f"{st.session_state.meta_year} {st.session_state.meta_subject}{lvl_str}".strip()
                        work_dir = _get_or_create_work_dir()

                        p_bytes, t_bytes, w_bytes, log, word_reason = _render_exam_files(
                            work_dir, st.session_state.display_topic, title,
                            st.session_state.questions_text, st.session_state.answers_text, s_sanitized,
                            st.session_state.meta_n,
                        )

                        st.session_state.pdf_bytes = p_bytes
                        st.session_state.tex_bytes = t_bytes
                        st.session_state.word_bytes = w_bytes
                        st.session_state.word_skip_reason = word_reason
                        st.session_state.compiler_log = log
                        st.rerun()
                    else:
                        st.error(f"⚠️ {friendly_error_message(last_error or Exception('No response from the AI.'))}")
                except Exception as e:
                    st.error(f"⚠️ {friendly_error_message(e)}")
                    with st.expander("🛠️ Technical details (for tech support)"):
                        st.code(str(e))

    # ── CLOUD LIBRARY SAVE / OVERWRITE ──────────────────────────────────────────
    if not st.session_state.cloud_saved:
        save_btn_label = "💾 Save Exam (with Solutions) to Cloud Library" if st.session_state.solutions_text else "💾 Save Exam to Cloud Library"
        if st.button(save_btn_label, type="secondary"):
            with st.spinner("☁️ Archiving to database..."):
                success, save_result = save_to_supabase(
                    _t, _s, _y, _d, _set, st.session_state.pdf_bytes, st.session_state.word_bytes,
                    num_mc=st.session_state.meta_mc or 0, num_easy=st.session_state.meta_easy or 0,
                    num_med=st.session_state.meta_med or 0, num_hard=st.session_state.meta_hard or 0,
                    num_xh=st.session_state.meta_xh or 0,
                    existing_id=st.session_state.saved_exam_id,
                    cost=st.session_state.get("meta_total_cost"),
                    model=st.session_state.get("meta_model_used"),
                    extra_instructions=st.session_state.get("meta_extra_instructions", ""),
                    created_by=st.session_state.get("user_display_name", "Senior Teacher"),
                )
                if success:
                    st.session_state.cloud_saved = True
                    st.session_state.saved_exam_id = save_result
                    st.session_state.saved_has_solutions = bool(st.session_state.solutions_text)
                    st.rerun()
                else:
                    st.error("⚠️ Couldn't save this exam to the cloud library. Please try again, or contact tech support if it keeps happening.")
                    with st.expander("🛠️ Technical details (for tech support)"):
                        st.code(str(save_result))
    else:
        if st.session_state.solutions_text and not st.session_state.saved_has_solutions:
            st.info("💡 **Worked solutions have been generated!** This exam was previously saved to your Library without solutions. Click below to overwrite and update the library version with the fully worked solutions.")
            if st.button("💾 Save & Overwrite Exam in Cloud Library (with Solutions)", type="primary"):
                with st.spinner("☁️ Updating and overwriting in database..."):
                    success, save_result = save_to_supabase(
                        _t, _s, _y, _d, _set, st.session_state.pdf_bytes, st.session_state.word_bytes,
                        num_mc=st.session_state.meta_mc or 0, num_easy=st.session_state.meta_easy or 0,
                        num_med=st.session_state.meta_med or 0, num_hard=st.session_state.meta_hard or 0,
                        num_xh=st.session_state.meta_xh or 0,
                        existing_id=st.session_state.saved_exam_id,
                        cost=st.session_state.get("meta_total_cost"),
                        model=st.session_state.get("meta_model_used"),
                        extra_instructions=st.session_state.get("meta_extra_instructions", ""),
                        created_by=st.session_state.get("user_display_name", "Senior Teacher"),
                    )
                    if success:
                        st.session_state.cloud_saved = True
                        st.session_state.saved_exam_id = save_result
                        st.session_state.saved_has_solutions = True
                        st.rerun()
                    else:
                        st.error("⚠️ Couldn't update this exam in the cloud library. Please try again, or contact tech support if it keeps happening.")
                        with st.expander("🛠️ Technical details (for tech support)"):
                            st.code(str(save_result))
        else:
            if st.session_state.saved_has_solutions:
                st.success("✅ Exam (with fully worked solutions) successfully archived to Library!")
            else:
                st.success("✅ Exam successfully archived to Library!")

    st.markdown("---")

    if not st.session_state.pdf_bytes:
        st.error("⚠️ **PDF Compiler Failed**")
        with st.expander("🛠️ View Log"):
            st.code(st.session_state.compiler_log, language="text")
    else:
        b64 = base64.b64encode(st.session_state.pdf_bytes).decode("utf-8")
        canvas_preview_html = f"""
        <div id="pdf-container" style="height: 830px; overflow-y: auto; background-color: #525659; padding: 20px; border-radius: 8px; border: 1px solid #ccc;"></div>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/2.16.105/pdf.min.js"></script>
        <script>
            const pdfData = atob("{b64}");
            const pdfjsLib = window['pdfjs-dist/build/pdf'];
            pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/2.16.105/pdf.worker.min.js';
            const loadingTask = pdfjsLib.getDocument({{data: pdfData}});
            loadingTask.promise.then(pdf => {{
                const container = document.getElementById('pdf-container');
                const canvases = [];
                for (let i = 1; i <= pdf.numPages; i++) {{
                    const canvas = document.createElement('canvas');
                    canvas.style.display = 'block'; canvas.style.margin = '0 auto 20px auto';
                    canvas.style.backgroundColor = '#ffffff'; canvas.style.boxShadow = '0 4px 12px rgba(0,0,0,0.3)';
                    container.appendChild(canvas); canvases.push(canvas);
                }}
                for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {{
                    pdf.getPage(pageNum).then(page => {{
                        const scale = 1.3; const viewport = page.getViewport({{scale: scale}});
                        const canvas = canvases[pageNum - 1];
                        canvas.height = viewport.height; canvas.width = viewport.width;
                        const context = canvas.getContext('2d');
                        page.render({{canvasContext: context, viewport: viewport}});
                    }});
                }}
            }}).catch(err => {{
                document.getElementById('pdf-container').innerHTML = '<div style="color:white; text-align:center; padding-top:40px;">Rendering Preview Failed. Please use the download link below.</div>';
            }});
        </script>
        """
        import streamlit.components.v1 as components
        components.html(canvas_preview_html, height=850)

    dist_parts = []
    if st.session_state.meta_mc:
        dist_parts.append(f"{st.session_state.meta_mc} MC")
    if st.session_state.meta_easy:
        dist_parts.append(f"{st.session_state.meta_easy} Easy")
    if st.session_state.meta_med:
        dist_parts.append(f"{st.session_state.meta_med} Med")
    if st.session_state.meta_hard:
        dist_parts.append(f"{st.session_state.meta_hard} Hard")
    if st.session_state.meta_xh:
        dist_parts.append(f"{st.session_state.meta_xh} Ext Hard")
    dist_str = f" ({', '.join(dist_parts)})" if dist_parts else ""

    yr_short = _y.replace("Year ", "Yr")
    lvl_part = f" {_d}" if _d else ""
    safe_name = f"{_t.replace('/', '_')} Set {_set}{dist_str} - {yr_short} {_s}{lvl_part}"

    dl1, dl2, dl3 = st.columns(3)
    with dl1:
        if st.session_state.pdf_bytes:
            st.download_button("🔴 Download PDF", data=st.session_state.pdf_bytes, file_name=f"{safe_name}.pdf", mime="application/pdf", use_container_width=True, type="primary")
    with dl2:
        st.download_button("📜 Download LaTeX", data=st.session_state.tex_bytes, file_name=f"{safe_name}.tex", use_container_width=True)
    with dl3:
        if st.session_state.word_bytes:
            st.download_button("📄 Download Word Doc", data=st.session_state.word_bytes, file_name=f"{safe_name}.docx", use_container_width=True)

    _word_reason = st.session_state.get("word_skip_reason")
    if _word_reason == "diagram":
        st.caption("📄 Word Doc isn't available for this exam — it includes diagrams that don't convert cleanly to Word. Please use the PDF version.")
    elif _word_reason == "pandoc_missing":
        st.caption("📄 Word Doc export isn't set up on the server yet. Contact tech support to enable it.")
    elif _word_reason == "build_failed":
        st.caption("📄 Word Doc conversion failed for this exam. Please use the PDF version.")