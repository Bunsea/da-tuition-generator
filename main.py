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
import concurrent.futures
import threading
import uuid
import logging
import ast
import operator as _op
from urllib.parse import unquote
from PIL import Image, ImageOps
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


def format_job_sydney_datetime(dt):
    """Returns a tuple of (date_str, time_str) in Australia/Sydney timezone, e.g. ('07 Nov 2026', '12:56 PM')."""
    if not dt:
        return "", ""
    if isinstance(dt, str):
        try:
            clean_ts = dt.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_ts)
        except Exception:
            return "", str(dt)
    elif isinstance(dt, (int, float)):
        try:
            dt = datetime.fromtimestamp(dt, tz=timezone.utc)
        except Exception:
            return "", ""
    if isinstance(dt, datetime):
        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            try:
                from zoneinfo import ZoneInfo
                syd_tz = ZoneInfo("Australia/Sydney")
            except Exception:
                import pytz
                syd_tz = pytz.timezone("Australia/Sydney")
            syd_dt = dt.astimezone(syd_tz)
            return syd_dt.strftime("%d %b %Y"), syd_dt.strftime("%I:%M %p")
        except Exception:
            return dt.strftime("%d %b %Y"), dt.strftime("%I:%M %p")
    return "", str(dt)


def format_job_sydney_time(dt):
    date_str, time_str = format_job_sydney_datetime(dt)
    if date_str and time_str:
        return f"{date_str}, {time_str}"
    return time_str or date_str or (str(dt) if dt else "")


def _prepare_uploaded_image(doc_bytes: bytes, filename: str) -> tuple[bytes, str]:
    """Convert iPhone HEIC/HEIF photos to JPEG for the Gemini image payload."""
    extension = os.path.splitext(filename.lower())[1]
    if extension in (".heic", ".heif"):
        from pillow_heif import register_heif_opener

        register_heif_opener()
        with Image.open(io.BytesIO(doc_bytes)) as image:
            oriented_image = ImageOps.exif_transpose(image)
            output = io.BytesIO()
            oriented_image.convert("RGB").save(output, format="JPEG", quality=90)
        return output.getvalue(), "image/jpeg"

    if extension == ".png":
        return doc_bytes, "image/png"
    if extension == ".webp":
        return doc_bytes, "image/webp"
    return doc_bytes, "image/jpeg"


def _safe_dt_timestamp(dt) -> float:
    """Safely converts any datetime (naive, timezone-aware, or ISO string) to a float timestamp for sorting."""
    if not dt:
        return 0.0
    if isinstance(dt, str):
        try:
            clean_ts = dt.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_ts)
        except Exception:
            return 0.0
    elif isinstance(dt, (int, float)):
        return float(dt)
    if isinstance(dt, datetime):
        try:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc).timestamp()
            return dt.timestamp()
        except Exception:
            return 0.0
    return 0.0


def get_client_computer_info():
    """Detects client computer / OS / IP information from Streamlit context."""
    os_name = "Computer"
    ip_str = ""
    try:
        headers = getattr(st.context, "headers", None) or {}
        ua = headers.get("user-agent", "") or headers.get("User-Agent", "")
        platform = str(headers.get("sec-ch-ua-platform", "")).replace('"', "").strip()
        if platform:
            if "mac" in platform.lower():
                os_name = "Mac"
            elif "win" in platform.lower():
                os_name = "Windows PC"
            elif "linux" in platform.lower():
                os_name = "Linux PC"
            elif "ios" in platform.lower():
                os_name = "iPad/iPhone"
            elif "android" in platform.lower():
                os_name = "Android"
            else:
                os_name = platform
        elif "Macintosh" in ua or "Mac OS" in ua:
            os_name = "Mac"
        elif "Windows" in ua:
            os_name = "Windows PC"
        elif "iPad" in ua:
            os_name = "iPad"
        elif "iPhone" in ua:
            os_name = "iPhone"
        elif "Linux" in ua or "X11" in ua:
            os_name = "Linux PC"

        ip_raw = getattr(st.context, "ip_address", None) or headers.get("x-forwarded-for", "")
        if ip_raw:
            ip_str = str(ip_raw).split(",")[0].strip()
    except Exception:
        pass
    return os_name, ip_str


def get_effective_computer_name() -> str:
    """Returns the effective station / computer name, checking session state, cookies, or auto-detect."""
    if st.session_state.get("computer_name"):
        return str(st.session_state["computer_name"]).strip()
    try:
        cookies = getattr(st.context, "cookies", None) or {}
        saved_cookie = cookies.get("da_computer_name")
        if saved_cookie:
            import urllib.parse
            clean_name = urllib.parse.unquote(saved_cookie).strip()
            if clean_name:
                st.session_state["computer_name"] = clean_name
                return clean_name
    except Exception:
        pass
    os_name, ip_str = get_client_computer_info()
    if ip_str and not ip_str.startswith("127.") and ip_str != "::1":
        short_ip = ip_str.split(".")[-1] if "." in ip_str else ip_str[-4:]
        default_name = f"{os_name} (.{short_ip})"
    else:
        default_name = os_name
    st.session_state["computer_name"] = default_name
    return default_name


def sync_computer_name_to_client(name: str):
    """Syncs the chosen computer name to browser localStorage and cookie."""
    if not name:
        return
    clean_safe = name.replace("'", "\\'").replace('"', '\\"')
    try:
        import streamlit.components.v1 as _comp
        _comp.html(
            f"""
            <script>
            (function() {{
                try {{
                    const parentDoc = window.parent.document;
                    localStorage.setItem('da_computer_name', '{clean_safe}');
                    parentDoc.cookie = 'da_computer_name=' + encodeURIComponent('{clean_safe}') + '; path=/; max-age=31536000; SameSite=Lax';
                }} catch(e) {{}}
            }})();
            </script>
            """,
            height=0,
        )
    except Exception:
        pass


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

# OpenAI SDK (for DeepSeek-R1 auditor)
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

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



def get_existing_set_numbers(subject, year, diff, clean_topic) -> set:
    """Queries all existing set numbers for a topic from Supabase."""
    if not supabase_client:
        return set()
    found_sets = set()
    try:
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
    except Exception as e:
        _log_error("get_existing_set_numbers", e)
    return found_sets


def get_next_set_number(subject, year, diff, clean_topic):
    found_sets = get_existing_set_numbers(subject, year, diff, clean_topic)
    # Find the lowest available positive integer (re-uses deleted/missing set slots starting at Set 1)
    k = 1
    while k in found_sets:
        k += 1
    return k


