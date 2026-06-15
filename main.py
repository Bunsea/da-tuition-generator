import streamlit as st
from google import genai
import os
import io
import re
import subprocess
import base64
import shutil
import time
from urllib.parse import unquote
from PIL import Image
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback for older Python versions
    import pytz

    def get_sydney_time(utc_string):
        if not utc_string:
            return "N/A"
        # Parse Supabase ISO timestamp (handling 'Z' or offset strings)
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


# ── SUPABASE CONNECTION ────────────────────────────────────────────────────────
@st.cache_resource
def init_supabase() -> Client | None:
    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = os.environ.get("SUPABASE_KEY")
    if sb_url and sb_key:
        return create_client(sb_url, sb_key)
    return None


supabase_client = init_supabase()


def get_next_set_number(subject, year, diff, clean_topic):
    if not supabase_client:
        return 1
    try:
        res = (
            supabase_client.table("saved_exams")
            .select("topic")
            .eq("subject", subject)
            .eq("difficulty", diff)
            .ilike("topic", f"{clean_topic}%Set%")
            .execute()
        )
        return len(res.data) + 1
    except:
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
):
    if not supabase_client:
        return "Supabase is not connected. Missing URL or Key."

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
    # 1. Map the full level names to their short abbreviations
    level_map = {
        "Advanced": "Adv",
        "Extension": "Ext1",
        "Extension 2": "Ext2",
        "Standard": "Std"
    }

    # 2. Translate the difficulty. If it's in our map, shorten it. If not, just use the original word.
    short_diff = level_map.get(diff, diff) if diff else ""

    # 3. Create a clean level string (e.g., " Adv")
    lvl_text = f" {short_diff}" if short_diff else ""

    # 4. Keep spaces in the topic name, but remove slashes so the file saves safely
    safe_topic = clean_topic.replace("/", "-").replace("\\", "-")

    # 5. Build the beautiful new filename using the variables that already exist!
    file_base = f"{safe_topic} Set {set_num}{dist_str} - {yr_short} {subject}{lvl_text}"
    pdf_path = f"{subject}/{year}/{file_base}.pdf"
    docx_path = f"{subject}/{year}/{file_base}.docx"

    try:
        supabase_client.storage.from_("exam-files").upload(
            pdf_path, pdf_bytes, {"content-type": "application/pdf"}
        )
        pdf_url = supabase_client.storage.from_("exam-files").get_public_url(pdf_path)

        docx_url = ""
        if word_bytes:
            supabase_client.storage.from_("exam-files").upload(
                docx_path,
                word_bytes,
                {
                    "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                },
            )
            docx_url = supabase_client.storage.from_("exam-files").get_public_url(
                docx_path
            )

        data = {
            "subject": subject,
            "year_group": year,
            "difficulty": diff,
            "topic": db_topic,
            "pdf_url": pdf_url,
            "docx_url": docx_url,
        }
        supabase_client.table("saved_exams").insert(data).execute()
        return True
    except Exception as e:
        return str(e)


# ── NEW DYNAMIC FILE ENGINE ────────────────────────────────────────────────────
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
    topics = [
        d
        for d in os.listdir(parent_path)
        if os.path.isdir(os.path.join(parent_path, d))
    ]
    return sorted(topics)


def _read_files_in_dir(directory: str, files_only=True) -> list:
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
                except Exception:
                    pass
            elif fname.endswith(".pdf"):
                try:
                    reader = PdfReader(fpath)
                    pdf_text = "".join(
                        [page.extract_text() or "" for page in reader.pages]
                    )
                    if pdf_text.strip():
                        samples.append(pdf_text)
                except Exception:
                    pass
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


# ── UPGRADED MULTI-TYPE PYTHON GRAPHING ENGINE ─────────────────────────────────
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
        except:
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
            for fn in [
                "sin",
                "cos",
                "tan",
                "arcsin",
                "arccos",
                "arctan",
                "sinh",
                "cosh",
                "tanh",
                "exp",
                "log",
                "log10",
                "log2",
                "sqrt",
                "abs",
                "floor",
                "ceil",
            ]:
                py_expr = re.sub(rf"\b{fn}\b", f"np.{fn}", py_expr)

            x = np.linspace(xmin, xmax, 1000)
            with np.errstate(divide="ignore", invalid="ignore"):
                y = eval(
                    py_expr,
                    {"x": x, "np": np, "e": np.e, "pi": np.pi, "__builtins__": {}},
                )
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
            for fn in ["sin", "cos", "tan", "exp", "log"]:
                py_expr = re.sub(rf"\b{fn}\b", f"np.{fn}", py_expr)

            x_vals = np.linspace(xmin, xmax, 20)
            y_vals = np.linspace(ymin, ymax, 20)
            X, Y = np.meshgrid(x_vals, y_vals)

            with np.errstate(divide="ignore", invalid="ignore"):
                dy = eval(
                    py_expr,
                    {
                        "x": X,
                        "y": Y,
                        "np": np,
                        "e": np.e,
                        "pi": np.pi,
                        "__builtins__": {},
                    },
                )

            dx = np.ones_like(dy)
            norm = np.sqrt(dx**2 + dy**2)
            dx = dx / norm
            dy = dy / norm

            ax.quiver(
                X,
                Y,
                dx,
                dy,
                color=DA,
                headwidth=1,
                headlength=0,
                pivot="middle",
                scale=30,
            )

        elif g_type == "normal":
            mean = float(spec["mean"])
            std = float(spec["std"])
            x = np.linspace(mean - 4 * std, mean + 4 * std, 1000)
            y = (1 / (std * np.sqrt(2 * np.pi))) * np.exp(
                -0.5 * ((x - mean) / std) ** 2
            )
            ax.plot(x, y, color=DA, linewidth=2)

            if spec.get("shade_min") and spec.get("shade_max"):
                s_min = float(spec["shade_min"])
                s_max = float(spec["shade_max"])
                sx = np.linspace(s_min, s_max, 100)
                sy = (1 / (std * np.sqrt(2 * np.pi))) * np.exp(
                    -0.5 * ((sx - mean) / std) ** 2
                )
                ax.fill_between(sx, sy, alpha=0.3, color=DA)

            xmin, xmax = mean - 4 * std, mean + 4 * std
            auto_ymin, auto_ymax = 0, y.max() * 1.2
            ax.set_xticks(
                [
                    mean - 3 * std,
                    mean - 2 * std,
                    mean - std,
                    mean,
                    mean + std,
                    mean + 2 * std,
                    mean + 3 * std,
                ]
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
        print(f"Graphing Error: {e}")
        return None


def inject_python_graphs(text: str) -> str:
    segments = re.split(r"(GRAPH_START.*?GRAPH_END)", text, flags=re.DOTALL)
    out_text = ""
    for i, seg in enumerate(segments):
        if seg.startswith("GRAPH_START"):
            gspec = parse_graph_spec(
                seg.replace("GRAPH_START", "").replace("GRAPH_END", "").strip()
            )
            if gspec and (png_bytes := draw_function_graph(gspec)):
                img_path = os.path.join(_BASE_DIR, f"temp_graph_{i}.png")
                with open(img_path, "wb") as f:
                    f.write(png_bytes)
                # Normalize slash styles so pdflatex tracks assets seamlessly
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
    """Escape bare & → \\& except inside environments where & is a column/row separator."""
    parts = _SAFE_AMP_ENV_RE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(re.sub(r"(?<!\\)&", r"\\&", part))
        else:
            out.append(part)
    return "".join(out)


def sanitize_ai_latex(text: str) -> str:
    text = re.sub(r"\\documentclass\[.*?\]\{.*?\}|\\documentclass\{.*?\}", "", text)
    text = re.sub(r"\\usepackage\[.*?\]\{.*?\}|\\usepackage\{.*?\}", "", text)
    text = re.sub(r"\\begin\{document\}", "", text)
    text = re.sub(r"\\end\{document\}", "", text)

    text = re.sub(r"\*\*(.*?)\*\*", r"\\textbf{\1}", text)
    text = re.sub(r"(?<!\\)%", r"\%", text)
    text = _escape_bare_ampersands(text)
    text = re.sub(r"\\\]\s*\\hfill", r"\\]\n\\vspace{0.2cm}\\hfill", text)
    text = re.sub(r"\\begin{enumerate}\[.*?\]", r"\\begin{enumerate}", text)
    text = re.sub(r"\\setcounter\{.*?\}\{.*?\}", "", text)
    text = re.sub(r"\\hr\b", r"\\hrule", text)
    return text.strip()


# ── PANDOC & PDF BUILDERS ──────────────────────────────────────────────────────
def build_word_doc_pandoc(
    content, answers, solutions, topic, header_title, total_marks, time_allowed
):
    work_dir = _BASE_DIR
    tex_filename = os.path.join(work_dir, "temp_pandoc.tex")
    docx_filename = os.path.join(work_dir, "temp_exam.docx")

    def format_word(text):
        text = re.sub(r"\\item\[\(([A-D])\)\]\s*", r"\n\n(\1) ", text)
        return text.replace(r"\begin{itemize}", "").replace(r"\end{itemize}", "")

    # We use a clean centered logo for Pandoc to avoid layout breakage in Word
    full_tex = f"""\\documentclass{{article}}
\\usepackage{{amsmath, amssymb, graphicx, booktabs, array}}
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
\\newpage
\\begin{{center}}\\Large \\textbf{{FULLY WORKED SOLUTIONS}}\\end{{center}}\\vspace{{0.2cm}}\\hrule\\vspace{{0.5cm}}
{format_word(solutions)}
\\end{{document}}"""

    with open(tex_filename, "w", encoding="utf-8") as f:
        f.write(full_tex)
    if os.path.exists(docx_filename):
        os.remove(docx_filename)
    if not shutil.which("pandoc"):
        return None

    try:
        subprocess.run(
            ["pandoc", tex_filename, "-o", docx_filename],
            cwd=work_dir,
            capture_output=True,
        )
        if os.path.exists(docx_filename):
            doc = docx.Document(docx_filename)
            for section in doc.sections:
                section.top_margin = section.bottom_margin = section.left_margin = (
                    section.right_margin
                ) = Cm(1.5)
            doc.save(docx_filename)
            with open(docx_filename, "rb") as f:
                return f.read()
    except:
        pass
    return None


def build_latex_pdf(
    display_topic, header_title, content, answers, solutions, total_marks, time_allowed
):
    work_dir = _BASE_DIR
    filename = os.path.join(work_dir, "temp_exam.tex")
    pdf_filename = os.path.join(work_dir, "temp_exam.pdf")
    if os.path.exists(pdf_filename):
        os.remove(pdf_filename)

    # Safely escape LaTeX special characters in the titles!
    safe_title = (
        header_title.replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("_", r"\_")
    )
    safe_topic = (
        display_topic.replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("_", r"\_")
    )

    # Double-escaped minipage block ensuring Python strings format cleanly into pdflatex
    tex_template = f"""\\documentclass[11pt,a4paper]{{article}}
\\usepackage[margin=1.5cm]{{geometry}}
\\usepackage{{lmodern}}
\\usepackage[utf8]{{inputenc}}
\\usepackage[T1]{{fontenc}}
\\usepackage{{amsmath, amssymb, booktabs, array}}
\\usepackage{{fancyhdr}}
\\usepackage{{graphicx}}
\\usepackage{{tikz}}
\\usetikzlibrary{{arrows.meta, positioning, calc, shapes.geometric}}
\\usepackage{{pgfplots}}
\\pgfplotsset{{compat=1.18}}
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

{content}
\\newpage
\\begin{{center}}\\Large \\textbf{{ANSWERS}}\\end{{center}}\\vspace{{0.2cm}}\\hrule\\vspace{{0.4cm}}
{answers}
\\newpage
\\begin{{center}}\\Large \\textbf{{FULLY WORKED SOLUTIONS}}\\end{{center}}\\vspace{{0.2cm}}\\hrule\\vspace{{0.4cm}}
{solutions}
\\end{{document}}"""

    with open(filename, "w", encoding="utf-8") as f:
        f.write(tex_template)
    tex_bytes = tex_template.encode("utf-8")

    if not shutil.which("pdflatex"):
        return None, tex_bytes, "CRITICAL ERROR: 'pdflatex' not found."

    try:
        proc = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", filename],
            cwd=work_dir,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", filename],
            cwd=work_dir,
            capture_output=True,
            text=True,
        )
        if os.path.exists(pdf_filename):
            with open(pdf_filename, "rb") as f:
                return f.read(), tex_bytes, ""
        return None, tex_bytes, proc.stdout
    except Exception as e:
        return None, tex_bytes, str(e)


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
        /* Base background color and custom header rules */
        .stApp { background-color: #EAF6FF; }
        [data-testid="stHeader"] { background-color: transparent; }
        
        /* Scale up all base body elements, labels, buttons, and text fields */
        html, body, p, span, label, input, textarea, button, 
        [data-testid="stWidgetLabel"] p, 
        .stMarkdown p, 
        .stButton button, 
        div[role="listbox"], 
        div[role="combobox"] span,
        .stSelectbox div,
        .stMultiSelect div {
            font-size: 18px !important;
        }
        
        /* Upscale headings proportionally so the hierarchy stays beautiful */
        h1 { font-size: 2.6rem !important; font-weight: 700 !important; }
        h2 { font-size: 2.2rem !important; font-weight: 700 !important; }
        h3 { font-size: 1.8rem !important; font-weight: 600 !important; }
        h4 { font-size: 1.5rem !important; font-weight: 600 !important; }
        h5 { font-size: 1.3rem !important; font-weight: 600 !important; }

        /* Adjust caption styling so helper text remains crisp but legible */
        .stCaption, caption, small {
            font-size: 14px !important;
        }
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
        .massive-header {
            font-size: 35px !important;
            font-weight: 600 !important;
            color: #31333F !important;
            line-height: 1.2 !important;
            margin-bottom: 0px !important;
        }
        </style>
        <div class="massive-header">DA Tuition | NESA Question Generator</div>
        """,
        unsafe_allow_html=True,
    )

st.markdown("---")

with st.sidebar:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, width="stretch")
    st.markdown("---")
    password = st.text_input("Enter Password", type="password")
    if password != "DA2026":
        st.info("Enter the password to access the generator.")
        st.stop()
    st.success("Access granted!")
    st.markdown("---")
    app_mode = st.radio("App Mode", ["✨ Generator", "📚 Exam Library"])
    st.markdown("---")
    if app_mode == "✨ Generator":
        st.header("⚙️ Advanced Settings")

        # Enable live web search grounding optionally
        use_live_search = st.checkbox(
            "🌍 Enable Live Web Search (Use only if recent facts are needed)",
            value=False,
        )

        extra_instructions = st.text_area(
            "Extra Instructions (Optional)",
            placeholder="e.g., 'Make 80% about Trigonometry'",
        )
        st.markdown("---")
        with st.expander("🔐 Admin Access"):
            st.text_input(
                "Admin PIN",
                type="password",
                key="admin_pin",
                help="Enter PIN to unlock diagnostic and cost data.",
            )

# LIBRARY MODE RENDERED IN MAIN AREA
if app_mode == "📚 Exam Library":
    st.header("📚 Exam Library")
    if not supabase_client:
        st.warning("Supabase backend is not connected.")
    else:
        with st.spinner("Loading exams..."):
            response = (
                supabase_client.table("saved_exams")
                .select("*")
                .order("created_at", desc=True)
                .execute()
            )
        if not response.data:
            st.info("No exams found.")
        else:
            all_exams = response.data

            # 1. Extract unique subjects and years from your database to populate the dropdowns
            available_subjects = ["All"] + sorted(
                list(set([ex["subject"] for ex in all_exams if ex.get("subject")]))
            )
            available_years = ["All"] + sorted(
                list(
                    set([ex["year_group"] for ex in all_exams if ex.get("year_group")])
                )
            )

            # 2. Draw the filters AND the new Sort dropdown
            st.markdown("### 🔍 Search, Filter & Sort")
            f_col1, f_col2, f_col3, f_col4 = st.columns(4)
            with f_col1:
                filter_subject = st.selectbox("Subject", available_subjects)
            with f_col2:
                filter_year = st.selectbox("Year Group", available_years)

            # 3. Pre-filter the list to figure out which topics ACTUALLY exist for this Subject/Year
            temp_filtered = []
            for ex in all_exams:
                subj_match = (
                    filter_subject == "All" or ex.get("subject") == filter_subject
                )
                year_match = filter_year == "All" or ex.get("year_group") == filter_year
                if subj_match and year_match:
                    temp_filtered.append(ex)

            # 4. Clean up the database topic names (stripping off the "Set 1" text) for the dropdown
            available_topics = ["All"] + sorted(
                list(
                    set(
                        [
                            ex.get("topic", "").split(" (")[0]
                            for ex in temp_filtered
                            if ex.get("topic")
                        ]
                    )
                )
            )

            with f_col3:
                filter_topic = st.selectbox("Topic", available_topics)

            with f_col4:
                # NEW: Add the sorting dropdown menu
                sort_option = st.selectbox(
                    "Sort By",
                    [
                        "Date Added (Newest)",
                        "Date Added (Oldest)",
                        "Name (A-Z)",
                        "Name (Z-A)",
                    ],
                )

            st.markdown("---")

            # 5. Apply the final Topic filter
            filtered_exams = []
            for ex in temp_filtered:
                base_topic = ex.get("topic", "").split(" (")[0]
                if filter_topic == "All" or base_topic == filter_topic:
                    filtered_exams.append(ex)

            # 6. Apply the dynamic sorting logic!
            if sort_option == "Date Added (Newest)":
                filtered_exams.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            elif sort_option == "Date Added (Oldest)":
                filtered_exams.sort(key=lambda x: x.get("created_at", ""))
            elif sort_option == "Name (A-Z)":
                filtered_exams.sort(key=lambda x: x.get("topic", "").lower())
            elif sort_option == "Name (Z-A)":
                filtered_exams.sort(
                    key=lambda x: x.get("topic", "").lower(), reverse=True
                )

            # 7. Display the filtered results
            if not filtered_exams:
                st.info("No exams match your current filters.")
            else:
                st.caption(f"Showing {len(filtered_exams)} exam(s)")
                for exam in filtered_exams:
                    lvl_str = f" {exam['difficulty']}" if exam.get("difficulty") else ""
                    with st.expander(
                        f"📝 {exam['topic']} ({exam['subject']} - {exam['year_group']}{lvl_str})"
                    ):
                        # Convert the database UTC timestamp straight to Sydney local time!
                        sydney_timestamp = get_sydney_time(exam.get("created_at", ""))
                        st.caption(f"📅 **Generated:** {sydney_timestamp}")

                        e_col1, e_col2, e_col3 = st.columns([2, 2, 1])
                        if exam.get("pdf_url"):
                            e_col1.markdown(f"[📥 Download PDF]({exam['pdf_url']})")
                        if exam.get("docx_url"):
                            e_col2.markdown(
                                f"[📄 Download Word Doc]({exam['docx_url']})"
                            )

                        if e_col3.button("🗑️ Delete", key=f"del_{exam['id']}"):
                            with st.spinner("Deleting from Cloud..."):
                                paths_to_delete = []
                                if exam.get("pdf_url"):
                                    paths_to_delete.append(
                                        unquote(
                                            exam["pdf_url"].split("/exam-files/")[-1]
                                        )
                                    )
                                if exam.get("docx_url"):
                                    paths_to_delete.append(
                                        unquote(
                                            exam["docx_url"].split("/exam-files/")[-1]
                                        )
                                    )

                                if paths_to_delete:
                                    try:
                                        supabase_client.storage.from_(
                                            "exam-files"
                                        ).remove(paths_to_delete)
                                    except Exception:
                                        pass

                                try:
                                    supabase_client.table("saved_exams").delete().eq(
                                        "id", exam["id"]
                                    ).execute()
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Failed to delete record: {e}")

    st.stop()

# ── GENERATOR MODE ─────────────────────────────────────────────────────────────
st.markdown("### 📝 Exam Details")
c1, c2, c3 = st.columns(3)
with c1:
    year_group = st.multiselect(
        "Year", ["Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"]
    )
with c2:
    subject = st.selectbox(
        "Subject",
        [
            "Maths",
            "English",
            "Biology",
            "Business Studies",
            "Chemistry",
            "Legal Studies",
            "Science",
        ],
    )

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

# 1. Collect topics from ALL selected years
available_topics = []
for y in year_group:
    p_path = get_parent_path(y, subject, actual_level)
    available_topics.extend(get_available_topics(p_path))

# Natural alphanumeric sort to keep 1, 2, 3... 10, 11 in correct order
available_topics = sorted(
    list(set(available_topics)),
    key=lambda x: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', x)]
)

c4, c5 = st.columns([2, 2])
with c4:
    if available_topics:
        topic = st.multiselect("Topic", available_topics)
    else:
        topic = st.text_input("Topic", placeholder="e.g. Algebra, Calculus")
with c5:
    sub_topic = st.text_input(
        "Specific Sub-topic (Optional)", placeholder="e.g. Slope Fields"
    )

# ---> NEW: Add the copy-paste box directly underneath the topic row <---
exemplar_questions = st.text_area(
    "🧬 Question Cloner (Optional Text Input)", 
    placeholder="Paste specific questions here. The AI will generate variations matching their exact style and difficulty!"
)

# ---> NEW: Add Photo Upload Option Here <---
st.markdown("##### 📸 Or, Upload an Exam Notification / Worksheet Photo")
uploaded_photo = st.file_uploader(
    "Upload a picture of a school worksheet or exam notice to automatically extract the context",
    type=["png", "jpg", "jpeg", "pdf"],
)

st.markdown("### 📊 Question Distribution")
dist_cols = st.columns(5)
with dist_cols[0]:
    use_mc = st.checkbox("Multiple Choice", value=True)
    num_mc = (
        st.number_input(
            "MC Qty", min_value=0, max_value=100, value=5, label_visibility="collapsed"
        )
        if use_mc
        else 0
    )
with dist_cols[1]:
    use_easy = st.checkbox("Easy", value=True)
    num_easy = (
        st.number_input(
            "Easy Qty",
            min_value=0,
            max_value=100,
            value=5,
            label_visibility="collapsed",
        )
        if use_easy
        else 0
    )
with dist_cols[2]:
    use_med = st.checkbox("Medium", value=True)
    num_med = (
        st.number_input(
            "Med Qty", min_value=0, max_value=100, value=5, label_visibility="collapsed"
        )
        if use_med
        else 0
    )
with dist_cols[3]:
    use_hard = st.checkbox("Hard", value=False)
    num_hard = (
        st.number_input(
            "Hard Qty",
            min_value=0,
            max_value=100,
            value=5,
            label_visibility="collapsed",
        )
        if use_hard
        else 0
    )
with dist_cols[4]:
    use_xh = st.checkbox("Extremely Hard", value=False)
    num_xh = (
        st.number_input(
            "Ext. Hard Qty",
            min_value=0,
            max_value=100,
            value=5,
            label_visibility="collapsed",
        )
        if use_xh
        else 0
    )

total_q = num_mc + num_easy + num_med + num_hard + num_xh

st.markdown("### 🖨️ Format & Spacing")
layout_mode = st.radio(
    "Layout:",
    ["Worksheet (No working out space)", "Exam (Space for working out)"],
    horizontal=True,
    label_visibility="collapsed",
)

safe_topic = (
    topic[0]
    if isinstance(topic, list) and len(topic) > 0
    else (topic if isinstance(topic, str) else "")
)

# 2. Fetch sample files from ALL selected years
combined_samples = []
msgs = []
for y in year_group:
    s, m = load_style_samples(
        y, subject, actual_level, safe_topic if available_topics else ""
    )
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
    search_dirs = ", ".join(year_group) if year_group else "None"
    st.caption(f"Searching in: {search_dirs}")

_SS_KEYS = (
    "questions_text",
    "answers_text",
    "solutions_text",
    "pdf_bytes",
    "tex_bytes",
    "word_bytes",
    "compiler_log",
    "meta_topic",
    "meta_subject",
    "meta_year",
    "meta_diff",
    "meta_n",
    "meta_set",
    "cloud_saved",
    "display_topic",
    "meta_mc",
    "meta_easy",
    "meta_med",
    "meta_hard",
    "meta_xh",
    "meta_input_tokens",
    "meta_output_tokens",
    "meta_model_used",  # NEW: Tracks which model generated the exam
    "used_search",
)
for _key in _SS_KEYS:
    if _key not in st.session_state:
        st.session_state[_key] = None

st.markdown("---")
btn_col1, btn_col2 = st.columns([5, 1])
with btn_col1:
    generate_btn = st.button(
        "✨ It's go time!", type="primary", use_container_width=True
    )
with btn_col2:
    if st.button("🗑 Clear", use_container_width=True):
        for _key in _SS_KEYS:
            st.session_state[_key] = None
        st.rerun()

if generate_btn:
    # Check if the topic box is blank
    is_topic_empty = not topic or (isinstance(topic, str) and not topic.strip())

    # 1. STRICT: Enforce Year selection so the database stays organized
    if not year_group:
        st.warning(
            "⚠️ Please select at least one Year group to keep the library organized."
        )

    # 2. HYBRID: They must provide EITHER a Topic OR a Photo Upload
    elif is_topic_empty and uploaded_photo is None:
        st.warning("⚠️ Please enter a Topic, or upload a photo for the AI to extract.")

    # 3. STRICT: Enforce Question count
    elif total_q == 0:
        st.warning("⚠️ Please select at least one question to generate.")

    else:
        # 4. THE FREE PASS: If they uploaded a photo but skipped the Topic, auto-name it!
        if is_topic_empty and uploaded_photo is not None:
            # This creates a clean placeholder name in the database for them
            topic = ["Worksheet Extract"]

        api_key = os.environ.get("GEMINI_API_KEY")
        client = genai.Client(api_key=api_key)

        topic_list = topic if isinstance(topic, list) else [topic]
        clean_topic = ", ".join(
            [re.sub(r"^\d+[\.\-]\s*", "", t).strip() for t in topic_list]
        )
        
        # ---> NEW: Safely append the sub-topic to the main topic string <---
        if sub_topic:
            safe_sub = sub_topic.replace("/", "-").replace("\\", "-")
            clean_topic = f"{clean_topic} - {safe_sub}"
            
        grades_string = ", ".join(year_group)

        current_set_number = get_next_set_number(
            subject, grades_string, actual_level, clean_topic
        )
        display_topic = f"{clean_topic} Set {current_set_number}"

        style_block = (
            f"STYLE & SYLLABUS REFERENCE:\n{style_samples}\n\n" if style_samples else ""
        )
        custom_instructions_block = (
            f"EXTRA TUTOR INSTRUCTIONS:\n{extra_instructions}\n\n"
            if extra_instructions.strip()
            else ""
        )

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
            layout_instruction = "CRITICAL SPACING RULE: Add `\\vspace*{4cm}` (or more) after EVERY Free-Response question and sub-question."
        else:
            layout_instruction = "CRITICAL SPACING RULE: Add exactly `\\vspace{0.5cm}` after EVERY question and sub-question."

        exam_focus = f"{clean_topic}" + (
            f" - specifically focusing on: {sub_topic}" if sub_topic else ""
        )
        level_text = f" {actual_level}" if actual_level else ""

        # ---> UPGRADED: Conditional Auto-Naming <---
        template_f = ""
        auto_name_rule = ""

        if uploaded_photo is not None:
            template_f = "===FILENAME_START===\nSchool_Subject_Year_Level_Topic\n===FILENAME_END===\n"
            auto_name_rule = "12. AUTO-NAMING: You MUST generate a precise file name based on the attached document. Format EXACTLY like this: School_Subject_Year_Level_Topic. Use underscores. Place inside ===FILENAME_START=== and ===FILENAME_END=== tags. CRITICAL: If there is no explicit school name, DO NOT guess or invent an acronym like 'SHS'. Simply omit the school entirely."
        template_c = "===LATEX_CONTENT_START===\n"
        template_a = "===LATEX_ANSWERS_START===\n"
        template_s = "===LATEX_SOLUTIONS_START===\n"

        if has_mc:
            template_c += "\\section*{Section 1}\n\\begin{enumerate}\n    \\item Multiple choice question...\n    \\begin{itemize}\n        \\item[(A)] Option 1\n        \\item[(B)] Option 2\n    \\end{itemize}\n\\end{enumerate}\n\n"
            template_a += "\\section*{Section 1}\n\\begin{enumerate}\n    \\item A\n\\end{enumerate}\n\n"
            template_s += "\\section*{Section 1}\n\\begin{enumerate}\n    \\item \\textbf{A} - Brief explanation...\n\\end{enumerate}\n\n"

        if has_fr:
            sec_title = "\\section*{Section 2}" if has_mc else "\\section*{Section 1}"
            template_c += f"{sec_title}\n\\begin{{enumerate}}\n    \\item Single part question... \\hfill \\textbf{{(2 marks)}}\n    \\vspace{{...}}\n\\end{{enumerate}}\n"
            template_a += f"{sec_title}\n\\begin{{enumerate}}\n    \\item $x = 5$\n\\end{{enumerate}}\n\n"
            template_s += f"{sec_title}\n\\begin{{enumerate}}\n    \\item Fully worked solution...\n\\end{{enumerate}}\n\n"

        template_c += "===LATEX_CONTENT_END==="
        template_a += "===LATEX_ANSWERS_END==="
        template_s += "===LATEX_SOLUTIONS_END==="

        strict_negatives = ""
        if not has_fr:
            strict_negatives = "CRITICAL: DO NOT GENERATE ANY FREE-RESPONSE QUESTIONS OR A SECTION 2. ONLY GENERATE MULTIPLE CHOICE."
        elif not has_mc:
            strict_negatives = (
                "CRITICAL: DO NOT GENERATE ANY MULTIPLE CHOICE QUESTIONS."
            )

        # ---> UPGRADED: Dynamic Syllabus & Level Guardrails <---
        syllabus_ban = ""
        if subject == "Maths":
            ext_ban = ""
            # If the user selects Advanced or Standard, explicitly ban Extension topics
            if actual_level in ["Advanced", "Standard"]:
                ext_ban = " CRITICAL: DO NOT include Extension 1 topics (e.g., Compound/Double Angles, Sum/Difference identities, t-formulae, Inverse Trig functions, Polynomial remainder theorem, or Combinatorics)."

            syllabus_ban = f"11. SYLLABUS STRICTNESS (CRITICAL): Strictly adhere to the post-2019 NESA {actual_level} syllabus. NEVER generate questions on obsolete topics (e.g., Locus, 3D Trigonometry, Perpendicular Distance, Angle of Inclination).{ext_ban}"

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
6. CRITICAL LATEX FIX: The VERY FIRST command immediately after `\\begin{{enumerate}}` or `\\begin{{itemize}}` MUST be `\\item`. NEVER place introductory text, spacing commands, or any other characters between the begin environment and the first item. If you have an introduction, place it AFTER the `\\item`.
7. SECTION TITLES: Name each section purely as "Section 1", "Section 2", etc. You MUST use the exact standard syntax `\\section*{{Section X}}`. NEVER invent undefined commands like `\\sectionsection`.
8. MULTIPLE CHOICE: You MUST heavily randomize the correct answer options (A, B, C, D) across the multiple-choice section. The correct answer must NOT always be 'A'.
9. TOKEN ECONOMY & STRICT MATHEMATICAL CONCISENESS: Output the absolute minimum text required for "FULLY WORKED SOLUTIONS". Show only the primary formula, key substitutions, and final evaluation. Completely eliminate conversational text, narrative explanations, and trivial arithmetic. Group multiple algebraic simplifications onto a single line. Your primary goal is to minimize total output tokens while keeping the logic mathematically sound.
10. PYTHON CALCULATOR SANDBOX (CRITICAL): You are equipped with a Python Code Execution tool. You MUST use it to calculate every single final decimal answer for financial mathematics, compound interest, annuities, or complex algebraic expressions. NEVER try to guess or hallucinate the final number. Write a Python script to evaluate the formula, run it, and inject the exact, calculated decimal into the "ANSWERS" and "FULLY WORKED SOLUTIONS" sections. CRITICAL EXCEPTION: DO NOT use the Python tool to solve Networks, Critical Path Analysis, Maximum Flow, or Geometry problems. Draw the TikZ diagrams and evaluate those structural networks purely using your internal reasoning.
11. UNIFIED QUESTION CLONING RULE (CRITICAL): If the user provides text inside [CLONE EXEMPLARS] or attaches an image/PDF file, your primary objective shifts to reverse-engineering those target items. Analyze their mathematical mechanics, formatting phrasing, structural complexity, and cognitive depth. You MUST generate original, highly precise variations that test the exact same competency tier. Change numeric values, algebraic configurations, or contextual word scenarios so the output operates as a perfect parallel practice set. Do not clone formatting errors or unrelated headers.
{syllabus_ban}
{auto_name_rule}

DIAGRAM RULES (CRITICAL):
You have TWO graphing engines available. You MUST choose the correct one based on the diagram type.

ENGINE 1: THE PYTHON ENGINE
Use this strictly for continuous 2D Cartesian functions, derivatives, Projectile Motion trajectories, Slope Fields, and Normal Distributions.
Syntax:
GRAPH_START
type: slope_field    (Options: function, slope_field, normal)
expr: x + y          (For function/slope_field. Use 'x' and 'y')
mean: 0              (For normal only)
std: 1               (For normal only)
shade_min: -1        (For normal only)
shade_max: 1         (For normal only)
xmin: -5
xmax: 5
ymin: -5
ymax: 5
GRAPH_END

ENGINE 2: NATIVE TikZ
Use this strictly for structural geometry: Networks, Critical Paths, 3D Trig diagrams, 3D Vectors, and Forces/Inclined Planes. The compiler has `\\usepackage{{tikz}}` installed (do NOT use pgfplots). Inject your `\\begin{{tikzpicture}}` code directly into the LaTeX output.

CRITICAL GRAPHING REQUIREMENT:
Whenever a question asks the student to "sketch" or "draw" a graph, you MUST provide the actual rendered graph in the Solutions and Answers sections using the LaTeX `pgfplots` package. 
- You must code the visual plot using \begin{{tikzpicture}} \begin{{axis}}[...] ... \end{{axis}} \end{{tikzpicture}}. 
- NEVER just describe the graph in text. You must mathematically plot the curves, asymptotes, and intercepts using pgfplots.
- NEVER use the setting `trig format plots=none` in your axis options. It does not exist and will crash the compiler. If plotting trigonometric functions, use `trig format plots=rad` or omit the setting entirely.
- PREVENT DIMENSION ERRORS: When plotting rational functions or graphs with vertical asymptotes, you MUST restrict the vertical plotting domain to prevent "Dimension too large" LaTeX crashes. You must include `ymin=-10, ymax=10` (or appropriate limits) inside the \begin{{axis}}[...] options, AND you must include `restrict y to domain=-15:15` inside the \addplot[...] options to safely clip the asymptotes.
- MANDATORY SEMICOLONS: Every single drawing command inside the axis environment (such as \addplot, \draw, \node, \coordinate) MUST end with a semicolon (;). Do not forget the semicolon, or the LaTeX compiler will crash.
- MANDATORY GEOMETRY DIAGRAMS: For any trigonometry, geometry, or bearing word problems in the Solutions section, you MUST draw a clear, labeled diagram using standard TikZ (e.g., \begin{{tikzpicture}} \draw ... \end{{tikzpicture}} without the axis environment). You must visually label all known lengths, angles, compass directions, and vertices (e.g., A, B, C) to help students understand the spatial setup.
- TIKZ LABELS AND ANCHORS SECURING: When creating labels or polar positioning elements in TikZ, you MUST use explicit standard syntax (e.g., label=90:{{$P_1$}} or [above=of P_1]). NEVER use shorthand styles like [90:P_1] directly inside bracket options, as this will trigger a fatal pgfkeys compiler crash.
- TOKEN SAVING RULE FOR NETWORKS (CRITICAL): To conserve tokens and prevent output truncation, DO NOT redraw TikZ network diagrams in the "ANSWERS" section. You are strictly permitted to redraw the network ONLY in the "FULLY WORKED SOLUTIONS" section to visually demonstrate minimum cuts, flow paths, or EST/LST boxes. Ensure the TikZ code is as efficient as possible.

{custom_instructions_block}
OUTPUT EXACTLY LIKE THIS TEMPLATE:
{template_f}

{template_c}

{template_a}

{template_s}
"""
        # Dynamic custom loading badge wrapper block (Centered)
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            st.markdown(
                f"""
                <div style="text-align: center; padding: 40px 20px;">
                    <img src="data:image/png;base64,{base64.b64encode(open(LOGO_PATH, "rb").read()).decode()}" 
                         style="animation: pulse 1.5s infinite ease-in-out; width: 120px; height: auto;" />
                    <h4 style="color: #1A3A8A; margin-top: 20px; font-weight: bold;">✨ Compiling Exam...</h4>
                    <p style="color: #666; font-size: 15px;">Assembling {display_topic} structural modules.</p>
                </div>
                <style>
                    /* THIS LINE HIDES THE DEFAULT STICK FIGURE COMPLETELY */
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
            # --- NEW RETRY LOGIC ---
            max_retries = 3
            out = None
            for attempt in range(max_retries):
                try:
                    # 1. Decide if this subject needs live web access
                    live_search_subjects = [
                        "Business Studies",
                        "Legal Studies",
                        "Science",
                    ]

                    # ---> NEW: Cost-Saving Token Limiter & PYTHON SANDBOX <---
                    from google.genai import types
                    
                    # Initialize the tool list with the Python Code Execution sandbox!
                    active_tools = [types.Tool(code_execution=types.ToolCodeExecution())]
                    
                    if subject in live_search_subjects and use_live_search:
                        active_tools.append(types.Tool(google_search=types.GoogleSearch()))
                    
                    gen_config = types.GenerateContentConfig(
                        max_output_tokens=8192,
                        tools=active_tools
                    )

                    # ---> UPGRADED: Dual Payload Builder <---
                    ai_payload = [prompt]

                    # 1. Catch text-based copy-pasted questions
                    if exemplar_questions.strip():
                        ai_payload.append(f"\n\n[CLONE EXEMPLARS - TEXT INPUT]:\n{exemplar_questions}")

                    # 2. Catch file/photo uploads (Your existing logic)
                    if uploaded_photo is not None:
                        if uploaded_photo.name.lower().endswith(".pdf"):
                            reader = PdfReader(uploaded_photo)
                            pdf_text = "\n".join(
                                [page.extract_text() or "" for page in reader.pages]
                            )
                            ai_payload.append(f"\n\n[CLONE EXEMPLARS - ATTACHED PDF TEXT]:\n{pdf_text}")
                        else:
                            img_data = Image.open(uploaded_photo)
                            ai_payload.append(img_data)

                        # Inject a contextual instruction telling the AI to read the attachment
                        ai_payload.append(
                            "\n\n[ATTACHMENT INSTRUCTION]: A school document (image or PDF) has been attached. "
                            "Analyze the attached document, extract the primary sub-topics, syllabus references, or question layouts, "
                            "and generate matching practice questions strictly adhering to the distribution settings requested above."
                        )

                    # 3. Call the AI with a Multi-Tiered Fallback Queue
                    models_to_try = [
                        "gemini-3.5-flash",  # Tier 1: Blazing Fast & Cheap (First Choice)
                        "gemini-3.1-pro-preview",  # Tier 2: Heavy Reasoning (Backup)
                        "gemini-2.5-pro",  # Tier 3: Stable Bedrock (Ultimate Safety Net)
                    ]

                    response = None
                    last_error = None

                    for model_name in models_to_try:
                        try:
                            response = client.models.generate_content(
                                model=model_name,
                                contents=ai_payload,
                                config=gen_config,
                            )
                            # Log which model successfully won the job!
                            st.toast(f"✅ Success! Engine used: {model_name}")
                            st.session_state.meta_model_used = model_name
                            break  # Success! Break out of the fallback queue
                        except Exception as e:
                            last_error = e
                            if "503" in str(e):
                                st.toast(
                                    f"🚦 {model_name} busy. Pivoting to next model..."
                                )
                                continue  # Move to the next model in the list
                            else:
                                raise e  # If it's a different error (like a bad API key), stop immediately

                    if not response:
                        # If ALL models in the queue fail with a 503, push the error to the outer
                        # retry loop so the app waits 5 seconds before trying the whole queue again.
                        raise last_error
                    out = response.text

                    # 4. Token Tracking
                    if response.usage_metadata:
                        st.session_state.meta_input_tokens = (
                            response.usage_metadata.prompt_token_count
                        )
                        st.session_state.meta_output_tokens = (
                            response.usage_metadata.candidates_token_count
                        )
                    st.session_state.used_search = (subject in live_search_subjects and use_live_search)

                    break  # If successful, break out of the loop
                except Exception as e:
                    if "503" in str(e) and attempt < max_retries - 1:
                        st.toast(
                            f"Google servers busy. Retrying automatically in 3 seconds... (Attempt {attempt + 1}/{max_retries})"
                        )
                        time.sleep(3)
                        continue
                    else:
                        raise e

            if not out:
                raise Exception("Failed to generate content after multiple attempts.")
            # --- END NEW RETRY LOGIC ---

            # Flexible whitespace robust regex expressions search matching splitters
            fn_m = re.search(
                r"===?\s*FILENAME_START\s*===?(.*?)===?\s*FILENAME_END\s*===?",
                out,
                re.DOTALL | re.IGNORECASE,
            )

            # If it finds the AI name, clean it up. Otherwise, fall back to a safe default.
            if fn_m and fn_m.group(1).strip():
                ai_generated_name = re.sub(
                    r"[^A-Za-z0-9_\-\(\) ]", "_", fn_m.group(1).strip()
                )
                clean_topic = ai_generated_name.replace("_", " ")

                # Recalculate the set number for this brand new custom AI name!
                current_set_number = get_next_set_number(
                    subject, grades_string, actual_level, clean_topic
                )
                display_topic = f"{clean_topic} Set {current_set_number}"
            else:
                display_topic = f"{clean_topic} Set {current_set_number}"

            c_m = re.search(
                "===?\\s*LATEX_CONTENT_START\\s*===?(.*?)===?\\s*LATEX_CONTENT_END\\s*===?",
                out,
                re.DOTALL | re.IGNORECASE,
            )
            a_m = re.search(
                "===?\\s*LATEX_ANSWERS_START\\s*===?(.*?)===?\\s*LATEX_ANSWERS_END\\s*===?",
                out,
                re.DOTALL | re.IGNORECASE,
            )
            s_m = re.search(
                "===?\\s*LATEX_SOLUTIONS_START\\s*===?(.*?)===?\\s*LATEX_SOLUTIONS_END\\s*===?",
                out,
                re.DOTALL | re.IGNORECASE,
            )

            c_latex = inject_python_graphs(
                sanitize_ai_latex(c_m.group(1).strip() if c_m else "Failed.")
            )
            a_latex = inject_python_graphs(
                sanitize_ai_latex(a_m.group(1).strip() if a_m else "Failed.")
            )
            s_latex = inject_python_graphs(
                sanitize_ai_latex(s_m.group(1).strip() if s_m else "Failed.")
            )

            marks = sum(
                int(m)
                for m in re.findall(r"\((\d+)\s*marks?\)", c_latex, re.IGNORECASE)
            ) or (total_q * 2)
            title = f"{grades_string} {subject}{level_text}".strip()

            p_bytes, t_bytes, log = build_latex_pdf(
                display_topic,
                title,
                c_latex,
                a_latex,
                s_latex,
                marks,
                int(marks * 1.5),
            )
            w_bytes = build_word_doc_pandoc(
                c_latex,
                a_latex,
                s_latex,
                display_topic,
                title,
                marks,
                int(marks * 1.5),
            )

            st.session_state.update(
                {
                    "questions_text": c_latex,
                    "answers_text": a_latex,
                    "solutions_text": s_latex,
                    "pdf_bytes": p_bytes,
                    "tex_bytes": t_bytes,
                    "word_bytes": w_bytes,
                    "compiler_log": log,
                    "meta_topic": clean_topic,
                    "meta_subject": subject,
                    "meta_year": grades_string,
                    "meta_diff": actual_level,
                    "meta_n": total_q,
                    "meta_set": current_set_number,
                    "display_topic": display_topic,
                    "cloud_saved": False,
                    "meta_mc": num_mc,
                    "meta_easy": num_easy,
                    "meta_med": num_med,
                    "meta_hard": num_hard,
                    "meta_xh": num_xh,
                }
            )

            # Wipe out custom layout floating loader badge safely on termination
            loading_placeholder.empty()

        except Exception as e:
            loading_placeholder.empty()
            st.error(f"Error: {e}")

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
    st.markdown(f"### {_disp} ({_y}{lvl_str})")

    # ---> NEW: Dynamic Pricing Calculator <---
    if st.session_state.get("admin_pin") == "DA_ADMIN":
        in_tok = st.session_state.meta_input_tokens or 0
        out_tok = st.session_state.meta_output_tokens or 0
        model_used = st.session_state.meta_model_used or "Unknown"

        # Determine true pricing based on the engine used
        if "flash" in model_used.lower():
            # Flash is approx $0.075 per 1M in / $0.30 per 1M out
            in_cost = (in_tok / 1_000_000) * 0.075
            out_cost = (out_tok / 1_000_000) * 0.30
        else:
            # Pro models are approx $1.50 per 1M in / $6.00 per 1M out
            in_cost = (in_tok / 1_000_000) * 1.50
            out_cost = (out_tok / 1_000_000) * 6.00

        search_cost = 0.014 if st.session_state.used_search else 0.00
        total_cost = in_cost + out_cost + search_cost

        search_badge = (
            " &nbsp;&nbsp;|&nbsp;&nbsp; 🌍 *Live Web Search Grounding Applied*"
            if st.session_state.used_search
            else ""
        )
        st.caption(
            f"**💸 True Generation Cost:** ${total_cost:.5f} &nbsp;&nbsp;|&nbsp;&nbsp; **Engine:** `{model_used}` &nbsp;&nbsp;|&nbsp;&nbsp; **Tokens:** {in_tok:,} In / {out_tok:,} Out{search_badge}"
        )

    if not st.session_state.cloud_saved:
        if st.button("💾 Save Exam to Cloud Library", type="secondary"):
            with st.spinner("☁️ Archiving to database..."):
                save_result = save_to_supabase(
                    _t,
                    _s,
                    _y,
                    _d,
                    _set,
                    st.session_state.pdf_bytes,
                    st.session_state.word_bytes,
                    num_mc=st.session_state.meta_mc or 0,
                    num_easy=st.session_state.meta_easy or 0,
                    num_med=st.session_state.meta_med or 0,
                    num_hard=st.session_state.meta_hard or 0,
                    num_xh=st.session_state.meta_xh or 0,
                )
                if save_result is True:
                    st.session_state.cloud_saved = True
                    st.rerun()
                else:
                    st.error(f"⚠️ Cloud Save Blocked: {save_result}")
    else:
        st.success("✅ Exam successfully archived to Library!")

    st.markdown("---")

    if not st.session_state.pdf_bytes:
        st.error("⚠️ **PDF Compiler Failed**")
        with st.expander("🛠️ View Log"):
            st.code(st.session_state.compiler_log, language="text")
    else:
        # High-performance PDF parsing engine using HTML5 canvas rendering
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
                    canvas.style.display = 'block';
                    canvas.style.margin = '0 auto 20px auto';
                    canvas.style.backgroundColor = '#ffffff';
                    canvas.style.boxShadow = '0 4px 12px rgba(0,0,0,0.3)';
                    container.appendChild(canvas);
                    canvases.push(canvas);
                }}
                
                for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {{
                    pdf.getPage(pageNum).then(page => {{
                        const scale = 1.3;
                        const viewport = page.getViewport({{scale: scale}});
                        const canvas = canvases[pageNum - 1];
                        canvas.height = viewport.height;
                        canvas.width = viewport.width;
                        
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

    # Build the distribution string for the local download filename
    dist_parts = []
    if st.session_state.meta_mc: dist_parts.append(f"{st.session_state.meta_mc} MC")
    if st.session_state.meta_easy: dist_parts.append(f"{st.session_state.meta_easy} Easy")
    if st.session_state.meta_med: dist_parts.append(f"{st.session_state.meta_med} Med")
    if st.session_state.meta_hard: dist_parts.append(f"{st.session_state.meta_hard} Hard")
    if st.session_state.meta_xh: dist_parts.append(f"{st.session_state.meta_xh} Ext Hard")
    dist_str = f" ({', '.join(dist_parts)})" if dist_parts else ""

    yr_short = _y.replace("Year ", "Yr")
    lvl_part = f" {_d}" if _d else ""
    safe_name = f"{yr_short} {_s}{lvl_part} {_t.replace('/', '_')} Set {_set}{dist_str}"

    dl1, dl2, dl3 = st.columns(3)
    with dl1:
        if st.session_state.pdf_bytes:
            st.download_button(
                "🔴 Download PDF",
                data=st.session_state.pdf_bytes,
                file_name=f"{safe_name}.pdf",
                mime="application/pdf",
                use_container_width=True,
                type="primary",
            )
    with dl2:
        st.download_button(
            "📜 Download LaTeX",
            data=st.session_state.tex_bytes,
            file_name=f"{safe_name}.tex",
            use_container_width=True,
        )
    with dl3:
        if st.session_state.word_bytes:
            st.download_button(
                "📄 Download Word Doc",
                data=st.session_state.word_bytes,
                file_name=f"{safe_name}.docx",
                use_container_width=True,
            )