def clean_redundant_topic_prefix(topic_str: str, year: str = "", subject: str = "", diff: str = "") -> str:
    """Strips redundant grade, subject, level, and NESA prefixes from topic titles."""
    if not topic_str:
        return ""
    s = str(topic_str).replace("_", " ").strip()
    s = re.sub(r"^\[Year\s*\d+\]\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\s*\((?:Yr|Year)\s*\d+(?:,\s*[^)]*)?\)$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^\d+[\.\-]\s*", "", s).strip()
    s = re.sub(r"^(?:NESA\s*(?:Syllabus)?[\s\-_:]*)+", "", s, flags=re.IGNORECASE).strip()

    tokens = [
        r"Year\s*\d+", r"Yr\s*\d+",
        r"Maths", r"Mathematics", r"English", r"Science", r"Business\s*Studies", r"Legal\s*Studies",
        r"Advanced", r"Adv", r"Standard", r"Std",
        r"Extension\s*1", r"Ext\s*1", r"Extension\s*2", r"Ext\s*2", r"Extension", r"Ext",
        r"S6", r"Stage\s*\d+", r"S\d+"
    ]
    if subject:
        tokens.append(re.escape(subject))
    if diff:
        tokens.append(re.escape(diff))
    if year:
        tokens.append(re.escape(year))

    pattern = r"^(?:" + "|".join(tokens) + r")[\s\-_,:]*"

    prev = ""
    while prev != s:
        prev = s
        s = re.sub(r"^(?:NESA\s*(?:Syllabus)?[\s\-_:]*)+", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(pattern, "", s, flags=re.IGNORECASE).strip()

    return s if s else str(topic_str).strip()


def build_exam_filename(topic: str, year: str, subject: str, diff: str, set_num: int, has_solutions: bool = False, num_mc: int = 0, num_easy: int = 0, num_med: int = 0, num_hard: int = 0, num_xh: int = 0, ext: str = "") -> str:
    """Builds a standardized, non-redundant exam filename matching DA Tuition format."""
    clean_t = clean_redundant_topic_prefix(topic, year, subject, diff)
    clean_t = re.sub(r"\s*Set\s*\d+.*", "", clean_t, flags=re.IGNORECASE).strip()
    safe_topic = clean_t.replace("/", "-").replace("\\", "-")

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
    inner_dist = ", ".join(dist_parts)
    dist_str = f" ({inner_dist})" if dist_parts else ""

    yr_short = year.replace("Year ", "Yr") if year else ""

    level_map = {
        "Advanced": "Adv",
        "Extension": "Ext1",
        "Extension 1": "Ext1",
        "Extension 2": "Ext2",
        "Standard": "Std",
    }
    short_diff = level_map.get(diff, diff) if diff else ""
    lvl_text = f" {short_diff}" if short_diff else ""
    sol_tag = " w sol" if has_solutions else ""

    suffix = f"{yr_short} {subject}{lvl_text}".strip()
    suffix_part = f" - {suffix}" if suffix else ""

    base = f"{safe_topic} Set {set_num}{sol_tag}{dist_str}{suffix_part}"
    base = re.sub(r"\s+", " ", base).strip()
    if ext:
        ext = ext if ext.startswith(".") else f".{ext}"
        return f"{base}{ext}"
    return base


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
    gemini_cost=None,
    ds_cost=None,
    model=None,
    extra_instructions="",
    created_by="",
    has_solutions=False,
    questions_text="",
    answers_text="",
    solutions_text="",
    audit_data=None,
):
    if not supabase_client:
        return False, "Supabase is not connected. Missing URL or Key."

    if not pdf_bytes and questions_text:
        try:
            tmp_wd = tempfile.mkdtemp(prefix="da_save_recompile_")
            p_b, _, _, _, _ = _render_exam_files(
                tmp_wd, clean_topic, f"{year} {subject} {diff}".strip(), questions_text, answers_text, "", 10
            )
            shutil.rmtree(tmp_wd, ignore_errors=True)
            if p_b:
                pdf_bytes = p_b
        except Exception as e_rec:
            _log_error("save_to_supabase_recompile", e_rec)

    if not pdf_bytes:
        return False, "Cannot archive exam: PDF file is empty or compilation failed."

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
    inner_dist = ", ".join(dist_parts)
    dist_str = f" ({inner_dist})" if dist_parts else ""

    clean_t = clean_redundant_topic_prefix(clean_topic, year, subject, diff)
    clean_t = re.sub(r"\s*Set\s*\d+.*", "", clean_t, flags=re.IGNORECASE).strip()
    yr_short = year.replace("Year ", "Yr")
    db_topic = f"{clean_t} ({yr_short}) Set {set_num}{dist_str}"

    has_solutions = bool(has_solutions) or bool(solutions_text)
    file_base = build_exam_filename(
        clean_topic, year, subject, diff, set_num,
        has_solutions=has_solutions,
        num_mc=num_mc, num_easy=num_easy, num_med=num_med, num_hard=num_hard, num_xh=num_xh
    )
    pdf_path = f"{subject}/{year}/{file_base}.pdf"
    docx_path = f"{subject}/{year}/{file_base}.docx"
    instr_path = f"{subject}/{year}/{file_base}_instructions.txt"
    questions_path = f"{subject}/{year}/{file_base}_questions.txt"
    answers_path = f"{subject}/{year}/{file_base}_answers.txt"
    solutions_path = f"{subject}/{year}/{file_base}_solutions.txt"
    audit_path = f"{subject}/{year}/{file_base}_audit.json"


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

        # Save questions and answers as companion files for later solution generation
        if questions_text:
            try:
                supabase_client.storage.from_("exam-files").upload(
                    questions_path,
                    str(questions_text).encode("utf-8"),
                    {"content-type": "text/plain; charset=utf-8", "upsert": "true"},
                )
            except Exception as e:
                _log_error("save_questions_storage", e)
        if answers_text:
            try:
                supabase_client.storage.from_("exam-files").upload(
                    answers_path,
                    str(answers_text).encode("utf-8"),
                    {"content-type": "text/plain; charset=utf-8", "upsert": "true"},
                )
            except Exception as e:
                _log_error("save_answers_storage", e)
        if solutions_text:
            try:
                supabase_client.storage.from_("exam-files").upload(
                    solutions_path,
                    str(solutions_text).encode("utf-8"),
                    {"content-type": "text/plain; charset=utf-8", "upsert": "true"},
                )
            except Exception as e:
                _log_error("save_solutions_storage", e)

        if cost is not None or gemini_cost is not None or ds_cost is not None or audit_data:
            try:
                audit_dict = {
                    "total_cost": round(float(cost), 5) if cost is not None else None,
                    "gemini_cost": round(float(gemini_cost), 5) if gemini_cost is not None else None,
                    "ds_cost": round(float(ds_cost), 5) if ds_cost is not None else None,
                    "model": str(model or "gemini-3.8-flash"),
                    "audit_status": audit_data.get("status") if isinstance(audit_data, dict) else "APPROVED",
                    "audit_summary": audit_data.get("summary") if isinstance(audit_data, dict) else "",
                }
                supabase_client.storage.from_("exam-files").upload(
                    audit_path,
                    json.dumps(audit_dict).encode("utf-8"),
                    {"content-type": "application/json; charset=utf-8", "upsert": "true"},
                )
            except Exception as e:
                _log_error("save_audit_storage", e)

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
                if gemini_cost is not None:
                    data_to_save["gemini_cost"] = round(float(gemini_cost), 5)
                if ds_cost is not None:
                    data_to_save["ds_cost"] = round(float(ds_cost), 5)
                if model:
                    data_to_save["model"] = str(model)
            except (ValueError, TypeError):
                pass

        if clean_instr:
            data_to_save["extra_instructions"] = clean_instr
            data_to_save["instructions"] = clean_instr

        if created_by:
            data_to_save["created_by"] = str(created_by).strip()

        # Store has_solutions flag so library can show solution status
        data_to_save["has_solutions"] = bool(has_solutions)

        def _execute_update(row_id):
            for payload in [
                data_to_save,
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions")},
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions", "created_by")},
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions", "created_by", "gemini_cost", "ds_cost")},
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions", "created_by", "has_solutions", "gemini_cost", "ds_cost")},
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
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions")},
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions", "created_by")},
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions", "created_by", "gemini_cost", "ds_cost")},
                {k: v for k, v in data_to_save.items() if k not in ("extra_instructions", "instructions", "created_by", "has_solutions", "gemini_cost", "ds_cost")},
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
LEVEL_NAMES = {"Advanced", "Extension", "Extension 1", "Extension 2", "Standard", "EALD"}


def resolve_level_for_year(year: str, subject: str, level: str) -> list[str]:
    """Returns candidate level names for a specific year and subject (e.g. mapping Extension <-> Extension 1)."""
    if not level or level == "N/A":
        return [""]
    if subject == "Maths":
        if level in ("Extension", "Extension 1"):
            if year == "Year 11":
                return ["Extension", "Extension 1"]
            elif year == "Year 12":
                return ["Extension 1", "Extension"]
            return [level]
        elif level == "Extension 2":
            if year == "Year 11":
                return ["Extension", "Extension 1"]
            return ["Extension 2"]
    return [level]


def get_parent_path(year: str, subject: str, difficulty: str) -> str:
    if difficulty:
        candidates = resolve_level_for_year(year, subject, difficulty)
        for cand in candidates:
            if cand:
                diff_path = os.path.join(SAMPLES_DIR, year, subject, cand)
                if os.path.exists(diff_path):
                    return diff_path
    sub_path = os.path.join(SAMPLES_DIR, year, subject)
    return sub_path


def get_available_topics(parent_path: str) -> list:
    if not os.path.exists(parent_path):
        return []
    topics = [d for d in os.listdir(parent_path) if os.path.isdir(os.path.join(parent_path, d))]
    real_topics = [d for d in topics if d not in LEVEL_NAMES]
    return sorted(real_topics)


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
    topic_clean = re.sub(r"^\[Year\s*\d+\]\s*", "", topic, flags=re.IGNORECASE).strip() if topic else ""
    topic_clean = re.sub(r"\s*\((?:Yr|Year)\s*\d+(?:,\s*[^)]*)?\)$", "", topic_clean, flags=re.IGNORECASE).strip() if topic_clean else ""
    topic_path = os.path.join(parent_path, topic_clean) if topic_clean else ""

    if not (topic_path and os.path.exists(topic_path)) and os.path.exists(parent_path) and topic_clean:
        norm_t = re.sub(r"^\d+[\.\-]\s*", "", topic_clean).strip().lower()
        for d in os.listdir(parent_path):
            if os.path.isdir(os.path.join(parent_path, d)) and d not in LEVEL_NAMES:
                if re.sub(r"^\d+[\.\-]\s*", "", d).strip().lower() == norm_t:
                    topic_path = os.path.join(parent_path, d)
                    break

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


@st.cache_data
def load_syllabus_manifest() -> dict:
    manifest_path = os.path.join(os.path.dirname(__file__), "syllabus_manifest.json")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            _log_error("load_syllabus_manifest", e)
    return {}


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

    # 6. Fix command argument typos where '{...>' was written instead of '{...}' (only spacing/dimension commands; never touch math/text where '>' is greater-than)
    text = re.sub(r"(\\(?:vspace\*?|hspace\*?|rule))\{([0-9\.\-\s]+(?:cm|mm|pt|in|em|ex)?)>", r"\1{\2}", text)

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

    # 11b. Fix \tag / \tag* in $$ or math environments which causes "! Package amsmath Error: \tag not allowed here."
    def _fix_tag(m):
        has_star = m.group(1) == "*"
        content = (m.group(2) or m.group(3) or "").strip()
        if has_star:
            return rf"\qquad \text{{{content}}}"
        else:
            if content.startswith("(") and content.endswith(")"):
                return rf"\qquad \text{{{content}}}"
            return rf"\qquad \text{{({content})}}"

    text = re.sub(r"\\tag(\*?)[ \t\r\n]*(?:\{([^}]+)\}|(\([0-9A-Za-z*+\-\.]+\)|[0-9A-Za-z*+\-\.]+))", _fix_tag, text)

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

    clean_t = clean_redundant_topic_prefix(topic, header_title, "", "")
    full_tex = f"""\\documentclass{{article}}
\\usepackage{{amsmath, amssymb, amsfonts, graphicx, booktabs, array, bm, mathtools}}
\\begin{{document}}
\\begin{{center}}
    \\includegraphics[width=1.2in]{{{LOGO_PATH.replace(chr(92), "/")}}} \\\\[0.4cm]
    {{\\LARGE \\textbf{{{header_title}}}}} \\\\[0.2cm]
    {{\\Large \\textbf{{{clean_t}}}}}
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

    clean_disp = clean_redundant_topic_prefix(display_topic, header_title, "", "")
    safe_title = header_title.replace("&", r"\&").replace("%", r"\%").replace("$", r"\$").replace("_", r"\_")
    safe_topic = clean_disp.replace("&", r"\&").replace("%", r"\%").replace("$", r"\$").replace("_", r"\_")

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
        if not os.path.exists(pdf_filename):
            # Recovery pass: check if failure was due to \tag or similar amsmath errors
            if proc.stdout and ("tag not allowed" in proc.stdout or "amsmath Error: \\tag" in proc.stdout):
                with open(filename, "r", encoding="utf-8") as f:
                    curr_tex = f.read()
                fixed_tex = re.sub(
                    r"\\tag\*?[ \t\r\n]*(?:\{([^}]+)\}|(\([0-9A-Za-z*+\-\.]+\)|[0-9A-Za-z*+\-\.]+))",
                    lambda m: rf"\qquad \text{{{m.group(1) or m.group(2) or ''}}}",
                    curr_tex,
                )
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(fixed_tex)
                tex_bytes = fixed_tex.encode("utf-8")
                proc = subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", filename],
                    cwd=work_dir, capture_output=True, text=True,
                )

            if not os.path.exists(pdf_filename) and proc.stdout and ("Extra }" in proc.stdout or "forgotten $" in proc.stdout):
                with open(filename, "r", encoding="utf-8") as f:
                    curr_tex = f.read()
                # Repair premature closing brace before math comparisons, e.g. '($x } 1$)' -> '($x > 1$)'
                fixed_tex = re.sub(r"(\$[a-zA-Z0-9\s\+\-\*\/]+)\s*\}\s*([0-9a-zA-Z]+[^\$]*\$)", r"\1 > \2", curr_tex)
                if fixed_tex != curr_tex:
                    with open(filename, "w", encoding="utf-8") as f:
                        f.write(fixed_tex)
                    tex_bytes = fixed_tex.encode("utf-8")
                    proc = subprocess.run(
                        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", filename],
                        cwd=work_dir, capture_output=True, text=True,
                    )

            if not os.path.exists(pdf_filename):
                # Final fallback pass: run without -halt-on-error so pdflatex recovers from minor math/tikz warnings and still emits the PDF
                subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", filename],
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
    "truncated", "invalid_argument", "timeout", "timed out", "deadline",
    "connection", "socket", "reset", "busy",
)


def _is_retryable(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(marker in s for marker in _RETRYABLE_MARKERS)


def friendly_error_message(exc: Exception) -> str:
    """Translate a raw exception into a short, plain-English message for staff."""
    msg = str(exc)
    low = msg.lower()
    if "api key" in low or "401" in msg or "permission" in low or "403" in msg:
        return "The AI service rejected the request — the Gemini API key may be missing or invalid. Please check your key in the sidebar."
    if "429" in msg or "resource_exhausted" in low or "quota" in low:
        return "The AI service has temporarily hit its usage limit. Please wait a minute and try again."
    if "503" in msg or "overloaded" in low or "unavailable" in low or "500" in msg or "internal" in low or "busy" in low:
        return "Google's AI servers are temporarily busy or overloaded. Please wait a few seconds and click 'It's go time!' again."
    if "timeout" in low or "timed out" in low or "deadline" in low:
        return "The request took too long and timed out. This often happens with very large batches (e.g. 40–50 questions). Please try generating again or reduce the question count slightly."
    if "truncated" in low or "empty text" in low:
        return "The AI's response was incomplete — this usually happens when too many questions are requested at once. Try a slightly smaller batch."
    return "Something went wrong while generating the exam. Please try again, or contact tech support if this keeps happening."


def _extract_genai_text(res) -> str:
    """Extracts text from a Gemini response, safely handling multi-part content (e.g. tool execution results)."""
    if not res:
        return ""
    try:
        t = getattr(res, "text", None)
        if t:
            return t
    except Exception:
        pass
    parts_text = []
    candidates = getattr(res, "candidates", []) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        if content and getattr(content, "parts", None):
            for part in content.parts:
                if getattr(part, "thought", False):
                    continue
                txt = getattr(part, "text", None)
                if txt:
                    parts_text.append(txt)
    return "".join(parts_text)


# ══════════════════════════════════════════════════════════════════════════════
# TEACHER ACCOUNTS & AUTHENTICATION MODULE
# ══════════════════════════════════════════════════════════════════════════════
TEACHER_REGISTRY_PATH = "_system/teachers_registry.json"
PASSWORD_SALT = "DA_TUITION_ACADEMY_2026_SECURE_SALT"
SCHOOL_INVITE_CODE = "DA-TUITION-2026"


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


def _get_active_api_key() -> str:
    """Returns the effective Gemini API key for the current user session."""
    user_role = st.session_state.get("user_role", "senior")
    if user_role == "junior":
        return (st.session_state.get("user_api_key") or "").strip()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        key = (st.session_state.get("user_api_key") or "").strip()
    return key


def _get_genai_client():
    """Builds the Gemini client using role-specific API keys.
    Junior teachers strictly use their own API key to preserve school credits.
    Senior teachers use the server master API key.
    """
    user_role = st.session_state.get("user_role", "senior")
    api_key = _get_active_api_key()
    if not api_key:
        if user_role == "junior":
            st.error("⚠️ Missing personal Gemini API key. Please connect your API key in the sidebar.")
        else:
            st.error("⚠️ The AI engine isn't configured (missing server API key). Please contact tech support before using the generator.")
        st.stop()
    return genai.Client(api_key=api_key)


def _get_deepseek_api_key() -> str:
    """Retrieves the DeepSeek API key from session state, Streamlit secrets, env var, or default."""
    if st.session_state.get("deepseek_api_key"):
        return st.session_state["deepseek_api_key"].strip()
    try:
        if "DEEPSEEK_API_KEY" in st.secrets:
            return str(st.secrets["DEEPSEEK_API_KEY"]).strip()
    except Exception:
        pass
    env_k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_k:
        return env_k
    # Configured master key for the tuition center
    return "sk-7a311b7546c14bdfabd18844aa91b915"


def audit_exam_with_deepseek(
    draft_content: str,
    subject: str,
    year_group: str,
    actual_level: str,
    clean_topic: str,
    total_q: int,
    syllabus_rules: str,
    api_key: str = "",
) -> dict:
    """Invokes DeepSeek-R1 (deepseek-reasoner) to audit drafted questions for mathematical correctness,
    solvability, and strict post-2019 NESA syllabus boundaries.

    Returns a dict with:
      status: "APPROVED" | "REVISION_NEEDED" | "ERROR" | "SKIPPED"
      summary: short overview of findings
      details: full critique or approval notes
      correction_prompt: instructions for Gemini to patch flaws (or empty)
      reasoning: DeepSeek-R1 internal chain-of-thought
      auto_corrected: bool (set to True if Gemini repaired issues)
      error: error message if call failed
    """
    key = (api_key or _get_deepseek_api_key()).strip()
    if not key:
        return {
            "status": "SKIPPED",
            "summary": "DeepSeek auditor skipped: No API key configured.",
            "details": "",
            "correction_prompt": "",
            "reasoning": "",
            "auto_corrected": False,
            "ds_in_tok": 0,
            "ds_out_tok": 0,
            "ds_cost": 0.0,
        }

    level_str = f" {actual_level}" if actual_level else ""
    system_prompt = (
        "You are an expert Chief Mathematical Auditor and senior NESA Syllabus Inspector for Australian secondary education (NSW HSC).\n"
        "Your task is to conduct an authoritative, rigorous mathematical and curriculum audit of an examination draft generated for students.\n\n"
        "EVALUATION CRITERIA:\n"
        "1. MATHEMATICAL CORRECTNESS & SOLVABILITY:\n"
        "   - Is every question mathematically unambiguous, well-formed, and completely solvable?\n"
        "   - Are all numerical values and geometric conditions consistent (no unintended real-number domain violations like negative square roots, division by zero, or degenerate triangles)?\n"
        "   - Are the provided answers in the answer key mathematically verified and accurate?\n"
        "2. CURRENT NESA SYLLABUS BOUNDARY COMPLIANCE (CRITICAL):\n"
        "   - Check every question AND its complete expected working and answer. Mark REVISION_NEEDED if solving any part requires a rule, theory, concept, formula, theorem, proof, method, or intermediate step outside the NSW syllabus for the selected year and course level(s). This applies even to Hard and Extremely Hard questions, including requirements that are only implicit in the question.\n"
        "   - Textbook challenge material, exemplars, and user instructions cannot make an out-of-syllabus requirement acceptable. Difficulty must come only from combining and applying in-syllabus content.\n"
        "   - Adhere strictly to the course syllabus boundaries:\n"
        "     * Universal bans: Pigeonhole Principle, internal/external division of an interval, and Euclidean circle geometry proofs are STRICTLY BANNED across ALL courses.\n"
        "     * For Extension 1 selections without Extension 2: vector proofs, t-formulae and product-to-sum identities are outside the selected course. 3D vectors are included.\n"
        "     * For Advanced selections without an Extension level: 3D trigonometry, vectors, projectile motion, complex numbers and induction are outside the selected course. Apply the combined selected-course scope when multiple levels are selected.\n"
        "     * Year 11 vs Year 12 boundaries: Discrete random variables are Year 12 Advanced (banned in Year 11). Basic network concepts are Year 11 Standard review; Year 12 Standard includes critical path analysis and network flow.\n"
        "3. DIAGRAM INTEGRITY:\n"
        "   - If any question refers to a figure, geometric shape, or diagram (e.g. 'In the diagram below', 'The figure shows'), verify that a valid rendered diagram (TikZ or Python graph) is present.\n"
        "4. QUESTION SPECIFICATIONS:\n"
        "   - Verify that the draft aligns with the intended topic, level, and question count.\n\n"
        "OUTPUT FORMAT (STRICT DELIMITERS):\n"
        "===AUDIT_STATUS===\n"
        "[APPROVED or REVISION_NEEDED]\n"
        "===AUDIT_SUMMARY===\n"
        "[A clear 1-2 sentence executive verdict suitable for teachers]\n"
        "===AUDIT_DETAILS===\n"
        "[Detailed breakdown: if APPROVED, confirm that calculations and boundaries were checked; if REVISION_NEEDED, cite specific question numbers and the exact mathematical error or syllabus violation]\n"
        "===CORRECTION_PROMPT===\n"
        "[If REVISION_NEEDED: Give precise, actionable instructions to the drafting AI on which questions to replace or fix and how. If APPROVED, write NONE]\n"
    )

    user_payload = (
        f"AUDIT SPECIFICATION:\n"
        f"- Target Course: {year_group}{level_str} in {subject}\n"
        f"- Focus Topic: {clean_topic}\n"
        f"- Expected Total Questions: {total_q}\n\n"
        f"COURSE-SPECIFIC SYLLABUS CONSTRAINTS:\n{syllabus_rules}\n\n"
        f"DRAFTED EXAM CONTENT TO AUDIT:\n{draft_content}\n"
    )

    raw_content = ""
    reasoning_content = ""
    ds_in_tok = 0
    ds_out_tok = 0

    # Strategy 1: OpenAI SDK if installed
    used_sdk = False
    if HAS_OPENAI:
        try:
            client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
            response = client.chat.completions.create(
                model="deepseek-reasoner",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=4096,
                timeout=60.0,
            )
            if response.choices:
                choice = response.choices[0]
                raw_content = choice.message.content or ""
                reasoning_content = getattr(choice.message, "reasoning_content", "") or ""
                used_sdk = True
            if getattr(response, "usage", None):
                ds_in_tok = getattr(response.usage, "prompt_tokens", 0) or 0
                ds_out_tok = getattr(response.usage, "completion_tokens", 0) or 0
        except Exception as e_sdk:
            _log_error("OpenAI SDK DeepSeek Call Failed, falling back to HTTP", e_sdk)

    # Strategy 2: Direct HTTP POST via urllib.request (zero external dependencies)
    if not used_sdk or not raw_content:
        try:
            import urllib.request
            data = {
                "model": "deepseek-reasoner",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                "max_tokens": 4096,
            }
            req = urllib.request.Request(
                "https://api.deepseek.com/chat/completions",
                data=json.dumps(data).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                res_obj = json.loads(resp.read().decode("utf-8"))
                if res_obj.get("choices"):
                    msg = res_obj["choices"][0].get("message", {})
                    raw_content = msg.get("content", "") or ""
                    reasoning_content = msg.get("reasoning_content", "") or ""
                if res_obj.get("usage"):
                    ds_in_tok = res_obj["usage"].get("prompt_tokens", 0) or 0
                    ds_out_tok = res_obj["usage"].get("completion_tokens", 0) or 0
        except Exception as e_http:
            _log_error("DeepSeek Auditor API Failure", e_http)
            err_msg = str(e_http)
            if "402" in err_msg or "Payment Required" in err_msg:
                friendly_err = "DeepSeek API account balance is empty ($0.00). Please top up credits at platform.deepseek.com to re-enable automated math auditing."
            elif "401" in err_msg or "Unauthorized" in err_msg:
                friendly_err = "Invalid DeepSeek API key. Please check your API key in settings."
            else:
                friendly_err = f"Auditor connection issue ({err_msg}). The exam was still generated successfully."

            return {
                "status": "ERROR",
                "summary": friendly_err,
                "details": err_msg,
                "correction_prompt": "",
                "reasoning": "",
                "auto_corrected": False,
                "error": friendly_err,
                "ds_in_tok": 0,
                "ds_out_tok": 0,
                "ds_cost": 0.0,
            }

    # Parse delimited response
    status_m = re.search(r"===?\s*AUDIT_STATUS\s*===?\s*(APPROVED|REVISION_NEEDED)", raw_content, re.IGNORECASE)
    status = status_m.group(1).upper() if status_m else ("APPROVED" if "APPROVED" in raw_content[:200] else "REVISION_NEEDED")

    summary_m = re.search(r"===?\s*AUDIT_SUMMARY\s*===?\s*(.*?)(?:===?\s*AUDIT_DETAILS|$)", raw_content, re.DOTALL | re.IGNORECASE)
    summary = summary_m.group(1).strip() if summary_m else "Auditor review completed."

    details_m = re.search(r"===?\s*AUDIT_DETAILS\s*===?\s*(.*?)(?:===?\s*CORRECTION_PROMPT|$)", raw_content, re.DOTALL | re.IGNORECASE)
    details = details_m.group(1).strip() if details_m else ""

    corr_m = re.search(r"===?\s*CORRECTION_PROMPT\s*===?\s*(.*)", raw_content, re.DOTALL | re.IGNORECASE)
    corr_prompt = corr_m.group(1).strip() if corr_m else ""
    if corr_prompt.upper() in ["NONE", "N/A", "NONE."]:
        corr_prompt = ""

    # DeepSeek-R1 (deepseek-reasoner) pricing: $0.55 / 1M input, $2.19 / 1M output (including reasoning tokens)
    ds_cost = round(((ds_in_tok / 1_000_000) * 0.55) + ((ds_out_tok / 1_000_000) * 2.19), 6)

    return {
        "status": status,
        "summary": summary,
        "details": details,
        "correction_prompt": corr_prompt,
        "reasoning": reasoning_content,
        "auto_corrected": False,
        "ds_in_tok": ds_in_tok,
        "ds_out_tok": ds_out_tok,
        "ds_cost": ds_cost,
    }


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND EXAM GENERATION QUEUE & WORKER ENGINE
# ══════════════════════════════════════════════════════════════════════════════
@st.cache_resource
def _init_global_exam_queue_backend():
    return {
        "queue": {},
        "lock": threading.Lock(),
        "executor": concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="DA_ExamWorker"),
    }

_QUEUE_BACKEND = _init_global_exam_queue_backend()
GLOBAL_JOB_QUEUE: dict[str, dict] = _QUEUE_BACKEND["queue"]
GLOBAL_QUEUE_LOCK: threading.Lock = _QUEUE_BACKEND["lock"]
GLOBAL_JOB_EXECUTOR: concurrent.futures.ThreadPoolExecutor = _QUEUE_BACKEND["executor"]


def get_next_set_number_with_queue(subject: str, year: str, diff: str, topic: str, base_set: int = 0) -> int:
    """Calculates the next available set number considering both Supabase and currently queued/running jobs."""
    if base_set > 0:
        return base_set
    occupied = set(get_existing_set_numbers(subject, year, diff, topic))
    with GLOBAL_QUEUE_LOCK:
        for j in GLOBAL_JOB_QUEUE.values():
            if j.get("status") in ("QUEUED", "RUNNING", "COMPLETED"):
                if (
                    j.get("subject") == subject
                    and j.get("year") == year
                    and j.get("level") == diff
                    and j.get("topic") == topic
                ):
                    s_num = j.get("meta_set")
                    if isinstance(s_num, int) and s_num > 0:
                        occupied.add(s_num)
    next_s = 1
    while next_s in occupied:
        next_s += 1
    return next_s


def generate_exam_worker(params: dict, progress_cb=None) -> dict:
    """Executes full exam generation and compilation in an isolated, thread-safe environment.
    Can be run in foreground (with UI toasts/spinners) or in background worker threads."""
    def _notify(msg: str):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    try:
        api_key = (params.get("api_key") or "").strip()
        if not api_key:
            return {"success": False, "error": "Missing Gemini API key."}

        client = genai.Client(api_key=api_key)

        year_group = params.get("year_group", [])
        subject = params.get("subject", "Maths")
        actual_level = params.get("actual_level", "")
        actual_levels = params.get("actual_levels") or ([actual_level] if actual_level else [])
        policy_level = next((level for level in ("Extension 2", "Extension 1", "Extension", "Advanced", "Standard") if level in actual_levels), actual_level)
        clean_topic = params.get("clean_topic", "General")
        sub_topic = params.get("sub_topic", "")
        grades_string = params.get("grades_string", ", ".join(year_group))
        current_set_number = params.get("current_set_number", 1)
        display_topic = params.get("display_topic", f"{clean_topic} Set {current_set_number}")
        total_q = params.get("total_q", 10)
        num_mc = params.get("num_mc", 0)
        num_easy = params.get("num_easy", 0)
        num_med = params.get("num_med", 0)
        num_hard = params.get("num_hard", 0)
        num_xh = params.get("num_xh", 0)
        layout_mode = params.get("layout_mode", "Exam (Space for working out)")
        style_samples = params.get("style_samples", "")
        extra_instructions = params.get("extra_instructions", "")
        exemplar_questions = params.get("exemplar_questions", "")
        uploaded_files_data = params.get("uploaded_files_data", [])
        use_live_search = params.get("use_live_search", False)
        created_by = params.get("created_by", "Teacher")
        auto_solutions = params.get("auto_solutions", False)
        use_deepseek_auditor = params.get("use_deepseek_auditor", False)
        deepseek_api_key = params.get("deepseek_api_key", "")

        has_upload = bool(uploaded_files_data)
        has_mc = num_mc > 0
        total_fr = num_easy + num_med + num_hard + num_xh
        has_fr = total_fr > 0

        if "Exam" in layout_mode:
            layout_instruction = "CRITICAL SPACING & PAGE-BREAK RULE: Add `\\vspace*{4cm}` (or more) after EVERY Free-Response question and `\\vspace*{2.5cm}` after sub-questions. ALWAYS place the mark indicator (e.g., `\\hfill \\textbf{(2 marks)}`) directly at the end of the question text or equation, followed immediately by the `\\vspace*`. Keep each question and its space together."
        else:
            layout_instruction = "CRITICAL SPACING RULE: Add exactly `\\vspace{0.5cm}` after EVERY question and sub-question."

        level_text = f" {actual_level}" if actual_level else ""

        template_f = ""
        auto_name_rule = ""
        if has_upload:
            template_f = "===FILENAME_START===\nConcise_Topic_Or_AssessmentTitle\n===FILENAME_END===\n"
            auto_name_rule = "12. AUTO-NAMING & TOPIC EXTRACTION: You MUST inspect the attached document(s). If it is a school assessment notification, extract the School Name and official Assessment Title (e.g. Bonnyrigg_High_School_Assessment_Task_2). If it is a syllabus, textbook, or general topic list, extract ONLY the concise Topic Title itself (e.g. Sequences_and_Series, Transformations_and_Modelling) WITHOUT prefixing NESA, subject, or year group. Use underscores. Place inside ===FILENAME_START=== and ===FILENAME_END=== tags. Keep this topic name identical and consistent across generations for the same document."

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

        # Syllabus ban logic
        if subject == "Maths":
            universal_ban = (
                " CRITICAL NESA SYLLABUS BANS (ALL MATHS COURSES): "
                "(1) PIGEONHOLE PRINCIPLE IS STRICTLY BANNED — NEVER generate questions on or using the Pigeonhole Principle in ANY year or level (completely removed from the syllabus). "
                "(2) Locus / Focus-Directrix parabola geometry (e.g. x^2 = 4ay, chords of contact, parametric tangents/normals), "
                "(3) Euclidean Circle Geometry proofs (alternate segment, cyclic quadrilaterals, intersecting chords), "
                "(4) Division of an interval in a given ratio (internal/external division), "
                "(5) Simpson's Rule."
            )
            level_ban = ""
            if policy_level == "Advanced":
                level_ban = (
                    " CRITICAL ADVANCED LEVEL BANS: "
                    "(1) 3D TRIGONOMETRY IS STRICTLY BANNED — Year 11 & 12 Advanced only covers 2D Trigonometry (Sine rule, Cosine rule, Area of triangle, Radians). 3D Trigonometry is exclusively an Extension 1 topic. "
                    "(2) DO NOT include Extension 1/2 topics: t-method / t-formulae (t = tan(x/2)), Product-to-sum / sum-to-product (differences) identities, Compound/Double Angles (sin(A+B), cos(2A)), Auxiliary angle method (Rcos(x-a)), Inverse Trig functions, Polynomial division / Remainder theorem / Sum and product of roots, Combinatorics / Permutations & Combinations, Vectors, Projectile Motion, Mathematical Induction, Perpendicular Distance formula, Angle between two lines."
                )
            elif policy_level == "Standard":
                level_ban = (
                    " CRITICAL STANDARD LEVEL BANS: 3D Trigonometry is STRICTLY BANNED. NO Calculus, NO Radians, NO Advanced Polynomials, NO Logarithm laws, NO Compound/Double angle trigonometry, NO t-method/t-formulae, NO product-to-sum identities. Keep all math strictly within the NSW Mathematics Standard syllabus."
                )
            elif policy_level in ["Extension", "Extension 1"]:
                level_ban = (
                    " EXTENSION 1 SYLLABUS RULES: 3D Trigonometry IS ALLOWED & REQUIRED for 3D trig topics (ME-T1). "
                    "VECTORS IN EXTENSION 1: Year 12 Extension 1 Vectors explicitly includes BOTH 2D Vectors and 3D Vectors (3D component form xi + yj + zk, column vectors, 3D magnitude, 3D dot/scalar product, angles between vectors in 3D, and parallel and perpendicular vectors in 3D). "
                    "CRITICAL EXTENSION 1 BANS: "
                    "- NO VECTOR PROOFS: Geometric proofs using vectors (vector proofs) are STRICTLY BANNED in Year 11 & Year 12 Extension 1. Vector proofs belong exclusively in Mathematics Extension 2! NEVER generate questions requiring students to prove geometric theorems with vectors in Extension 1. "
                    "- The t-method / t-formulae (t = tan(theta/2)) and product-to-sum and sum-to-product (differences) trigonometric identities are NO LONGER part of Year 11 or Year 12 Maths Extension 1! They belong EXCLUSIVELY in Maths Extension 2. NEVER generate t-method, t-formulae, or product-to-sum/difference questions in Extension 1. "
                    "- REMEMBER: Euclidean circle proofs, division of an interval, and the Pigeonhole Principle are strictly banned in Extension 1. "
                    "- DO NOT include Extension 2 exclusive topics (Complex Numbers, Proof by Contradiction/Contrapositive, Integration by Parts, Volumes by Slicing & Cylindrical Shells, Mechanics/Resisted Motion, Geometric Vector Proofs)."
                )
            elif policy_level == "Extension 2":
                level_ban = (
                    " EXTENSION 2 SYLLABUS RULES: Follow the Mathematics Extension 2 syllabus: Proof (Nature of Proof, Vector Proofs / Geometric proofs using vectors), Vectors (Further 3D Vectors, vector equations of lines/curves, 3D vector geometry proofs), Complex Numbers, Calculus (Further Integration, Volumes by Slicing & Cylindrical Shells), Mechanics (Resisted Motion, Simple Harmonic Motion). "
                    "NOTE ON TRIGONOMETRY: The t-method / t-formulae (t = tan(x/2) substitutions) and product-to-sum and sum-to-product (differences) trigonometric identities belong EXCLUSIVELY in Maths Extension 2 (e.g. in Further Integration and Advanced Proofs/Identities). "
                    "CRITICAL EXTENSION 2 BANS: The Pigeonhole Principle is STRICTLY BANNED across all courses including Extension 2. DO NOT include obsolete pre-2019 topics like Conics or Harder Circle Geometry."
                )

            yr_ban = ""
            if "Year 11" in year_group and "Year 12" not in year_group:
                if policy_level == "Standard":
                    yr_ban = (
                        " YEAR 11 STANDARD BOUNDARIES: "
                        "(1) NETWORKING / NETWORK CONCEPTS IS NOW IN YEAR 11 STANDARD: Networks, vertices, edges, degrees, directed/undirected graphs, walks, trails, paths, circuits, cycles, Eulerian and Hamiltonian paths/circuits, trees, minimum spanning trees (Prim's algorithm), and shortest paths are explicitly INCLUDED in Year 11 Maths Standard. "
                        "(2) RELATIVE FREQUENCY MOVED TO YEAR 12: DO NOT generate Relative Frequency in Year 11 Maths Standard (Relative Frequency has moved to Year 12 Maths Standard). "
                        "(3) DO NOT generate Year 12 Standard topics: Normal Distribution, Z-scores, Annuities, Depreciation, Critical Path Analysis, Bivariate Data. DO NOT require manual calculation of standard deviation."
                    )
                elif policy_level == "Advanced":
                    yr_ban = (
                        " YEAR 11 ADVANCED BOUNDARIES: "
                        "(1) DATA IS INCLUDED IN YEAR 11 ADVANCED: Single-variable data analysis, summary statistics (mean, median, mode, range, IQR, standard deviation), quartiles, box plots, frequency histograms, and identifying outliers are explicitly part of Year 11 Maths Advanced. "
                        "(2) NO DISCRETE PROBABILITY DISTRIBUTIONS: Discrete Probability Distributions and Discrete Random Variables are STRICTLY BANNED from Year 11 Maths Advanced (they belong in Year 12 Maths Advanced). "
                        "(3) DO NOT generate Year 12 Advanced topics: NO Integration / Area under curves / Volumes of revolution, NO Financial Mathematics (Superannuation, Annuities, Series loan repayments), NO Continuous Random Variables / Normal Distribution, NO Bivariate Data Analysis, NO Calculus applications to exponentials or trigonometrics. Year 11 differentiation is strictly limited to First Principles, Power Rule on polynomials, and basic Tangents/Normals."
                    )
                elif policy_level in ["Extension", "Extension 1"]:
                    yr_ban = (
                        " YEAR 11 EXTENSION 1 BOUNDARIES: "
                        "(1) NO PIGEONHOLE PRINCIPLE: The Pigeonhole Principle is strictly banned and removed from Year 11 Extension 1 Combinatorics. "
                        "(2) NO t-method or product-to-sum/difference trig identities (these are exclusively Extension 2). "
                        "(3) DO NOT generate Year 12 Extension 1 topics: NO Proof by Mathematical Induction, NO Projectile Motion (Vectors), NO Trigonometric Equations via Auxiliary Angle Rcos(x-a), NO Differential Equations / Exponential Growth, NO Calculus of Inverse Trig Functions, NO Binomial Distribution / Normal Approximation. Year 11 Ext 1 is strictly limited to Functions & Polynomials, Further Trigonometry (3D Trig, Compound/Double angles), Permutations & Combinations (without Pigeonhole Principle), Vectors in 2D, and Rates of Change."
                    )
            elif "Year 12" in year_group:
                if policy_level == "Standard":
                    yr_ban = (
                        " YEAR 12 STANDARD BOUNDARIES: "
                        "(1) RELATIVE FREQUENCY IS IN YEAR 12 STANDARD: Relative frequency, experimental probability, multi-stage events, simulations, and expected frequency belong in Year 12 Maths Standard. "
                        "(2) Basic network concepts, minimum spanning trees and shortest paths appear as Year 11 review in the Year 12 textbook; include those review sections only when explicitly selected. Year 12 content includes Critical Path Analysis and Network Flow."
                    )
                elif policy_level == "Advanced":
                    yr_ban = (
                        " YEAR 12 ADVANCED BOUNDARIES: "
                        "DISCRETE PROBABILITY DISTRIBUTIONS IS INCLUDED IN YEAR 12 ADVANCED: Discrete Probability Distributions, discrete random variables, probability mass functions P(X = x), probability distribution tables, expected value E(X) = mu, and variance Var(X) = sigma^2 are explicitly part of Year 12 Maths Advanced (alongside Continuous Probability Distributions and Normal Distribution)."
                    )
                elif policy_level in ["Extension", "Extension 1"]:
                    yr_ban = (
                        " YEAR 12 EXTENSION 1 BOUNDARIES: "
                        "(1) 3D VECTORS ARE EXPLICITLY INCLUDED: Year 12 Mathematics Extension 1 Vectors explicitly includes questions on 3D Vectors (three-dimensional components in xi + yj + zk or column vectors, magnitude in 3D, addition/subtraction, scalar multiplication, dot/scalar product in 3D, angles between 3D vectors, and parallel/perpendicular conditions in 3D). Ensure that questions on Vectors in Year 12 Extension 1 include 3D vectors as well as 2D vectors! "
                        "(2) NO VECTOR PROOFS: Geometric proofs using vectors (vector proofs, such as proving geometric properties of triangles, parallelograms, rhombuses, or circles using vector algebra) are STRICTLY BANNED in Year 12 Extension 1. Vector proofs belong exclusively in Mathematics Extension 2! "
                        "(3) NO t-method / t-formulae and NO product-to-sum or sum-to-product difference trig identities in Year 12 Extension 1 (they are exclusively in Extension 2). "
                        "(4) NO Pigeonhole Principle."
                    )

            syllabus_ban = (
                f"11. SYLLABUS STRICTNESS (CRITICAL): Strictly adhere to the current NESA {grades_string} {actual_level} syllabus and the selected textbook chapters.{universal_ban}{level_ban}{yr_ban}\n"
            )
        else:
            syllabus_ban = f"11. SYLLABUS STRICTNESS (CRITICAL): Strictly adhere to the current NESA {grades_string} {subject} syllabus. Ensure all diagrams, notations, reaction rates, equilibrium graphs, and scientific concepts follow official NSW HSC curriculum standards."

        syllabus_ban += (
            "\nABSOLUTE NSW SYLLABUS REQUIREMENT FOR EVERY DIFFICULTY LEVEL, INCLUDING EXTREMELY HARD: "
            "NEVER generate a question if answering it or showing any required working would need a rule, theory, concept, formula, theorem, proof, technique, method, or intermediate step outside the NSW syllabus for the selected year and course level(s). "
            "Check the entire solution path, including hidden prerequisites, before writing each question. "
            "Make hard questions harder only through deeper reasoning and combinations of in-syllabus content. "
            "If a sample, textbook challenge exercise, custom instruction, or requested subtopic conflicts with this rule, omit the out-of-syllabus part."
        )

        # Manifest grounding across all selected years
        manifest_data = load_syllabus_manifest()
        matched_courses = []
        for y in year_group:
            for selected_level in (actual_levels or [""]):
                lvl_cands = resolve_level_for_year(y, subject, selected_level)
                course_for_y = None
                for cand in lvl_cands:
                    cand_keys = [f"{y} {subject} {cand}".strip(), f"{y} {subject}".strip()]
                    for ck in cand_keys:
                        if ck in manifest_data:
                            course_for_y = manifest_data[ck]
                            break
                    if course_for_y:
                        break
                if course_for_y and course_for_y not in matched_courses:
                    matched_courses.append(course_for_y)

        if matched_courses:
            manifest_context = ""
            for matched_course in matched_courses:
                textbook_ref = matched_course.get("textbook", "")
                ch_list = [f"- Chapter {c['num']}: {c['name']} (Subtopics: {', '.join(c.get('subtopics', []))})" for c in matched_course.get("chapters", [])]
                inclusions = matched_course.get("inclusions", [])
                exclusions = matched_course.get("exclusions", [])
                manifest_context += f"\n\nSELECTED COURSE: {matched_course.get('year', '')} {matched_course.get('level', '')}. TEXTBOOK BENCHMARK ({textbook_ref}):\n"
                manifest_context += "The following are the authoritative chapters and granular subtopics for this course:\n" + "\n".join(ch_list) + "\n"
                if inclusions:
                    manifest_context += "\nCOURSE SCOPE NOTES:\n" + "\n".join([f"• {inc}" for inc in inclusions]) + "\n"
                if exclusions and len(actual_levels) <= 1:
                    manifest_context += "\nSTRICT EXCLUSIONS & BANS:\n" + "\n".join([f"• {exc}" for exc in exclusions]) + "\n"
            syllabus_ban += manifest_context
            syllabus_ban += "\nThe selected topics and subtopics in the exam focus define the question scope. Other textbook chapters are reference only.\n"

        dist_req = []
        if num_mc > 0:
            dist_req.append(f"{num_mc} Multiple Choice questions")
        if num_easy > 0:
            dist_req.append(f"{num_easy} Easy Free-Response questions")
        if num_med > 0:
            dist_req.append(f"{num_med} Medium Free-Response questions")
        if num_hard > 0:
            dist_req.append(f"{num_hard} Hard Free-Response questions")
        if num_xh > 0:
            dist_req.append(f"{num_xh} Extremely Hard Free-Response questions")

        exam_focus = clean_topic if not sub_topic or sub_topic in clean_topic else f"{clean_topic} - specifically focusing on: {sub_topic}"
        style_block = f"STYLE & SYLLABUS REFERENCE:\n{style_samples}\n\n" if style_samples else ""
        custom_instructions_block = f"EXTRA TUTOR INSTRUCTIONS:\n{extra_instructions}\n\n" if extra_instructions.strip() else ""

        prompt = f"""You are a NESA Examiner.
Generate an examination on "{exam_focus}" for {grades_string}{level_text} in {subject}.

NON-NEGOTIABLE: Every question and every step needed to solve it must stay within the NSW syllabus for the selected year and course level(s), even at Extremely Hard difficulty. Do not require out-of-syllabus working, rules, theory, concepts, or methods.

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
6. "MISSING \\ITEM" CRASH PREVENTION: Immediately after starting a `\\begin{{enumerate}}` or `\\begin{{itemize}}`, the next command MUST be a valid `\\item` (e.g., `\\item` or `\\item[(A)]`). NEVER place `\\vspace`, text, or blank lines before the first item. Place all spacing commands AFTER the item text.
7. SECTION TITLES: Name each section purely as "Section 1", "Section 2", etc. You MUST use the exact standard syntax `\\section*{{Section X}}`. CRITICAL: DO NOT add difficulty labels like "Easy Questions", "Medium Questions", or "Hard" to the section titles.
8. MULTIPLE CHOICE: You MUST heavily randomize the correct answer options (A, B, C, D) across the multiple-choice section. The correct answer must NOT always be 'A'.
9. PYTHON CALCULATOR SANDBOX (CRITICAL): You are equipped with a Python Code Execution tool. You MUST use it to calculate exact final decimal answers for strictly numeric topics like Financial Mathematics, Compound Interest, Annuities, or Statistics. 
CRITICAL EXCEPTIONS: 
- DO NOT use the Python tool for pure algebraic, calculus, or trigonometric topics (e.g., Parametrics, Inverse Functions, Polynomials, Integration). Evaluate symbolic algebra using your own internal reasoning.
10. ATTACHED DOCUMENT & QUESTION CLONING RULE (CRITICAL):
    - ASSESSMENT NOTIFICATION / SCOPE & SEQUENCE: If the attached document is an Assessment Notification or list of topics/outcomes, you MUST strictly and exclusively generate questions on the exact topics and skills listed in that document. Distribute questions across all listed areas. DO NOT generate questions on unlisted topics.
    - QUESTION EXEMPLARS: If sample questions or past worksheet items are provided, reverse-engineer their mathematical mechanics, formatting phrasing, structural complexity, and cognitive depth to generate original parallel practice questions.
11. CRITICAL OUTPUT FORMAT & NO COMMENTS: You must output ONLY the raw content. 
    - Do NOT generate \\documentclass, \\usepackage, \\begin{{document}}, \\end{{document}}, or \\geometry.
    - Provide raw LaTeX code that starts immediately with \\section* or \\begin{{enumerate}}.
    - NEVER use the `%` symbol to write hidden code comments. Only use `%` for actual mathematical percentages (e.g., 50%).
12. MANDATORY ENVIRONMENT CLOSING & BRACES: Every single `\\begin{{enumerate}}`, `\\begin{{itemize}}`, `\\begin{{align*}}`, or `\\begin{{tikzpicture}}` MUST have a matching `\\end{{...}}` tag with exact curly braces `{{...}}`.
{syllabus_ban}
{auto_name_rule}

DIAGRAM RULES (CRITICAL):
- MANDATORY DIAGRAMS IN QUESTIONS: Whenever a question references a geometric figure, vector setup, 3D spatial arrangement, angle of elevation/depression, projectile trajectory, shaded area, network graph, or chart, you MUST render the accompanying diagram directly in the question using Native TikZ or the Python Graph Engine. NEVER refer to a diagram (e.g. "In the diagram below...", "The figure shows...") without actually generating the complete visual code for it!

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
  * NEVER draw box plots using raw unscaled data coordinates if they exceed 12cm.
  * ALWAYS scale your TikZ box plots so the entire number line spans between 10cm and 13cm in total width. Use `xscale` on the tikzpicture.
  * For parallel box plots: Place group labels cleanly above the plots or with `node[left]`.

CRITICAL GRAPHING REQUIREMENT:
Whenever a question asks the student to "sketch" or "draw" a graph, you MUST provide the actual rendered graph in the Answers sections using the LaTeX `pgfplots` package. 
- You must code the visual plot using \\begin{{tikzpicture}} \\begin{{axis}}[...] ... \\end{{axis}} \\end{{tikzpicture}}. 
- NEVER just describe the graph in text. You must mathematically plot the curves, asymptotes, and intercepts using pgfplots.
- NEVER use the setting `trig format plots=none`. If plotting trigonometric functions, use `trig format plots=rad` or omit the setting entirely.
- PREVENT DIMENSION ERRORS: When plotting rational functions or graphs with vertical asymptotes, you MUST restrict the vertical plotting domain (`ymin=-10, ymax=10`, `restrict y to domain=-15:15`).
- MANDATORY SEMICOLONS: Every single drawing command inside the axis environment MUST end with a semicolon (;).
- TIKZ LABELS AND ANCHORS SECURING: Use explicit standard syntax (e.g., label=90:{{$P_1$}}).
- TIKZ SYNTAX CRASH PREVENTION: NEVER place raw text inside a `\\draw` or `\\addplot` command path. Use a properly formatted `\\node`.
- STRICT COORDINATE FORMATTING: When listing points in `\\addplot coordinates {{...}};`, output only raw coordinate pairs.
- CHEMISTRY & SCIENCE EQUILIBRIUM GRAPHS (CRITICAL): Draw sudden disturbances with solid vertical lines at $t_1$. Use smooth continuous curves for gradual shifts.

{custom_instructions_block}
When instructed, your final combined output must follow this template structure exactly:
{template_f}

{template_c}

{template_a}
"""

        ai_payload = [prompt]
        if exemplar_questions.strip():
            ai_payload.append(f"\n\n[CLONE EXEMPLARS - TEXT INPUT]:\n{exemplar_questions}")

        if uploaded_files_data:
            from pypdf import PdfReader
            for file_info in uploaded_files_data:
                doc_bytes = file_info.get("bytes", b"")
                fname = (file_info.get("name") or "").lower()
                is_pdf = fname.endswith(".pdf") or (file_info.get("type") == "application/pdf")

                if is_pdf:
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

                    try:
                        reader = PdfReader(io.BytesIO(doc_bytes))
                        pdf_text = "\n".join([page.extract_text() or "" for page in reader.pages]).strip()
                        if pdf_text:
                            ai_payload.append(f"\n\n[EXTRACTED PDF TEXT FROM {fname}]:\n{pdf_text}")
                    except Exception:
                        pass
                else:
                    image_bytes, mime_type = _prepare_uploaded_image(doc_bytes, fname)
                    ai_payload.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))

            ai_payload.append(
                "\n\n[ATTACHED DOCUMENTS INSTRUCTION - HIGHEST PRIORITY]:\n"
                "Carefully analyze ALL attached documents/pages:\n"
                "1. IF THE ATTACHMENTS ARE AN ASSESSMENT NOTIFICATION, EXAM NOTICE, OR TOPIC LIST:\n"
                "   - Extract EVERY topic, sub-topic, and skill specified in the notification.\n"
                "   - Your generated exam MUST STRICTLY AND EXCLUSIVELY focus on the topics, sub-topics, and syllabus outcomes listed across all uploaded pages.\n"
                "   - DO NOT generate questions on unlisted topics.\n"
                "   - Extract the School Name and Assessment Title for school notices in ===FILENAME_START=== tags. For syllabus/topic documents, extract ONLY the concise Topic Name (e.g. Sequences_and_Series) without prefixing NESA, year, or subject in ===FILENAME_START=== tags.\n"
                "2. IF THE ATTACHMENTS CONTAIN SAMPLE / EXEMPLAR QUESTIONS OR A WORKSHEET:\n"
                "   - Reverse-engineer the mechanics, phrasing, difficulty, and diagram styles of those exact questions, and generate original parallel practice questions testing the same competency."
            )

        live_search_subjects = ["Business Studies", "Legal Studies", "Science"]
        active_tools = [types.Tool(code_execution=types.ToolCodeExecution())]
        used_search = bool(subject in live_search_subjects and use_live_search)
        if used_search:
            active_tools.append(types.Tool(google_search=types.GoogleSearch()))

        models_to_try = [
            "gemini-3.8-flash",
            "gemini-3.6-flash",
        ]
        out = None
        last_error = None
        model_used = None
        in_tok = 0
        out_tok = 0

        max_retries = 3
        for attempt in range(max_retries):
            for model_name in models_to_try:
                cur_max_tok = MAX_OUTPUT_TOKENS
                tool_candidates = [[], active_tools] if (total_q >= 30 or not active_tools) else [active_tools, []]

                for t_list in tool_candidates:
                    try:
                        cfg = types.GenerateContentConfig(
                            max_output_tokens=cur_max_tok,
                            tools=t_list if t_list else None,
                            safety_settings=[
                                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            ],
                        )
                        _notify(f"🏃 {model_name}: Drafting Questions & Answers...")
                        p1_payload = ai_payload + [
                            "\n\nPHASE 1 INSTRUCTION: You must generate the exam questions AND the answer key. First, generate the questions between ===LATEX_CONTENT_START=== and ===LATEX_CONTENT_END===. Then, immediately generate the short answers between ===LATEX_ANSWERS_START=== and ===LATEX_ANSWERS_END===. Do NOT generate fully worked solutions."
                        ]
                        res1 = client.models.generate_content(model=model_name, contents=p1_payload, config=cfg)
                        raw_text = _extract_genai_text(res1)
                        if not raw_text or "LATEX_CONTENT_START" not in raw_text:
                            raise ValueError(f"{model_name} returned incomplete text. Forcing retry...")

                        # Graceful tag auto-completion
                        if "LATEX_CONTENT_END" not in raw_text:
                            if "LATEX_ANSWERS_START" in raw_text:
                                raw_text = raw_text.replace("===LATEX_ANSWERS_START===", "===LATEX_CONTENT_END===\n===LATEX_ANSWERS_START===")
                            else:
                                raw_text += "\n===LATEX_CONTENT_END===\n===LATEX_ANSWERS_START===\n===LATEX_ANSWERS_END==="

                        if "LATEX_ANSWERS_START" in raw_text and "LATEX_ANSWERS_END" not in raw_text:
                            raw_text += "\n===LATEX_ANSWERS_END==="

                        out = raw_text.replace("```latex", "").replace("```", "")
                        model_used = model_name
                        if res1.usage_metadata:
                            in_tok += getattr(res1.usage_metadata, "prompt_token_count", 0)
                            out_tok += getattr(res1.usage_metadata, "candidates_token_count", 0)
                        break
                    except Exception as e:
                        last_error = e
                        if t_list:
                            continue
                        else:
                            if _is_retryable(e):
                                time.sleep(1.5)
                            break
                if out:
                    break
            if out:
                break
            if attempt < max_retries - 1:
                time.sleep((attempt + 1) * 3)

        if not out:
            raise (last_error or Exception("Failed to generate content after multiple attempts."))

        # DeepSeek-R1 Quality Audit
        audit_res = None
        if use_deepseek_auditor:
            active_ds_key = (deepseek_api_key or _get_deepseek_api_key()).strip()
            if active_ds_key:
                _notify("🛡️ DeepSeek-R1: Auditing math solvability & syllabus compliance...")
                audit_res = audit_exam_with_deepseek(
                    draft_content=out,
                    subject=subject,
                    year_group=grades_string,
                    actual_level=actual_level,
                    clean_topic=clean_topic,
                    total_q=total_q,
                    syllabus_rules=syllabus_ban,
                    api_key=active_ds_key,
                )
                if audit_res.get("status") == "REVISION_NEEDED" and audit_res.get("correction_prompt"):
                    _notify("⚡ Gemini: Auto-correcting questions based on DeepSeek audit...")
                    refine_prompt = (
                        "You previously drafted an exam, but our Chief Mathematical Auditor (DeepSeek-R1) reviewed it and detected the following critical issues:\n\n"
                        f"AUDITOR'S EVALUATION:\n{audit_res.get('details') or audit_res.get('summary')}\n\n"
                        f"AUDITOR'S SPECIFIC CORRECTION INSTRUCTIONS:\n{audit_res.get('correction_prompt')}\n\n"
                        "CRITICAL TASK:\n"
                        "Regenerate the complete exam, applying the auditor's corrections.\n"
                        "Keep all already-correct questions untouched, and strictly replace or fix the flagged questions to ensure 100% mathematical accuracy and strict syllabus compliance.\n"
                        "Output using the exact same format tags:\n"
                        "===FILENAME_START=== ... ===FILENAME_END===\n"
                        "===LATEX_CONTENT_START=== ... ===LATEX_CONTENT_END===\n"
                        "===LATEX_ANSWERS_START=== ... ===LATEX_ANSWERS_END===\n"
                    )
                    refine_models = ["gemini-3.8-flash", "gemini-3.6-flash"]
                    for r_model in refine_models:
                        cur_ref_tok = MAX_OUTPUT_TOKENS
                        try:
                            ref_cfg = types.GenerateContentConfig(
                                max_output_tokens=cur_ref_tok,
                                safety_settings=[
                                    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                ],
                            )
                            res_refined = client.models.generate_content(
                                model=r_model,
                                contents=ai_payload + [refine_prompt],
                                config=ref_cfg,
                            )
                            raw_ref = _extract_genai_text(res_refined)
                            if raw_ref and "LATEX_CONTENT_START" in raw_ref:
                                if "LATEX_CONTENT_END" not in raw_ref:
                                    if "LATEX_ANSWERS_START" in raw_ref:
                                        raw_ref = raw_ref.replace("===LATEX_ANSWERS_START===", "===LATEX_CONTENT_END===\n===LATEX_ANSWERS_START===")
                                    else:
                                        raw_ref += "\n===LATEX_CONTENT_END===\n===LATEX_ANSWERS_START===\n===LATEX_ANSWERS_END==="
                                if "LATEX_ANSWERS_START" in raw_ref and "LATEX_ANSWERS_END" not in raw_ref:
                                    raw_ref += "\n===LATEX_ANSWERS_END==="
                                out = raw_ref.replace("```latex", "").replace("```", "")
                                audit_res["auto_corrected"] = True
                                if res_refined.usage_metadata:
                                    in_tok += getattr(res_refined.usage_metadata, "prompt_token_count", 0)
                                    out_tok += getattr(res_refined.usage_metadata, "candidates_token_count", 0)
                                break
                        except Exception:
                            continue

        # Extract filename if AI suggested one
        fn_m = re.search(r"===?\s*FILENAME_START\s*===?(.*?)===?\s*FILENAME_END\s*===?", out, re.DOTALL | re.IGNORECASE)
        if fn_m and fn_m.group(1).strip():
            ai_generated_name = re.sub(r"[^A-Za-z0-9_\-\(\) ]", "_", fn_m.group(1).strip())
            raw_ai = ai_generated_name.replace("_", " ").strip()
            clean_topic = clean_redundant_topic_prefix(raw_ai, grades_string, subject, actual_level) or raw_ai
            display_topic = f"{clean_topic} Set {current_set_number}"

        c_m = re.search(r"===?\s*LATEX_CONTENT_START\s*===?(.*?)(?:===?\s*LATEX_CONTENT_END\s*===?|===?\s*LATEX_ANSWERS_START|$)", out, re.DOTALL | re.IGNORECASE)
        a_m = re.search(r"===?\s*LATEX_ANSWERS_START\s*===?(.*?)(?:===?\s*LATEX_ANSWERS_END\s*===?|$)", out, re.DOTALL | re.IGNORECASE)

        q_sanitized = sanitize_ai_latex(c_m.group(1).strip() if c_m else "Failed.")
        a_sanitized = sanitize_ai_latex(a_m.group(1).strip() if a_m else "Failed.")

        # Phase 2: Solutions (if auto_solutions requested)
        s_sanitized = ""
        if auto_solutions:
            _notify("🧠 Calculating deep step-by-step mathematical solutions...")
            p2_payload = ai_payload + [
                out,
                "\n\nPHASE 2 INSTRUCTION: Excellent. Now, generate the comprehensive step-by-step fully worked solutions for these EXACT questions. You must place your entire output between the tags ===LATEX_SOLUTIONS_START=== and ===LATEX_SOLUTIONS_END===.\n"
                "MANDATORY DIAGRAMS IN WORKED SOLUTIONS: You MUST ALWAYS include visual diagrams (using Native TikZ or the Python Graph Engine) in the fully worked solutions whenever a diagram helps the student visualize, follow, or understand the working out (e.g. geometric proofs, vector triangles/addition, coordinate geometry sketches, integration regions/areas between curves, sign diagrams, normal distribution shading, or 3D angles). Do NOT rely purely on text and algebra when a visual sketch aids understanding.\n"
                "Group trivial algebra. Show all key mathematical steps and justifications. Never use \\tag inside $$ ... $$ or \\[ ... \\]; use \\qquad (1) if numbering equations.",
            ]
            s_out = None
            for s_model in models_to_try:
                cur_max_tok = MAX_OUTPUT_TOKENS
                tool_candidates = [[], active_tools] if (total_q >= 30 or not active_tools) else [active_tools, []]
                for t_list in tool_candidates:
                    try:
                        cfg = types.GenerateContentConfig(
                            max_output_tokens=cur_max_tok,
                            tools=t_list if t_list else None,
                            safety_settings=[
                                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            ],
                        )
                        res2 = client.models.generate_content(model=s_model, contents=p2_payload, config=cfg)
                        raw_s = _extract_genai_text(res2)
                        if not raw_s or "LATEX_SOLUTIONS_START" not in raw_s:
                            raise ValueError("Solutions truncated. Retrying...")
                        if "LATEX_SOLUTIONS_END" not in raw_s:
                            raw_s += "\n===LATEX_SOLUTIONS_END==="
                        s_out = raw_s.replace("```latex", "").replace("```", "")
                        if res2.usage_metadata:
                            in_tok += getattr(res2.usage_metadata, "prompt_token_count", 0)
                            out_tok += getattr(res2.usage_metadata, "candidates_token_count", 0)
                        break
                    except Exception:
                        if t_list:
                            continue
                        break
                if s_out:
                    break

            if s_out:
                s_m = re.search(r"===?\s*LATEX_SOLUTIONS_START\s*===?(.*?)(?:===?\s*LATEX_SOLUTIONS_END\s*===?|$)", s_out, re.DOTALL | re.IGNORECASE)
                s_sanitized = sanitize_ai_latex(s_m.group(1).strip() if s_m else "")

        _notify("⚙️ Compiling PDF & Word documents...")
        title = f"{grades_string} {subject}{level_text}".strip()
        work_dir = tempfile.mkdtemp(prefix="da_exam_worker_")
        p_bytes, t_bytes, w_bytes, log, word_reason = _render_exam_files(
            work_dir, display_topic, title, q_sanitized, a_sanitized, s_sanitized, total_q
        )
        shutil.rmtree(work_dir, ignore_errors=True)

        # Pricing: Gemini 3.8 Flash ($0.75 / 1M in, $3.75 / 1M out) + Google Search ($0.014) + DeepSeek-R1 Auditor ($0.55 in, $2.19 out)
        in_cost = (in_tok / 1_000_000) * 0.75
        out_cost = (out_tok / 1_000_000) * 3.75
        search_cost = 0.014 if used_search else 0.00
        gemini_cost = round(in_cost + out_cost + search_cost, 5)
        ds_cost = round(float(audit_res.get("ds_cost", 0.0) if audit_res else 0.0), 5)
        total_cost = round(gemini_cost + ds_cost, 5)

        return {
            "success": True,
            "questions_text": q_sanitized,
            "answers_text": a_sanitized,
            "solutions_text": s_sanitized,
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
            "saved_has_solutions": bool(s_sanitized),
            "meta_mc": num_mc,
            "meta_easy": num_easy,
            "meta_med": num_med,
            "meta_hard": num_hard,
            "meta_xh": num_xh,
            "meta_extra_instructions": extra_instructions.strip() if extra_instructions else "",
            "deepseek_audit": audit_res,
            "meta_model_used": model_used or "gemini-3.8-flash",
            "meta_input_tokens": in_tok,
            "meta_output_tokens": out_tok,
            "meta_total_cost": total_cost,
            "meta_gemini_cost": gemini_cost,
            "meta_ds_cost": ds_cost,
            "used_search": used_search,
            "phase_1_raw": out,
            "saved_ai_payload": ai_payload,
            "created_by": created_by,
            "computer_name": params.get("computer_name") or get_effective_computer_name(),
        }
    except Exception as e:
        _log_error("generate_exam_worker", e)
        err_str = str(e)
        if "402" in err_str or "prepayment credits are depleted" in err_str.lower():
            friendly_msg = "Google Gemini API account balance is depleted ($0.00). Please top up credits or manage billing at https://ai.studio/projects to re-enable exam generation."
            return {"success": False, "error": friendly_msg, "raw_error": e}
        return {"success": False, "error": err_str, "raw_error": e}


def submit_exam_job(params: dict, teacher_username: str = "") -> str:
    """Submits an exam generation job to the background worker pool."""
    job_id = str(uuid.uuid4())[:8]
    comp_name = params.get("computer_name") or get_effective_computer_name()
    with GLOBAL_QUEUE_LOCK:
        GLOBAL_JOB_QUEUE[job_id] = {
            "id": job_id,
            "title": params.get("display_topic", "Exam"),
            "year": params.get("grades_string", ""),
            "subject": params.get("subject", ""),
            "level": params.get("actual_level", ""),
            "topic": params.get("clean_topic", ""),
            "total_q": params.get("total_q", 0),
            "meta_set": params.get("current_set_number", 1),
            "auto_solutions": params.get("auto_solutions", False),
            "created_at": datetime.now(timezone.utc),
            "created_by": params.get("created_by", "Teacher"),
            "computer_name": comp_name,
            "username": teacher_username,
            "status": "QUEUED",  # QUEUED, RUNNING, COMPLETED, FAILED
            "progress": "Waiting in queue...",
            "result": None,
            "error": None,
            "saved_to_cloud": False,
        }

    def _worker():
        with GLOBAL_QUEUE_LOCK:
            if job_id in GLOBAL_JOB_QUEUE:
                GLOBAL_JOB_QUEUE[job_id]["status"] = "RUNNING"
                GLOBAL_JOB_QUEUE[job_id]["progress"] = "Starting AI engine..."

        def cb(msg):
            with GLOBAL_QUEUE_LOCK:
                if job_id in GLOBAL_JOB_QUEUE:
                    GLOBAL_JOB_QUEUE[job_id]["progress"] = msg

        try:
            res = generate_exam_worker(params, progress_cb=cb)
            with GLOBAL_QUEUE_LOCK:
                if job_id in GLOBAL_JOB_QUEUE:
                    if res.get("success"):
                        GLOBAL_JOB_QUEUE[job_id]["status"] = "COMPLETED"
                        GLOBAL_JOB_QUEUE[job_id]["progress"] = "Ready!"
                        GLOBAL_JOB_QUEUE[job_id]["result"] = res
                        GLOBAL_JOB_QUEUE[job_id]["title"] = res.get("display_topic", GLOBAL_JOB_QUEUE[job_id]["title"])
                    else:
                        GLOBAL_JOB_QUEUE[job_id]["status"] = "FAILED"
                        GLOBAL_JOB_QUEUE[job_id]["error"] = res.get("error", "Generation failed.")
                        GLOBAL_JOB_QUEUE[job_id]["progress"] = "Failed"
        except Exception as e:
            _log_error(f"background_job_{job_id}", e)
            with GLOBAL_QUEUE_LOCK:
                if job_id in GLOBAL_JOB_QUEUE:
                    GLOBAL_JOB_QUEUE[job_id]["status"] = "FAILED"
                    GLOBAL_JOB_QUEUE[job_id]["error"] = friendly_error_message(e)
                    GLOBAL_JOB_QUEUE[job_id]["progress"] = "Failed"

    GLOBAL_JOB_EXECUTOR.submit(_worker)
    return job_id


def _load_job_into_session_state(res: dict):
    """Loads a completed job result dictionary directly into st.session_state for full previewing and editing."""
    work_dir = _start_new_work_dir()
    try:
        if res.get("pdf_bytes"):
            with open(os.path.join(work_dir, "exam.pdf"), "wb") as f:
                f.write(res["pdf_bytes"])
        if res.get("tex_bytes"):
            with open(os.path.join(work_dir, "exam.tex"), "wb") as f:
                f.write(res["tex_bytes"])
        if res.get("word_bytes"):
            with open(os.path.join(work_dir, "exam.docx"), "wb") as f:
                f.write(res["word_bytes"])
    except Exception:
        pass

    st.session_state.update({
        "questions_text": res.get("questions_text", ""),
        "answers_text": res.get("answers_text", ""),
        "solutions_text": res.get("solutions_text", ""),
        "pdf_bytes": res.get("pdf_bytes"),
        "tex_bytes": res.get("tex_bytes"),
        "word_bytes": res.get("word_bytes"),
        "word_skip_reason": res.get("word_skip_reason"),
        "compiler_log": res.get("compiler_log", ""),
        "meta_topic": res.get("meta_topic", ""),
        "meta_subject": res.get("meta_subject", ""),
        "meta_year": res.get("meta_year", ""),
        "meta_diff": res.get("meta_diff", ""),
        "meta_n": res.get("meta_n", 0),
        "meta_set": res.get("meta_set", 1),
        "display_topic": res.get("display_topic", ""),
        "cloud_saved": res.get("cloud_saved", False),
        "saved_exam_id": res.get("saved_exam_id"),
        "saved_has_solutions": res.get("saved_has_solutions", False),
        "meta_mc": res.get("meta_mc", 0),
        "meta_easy": res.get("meta_easy", 0),
        "meta_med": res.get("meta_med", 0),
        "meta_hard": res.get("meta_hard", 0),
        "meta_xh": res.get("meta_xh", 0),
        "meta_extra_instructions": res.get("meta_extra_instructions", ""),
        "deepseek_audit": res.get("deepseek_audit"),
        "meta_model_used": res.get("meta_model_used"),
        "meta_input_tokens": res.get("meta_input_tokens", 0),
        "meta_output_tokens": res.get("meta_output_tokens", 0),
        "meta_total_cost": res.get("meta_total_cost", 0.0),
        "meta_gemini_cost": res.get("meta_gemini_cost", 0.0),
        "meta_ds_cost": res.get("meta_ds_cost", 0.0),
        "used_search": res.get("used_search", False),
        "phase_1_raw": res.get("phase_1_raw", ""),
        "saved_ai_payload": res.get("saved_ai_payload", []),
    })


def _save_queue_job_to_cloud(job: dict) -> tuple[bool, str]:
    """Helper to save a completed queue job result to Supabase cloud library."""
    res = job.get("result")
    if not res:
        return False, "Exam result not ready."
    creator = job.get("created_by") or "Teacher"
    comp = job.get("computer_name") or ""
    c_by = res.get("created_by") or creator
    if comp and comp not in c_by:
        c_by = f"{c_by} ({comp})"
    s_ok, s_msg = save_to_supabase(
        res.get("meta_topic") or job.get("title", "Exam"),
        res.get("meta_subject") or job.get("subject", ""),
        res.get("meta_year") or job.get("year", ""),
        res.get("meta_diff") or job.get("level", ""),
        res.get("meta_set") or job.get("meta_set", 1),
        res.get("pdf_bytes"),
        res.get("word_bytes"),
        num_mc=res.get("meta_mc", 0),
        num_easy=res.get("meta_easy", 0),
        num_med=res.get("meta_med", 0),
        num_hard=res.get("meta_hard", 0),
        num_xh=res.get("meta_xh", 0),
        existing_id=None,
        cost=res.get("meta_total_cost"),
        gemini_cost=res.get("meta_gemini_cost"),
        ds_cost=res.get("meta_ds_cost"),
        model=res.get("meta_model_used"),
        extra_instructions=res.get("meta_extra_instructions", ""),
        created_by=c_by,
        has_solutions=bool(res.get("solutions_text")) or bool(job.get("auto_solutions")),
        questions_text=res.get("questions_text", ""),
        answers_text=res.get("answers_text", ""),
        solutions_text=res.get("solutions_text", ""),
        audit_data=res.get("deepseek_audit"),
    )
    if s_ok:
        job["saved_to_cloud"] = True
    return s_ok, s_msg


def render_exam_queue_dashboard():
    """Renders active, queued, and completed background generation jobs."""
    with GLOBAL_QUEUE_LOCK:
        active_jobs = [j for j in GLOBAL_JOB_QUEUE.values() if j.get("status") in ("QUEUED", "RUNNING")]
        done_jobs = [j for j in GLOBAL_JOB_QUEUE.values() if j.get("status") in ("COMPLETED", "FAILED")]

    active_jobs.sort(key=lambda x: _safe_dt_timestamp(x.get("created_at")))
    done_jobs.sort(key=lambda x: _safe_dt_timestamp(x.get("created_at")), reverse=True)
    valid_jobs = active_jobs + done_jobs[:100]

    if not valid_jobs:
        st.markdown("---")
        try:
            empty_container = st.container(border=True)
        except TypeError:
            empty_container = st.container()
        with empty_container:
            st.markdown("### 📋 Active Exam Generation Queue (Idle)")
            st.caption("No exams currently in the queue. Click **✨ Generate Exam** above to generate exams in the background. You can queue multiple exams back-to-back without waiting!")
        return

    st.markdown("---")
    n_running = sum(1 for j in active_jobs if j.get("status") == "RUNNING")
    n_queued = sum(1 for j in active_jobs if j.get("status") == "QUEUED")
    n_done = sum(1 for j in done_jobs if j.get("status") == "COMPLETED")

    badges = []
    if n_running:
        badges.append(f"{n_running} Generating")
    if n_queued:
        badges.append(f"{n_queued} Queued")
    if n_done:
        badges.append(f"{n_done} Ready")
    summary_str = " · ".join(badges) if badges else f"{len(valid_jobs)} Exams"

    unsaved_jobs = [
        j for j in done_jobs
        if j.get("status") == "COMPLETED" and not j.get("saved_to_cloud") and j.get("result")
    ]

    q_head_c1, q_head_c2, q_head_c3, q_head_c4 = st.columns([3, 1.3, 1.1, 1.1], vertical_alignment="center")
    with q_head_c1:
        st.markdown(f"### 📋 Active Exam Generation Queue ({summary_str})")
    with q_head_c2:
        save_all_btn_text = f"💾 Save All ({len(unsaved_jobs)})" if unsaved_jobs else "💾 Save All"
        if st.button(save_all_btn_text, key="btn_save_all_q", type="primary" if unsaved_jobs else "secondary", disabled=(len(unsaved_jobs) == 0), help="Save all completed exams in the queue to Cloud Library"):
            success_count = 0
            err_count = 0
            with st.spinner(f"Saving {len(unsaved_jobs)} exam(s) to Cloud Library..."):
                for j in unsaved_jobs:
                    s_ok, _ = _save_queue_job_to_cloud(j)
                    if s_ok:
                        success_count += 1
                    else:
                        err_count += 1
            if success_count > 0:
                st.toast(f"☁️ Successfully saved {success_count} exam(s) to Cloud Library!")
            if err_count > 0:
                st.warning(f"⚠️ {err_count} exam(s) failed to save.")
            st.rerun()
    with q_head_c3:
        if st.button("🔄 Refresh Status", key="btn_refresh_q", help="Refresh progress of background jobs"):
            st.rerun()
    with q_head_c4:
        if st.button("🗑️ Clear Done", key="btn_clear_done_q", help="Clear completed or failed jobs from this list"):
            with GLOBAL_QUEUE_LOCK:
                for j in done_jobs:
                    if j["id"] in GLOBAL_JOB_QUEUE:
                        del GLOBAL_JOB_QUEUE[j["id"]]
            st.rerun()

    for idx, job in enumerate(valid_jobs):
        jid = job["id"]
        status = job.get("status", "QUEUED")
        title = job.get("title", "Exam")
        total_q = job.get("total_q", 0)
        progress = job.get("progress", "")
        lvl = f" {job['level']}" if job.get("level") else ""
        creator = job.get("created_by") or "Teacher"
        comp = job.get("computer_name") or ""
        date_str, time_str = format_job_sydney_datetime(job.get("created_at"))
        meta_line = f"{job.get('year', '')}{lvl} · {job.get('subject', '')} · {total_q} Questions"
        if date_str:
            meta_line += f" · 📅 {date_str}"
        if time_str:
            meta_line += f" · 🕒 {time_str}"
        meta_line += f" · 👤 {creator}"
        if comp and comp not in creator:
            meta_line += f" · 💻 {comp}"
        if job.get("auto_solutions"):
            meta_line += " · 🧠 With Solutions"
        res_data = job.get("result") or {}
        cost_val = res_data.get("meta_total_cost") if res_data.get("meta_total_cost") is not None else job.get("cost")
        if cost_val is not None:
            try:
                g_cost = res_data.get("meta_gemini_cost")
                d_cost = float(res_data.get("meta_ds_cost") or 0.0)
                if g_cost is None:
                    g_cost = max(0.0, float(cost_val) - d_cost)
                else:
                    g_cost = float(g_cost)
                if d_cost > 0:
                    meta_line += f" · 💸 ${float(cost_val):.4f} (Gemini: ${g_cost:.4f} · DeepSeek: ${d_cost:.4f})"
                else:
                    meta_line += f" · 💸 ${float(cost_val):.4f} (Gemini: ${g_cost:.4f})"
            except Exception:
                meta_line += f" · 💸 ${float(cost_val):.4f}"

        try:
            card_container = st.container(border=True)
        except TypeError:
            card_container = st.container()
        with card_container:
            c_top1, c_top2 = st.columns([4, 2], vertical_alignment="center")
            with c_top1:
                st.markdown(f"**#{idx + 1}. {title}**")
                st.caption(meta_line)
            with c_top2:
                if status == "RUNNING":
                    st.markdown('<span style="background-color:#E3F2FD; color:#1565C0; padding:4px 10px; border-radius:12px; font-weight:bold; font-size:13px;">🔄 Generating</span>', unsafe_allow_html=True)
                elif status == "QUEUED":
                    st.markdown('<span style="background-color:#FFF3E0; color:#E65100; padding:4px 10px; border-radius:12px; font-weight:bold; font-size:13px;">⏳ Queued</span>', unsafe_allow_html=True)
                elif status == "COMPLETED":
                    st.markdown('<span style="background-color:#E8F5E9; color:#2E7D32; padding:4px 10px; border-radius:12px; font-weight:bold; font-size:13px;">✅ Completed</span>', unsafe_allow_html=True)
                elif status == "FAILED":
                    st.markdown('<span style="background-color:#FFEBEE; color:#C62828; padding:4px 10px; border-radius:12px; font-weight:bold; font-size:13px;">❌ Failed</span>', unsafe_allow_html=True)

            if status in ("RUNNING", "QUEUED"):
                st.progress(0.65 if status == "RUNNING" else 0.15)
                st.caption(f"ℹ️ {progress}")
            elif status == "FAILED":
                st.error(f"Error: {job.get('error', 'Unknown error')}")
            elif status == "COMPLETED":
                res = job.get("result", {})
                b1, b2, b3 = st.columns([2, 2, 2])
                with b1:
                    if st.button("👁️ Load to Preview", key=f"q_view_{jid}", type="primary", use_container_width=True):
                        _load_job_into_session_state(res)
                        st.toast(f"✅ Loaded {title} into Preview below!")
                        st.rerun()
                with b2:
                    if res.get("pdf_bytes"):
                        fn_pdf = build_exam_filename(
                            res.get("meta_topic") or title,
                            res.get("meta_year") or job.get("year", ""),
                            res.get("meta_subject") or job.get("subject", ""),
                            res.get("meta_diff") or job.get("level", ""),
                            res.get("meta_set") or job.get("meta_set", 1),
                            has_solutions=bool(res.get("solutions_text")),
                            num_mc=res.get("meta_mc", 0),
                            num_easy=res.get("meta_easy", 0),
                            num_med=res.get("meta_med", 0),
                            num_hard=res.get("meta_hard", 0),
                            num_xh=res.get("meta_xh", 0),
                            ext="pdf",
                        )
                        st.download_button(
                            "📥 Download PDF",
                            data=res["pdf_bytes"],
                            file_name=fn_pdf,
                            mime="application/pdf",
                            key=f"q_dl_{jid}",
                            use_container_width=True,
                        )
                with b3:
                    if not job.get("saved_to_cloud"):
                        if st.button("💾 Save to Library", key=f"q_save_{jid}", use_container_width=True):
                            s_ok, s_msg = _save_queue_job_to_cloud(job)
                            if s_ok:
                                st.toast(f"☁️ Saved {title} to Cloud Library!")
                                st.rerun()
                            else:
                                st.error(f"Failed to save: {s_msg}")
                    else:
                        st.caption("☁️ Saved to Cloud Library")


def render_exam_queue_section():
    """Wrapper that enables live 3-second auto-refresh if jobs are actively processing."""
    with GLOBAL_QUEUE_LOCK:
        has_running = any(
            j.get("status") in ("RUNNING", "QUEUED")
            for j in GLOBAL_JOB_QUEUE.values()
        )

    if has_running and hasattr(st, "fragment"):
        @st.fragment(run_every="3s")
        def _frag_queue():
            render_exam_queue_dashboard()
        _frag_queue()
    else:
        render_exam_queue_dashboard()



# ══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG & UI
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="DA Tuition Generator",
    page_icon=LOGO_PATH if os.path.exists(LOGO_PATH) else "📚",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(
    """
    <style>
        .stApp { background-color: #EAF6FF; }
        [data-testid="stHeader"] { background-color: transparent; }
        /* Ensure the sidebar open button (>> in top-left) is always visible and clickable */
        [data-testid="collapsedControl"],
        [data-testid="stSidebarCollapsedControl"],
        button[data-testid="stSidebarCollapseButton"] {
            display: flex !important;
            visibility: visible !important;
            opacity: 1 !important;
            color: #31333F !important;
            z-index: 999999 !important;
        }
        /* Hide ONLY the Streamlit Community Cloud Fork button, GitHub link, and 3-dots menu */
        .stAppDeployButton,
        [data-testid="stAppDeployButton"],
        header a[href*="github.com"],
        #MainMenu,
        footer,
        [data-testid="stDecoration"] {
            display: none !important;
            visibility: hidden !important;
        }
        html, body, p, span, label, input, textarea, button, 
        [data-testid="stWidgetLabel"] p, .stMarkdown p, .stButton button, 
        div[role="listbox"], div[role="combobox"] span, .stSelectbox div, .stMultiSelect div { font-size: 18px !important; }
        /* Completely hide "Press Enter to apply" overlay inside input boxes to prevent text overlap */
        [data-testid="InputInstructions"],
        [data-testid="stInputInstructions"],
        .stInputInstructions {
            display: none !important;
            visibility: hidden !important;
            height: 0 !important;
            width: 0 !important;
            opacity: 0 !important;
            pointer-events: none !important;
        }
        /* Keep dropdown popovers neat, properly aligned to their selectboxes, and bounded */
        div[data-baseweb="popover"] {
            max-width: min(550px, 90vw) !important;
        }
        div[data-baseweb="popover"] div[data-baseweb="menu"],
        div[data-baseweb="popover"] ul[role="listbox"],
        div[data-testid="stSelectboxVirtualDropdown"] {
            max-width: min(550px, 90vw) !important;
        }
        div[data-baseweb="popover"] li[role="option"],
        div[data-baseweb="popover"] div[role="option"],
        div[role="listbox"] [role="option"],
        ul[role="listbox"] li {
            white-space: nowrap !important;
            text-overflow: ellipsis !important;
            overflow: hidden !important;
            height: auto !important;
            min-height: 38px !important;
            line-height: 1.4 !important;
            padding-top: 6px !important;
            padding-bottom: 6px !important;
        }
        div[data-baseweb="popover"] li[role="option"] > div,
        div[data-baseweb="popover"] li[role="option"] span,
        div[data-baseweb="popover"] [role="option"] * {
            white-space: nowrap !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
        }
        /* Layout for topic dropdown options: topic name left, Yr label right */
        .da-topic-flex-row,
        div[data-baseweb="popover"] li[role="option"] > div.da-topic-flex-row,
        div[data-baseweb="popover"] li[role="option"] .da-topic-flex-row {
            display: flex !important;
            justify-content: space-between !important;
            align-items: center !important;
            width: 100% !important;
            min-width: 0 !important;
            overflow: visible !important;
        }
        .da-topic-text-left {
            flex: 1 1 auto !important;
            min-width: 0 !important;
            text-align: left !important;
            white-space: nowrap !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
        }
        .da-topic-yr-right {
            flex: 0 0 auto !important;
            margin-left: 14px !important;
            text-align: right !important;
            white-space: nowrap !important;
            font-size: 13px !important;
            font-weight: 600 !important;
            color: #475569 !important;
            background-color: #f1f5f9 !important;
            border: 1px solid #e2e8f0 !important;
            padding: 1px 7px !important;
            border-radius: 6px !important;
            letter-spacing: 0.3px !important;
            overflow: visible !important;
            text-overflow: clip !important;
        }
        div[data-baseweb="popover"] [role="option"]:hover .da-topic-yr-right,
        ul[role="listbox"] li:hover .da-topic-yr-right {
            background-color: #e2e8f0 !important;
            color: #1e293b !important;
            border-color: #cbd5e1 !important;
        }
        div[data-baseweb="popover"] [role="option"]:hover,
        ul[role="listbox"] li:hover {
            background-color: #f0f4f9 !important;
            cursor: pointer !important;
        }
        /* Completely hide 0-height custom components used for JS helpers */
        [data-testid="stCustomComponentV1"]:has(iframe[height="0"]),
        iframe[title="st_option_tooltip"],
        iframe[srcdoc*="da-tuition-option-tooltip"] {
            position: absolute !important;
            top: -9999px !important;
            left: -9999px !important;
            width: 0 !important;
            height: 0 !important;
            opacity: 0 !important;
            pointer-events: none !important;
        }
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

# ── FLOATING HOVER TOOLTIP INJECTION FOR LONG DROPDOWN OPTIONS ─────────────────
import streamlit.components.v1 as components

PASTE_COMPONENT_DIR = os.path.join(os.path.dirname(__file__), "paste_component")
_paste_component = None
if os.path.exists(PASTE_COMPONENT_DIR):
    try:
        _paste_component = components.declare_component("paste_uploader", path=PASTE_COMPONENT_DIR)
    except Exception as _e:
        _log_error("declare_paste_component", _e)

components.html(
    """
    <script>
    (function() {
        try {
            const parentDoc = window.parent.document;
            if (!parentDoc) return;

            // Only initialize listeners once per window session
            if (parentDoc.__da_tooltip_initialized) return;
            parentDoc.__da_tooltip_initialized = true;

            // Ensure computer name stored in localStorage is mirrored to document cookie
            try {
                const savedComp = localStorage.getItem('da_computer_name');
                if (savedComp && parentDoc.cookie.indexOf('da_computer_name=') === -1) {
                    parentDoc.cookie = 'da_computer_name=' + encodeURIComponent(savedComp) + '; path=/; max-age=31536000; SameSite=Lax';
                }
            } catch(e) {}

            let tooltip = parentDoc.getElementById('da-tuition-option-tooltip');
            if (!tooltip) {
                tooltip = parentDoc.createElement('div');
                tooltip.id = 'da-tuition-option-tooltip';
                tooltip.style.cssText = `
                    position: fixed;
                    display: none;
                    background-color: #1e293b;
                    color: #ffffff;
                    font-size: 14px;
                    font-weight: 500;
                    line-height: 1.4;
                    padding: 8px 12px;
                    border-radius: 8px;
                    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.25), 0 2px 6px rgba(0, 0, 0, 0.15);
                    border: 1px solid rgba(255, 255, 255, 0.15);
                    z-index: 99999999;
                    pointer-events: none;
                    max-width: 450px;
                    word-wrap: break-word;
                    white-space: normal;
                    transition: opacity 0.12s ease-in-out;
                    opacity: 0;
                `;
                parentDoc.body.appendChild(tooltip);
            }

            // Automatically split topic name (left) and year label (right) inside dropdown popovers
            function formatTopicOptions() {
                try {
                    const popovers = parentDoc.querySelectorAll('div[data-baseweb="popover"], [data-testid="stSelectboxVirtualDropdown"]');
                    if (!popovers.length) return;

                    popovers.forEach(function(popover) {
                        const options = popover.querySelectorAll('li[role="option"], div[role="option"]');
                        options.forEach(function(opt) {
                            if (opt.getAttribute('data-da-yr-aligned')) return;

                            const fullText = (opt.textContent || '').trim();
                            const match = fullText.match(/^(.*?)\s*(\((?:Yr|Year)\s*\d+\))$/i);
                            if (match) {
                                opt.setAttribute('data-da-yr-aligned', 'true');
                                const topicTitle = match[1].trim();
                                const yrBadge = match[2].trim();

                                if (!opt.getAttribute('title')) {
                                    opt.setAttribute('title', fullText);
                                }

                                const safeTitle = topicTitle.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
                                const safeBadge = yrBadge.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

                                let target = opt.querySelector('div') || opt;
                                const children = opt.querySelectorAll('div, span');
                                for (let i = children.length - 1; i >= 0; i--) {
                                    const c = children[i];
                                    if (c.children.length === 0 && /\((?:Yr|Year)\s*\d+\)$/i.test((c.textContent || '').trim())) {
                                        target = c;
                                        break;
                                    }
                                }

                                target.innerHTML = '<span class="da-topic-text-left">' + safeTitle + '</span><span class="da-topic-yr-right">' + safeBadge + '</span>';
                                target.classList.add('da-topic-flex-row');
                            }
                        });
                    });
                } catch(e) {}
            }

            const daObserver = new MutationObserver(function() {
                formatTopicOptions();
            });
            daObserver.observe(parentDoc.body, { childList: true, subtree: true });

            parentDoc.addEventListener('scroll', function() {
                formatTopicOptions();
            }, true);

            parentDoc.addEventListener('mouseover', function(e) {
                formatTopicOptions();
                const opt = e.target.closest('[role="option"], div[data-baseweb="popover"] li, ul[role="listbox"] li, div[data-baseweb="menu"] li');
                if (opt) {
                    const text = (opt.innerText || opt.textContent || '').trim();
                    if (!text) return;

                    // Set title attribute as native fallback
                    if (!opt.getAttribute('title')) {
                        opt.setAttribute('title', text);
                    }

                    // Only show floating tooltip for long or truncated options
                    const isOverflowing = opt.scrollWidth > (opt.clientWidth + 2) ||
                                          (opt.firstElementChild && opt.firstElementChild.scrollWidth > (opt.firstElementChild.clientWidth + 2)) ||
                                          text.length > 22;

                    if (isOverflowing) {
                        tooltip.textContent = text;
                        tooltip.style.display = 'block';
                        tooltip.style.opacity = '1';

                        const rect = opt.getBoundingClientRect();
                        let left = rect.right + 10;
                        let top = rect.top + (rect.height / 2) - 16;

                        // If it would overflow right of window, position below or align left
                        if (left + 360 > window.parent.innerWidth) {
                            left = Math.max(12, rect.left);
                            top = rect.bottom + 6;
                        }

                        // If it would overflow bottom of window, flip above
                        if (top + 60 > window.parent.innerHeight) {
                            top = Math.max(10, rect.top - 45);
                        }

                        tooltip.style.left = left + 'px';
                        tooltip.style.top = top + 'px';
                    }
                }
            }, true);

            parentDoc.addEventListener('mouseout', function(e) {
                const opt = e.target.closest('[role="option"], div[data-baseweb="popover"] li, ul[role="listbox"] li, div[data-baseweb="menu"] li');
                if (opt && tooltip) {
                    tooltip.style.opacity = '0';
                    tooltip.style.display = 'none';
                }
            }, true);

            parentDoc.addEventListener('click', function() {
                setTimeout(formatTopicOptions, 50);
                if (tooltip) {
                    tooltip.style.opacity = '0';
                    tooltip.style.display = 'none';
                }
            }, true);

        } catch (err) {
            console.warn("Tooltip injection error:", err);
        }
    })();
    </script>
    """,
    height=0,
    width=0,
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
            station_input = st.text_input(
                "Computer / Workstation Name (Optional)",
                value=get_effective_computer_name(),
                placeholder="e.g. Bunsea's Mac, Front Desk PC, Lab 1",
                help="Enter a name for this computer so exams generated from it are labeled with your workstation name."
            ).strip()
            submit_master = st.form_submit_button("Log In as Senior Teacher", type="primary", use_container_width=True)

            if submit_master:
                if master_pwd in ("DA2026", "DA_ADMIN"):
                    st.session_state["logged_in"] = True
                    st.session_state["user_role"] = "senior"
                    st.session_state["username"] = "admin"
                    comp_name = station_input if station_input else get_effective_computer_name()
                    st.session_state["computer_name"] = comp_name
                    if station_input and station_input not in ("Computer", "Mac", "Windows PC"):
                        st.session_state["user_display_name"] = f"Senior Teacher ({station_input})"
                    else:
                        st.session_state["user_display_name"] = "Senior Teacher"
                    st.session_state["user_api_key"] = os.environ.get("GEMINI_API_KEY", "")
                    st.session_state["admin_pin"] = "DA_ADMIN"
                    sync_computer_name_to_client(comp_name)
                    st.success("Senior Teacher access granted!")
                    time.sleep(0.4)
                    st.rerun()
                else:
                    st.error("Incorrect password or PIN.")

    with login_tab3:
        st.markdown("##### Register New Junior Teacher Account")
        with st.form("junior_signup_form"):
            reg_name = st.text_input("Full Name", placeholder="e.g. Sarah Connor").strip()
            reg_user = st.text_input("Desired Username", placeholder="e.g. sarah_c").strip()
            reg_pwd1 = st.text_input("Password (min 4 characters)", type="password")
            reg_pwd2 = st.text_input("Confirm Password", type="password")
            reg_key = st.text_input(
                "Your Gemini API Key (Required)",
                type="password",
                placeholder="AIzaSy...",
                help="Get your free API key at https://aistudio.google.com/apikey",
            ).strip()
            submit_reg = st.form_submit_button("Create Account", type="primary", use_container_width=True)

            if submit_reg:
                if not reg_name or not reg_user:
                    st.error("Please enter your name and choose a username.")
                elif reg_pwd1 != reg_pwd2:
                    st.error("Passwords do not match.")
                elif len(reg_pwd1) < 4:
                    st.error("Password must be at least 4 characters.")
                elif not reg_key:
                    st.error("A Gemini API Key is required to generate exams. Get one free at https://aistudio.google.com/apikey")
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

    current_station = get_effective_computer_name()
    with st.expander(f"💻 Workstation: **{current_station}**", expanded=False):
        new_station_val = st.text_input(
            "Computer / Station Name",
            value=current_station,
            key="input_sidebar_station_name",
            placeholder="e.g. Bunsea's Mac, Front Desk PC, Lab 1",
            help="This station name tags every exam you generate so other teachers know which computer made it."
        ).strip()
        if st.button("💾 Save Station Name", key="btn_save_station_name", use_container_width=True):
            if new_station_val:
                st.session_state["computer_name"] = new_station_val
                if st.session_state.get("user_role") == "senior":
                    st.session_state["user_display_name"] = f"Senior Teacher ({new_station_val})"
                sync_computer_name_to_client(new_station_val)
                st.toast(f"💻 Workstation saved: {new_station_val}")
                time.sleep(0.3)
                st.rerun()

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
    # If a library button set the redirect flag, honour it by setting the
    # default index *before* the widget is instantiated (you cannot assign
    # to a widget-bound key after the widget has rendered).
    _redirect_mode = st.session_state.pop("_redirect_to_generator", False)
    _default_idx = 0 if _redirect_mode else app_modes.index(st.session_state.get("main_app_mode", "✨ Generator")) if st.session_state.get("main_app_mode") in app_modes else 0
    app_mode = st.radio("App Mode", app_modes, index=_default_idx, key="main_app_mode")
    st.markdown("---")
    use_deepseek_auditor = False
    if app_mode == "✨ Generator":
        st.header("⚙️ Advanced Settings")
        use_deepseek_auditor = st.checkbox(
            "🛡️ DeepSeek-R1 Quality Auditor",
            value=True,
            help="Uses DeepSeek-R1 (deepseek-reasoner) to audit questions and answers for mathematical correctness, solvability, and NESA syllabus compliance before finalizing.",
        )
        with st.expander("🛡️ Auditor Settings", expanded=False):
            st.caption("DeepSeek-R1 independently verifies questions and auto-instructs Gemini if corrections are required.")
            _saved_ds_key = st.session_state.get("deepseek_api_key", "")
            ds_key_input = st.text_input(
                "DeepSeek API Key",
                type="password",
                value=_saved_ds_key,
                placeholder="sk-...",
                key="ds_api_key_field",
                help="Enter your DeepSeek API key (defaults to tuition center key if empty).",
            )
            if ds_key_input:
                st.session_state["deepseek_api_key"] = ds_key_input.strip()
            _active_k = _get_deepseek_api_key()
            if _active_k:
                st.caption(f"Connected: `{_mask_api_key(_active_k)}` · Model: `deepseek-reasoner`")
            else:
                st.caption("⚠️ No DeepSeek key connected.")
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


@st.cache_data(ttl=600, show_spinner=False)
def _get_audit_meta_from_storage(pdf_url: str) -> dict:
    if not pdf_url or not supabase_client:
        return {}
    try:
        base_path = unquote(pdf_url.split("/exam-files/")[-1].split("?")[0])
        if not base_path.endswith(".pdf"):
            return {}
        stem = base_path[:-4]
        stems_to_try = [stem]
        stem_no_sol = re.sub(r"[\s_\-]w[\s_\-]sol$", "", stem, flags=re.IGNORECASE).strip()
        if stem_no_sol != stem:
            stems_to_try.append(stem_no_sol)
        for s in stems_to_try:
            try:
                res_bytes = supabase_client.storage.from_("exam-files").download(s + "_audit.json")
                if res_bytes:
                    return json.loads(res_bytes.decode("utf-8"))
            except Exception:
                pass
    except Exception:
        pass
    return {}


def _get_exam_cost_info(exam: dict) -> tuple[float, float, float, str]:
    """Extract stored cost or compute an accurate estimate including Gemini and DeepSeek-R1 breakdown.
    Returns (total_cost, gemini_cost, ds_cost, model_name)."""
    model_name = exam.get("model") or "gemini-3.8-flash"

    # 1. Direct columns in DB row if present
    if exam.get("gemini_cost") is not None and exam.get("ds_cost") is not None:
        try:
            g = float(exam["gemini_cost"])
            d = float(exam["ds_cost"])
            t = float(exam.get("cost") or (g + d))
            return round(t, 5), round(g, 5), round(d, 5), model_name
        except (ValueError, TypeError):
            pass

    # 2. Check storage companion _audit.json
    pdf_url = exam.get("pdf_url", "")
    if pdf_url:
        meta = _get_audit_meta_from_storage(pdf_url)
        if meta and meta.get("gemini_cost") is not None and meta.get("ds_cost") is not None:
            g = float(meta["gemini_cost"])
            d = float(meta["ds_cost"])
            t = float(meta.get("total_cost") or (g + d))
            m = meta.get("model") or model_name
            return round(t, 5), round(g, 5), round(d, 5), m

    # 3. If exam has stored total cost in DB
    if exam.get("cost") is not None:
        try:
            t = float(exam["cost"])
            d = float(exam.get("ds_cost") or 0.0)
            if d == 0.0:
                # Standard DeepSeek-R1 auditor rate on HSC maths exams (~$0.0035 to $0.0055)
                # If total cost is around ~$0.0349, DeepSeek portion is $0.0045, Gemini is $0.0304
                if t >= 0.0300:
                    d = 0.0045
                    g = max(0.0, round(t - d, 5))
                else:
                    g = round(t, 5)
                    d = 0.0
            else:
                g = max(0.0, round(t - d, 5))
            return round(t, 5), round(g, 5), round(d, 5), model_name
        except (ValueError, TypeError):
            pass

    # 4. Fallback estimation based on questions count
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

    # Gemini 3.8 Flash pricing: $0.75 / 1M in, $3.75 / 1M out
    g_cost = ((est_in_tokens / 1_000_000) * 0.75) + ((est_out_tokens / 1_000_000) * 3.75)
    # DeepSeek-R1 auditor pricing: $0.55 / 1M in (~4,000 in), $2.19 / 1M out (~1,200 reasoning out)
    d_cost = ((4000 / 1_000_000) * 0.55) + ((1200 / 1_000_000) * 2.19)
    total_cost = g_cost + d_cost
    return round(total_cost, 5), round(g_cost, 5), round(d_cost, 5), model_name


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


def _get_exam_content_from_storage(pdf_url: str) -> tuple[str, str]:
    """Download the questions and answers companion files stored alongside the PDF.
    Returns (questions_text, answers_text).  Either or both may be empty."""
    q_text, a_text = "", ""
    if not pdf_url or not supabase_client:
        return q_text, a_text
    try:
        base_path = unquote(pdf_url.split("/exam-files/")[-1].split("?")[0])
        if not base_path.endswith(".pdf"):
            return q_text, a_text
        stem = base_path[:-4]
        stems_to_try = [stem]
        stem_no_sol = re.sub(r"[\s_\-]w[\s_\-]sol$", "", stem, flags=re.IGNORECASE).strip()
        if stem_no_sol != stem:
            stems_to_try.append(stem_no_sol)

        for s in stems_to_try:
            if not q_text:
                try:
                    res_q = supabase_client.storage.from_("exam-files").download(s + "_questions.txt")
                    if res_q:
                        q_text = res_q.decode("utf-8").strip()
                except Exception:
                    pass
            if not a_text:
                try:
                    res_a = supabase_client.storage.from_("exam-files").download(s + "_answers.txt")
                    if res_a:
                        a_text = res_a.decode("utf-8").strip()
                except Exception:
                    pass
            if q_text and a_text:
                break
    except Exception as e:
        _log_error("_get_exam_content_from_storage", e)
    return q_text, a_text


def _generate_solutions_for_library_exam(exam: dict) -> tuple[bool, str]:
    """Generate fully worked solutions for an existing library exam, rebuild its PDF
    with solutions appended, and re-upload to cloud.  Returns (success, message)."""
    pdf_url = exam.get("pdf_url", "")
    q_text, a_text = _get_exam_content_from_storage(pdf_url)
    if not q_text:
        return False, "Cannot generate solutions — the original questions were not stored with this exam. This feature only works for exams saved after this update."

    try:
        client = _get_genai_client()

        sol_prompt = (
            "You are a senior mathematics examiner. Below are the exam questions and their answer key in LaTeX.\n\n"
            "===QUESTIONS===\n" + q_text + "\n===END QUESTIONS===\n\n"
            "===ANSWERS===\n" + a_text + "\n===END ANSWERS===\n\n"
            "Generate step-by-step FULLY WORKED SOLUTIONS for every question above.\n"
            "Place your entire output between the tags ===LATEX_SOLUTIONS_START=== and ===LATEX_SOLUTIONS_END===.\n"
            "Rules:\n"
            "- Use LaTeX formatting (enumerate, align, etc.).\n"
            "- MANDATORY DIAGRAMS IN SOLUTIONS: You MUST ALWAYS include visual diagrams (using Native TikZ or the Python Graph Engine) in the worked solutions whenever a diagram helps the student visualize, follow, or understand the working out (e.g. geometric proofs, vector addition/triangles, coordinate geometry sketches, integration regions/areas between curves, sign diagrams, normal distribution shading, or 3D angles). Do NOT rely purely on text and algebra when a visual sketch aids understanding.\n"
            "- Show all key mathematical steps.\n"
            "- Group trivial algebra.\n"
            "- Do NOT regenerate the questions or answers — only generate the solutions.\n"
            "- Do NOT generate \\documentclass, \\usepackage, \\begin{document}, \\end{document}.\n"
            "- Do NOT use \\tag inside $$ ... $$; use \\qquad (1) if numbering equations.\n"
        )

        gen_config = types.GenerateContentConfig(
            max_output_tokens=MAX_OUTPUT_TOKENS,
            tools=[types.Tool(code_execution=types.ToolCodeExecution())],
            safety_settings=[
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
            ],
        )

        models_to_try = [
            "gemini-3.8-flash",
            "gemini-3.6-flash",
        ]
        s_out = None
        q_count = int(exam.get("total_questions") or 0)
        for model_name in models_to_try:
            cur_max = MAX_OUTPUT_TOKENS
            tool_candidates = [[], [types.Tool(code_execution=types.ToolCodeExecution())]] if q_count >= 30 else [[types.Tool(code_execution=types.ToolCodeExecution())], []]
            for t_list in tool_candidates:
                try:
                    cfg = types.GenerateContentConfig(
                        max_output_tokens=cur_max,
                        tools=t_list if t_list else None,
                        safety_settings=[
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                        ],
                    )
                    st.toast(f"🧠 {model_name}: Solving equations...")
                    res = client.models.generate_content(model=model_name, contents=[sol_prompt], config=cfg)
                    raw_s = _extract_genai_text(res)
                    if not raw_s or "LATEX_SOLUTIONS_START" not in raw_s:
                        raise ValueError("Solutions truncated. Retrying...")
                    if "LATEX_SOLUTIONS_END" not in raw_s:
                        raw_s += "\n===LATEX_SOLUTIONS_END==="
                    s_out = raw_s.replace("```latex", "").replace("```", "")
                    break
                except Exception as e:
                    if t_list:
                        continue
                    if _is_retryable(e):
                        st.toast(f"🚦 {model_name} busy. Pivoting to next model...")
                        time.sleep(1.5)
                        break
                    raise e
            if s_out:
                break

        if not s_out:
            return False, "All AI models failed to generate solutions. Please try again later."

        s_m = re.search(r"===?\s*LATEX_SOLUTIONS_START\s*===?(.*?)(?:===?\s*LATEX_SOLUTIONS_END\s*===?|$)", s_out, re.DOTALL | re.IGNORECASE)
        s_sanitized = sanitize_ai_latex(s_m.group(1).strip() if s_m else "Failed.")

        # Reconstruct topic and title from DB record
        topic_str = exam.get("topic", "Unknown")
        subject = exam.get("subject", "Maths")
        year = exam.get("year_group", "Year 12")
        diff = exam.get("difficulty", "")
        lvl_str = f" {diff}" if diff else ""
        title = f"{year} {subject}{lvl_str}".strip()

        # Parse set number and clean topic from the stored topic string
        set_m = re.search(r"\bSet\s*(\d+)\b", topic_str, re.IGNORECASE)
        set_num = int(set_m.group(1)) if set_m else 1
        clean_topic = re.sub(r"\s*\(Yr\d+\)\s*Set\s*\d+.*", "", topic_str).strip()
        display_topic = f"{clean_topic} Set {set_num}"

        # Count total questions for marks estimation
        total_q = len(re.findall(r"\\item", q_text))
        if total_q == 0:
            total_q = 20

        # Build the updated PDF with solutions
        work_dir = tempfile.mkdtemp(prefix="da_lib_sol_")
        try:
            pdf_bytes, tex_bytes, word_bytes, log, word_reason = _render_exam_files(
                work_dir, display_topic, title, q_text, a_text, s_sanitized, total_q
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        if not pdf_bytes:
            return False, f"PDF compilation failed: {log}"

        # Parse distribution from topic string
        dist_m = re.search(r"\((\d+\s+MC.*?)\)", topic_str)
        num_mc = num_easy = num_med = num_hard = num_xh = 0
        if dist_m:
            dist_text = dist_m.group(1)
            mc_m = re.search(r"(\d+)\s*MC", dist_text, re.IGNORECASE)
            easy_m = re.search(r"(\d+)\s*Easy", dist_text, re.IGNORECASE)
            med_m = re.search(r"(\d+)\s*Medium", dist_text, re.IGNORECASE)
            hard_m = re.search(r"(\d+)\s*Hard", dist_text, re.IGNORECASE)
            xh_m = re.search(r"(\d+)\s*Ext", dist_text, re.IGNORECASE)
            if mc_m: num_mc = int(mc_m.group(1))
            if easy_m: num_easy = int(easy_m.group(1))
            if med_m: num_med = int(med_m.group(1))
            if hard_m: num_hard = int(hard_m.group(1))
            if xh_m: num_xh = int(xh_m.group(1))

        # Re-save to cloud with solutions
        success, save_result = save_to_supabase(
            clean_topic, subject, year, diff, set_num,
            pdf_bytes, word_bytes,
            num_mc=num_mc, num_easy=num_easy, num_med=num_med,
            num_hard=num_hard, num_xh=num_xh,
            existing_id=exam.get("id"),
            cost=exam.get("cost"),
            gemini_cost=exam.get("gemini_cost"),
            ds_cost=exam.get("ds_cost"),
            model=exam.get("model"),
            extra_instructions=exam.get("extra_instructions") or exam.get("instructions") or "",
            created_by=exam.get("created_by", ""),
            has_solutions=True,
            questions_text=q_text,
            answers_text=a_text,
            solutions_text=s_sanitized,
        )

        if success:
            # Update has_solutions flag in DB
            for col in ("has_solutions",):
                try:
                    supabase_client.table("saved_exams").update({col: True}).eq("id", exam.get("id")).execute()
                except Exception:
                    pass
            return True, "Solutions generated and exam updated successfully!"
        return False, f"Solutions generated but failed to save: {save_result}"
    except Exception as e:
        return False, f"Error generating solutions: {str(e)}"


def _exam_has_solutions(exam: dict) -> bool:
    """Accurately checks whether an exam includes fully worked solutions from:
    1. Direct DB boolean/string flag
    2. 'w sol' or 'with solutions' indicator in the PDF URL
    3. 'w sol' or 'with solutions' indicator in the Word document URL
    4. Topic string containing 'w sol' or 'with solutions'
    """
    if not isinstance(exam, dict):
        return False

    # 1. Direct database boolean flag
    val = exam.get("has_solutions")
    if val is True or val == 1 or str(val).strip().lower() in ("true", "t", "1", "yes"):
        return True

    # 2. Check PDF and Docx URLs (e.g. "...Set11_10M-10H-10XH w sol.pdf")
    pdf_u = unquote(str(exam.get("pdf_url") or "")).lower()
    docx_u = unquote(str(exam.get("docx_url") or "")).lower()
    for u in (pdf_u, docx_u):
        if not u:
            continue
        if re.search(r"(?:[\s_\-]w[\s_\-]sol\b|with[\s_\-]solutions?|worked[\s_\-]solutions?)", u):
            return True
        if "w sol" in u or "w_sol" in u or "with_solutions" in u or "%20w%20sol" in u:
            return True

    # 3. Check topic title
    topic = str(exam.get("topic") or "").lower()
    if re.search(r"(?:[\s_\-\(]w[\s_\/]sol\b|with[\s_\-]solutions?|\(solutions\))", topic):
        return True
    if "w sol" in topic or "with solutions" in topic:
        return True

    return False


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
            
            # Extract distinct individual year groups and sort them naturally (Year 7, Year 8, ... Year 12)
            year_set = set()
            for ex in all_exams:
                yg = ex.get("year_group") or ""
                matches = re.findall(r"Year\s*\d+", yg, flags=re.IGNORECASE)
                if matches:
                    for m in matches:
                        y_num = re.search(r"\d+", m).group(0)
                        year_set.add(f"Year {y_num}")
                elif yg.strip():
                    year_set.add(yg.strip())
            
            def _yg_sort_key(y_str):
                m = re.search(r"\d+", str(y_str))
                return (0, int(m.group(0))) if m else (1, str(y_str))
                
            available_years = ["All"] + sorted(list(year_set), key=_yg_sort_key)

            st.markdown("### 🔍 Search, Filter & Sort")
            
            # --- NEW SEARCH BAR ---
            search_query = st.text_input("Search exams by keyword...", placeholder="Type to search topics, subjects, or levels...", label_visibility="collapsed")
            
            f_col1, f_col2, f_col3, f_col4 = st.columns([1, 1, 2.5, 1.2])
            with f_col1:
                filter_subject = st.selectbox("Subject", available_subjects)
            with f_col2:
                filter_year = st.selectbox("Year Group", available_years)

            # --- UPDATED FILTER LOGIC ---
            sq = (search_query or "").strip().lower()
            temp_filtered = [
                ex for ex in all_exams
                if (filter_subject == "All" or ex.get("subject") == filter_subject)
                and (
                    filter_year == "All"
                    or (filter_year.lower() in str(ex.get("year_group") or "").lower())
                    or (ex.get("year_group") == filter_year)
                )
                and (
                    not sq
                    or (sq in str(ex.get("topic") or "").lower())
                    or (sq in str(ex.get("subject") or "").lower())
                    or (sq in str(ex.get("difficulty") or "").lower())
                )
            ]
            
            available_topics = ["All"] + sorted(list(set([str(ex.get("topic") or "").split(" (")[0] for ex in temp_filtered if ex.get("topic")])))

            with f_col3:
                filter_topic = st.selectbox("Topic", available_topics)
            with f_col4:
                sort_option = st.selectbox("Sort By", ["Date Added (Newest)", "Date Added (Oldest)", "Name (A-Z)", "Name (Z-A)"])
            st.markdown("---")

            filtered_exams = [ex for ex in temp_filtered if filter_topic == "All" or str(ex.get("topic") or "").split(" (")[0] == filter_topic]

            if sort_option == "Date Added (Newest)":
                filtered_exams.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
            elif sort_option == "Date Added (Oldest)":
                filtered_exams.sort(key=lambda x: str(x.get("created_at") or ""))
            elif sort_option == "Name (A-Z)":
                filtered_exams.sort(key=lambda x: str(x.get("topic") or "").lower())
            elif sort_option == "Name (Z-A)":
                filtered_exams.sort(key=lambda x: str(x.get("topic") or "").lower(), reverse=True)

            if not filtered_exams:
                st.info("No exams match your current filters.")
            else:
                st.caption(f"Showing {len(filtered_exams)} exam(s)")
                for exam in filtered_exams:
                    lvl_str_lib = f" {exam['difficulty']}" if exam.get("difficulty") else ""
                    total_cost, gemini_cost, ds_cost, model_val = _get_exam_cost_info(exam)
                    if ds_cost > 0:
                        cost_badge = f"${total_cost:.4f} (Gemini: ${gemini_cost:.4f} · DeepSeek: ${ds_cost:.4f})"
                        engine_val = f"{model_val} + deepseek-r1"
                    else:
                        cost_badge = f"${total_cost:.4f} (Gemini: ${gemini_cost:.4f})"
                        engine_val = model_val

                    with st.expander(f"📝 {exam['topic']} ({exam['subject']} - {exam['year_group']}{lvl_str_lib})  ·  💸 {cost_badge}"):
                        sydney_timestamp = get_sydney_time(exam.get("created_at", ""))
                        teacher_name = exam.get("created_by") or "Senior Teacher"
                        has_sol = _exam_has_solutions(exam)
                        sol_badge = "✅ Worked Solutions" if has_sol else "📋 Short Answers Only (No Worked Solutions)"
                        st.caption(f"📅 **Generated:** {sydney_timestamp} &nbsp;&nbsp;|&nbsp;&nbsp; 👤 **Teacher:** {teacher_name} &nbsp;&nbsp;|&nbsp;&nbsp; 💸 **Cost:** {cost_badge} &nbsp;&nbsp;|&nbsp;&nbsp; 🤖 **Engine:** `{engine_val}` &nbsp;&nbsp;|&nbsp;&nbsp; 📄 **{sol_badge}**")

                        e_col1, e_col2, e_col3, e_col4 = st.columns([2, 2, 2, 1])
                        if exam.get("pdf_url"):
                            e_col1.markdown(f"[📥 Download PDF]({exam['pdf_url']})")
                        if exam.get("docx_url"):
                            e_col2.markdown(f"[📄 Download Word Doc]({exam['docx_url']})")

                        if not has_sol:
                            if e_col3.button("🧠 Generate Solutions", key=f"gen_sol_{exam['id']}", type="primary"):
                                with st.spinner("🤖 Generating fully worked solutions... This may take a minute."):
                                    ok, msg = _generate_solutions_for_library_exam(exam)
                                    if ok:
                                        st.success(f"✅ {msg}")
                                        time.sleep(1)
                                        st.rerun()
                                    else:
                                        st.error(f"⚠️ {msg}")

                        if e_col4.button("🗑️ Delete", key=f"del_{exam['id']}"):
                            with st.spinner("Deleting from Cloud..."):
                                paths_to_delete = []
                                if exam.get("pdf_url"):
                                    p_path = unquote(exam["pdf_url"].split("/exam-files/")[-1].split("?")[0])
                                    paths_to_delete.append(p_path)
                                    if p_path.endswith(".pdf"):
                                        stem = p_path[:-4]
                                        paths_to_delete.append(stem + "_instructions.txt")
                                        paths_to_delete.append(stem + "_questions.txt")
                                        paths_to_delete.append(stem + "_answers.txt")
                                        paths_to_delete.append(stem + "_solutions.txt")
                                        paths_to_delete.append(stem + "_audit.json")
                                        stem_no_sol = re.sub(r"[\s_\-]w[\s_\-]sol$", "", stem, flags=re.IGNORECASE).strip()
                                        if stem_no_sol != stem:
                                            paths_to_delete.append(stem_no_sol + "_instructions.txt")
                                            paths_to_delete.append(stem_no_sol + "_questions.txt")
                                            paths_to_delete.append(stem_no_sol + "_answers.txt")
                                            paths_to_delete.append(stem_no_sol + "_solutions.txt")
                                            paths_to_delete.append(stem_no_sol + "_audit.json")
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

                        # --- SET NUMBER EDIT SECTION ---
                        m_set = re.search(r"\bSet\s*(\d+)\b", exam.get("topic", ""), re.IGNORECASE)
                        cur_set = int(m_set.group(1)) if m_set else 1

                        st.markdown("---")
                        set_col1, set_col2 = st.columns([2, 3], vertical_alignment="bottom")
                        with set_col1:
                            new_set_val = st.number_input(
                                "Change Set #",
                                min_value=1,
                                max_value=999,
                                value=cur_set,
                                key=f"edit_set_{exam['id']}",
                                help="Change the set number for this saved exam in the library",
                            )
                        with set_col2:
                            if st.button("💾 Update Set #", key=f"btn_save_set_{exam['id']}"):
                                if new_set_val != cur_set:
                                    old_topic = exam.get("topic", "")
                                    if m_set:
                                        updated_topic = re.sub(r"\bSet\s*\d+\b", f"Set {new_set_val}", old_topic, count=1, flags=re.IGNORECASE)
                                    else:
                                        updated_topic = f"{old_topic} Set {new_set_val}"
                                    try:
                                        supabase_client.table("saved_exams").update({"topic": updated_topic}).eq("id", exam["id"]).execute()
                                        st.toast(f"✅ Set number updated to Set {new_set_val}!")
                                        time.sleep(0.5)
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Failed to update set number: {e}")
                                else:
                                    st.info(f"Exam is already Set {cur_set}.")

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
                                    st.session_state["_redirect_to_generator"] = True
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
    if subject == "Maths":
        if "Year 12" in year_group:
            level_opts = ["Standard", "Advanced", "Extension 1", "Extension 2"]
        elif "Year 11" in year_group:
            level_opts = ["Standard", "Advanced", "Extension 1"]
        elif "Year 9" in year_group or "Year 10" in year_group:
            level_opts = ["Advanced", "Standard"]
        else:
            level_opts = ["N/A"]
            level_disabled = True
    elif subject == "English" and ("Year 11" in year_group or "Year 12" in year_group):
        level_opts = ["Standard", "Advanced", "EALD", "Extension"]
    else:
        level_opts = ["N/A"]
        level_disabled = True
    if subject == "Maths" and not level_disabled:
        actual_levels = st.multiselect("Level", level_opts, help="Select all courses to include in this exam.")
    else:
        level = st.selectbox("Level", level_opts, disabled=level_disabled)
        actual_levels = [] if level == "N/A" else [level]

actual_level = " + ".join(actual_levels)

available_topics = []
topic_subtopics = {}
manifest_data = load_syllabus_manifest()

for y in year_group:
    for selected_level in (actual_levels or ([""] if subject != "Maths" or y not in ("Year 11", "Year 12", "Year 9", "Year 10") else [])):
        lvl_cands = resolve_level_for_year(y, subject, selected_level)
        course = None
        for cand in lvl_cands:
            for ck in (f"{y} {subject} {cand}".strip(), f"{y} {subject}".strip()):
                if ck in manifest_data:
                    course = manifest_data[ck]
                    break
            if course:
                break

        # Year 12 Maths follows the supplied textbook chapters even when sample folders exist.
        use_manifest = y == "Year 12" and subject == "Maths" and course is not None
        y_topics = [] if use_manifest else sorted(
            [d for d in get_available_topics(get_parent_path(y, subject, selected_level)) if d not in LEVEL_NAMES],
            key=lambda x: [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", x)],
        )
        if not y_topics and course:
            y_topics = [f"{c['num']}. {c['name']}" for c in course.get("chapters", [])]

        yr_num = re.sub(r"\D", "", y)
        badge = f"Yr {yr_num}" + (f", {selected_level}" if selected_level else "")
        for t in y_topics:
            label = f"{t} ({badge})"
            if label not in topic_subtopics:
                available_topics.append(label)
                chapter_name = re.sub(r"^\d+[.\-]\s*", "", t).strip()
                chapter = next((c for c in (course or {}).get("chapters", []) if c["name"].casefold() == chapter_name.casefold()), None)
                topic_subtopics[label] = chapter.get("subtopics", []) if chapter else []

c4, c5, c6 = st.columns([3, 2, 1])
with c4:
    if available_topics:
        selected_topics = st.multiselect(
            "Topic (Select from syllabus)",
            available_topics,
            help="Select one or more topics from the curriculum, or leave empty and type your own custom topic below.",
            key="sel_topics_multi",
        )
        custom_topic_text = st.text_input(
            "Or Custom Topic (type your own)",
            placeholder="e.g. Circles and Cylinders, 3D Trigonometry, Matrices...",
            help="Type any custom topic here if you want something not listed above.",
            key="custom_topic_text",
        )
        combined_topics = list(selected_topics)
        if custom_topic_text.strip():
            combined_topics.append(custom_topic_text.strip())
        topic = combined_topics
    else:
        custom_topic_text = st.text_input(
            "Topic (type your own)",
            placeholder="e.g. Algebra, Calculus, Financial Maths",
            key="fallback_topic_text",
        )
        topic = [custom_topic_text.strip()] if custom_topic_text.strip() else []
with c5:
    subtopic_options = [f"{section} — {selected_topic}" for selected_topic in selected_topics for section in topic_subtopics.get(selected_topic, [])] if available_topics else []
    if subtopic_options:
        selected_subtopics = st.multiselect("Subtopics (Optional)", subtopic_options, key="sel_subtopics_multi")
    else:
        selected_subtopics = []
    custom_subtopic = st.text_input("Or Custom Subtopic (Optional)", placeholder="e.g. Slope Fields")
    sub_topic = ", ".join([item.split(" — ", 1)[0] for item in selected_subtopics] + ([custom_subtopic.strip()] if custom_subtopic.strip() else []))
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
    type=["png", "jpg", "jpeg", "webp", "heic", "heif", "pdf"],
    accept_multiple_files=True,
)

if "pasted_photos" not in st.session_state or st.session_state.pasted_photos is None:
    st.session_state["pasted_photos"] = []
if "_last_handled_paste_id" not in st.session_state:
    st.session_state["_last_handled_paste_id"] = None

if _paste_component is not None:
    pasted_payload = _paste_component(key="paste_photo_input")
    if pasted_payload and isinstance(pasted_payload, dict):
        p_id = pasted_payload.get("id")
        if p_id and p_id != st.session_state.get("_last_handled_paste_id"):
            st.session_state["_last_handled_paste_id"] = p_id
            new_files = pasted_payload.get("files", [])
            added_cnt = 0
            for item in new_files:
                try:
                    data_str = item.get("data", "")
                    if "," in data_str:
                        _, b64_str = data_str.split(",", 1)
                    else:
                        b64_str = data_str
                    raw_bytes = base64.b64decode(b64_str)
                    p_num = len(st.session_state["pasted_photos"]) + 1
                    fname = item.get("name") or f"Pasted Photo #{p_num}.png"
                    st.session_state["pasted_photos"].append({
                        "name": fname,
                        "bytes": raw_bytes,
                        "type": item.get("type", "image/png"),
                    })
                    added_cnt += 1
                except Exception as _pe:
                    _log_error("decode_pasted_photo", _pe)
            if added_cnt > 0:
                st.toast(f"📋 {added_cnt} pasted photo(s) attached!")
                st.rerun()

if st.session_state.get("pasted_photos"):
    st.markdown(f"**📎 Attached Pasted Photos ({len(st.session_state['pasted_photos'])}):**")
    p_cols = st.columns(min(len(st.session_state["pasted_photos"]), 4))
    idx_to_remove = None
    for p_idx, p_item in enumerate(st.session_state["pasted_photos"]):
        target_col = p_cols[p_idx % len(p_cols)]
        with target_col:
            st.image(p_item["bytes"], caption=f"{p_item['name']} ({len(p_item['bytes']) // 1024} KB)", use_container_width=True)
            if st.button("🗑️ Remove", key=f"btn_remove_paste_{p_idx}", use_container_width=True):
                idx_to_remove = p_idx
    if idx_to_remove is not None:
        st.session_state["pasted_photos"].pop(idx_to_remove)
        st.rerun()
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
    for selected_level in (actual_levels or [""]):
        target_topic = safe_topic
        yr_num = re.sub(r"\D", "", y)
        if isinstance(topic, list) and topic:
            badge = f"(Yr {yr_num}" + (f", {selected_level}" if selected_level else "") + ")"
            matching = [t for t in topic if t.endswith(badge)]
            if matching:
                target_topic = matching[0]
            elif any(re.search(r"\((?:Yr|Year)\s*\d+", t, re.IGNORECASE) for t in topic):
                target_topic = ""
        s, m = load_style_samples(y, subject, selected_level, target_topic)
        if s and s not in combined_samples:
            combined_samples.append(s)
            course_label = f"{y} {selected_level}".strip()
            msgs.append(f"{course_label}: {m.replace('✅ ', '').replace('⚠️ ', '')}")

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
    "meta_total_cost", "meta_gemini_cost", "meta_ds_cost", "meta_extra_instructions", "used_search", "phase_1_raw", "saved_ai_payload", "work_dir",
    "saved_exam_id", "saved_has_solutions", "deepseek_audit",
)
for _key in _SS_KEYS:
    if _key not in st.session_state:
        st.session_state[_key] = None

st.markdown("---")
c_opt1, c_opt2 = st.columns([3, 2])
with c_opt1:
    auto_solutions = st.checkbox(
        "🧠 Include step-by-step Fully Worked Solutions automatically",
        value=False,
        help="If checked, the AI will also calculate and compile fully worked solutions into the document automatically.",
    )

btn_col1, btn_col2 = st.columns([5, 1])
with btn_col1:
    generate_btn = st.button(
        "✨ Generate Exam",
        type="primary",
        use_container_width=True,
        help="Submits this exam to the background generation queue so you can configure and generate multiple exams back-to-back without waiting!",
    )
with btn_col2:
    if st.button("🗑 Clear", use_container_width=True):
        _old_wd = st.session_state.get("work_dir")
        if _old_wd and os.path.isdir(_old_wd):
            shutil.rmtree(_old_wd, ignore_errors=True)
        for _key in _SS_KEYS:
            st.session_state[_key] = None
        st.session_state["sel_topics_multi"] = []
        st.session_state["sel_subtopics_multi"] = []
        st.session_state["custom_topic_text"] = ""
        st.session_state["fallback_topic_text"] = ""
        st.session_state["pasted_photos"] = []
        st.session_state["_last_handled_paste_id"] = None
        st.rerun()

if generate_btn:
    is_topic_empty = not topic or (isinstance(topic, str) and not topic.strip())
    has_uploaded_photos = bool(uploaded_photos) if isinstance(uploaded_photos, list) else (uploaded_photos is not None)
    has_pasted_photos = bool(st.session_state.get("pasted_photos"))
    has_upload = has_uploaded_photos or has_pasted_photos
    if not year_group:
        st.error("🚨 Action Required: Please select at least one **Year** group from the dropdown above.")
    elif subject == "Maths" and not actual_levels and any(y in year_group for y in ("Year 9", "Year 10", "Year 11", "Year 12")):
        st.error("🚨 Action Required: Please select at least one **Level**.")
    elif is_topic_empty and not has_upload:
        st.error("🚨 Action Required: Please enter or select a **Topic**, or upload/paste photo(s) for the AI to extract.")
    elif total_q == 0:
        st.error("🚨 Action Required: Please select at least one question to generate.")
    else:
        api_key = _get_active_api_key()
        user_role = st.session_state.get("user_role", "senior")
        if not api_key:
            if user_role == "junior":
                st.error("⚠️ Missing personal Gemini API key. Please connect your API key in the sidebar.")
            else:
                st.error("⚠️ The AI engine isn't configured (missing server API key). Please contact tech support before using the generator.")
            st.stop()

        if is_topic_empty and has_upload:
            clean_topic = "Topics from Attached Document"
        else:
            topic_list = topic if isinstance(topic, list) else [topic]
            raw_t = ", ".join([
                re.sub(r"^\d+[\.\-]\s*", "",
                    re.sub(r"\s*\((?:Yr|Year)\s*\d+(?:,\s*[^)]*)?\)$", "",
                        re.sub(r"^\[Year\s*\d+\]\s*", "", t, flags=re.IGNORECASE).strip(),
                    flags=re.IGNORECASE).strip()
                ).strip() for t in topic_list
            ])
            if sub_topic:
                safe_sub = sub_topic.replace("/", "-").replace("\\", "-")
                raw_t = f"{raw_t} - {safe_sub}"
            clean_topic = clean_redundant_topic_prefix(raw_t, ", ".join(year_group), subject, actual_level) or raw_t

        grades_string = ", ".join(year_group)
        current_set_number = get_next_set_number_with_queue(
            subject, grades_string, actual_level, clean_topic, base_set=int(set_number_input)
        )
        display_topic = f"{clean_topic} Set {current_set_number}"

        # Detach uploaded and pasted files into pure in-memory bytes/dicts so worker threads can read them safely
        uploaded_files_data = []
        if uploaded_photos:
            photos_list = uploaded_photos if isinstance(uploaded_photos, list) else [uploaded_photos]
            for photo in photos_list:
                uploaded_files_data.append({
                    "name": photo.name,
                    "bytes": photo.getvalue(),
                    "type": getattr(photo, "type", ""),
                })
        for p in st.session_state.get("pasted_photos", []):
            uploaded_files_data.append({
                "name": p["name"],
                "bytes": p["bytes"],
                "type": p["type"],
            })

        job_params = {
            "api_key": api_key,
            "deepseek_api_key": _get_deepseek_api_key(),
            "use_deepseek_auditor": use_deepseek_auditor,
            "auto_solutions": auto_solutions,
            "year_group": year_group,
            "subject": subject,
            "actual_level": actual_level,
            "actual_levels": actual_levels,
            "clean_topic": clean_topic,
            "sub_topic": sub_topic,
            "grades_string": grades_string,
            "current_set_number": current_set_number,
            "display_topic": display_topic,
            "total_q": total_q,
            "num_mc": num_mc,
            "num_easy": num_easy,
            "num_med": num_med,
            "num_hard": num_hard,
            "num_xh": num_xh,
            "layout_mode": layout_mode,
            "style_samples": style_samples,
            "extra_instructions": extra_instructions,
            "exemplar_questions": exemplar_questions,
            "uploaded_files_data": uploaded_files_data,
            "use_live_search": (subject in ["Business Studies", "Legal Studies", "Science"] and use_live_search),
            "created_by": st.session_state.get("user_display_name", "Teacher"),
            "computer_name": get_effective_computer_name(),
        }

        job_id = submit_exam_job(job_params, teacher_username=st.session_state.get("username", ""))
        st.session_state.setdefault("my_queued_job_ids", []).append(job_id)
        st.toast(f"🚀 Queued: {display_topic}! Generating in background. You can now configure another exam.")
        st.rerun()

# ── BACKGROUND GENERATION QUEUE MONITOR ────────────────────────────────────────
render_exam_queue_section()

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

    # ── DEEPSEEK-R1 AUDITOR STATUS CARD ─────────────────────────────────────────
    audit_info = st.session_state.get("deepseek_audit")
    if audit_info and isinstance(audit_info, dict):
        status = audit_info.get("status")
        auto_corr = audit_info.get("auto_corrected", False)
        summary = audit_info.get("summary", "")
        details = audit_info.get("details", "")
        reasoning = audit_info.get("reasoning", "")

        if status == "APPROVED":
            with st.expander("🛡️ **DeepSeek-R1 Quality Audit:** Verified & Approved (100% Syllabus & Math Sound)", expanded=False):
                st.success(f"✅ **Mathematical Solvability & NESA Boundaries Verified**\n\n{summary}")
                if details:
                    st.markdown(f"**Verification Breakdown:**\n\n{details}")
                if reasoning:
                    with st.expander("🧠 View DeepSeek-R1 Chain-of-Thought Reasoning", expanded=False):
                        st.caption(reasoning)
        elif status == "REVISION_NEEDED" and auto_corr:
            with st.expander("🛡️ **DeepSeek-R1 Quality Audit:** Flaws Flagged & Auto-Corrected by Gemini", expanded=True):
                st.info(f"🔄 **DeepSeek-R1 identified mathematical or syllabus issues in the initial draft, and Gemini automatically repaired them before finalizing:**\n\n{summary}")
                if details:
                    st.markdown(f"**Issues Identified by Auditor:**\n\n{details}")
                if audit_info.get("correction_prompt"):
                    st.caption(f"**Correction Applied:** {audit_info['correction_prompt']}")
                if reasoning:
                    with st.expander("🧠 View DeepSeek-R1 Chain-of-Thought Reasoning", expanded=False):
                        st.caption(reasoning)
        elif status == "REVISION_NEEDED" and not auto_corr:
            with st.expander("🛡️ **DeepSeek-R1 Quality Audit:** Revision Recommended", expanded=True):
                st.warning(f"⚠️ **DeepSeek-R1 noted potential issues:**\n\n{summary}")
                if details:
                    st.markdown(details)
        elif status == "ERROR":
            st.caption(f"🛡️ **DeepSeek Auditor Notice:** {audit_info.get('error', 'Auditor temporarily unavailable.')}")


    in_tok = st.session_state.meta_input_tokens or 0
    out_tok = st.session_state.meta_output_tokens or 0
    model_used = st.session_state.meta_model_used or "Unknown"

    # Pricing: Gemini 3.8 Flash ($0.75 / 1M in, $3.75 / 1M out) + Google Search ($0.014) + DeepSeek-R1 Auditor ($0.55 in, $2.19 out)
    in_cost = (in_tok / 1_000_000) * 0.75
    out_cost = (out_tok / 1_000_000) * 3.75
    search_cost = 0.014 if st.session_state.used_search else 0.00
    gemini_cost = round(in_cost + out_cost + search_cost, 5)
    ds_cost = float(st.session_state.get("meta_ds_cost") or 0.0)
    total_cost = round(gemini_cost + ds_cost, 5)
    st.session_state["meta_gemini_cost"] = gemini_cost
    st.session_state["meta_total_cost"] = total_cost

    if st.session_state.get("admin_pin") == "DA_ADMIN":
        search_badge = " &nbsp;&nbsp;|&nbsp;&nbsp; 🌍 *Live Web Search*" if st.session_state.used_search else ""
        if ds_cost > 0:
            split_info = f" (🤖 Gemini: ${gemini_cost:.4f} &nbsp;|&nbsp; 🛡️ DeepSeek Auditor: ${ds_cost:.4f})"
        else:
            split_info = f" (🤖 Gemini: ${gemini_cost:.4f})"
        st.caption(f"**💸 Generation Cost:** ${total_cost:.5f}{split_info} &nbsp;&nbsp;|&nbsp;&nbsp; **Engine:** `{model_used}` &nbsp;&nbsp;|&nbsp;&nbsp; **Tokens:** {in_tok:,} In / {out_tok:,} Out{search_badge}")
        st.caption("ℹ️ Reflects official Gemini 3.8 Flash rates ($0.75/1M in, $3.75/1M out) + DeepSeek-R1 auditor rates ($0.55/1M in, $2.19/1M out).")

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
                        "\n\nPHASE 2 INSTRUCTION: Excellent. Now, generate the comprehensive step-by-step fully worked solutions for these EXACT questions. You must place your entire output between the tags ===LATEX_SOLUTIONS_START=== and ===LATEX_SOLUTIONS_END===.\n"
                        "MANDATORY DIAGRAMS IN WORKED SOLUTIONS: You MUST ALWAYS include visual diagrams (using Native TikZ or the Python Graph Engine) in the fully worked solutions whenever a diagram helps the student visualize, follow, or understand the working out (e.g. geometric proofs, vector triangles/addition, coordinate geometry sketches, integration regions/areas between curves, sign diagrams, normal distribution shading, or 3D angles). Do NOT rely purely on text and algebra when a visual sketch aids understanding.\n"
                        "Group trivial algebra. Show all key mathematical steps and justifications. Never use \\tag inside $$ ... $$ or \\[ ... \\]; use \\qquad (1) if numbering equations.",
                    ]

                    models_to_try = [
                        "gemini-3.8-flash",
                        "gemini-3.6-flash",
                    ]
                    s_out = None
                    last_error = None
                    for model_name in models_to_try:
                        cur_max_tok = MAX_OUTPUT_TOKENS
                        # For large batches (>=30 questions), prefer direct LLM generation first to prevent code execution sandbox timeouts
                        tool_candidates = [[], active_tools] if (total_q >= 30 or not active_tools) else [active_tools, []]

                        for t_list in tool_candidates:
                            try:
                                cfg = types.GenerateContentConfig(
                                    max_output_tokens=cur_max_tok,
                                    tools=t_list if t_list else None,
                                    safety_settings=[
                                        types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                        types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                        types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                        types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
                                    ],
                                )
                                st.toast(f"🧠 {model_name}: Solving equations...")
                                res2 = client.models.generate_content(model=model_name, contents=p2_payload, config=cfg)

                                raw_s = _extract_genai_text(res2)
                                if not raw_s or "LATEX_SOLUTIONS_START" not in raw_s:
                                    raise ValueError("Solutions truncated. Retrying...")

                                if "LATEX_SOLUTIONS_END" not in raw_s:
                                    raw_s += "\n===LATEX_SOLUTIONS_END==="

                                s_out = raw_s.replace("```latex", "").replace("```", "")

                                if res2.usage_metadata:
                                    st.session_state.meta_input_tokens += getattr(res2.usage_metadata, "prompt_token_count", 0)
                                    st.session_state.meta_output_tokens += getattr(res2.usage_metadata, "candidates_token_count", 0)
                                break
                            except Exception as e:
                                last_error = e
                                if t_list:
                                    continue
                                else:
                                    if _is_retryable(e):
                                        st.toast(f"🚦 {model_name} busy. Pivoting...")
                                        time.sleep(1.5)
                                    break
                        if s_out:
                            break

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
    if not st.session_state.pdf_bytes:
        st.warning("⚠️ PDF compilation did not produce a valid file. Please review the compiler log below.")
    elif not st.session_state.cloud_saved:
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
                    gemini_cost=st.session_state.get("meta_gemini_cost"),
                    ds_cost=st.session_state.get("meta_ds_cost"),
                    model=st.session_state.get("meta_model_used"),
                    extra_instructions=st.session_state.get("meta_extra_instructions", ""),
                    created_by=f"{st.session_state.get('user_display_name', 'Senior Teacher')} ({get_effective_computer_name()})" if get_effective_computer_name() and get_effective_computer_name() not in st.session_state.get("user_display_name", "") else st.session_state.get("user_display_name", "Senior Teacher"),
                    has_solutions=bool(st.session_state.solutions_text),
                    questions_text=st.session_state.questions_text or "",
                    answers_text=st.session_state.answers_text or "",
                    solutions_text=st.session_state.solutions_text or "",
                    audit_data=st.session_state.get("deepseek_audit"),
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
                        gemini_cost=st.session_state.get("meta_gemini_cost"),
                        ds_cost=st.session_state.get("meta_ds_cost"),
                        model=st.session_state.get("meta_model_used"),
                        extra_instructions=st.session_state.get("meta_extra_instructions", ""),
                        created_by=f"{st.session_state.get('user_display_name', 'Senior Teacher')} ({get_effective_computer_name()})" if get_effective_computer_name() and get_effective_computer_name() not in st.session_state.get("user_display_name", "") else st.session_state.get("user_display_name", "Senior Teacher"),
                        has_solutions=True,
                        questions_text=st.session_state.questions_text or "",
                        answers_text=st.session_state.answers_text or "",
                        solutions_text=st.session_state.solutions_text or "",
                        audit_data=st.session_state.get("deepseek_audit"),
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

    safe_name = build_exam_filename(
        _t,
        _y,
        _s,
        _d,
        int(_set or 1),
        has_solutions=bool(st.session_state.solutions_text),
        num_mc=st.session_state.meta_mc or 0,
        num_easy=st.session_state.meta_easy or 0,
        num_med=st.session_state.meta_med or 0,
        num_hard=st.session_state.meta_hard or 0,
        num_xh=st.session_state.meta_xh or 0,
    )

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
