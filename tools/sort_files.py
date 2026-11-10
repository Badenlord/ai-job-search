#!/usr/bin/env python3
"""Sort a messy folder into tidy subfolders, entirely offline.

Sort modes (--by):
  smart      (default) groups files by what they are about - Job Applications,
             Finance, Contracts & Legal, Travel & Tickets, Certificates &
             Diplomas, Manuals & Guides - judged from the file name and, for
             documents, the text inside. Everything else goes by kind:
             Photos, Screenshots, Images, Documents, Archives, ...
  type       Documents/, Images/, Archives/, ...
  date       2024/2024-03/ by last-modified time
  extension  PDF/, DOCX/, ...

Prefer clicking to typing? Run tools/file_sorter_app.py for the same sorter
in a window.

Uses only the Python standard library and never touches the network.

Safe by default:
  * Shows a preview and changes nothing unless --apply is given.
  * Never overwrites: a name clash becomes "report (1).pdf".
  * Skips hidden files, symlinks, system files and unfinished downloads.
  * With --recursive, never descends into hidden folders or code projects
    (folders containing .git), and refuses to run on the home folder.
  * Every applied move is logged, so --undo can put everything back.

Examples:
  python3 tools/sort_files.py ~/Downloads                    # preview
  python3 tools/sort_files.py ~/Downloads --apply            # smart sort
  python3 tools/sort_files.py ~/Downloads --duplicates --apply
  python3 tools/sort_files.py ~/Pictures --by date --apply   # 2024/2024-03/...
  python3 tools/sort_files.py ~/Downloads --undo ~/Downloads/.sort_files_log_<stamp>.json --apply
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import unicodedata
import zipfile
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# File kinds. Edit these tables to change where things go. Extensions are
# lowercase.
# ---------------------------------------------------------------------------

CATEGORIES = {
    "Documents": [
        ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".tex",
        ".pages", ".epub", ".mobi", ".html", ".htm",
    ],
    "Spreadsheets": [".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv", ".numbers"],
    "Presentations": [".ppt", ".pptx", ".odp", ".key"],
    "Images": [
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
        ".heic", ".heif", ".svg", ".ico", ".psd", ".raw", ".cr2", ".nef",
        ".arw", ".dng",
    ],
    "Audio": [".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".aiff"],
    "Video": [".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".m4v", ".flv", ".mpg", ".mpeg"],
    "Archives": [
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz",
        ".tar.gz", ".tar.bz2", ".tar.xz",
    ],
    "Installers": [".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".appimage", ".apk"],
    "Code": [
        ".py", ".ipynb", ".js", ".ts", ".css", ".json", ".xml", ".yaml",
        ".yml", ".sh", ".ps1", ".bat", ".c", ".cpp", ".h", ".java", ".go",
        ".rs", ".rb", ".php", ".sql", ".r", ".m",
    ],
    "Fonts": [".ttf", ".otf", ".woff", ".woff2"],
}
OTHER = "Other"
NO_EXTENSION = "No extension"
DUPLICATES = "Duplicates"

# Smart mode: topics recognised from file names ("names") and document text
# ("content"). English and Danish; matching ignores case and accents. A name
# keyword of 5+ letters also matches as the start or end of a longer word
# (receipts, lejekontrakt). Content needs two different keywords to count.
SMART_TOPICS = (
    {
        "folder": "Job Applications",
        "names": (
            "cv", "resume", "curriculum vitae", "cover letter", "motivation letter",
            "job application", "reference letter", "letter of recommendation",
            "ansøgning", "anbefaling",
        ),
        "content": (
            "curriculum vitae", "work experience", "professional experience",
            "employment history", "education", "dear hiring manager",
            "cover letter", "linkedin.com/in", "erhvervserfaring", "uddannelse",
            "kompetencer",
        ),
    },
    {
        "folder": "Finance",
        "names": (
            "invoice", "receipt", "payslip", "pay slip", "salary", "bank statement",
            "statement", "tax", "budget", "faktura", "kvittering", "lønseddel",
            "kontoudtog", "skat", "årsopgørelse",
        ),
        "content": (
            "invoice", "invoice number", "amount due", "total due", "subtotal",
            "vat", "payment terms", "due date", "receipt", "faktura",
            "fakturanummer", "moms", "at betale", "betalingsfrist", "kvittering",
        ),
    },
    {
        "folder": "Contracts & Legal",
        "names": (
            "contract", "agreement", "nda", "terms", "lease", "policy",
            "insurance", "kontrakt", "aftale", "forsikring", "fuldmagt",
        ),
        "content": (
            "this agreement", "the parties", "hereinafter", "governing law",
            "terms and conditions", "termination", "in witness whereof",
            "tenant", "landlord", "parterne", "opsigelse", "underskrift",
            "lejer", "udlejer",
        ),
    },
    {
        "folder": "Travel & Tickets",
        "names": (
            "ticket", "boarding pass", "boardingpass", "itinerary", "booking",
            "reservation", "flight", "hotel", "billet", "rejseplan",
        ),
        "content": (
            "boarding pass", "booking reference", "booking number",
            "confirmation number", "itinerary", "departure", "arrival",
            "check-in", "passenger", "flight", "afgang", "ankomst", "rejseplan",
        ),
    },
    {
        "folder": "Certificates & Diplomas",
        "names": (
            "diploma", "certificate", "certification", "transcript",
            "certifikat", "eksamensbevis", "karakterudskrift", "kursusbevis",
        ),
        "content": (
            "this is to certify", "certify that", "has successfully completed",
            "certificate of", "awarded to", "diploma", "transcript of records",
            "ects", "eksamensbevis", "karakterudskrift",
        ),
    },
    {
        "folder": "Manuals & Guides",
        "names": (
            "manual", "guide", "handbook", "instructions", "datasheet",
            "quickstart", "quick start", "vejledning", "brugsanvisning",
        ),
        "content": (
            "user manual", "user guide", "installation", "safety instructions",
            "troubleshooting", "warranty", "brugsanvisning", "vejledning",
            "sikkerhedsinstruktioner",
        ),
    },
)
TOPIC_FOLDERS = tuple(topic["folder"] for topic in SMART_TOPICS)
# Kinds whose names are checked for topics; the first three also by content.
NAME_TOPIC_KINDS = {"Documents", "Spreadsheets", "Presentations", "Images"}
CONTENT_TOPIC_KINDS = {"Documents", "Spreadsheets", "Presentations"}
SCREENSHOT_KEYWORDS = (
    "screenshot", "screen shot", "screen capture", "capture", "snip", "skærmbillede",
)
PHOTO_EXTENSIONS = {".heic", ".heif", ".raw", ".cr2", ".nef", ".arw", ".dng"}
PHOTO_NAME = re.compile(
    r"^(img|dsc|dscn|dscf|pxl|mvimg|photo|foto|gopr)[\s_-]?\d|^\d{8}[_-]\d{6}",
    re.IGNORECASE,
)
SMART_FOLDERS = TOPIC_FOLDERS + (
    "Documents", "Spreadsheets", "Presentations", "Photos", "Screenshots",
    "Images", "Audio", "Video", "Archives", "Installers", "Code", "Fonts",
    DUPLICATES, OTHER,
)

EXTENSION_TO_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
COMPOUND_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz")
UNFINISHED_DOWNLOAD_SUFFIXES = {
    ".crdownload", ".part", ".partial", ".download", ".opdownload", ".tmp",
}
SYSTEM_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}
LOG_PREFIX = ".sort_files_log_"
LOG_TOOL = "sort_files"
LOG_VERSION = 1
SORT_MODES = ("smart", "type", "date", "extension")

# Limits that keep content sniffing quick on big or hostile files.
MAX_READ_BYTES = 3_000_000
MAX_TEXT_CHARS = 60_000
MAX_PDF_STREAMS = 400


class SortError(Exception):
    """A problem the user must fix before anything can be sorted or restored."""


@dataclass(frozen=True)
class Move:
    source: Path
    target: Path
    reason: str = ""


@dataclass(frozen=True)
class Candidate:
    """A scanned file and the folder (relative to the destination) suggested for it."""
    path: Path
    folder: Path
    reason: str


@dataclass
class Scan:
    source: Path
    dest: Path
    candidates: list = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)


@dataclass
class Plan:
    source: Path
    dest: Path
    moves: list = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    already_sorted: int = 0


# ---------------------------------------------------------------------------
# Names and kinds
# ---------------------------------------------------------------------------

def split_extension(name):
    """Return (base, extension), keeping two-part extensions like .tar.gz whole."""
    lower = name.lower()
    for ext in COMPOUND_EXTENSIONS:
        if lower.endswith(ext) and len(name) > len(ext):
            return name[: -len(ext)], name[-len(ext):]
    suffix = Path(name).suffix
    if suffix in ("", "."):
        return name, ""
    return name[: -len(suffix)], suffix


def category_for(name):
    return EXTENSION_TO_CATEGORY.get(split_extension(name)[1].lower(), OTHER)


def _normalize(text):
    """Lowercase and strip accents, so 'Résumé' matches 'resume'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def name_words(name):
    base = split_extension(name)[0]
    base = re.sub(r"([a-z])([A-Z])", r"\1 \2", base)  # camelCase -> camel Case
    return re.findall(r"[^\W\d_]+", _normalize(base))


def _keyword_in_words(words, keyword):
    parts = _normalize(keyword).split()
    if len(parts) == 1:
        kw = parts[0]
        return any(
            w == kw or (len(kw) >= 5 and (w.startswith(kw) or w.endswith(kw)))
            for w in words
        )
    joined = "".join(parts)
    n = len(parts)
    return any(words[i:i + n] == parts for i in range(len(words) - n + 1)) or any(
        w.startswith(joined) for w in words
    )


def _name_topic(words):
    best, best_hits = None, []
    for topic in SMART_TOPICS:
        hits = [kw for kw in topic["names"] if _keyword_in_words(words, kw)]
        if len(hits) > len(best_hits):
            best, best_hits = topic["folder"], hits
    if best:
        return best, f'name mentions "{best_hits[0]}"'
    return None


_CONTENT_PATTERNS = [
    (
        topic["folder"],
        [
            (kw, re.compile(r"(?<!\w)" + re.escape(_normalize(kw)) + r"(?!\w)"))
            for kw in topic["content"]
        ],
    )
    for topic in SMART_TOPICS
]


def _content_topic(text):
    if not text:
        return None
    norm = _normalize(text)
    scored = []
    for folder, patterns in _CONTENT_PATTERNS:
        hits = [kw for kw, pattern in patterns if pattern.search(norm)]
        scored.append((len(hits), folder, hits))
    scored.sort(key=lambda s: -s[0])
    count, folder, hits = scored[0]
    if count >= 2 and count > scored[1][0]:
        shown = ", ".join(f'"{h}"' for h in hits[:2])
        return folder, f"document mentions {shown}"
    return None


def _kind_reason(name):
    ext = split_extension(name)[1]
    if not ext:
        return "no file extension"
    if category_for(name) == OTHER:
        return f"unrecognised type ({ext.lower()})"
    return f"{ext[1:].upper()} file"


def smart_classify(path, read_content=True):
    """Return (folder name, human-readable reason) for one file."""
    name = path.name
    kind = category_for(name)
    words = name_words(name)
    if kind in NAME_TOPIC_KINDS:
        topic = _name_topic(words)
        if topic:
            return topic
    if kind == "Images":
        if any(_keyword_in_words(words, kw) for kw in SCREENSHOT_KEYWORDS):
            return "Screenshots", "named like a screenshot"
        if split_extension(name)[1].lower() in PHOTO_EXTENSIONS or PHOTO_NAME.match(name):
            return "Photos", "named like a camera photo"
        return "Images", _kind_reason(name)
    if kind in CONTENT_TOPIC_KINDS and read_content:
        topic = _content_topic(extract_text(path))
        if topic:
            return topic
    return kind, _kind_reason(name)


def classify(path, by):
    """Return (folder relative to the destination, reason) for one file."""
    if by == "smart":
        folder, reason = smart_classify(path)
        return Path(folder), reason
    if by == "type":
        return Path(category_for(path.name)), _kind_reason(path.name)
    if by == "extension":
        ext = split_extension(path.name)[1]
        return Path(ext[1:].upper() if ext else NO_EXTENSION), _kind_reason(path.name)
    if by == "date":
        stamp = datetime.fromtimestamp(path.stat().st_mtime)
        return Path(f"{stamp:%Y}", f"{stamp:%Y-%m}"), f"modified {stamp:%d %b %Y}"
    raise ValueError(f"unknown sort mode: {by}")


def folder_for(path, by):
    return classify(path, by)[0]


# ---------------------------------------------------------------------------
# Reading document text (best effort, standard library only)
# ---------------------------------------------------------------------------

PLAIN_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".tex", ".rtf", ".html", ".htm"}
ZIP_XML_MEMBERS = {
    ".docx": ("word/document.xml",),
    ".odt": ("content.xml",),
    ".ods": ("content.xml",),
    ".odp": ("content.xml",),
    ".xlsx": ("xl/sharedStrings.xml",),
    ".xlsm": ("xl/sharedStrings.xml",),
    ".pptx": None,  # every ppt/slides/slideN.xml
}
_XML_BREAKS = re.compile(r"</w:p>|<w:tab/>|<w:br/>|</text:p>|</text:h>|</a:p>|</si>|<text:s/>")
_TAGS = re.compile(r"<[^>]+>")


def extract_text(path):
    """Readable text from a document, or "" if it cannot be read cheaply."""
    ext = split_extension(path.name)[1].lower()
    try:
        if ext == ".pdf":
            text = _pdf_text(path)
        elif ext in ZIP_XML_MEMBERS:
            text = _zip_xml_text(path, ext)
        elif ext in PLAIN_TEXT_EXTENSIONS:
            text = _plain_text(path, ext)
        else:
            return ""
    except Exception:  # noqa: BLE001 - malformed files must never stop a sort
        return ""
    return text[:MAX_TEXT_CHARS]


def _plain_text(path, ext):
    with path.open("rb") as fh:
        raw = fh.read(MAX_READ_BYTES)
    if ext == ".rtf":
        raw = re.sub(rb"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1).decode()), raw)
        text = raw.decode("cp1252", "ignore")
        text = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", " ", text)
        return text
    text = raw.decode("utf-8", "ignore")
    if ext in (".html", ".htm"):
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = html.unescape(_TAGS.sub(" ", text))
    return text


def _zip_xml_text(path, ext):
    parts = []
    budget = MAX_READ_BYTES
    with zipfile.ZipFile(path) as archive:
        members = ZIP_XML_MEMBERS[ext]
        if members is None:
            members = sorted(
                (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()),
            )
        for member in members:
            if budget <= 0:
                break
            with archive.open(member) as fh:
                data = fh.read(budget)
            budget -= len(data)
            parts.append(data.decode("utf-8", "ignore"))
    xml = _XML_BREAKS.sub(" ", " ".join(parts))
    return html.unescape(_TAGS.sub("", xml))


_PDF_STREAM = re.compile(rb"stream\r?\n")
_PDF_SHOW = re.compile(
    rb"\(((?:\\.|[^\\()])*)\)\s*(?:Tj|')|\[((?:\\.|[^\\\]])*)\]\s*TJ", re.DOTALL
)
_PDF_ARRAY_ITEM = re.compile(rb"\(((?:\\.|[^\\()])*)\)|(-?\d+(?:\.\d+)?)", re.DOTALL)
_PDF_INFO = re.compile(rb"/(?:Title|Subject|Keywords)\s*\(((?:\\.|[^\\()])*)\)", re.DOTALL)
_PDF_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"", b"f": b""}


def _pdf_unescape(raw):
    def replace(match):
        token = match.group(1)
        if token[:1].isdigit():
            return bytes([int(token, 8) & 0xFF])
        if token in (b"\n", b"\r\n", b"\r"):
            return b""
        return _PDF_ESCAPES.get(token, token)

    raw = re.sub(rb"\\([0-7]{1,3}|\r\n|.)", replace, raw, flags=re.DOTALL)
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "ignore")
    return raw.decode("latin-1")


def _pdf_text(path):
    """Text shown by a PDF's content streams, plus its title metadata.

    Works for PDFs that store text as plain strings (many generated by
    office suites and LaTeX); PDFs using hex-encoded glyph IDs give little
    or nothing back, and the file name is then all smart mode has to go on.
    """
    with path.open("rb") as fh:
        data = fh.read(MAX_READ_BYTES)
    pieces = [_pdf_unescape(m.group(1)) for m in _PDF_INFO.finditer(data)]
    size = 0
    for count, match in enumerate(_PDF_STREAM.finditer(data)):
        if count >= MAX_PDF_STREAMS or size >= MAX_TEXT_CHARS:
            break
        end = data.find(b"endstream", match.end())
        if end < 0:
            break
        raw = data[match.end():end]
        try:
            content = zlib.decompressobj().decompress(raw, 1_000_000)
        except zlib.error:
            content = raw
        pieces.extend(_pdf_unescape(m.group(1)) for m in _PDF_INFO.finditer(content))
        for show in _PDF_SHOW.finditer(content):
            if show.group(2) is None:
                text = _pdf_unescape(show.group(1))
            else:
                text = "".join(
                    _pdf_unescape(item.group(1)) if item.group(1) is not None
                    else (" " if float(item.group(2)) <= -200 else "")
                    for item in _PDF_ARRAY_ITEM.finditer(show.group(2))
                )
            pieces.append(text)
            size += len(text)
    return " ".join(pieces)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicates(paths):
    """Map each extra copy of a file to the copy that is kept (the oldest)."""
    by_size = defaultdict(list)
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size:
            by_size[size].append(path)
    duplicates = {}
    for group in by_size.values():
        if len(group) < 2:
            continue
        by_hash = defaultdict(list)
        for path in group:
            try:
                by_hash[_sha256(path)].append(path)
            except OSError:
                continue
        for same in by_hash.values():
            if len(same) < 2:
                continue
            same.sort(key=lambda p: (p.stat().st_mtime, len(p.name), p.name))
            for extra in same[1:]:
                duplicates[extra] = same[0]
    return duplicates


# ---------------------------------------------------------------------------
# Scanning and planning
# ---------------------------------------------------------------------------

def skip_reason(name, include_hidden):
    lower = name.lower()
    if name.startswith(LOG_PREFIX):
        return "undo log"
    if lower in SYSTEM_FILES:
        return "system file"
    if name.startswith(".") and not include_hidden:
        return "hidden"
    if split_extension(name)[1].lower() in UNFINISHED_DOWNLOAD_SUFFIXES:
        return "unfinished download"
    return None


def _key(path):
    # Case-insensitive so the plan is also safe on macOS and Windows disks.
    return str(path).casefold()


def unique_target(target, reserved):
    """First free name: report.pdf, report (1).pdf, report (2).pdf, ..."""
    base, ext = split_extension(target.name)
    candidate, n = target, 1
    while os.path.lexists(candidate) or _key(candidate) in reserved:
        candidate = target.with_name(f"{base} ({n}){ext}")
        n += 1
    return candidate


def _is_within(path, folder):
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{dev}{n}" for dev in ("com", "lpt") for n in range(1, 10)
}
_BAD_NAME_CHARS = set('<>:"|?*\\')


def safe_folder(folder):
    """Validate a destination subfolder like "Finance" or "2024/2024-03".

    Rejects anything that could escape the destination or that Windows or
    macOS cannot store, so a folder name typed into the app is always safe.
    """
    text = folder.as_posix() if isinstance(folder, Path) else str(folder)
    parts = [part.strip() for part in text.split("/")]
    ok = 0 < len(parts) <= 4 and all(
        part
        and part not in (".", "..")
        and len(part) <= 120
        and part == part.rstrip(". ")
        and not any(c in _BAD_NAME_CHARS or ord(c) < 32 for c in part)
        and part.split(".")[0].lower() not in _WINDOWS_RESERVED
        for part in parts
    )
    if not ok:
        raise SortError(f"not a usable folder name: {text!r}")
    return Path(*parts)


def iter_files(source, dest, recursive):
    if not recursive:
        yield from sorted(source.iterdir())
        return
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".")
            and not os.path.lexists(root_path / d / ".git")
            and not (dest != source and root_path / d == dest)
        )
        for name in sorted(files):
            yield root_path / name


def check_folders(source, dest, recursive):
    if not source.is_dir():
        raise SortError(f"not a folder: {source}")
    if source == Path(source.anchor):
        raise SortError("refusing to sort a whole drive; point it at a specific folder")
    if recursive and source == Path.home().resolve():
        raise SortError(
            "refusing to sort your entire home folder recursively; "
            "point it at a specific folder such as Downloads"
        )
    if os.path.lexists(dest) and not dest.is_dir():
        raise SortError(f"destination is not a folder: {dest}")


def scan(source, dest=None, by="smart", recursive=False, include_hidden=False,
         duplicates=False):
    """Find the files to sort and suggest a folder for each one."""
    if by not in SORT_MODES:
        raise SortError(f"unknown sort mode: {by}")
    source = Path(source).expanduser().resolve()
    dest = Path(dest).expanduser().resolve() if dest else source
    check_folders(source, dest, recursive)

    result = Scan(source=source, dest=dest)
    files = []
    try:
        for path in iter_files(source, dest, recursive):
            if path.is_symlink():
                result.skipped["shortcut/symlink"] += 1
                continue
            if not path.is_file():
                continue
            reason = skip_reason(path.name, include_hidden)
            if reason:
                result.skipped[reason] += 1
                continue
            files.append(path)
    except PermissionError as exc:
        raise SortError(f"no permission to read {exc.filename or source}") from exc

    extra_copies = find_duplicates(files) if duplicates else {}
    for path in files:
        if path in extra_copies:
            folder, reason = Path(DUPLICATES), f'same content as "{extra_copies[path].name}"'
        else:
            try:
                folder, reason = classify(path, by)
            except OSError:
                result.skipped["unreadable"] += 1
                continue
        result.candidates.append(Candidate(path, folder, reason))
    return result


def plan_moves(source, dest, choices, skipped=None):
    """Turn (path, folder, reason) choices into concrete, clash-free moves."""
    plan = Plan(source=source, dest=dest, skipped=Counter(skipped or {}))
    reserved = set()
    for path, folder, reason in choices:
        target_dir = dest / safe_folder(folder)
        if target_dir.is_dir() and target_dir.samefile(path.parent):
            plan.already_sorted += 1
            continue
        target = unique_target(target_dir / path.name, reserved)
        reserved.add(_key(target))
        plan.moves.append(Move(path, target, reason))
    return plan


def build_plan(source, dest=None, by="smart", recursive=False, include_hidden=False,
               duplicates=False):
    result = scan(source, dest, by, recursive, include_hidden, duplicates)
    return plan_moves(
        result.source,
        result.dest,
        [(c.path, c.folder, c.reason) for c in result.candidates],
        result.skipped,
    )


# ---------------------------------------------------------------------------
# Applying and undoing
# ---------------------------------------------------------------------------

def make_dirs(folder):
    """Create folder and any missing parents; return the ones created, top-down."""
    missing = []
    current = folder
    while not current.exists():
        missing.append(current)
        current = current.parent
    missing.reverse()
    for directory in missing:
        directory.mkdir(exist_ok=True)
    return missing


def write_log(plan, done, created_dirs):
    now = datetime.now()
    log_path = unique_target(plan.dest / f"{LOG_PREFIX}{now:%Y%m%d-%H%M%S}.json", set())
    data = {
        "tool": LOG_TOOL,
        "version": LOG_VERSION,
        "created": now.isoformat(timespec="seconds"),
        "source": str(plan.source),
        "dest": str(plan.dest),
        "moves": [{"from": str(m.source), "to": str(m.target)} for m in done],
        "created_dirs": [str(d) for d in created_dirs],
    }
    log_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return log_path


def apply_plan(plan, copy=False):
    """Carry out the plan. Returns (done, errors, log_path).

    The undo log is written even if the run is interrupted part-way, so
    whatever was moved can always be restored. Copies get no log: the
    originals are untouched, so there is nothing to undo.
    """
    done, errors, created_dirs = [], [], []
    log_path = None
    transfer = shutil.copy2 if copy else shutil.move
    try:
        for move in plan.moves:
            try:
                created_dirs.extend(make_dirs(move.target.parent))
                target = move.target
                if os.path.lexists(target):  # appeared since the preview
                    target = unique_target(target, set())
                transfer(str(move.source), str(target))
                done.append(Move(move.source, target, move.reason))
            except OSError as exc:
                errors.append((move, exc))
    finally:
        if done and not copy:
            log_path = write_log(plan, done, created_dirs)
    return done, errors, log_path


def load_log(log_path):
    try:
        data = json.loads(Path(log_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SortError(f"could not read undo log {log_path}: {exc}") from exc
    valid = (
        isinstance(data, dict)
        and data.get("tool") == LOG_TOOL
        and isinstance(data.get("moves"), list)
        and all(
            isinstance(m, dict) and isinstance(m.get("from"), str) and isinstance(m.get("to"), str)
            for m in data["moves"]
        )
    )
    if not valid:
        raise SortError(f"{log_path} is not an undo log written by this tool")
    return data


def plan_undo(data):
    restores, problems = [], []
    for entry in reversed(data["moves"]):
        current, original = Path(entry["to"]), Path(entry["from"])
        if not os.path.lexists(current):
            problems.append((current, "no longer where it was sorted to"))
        elif os.path.lexists(original):
            problems.append((current, f"something else now occupies {original}"))
        else:
            restores.append(Move(current, original))
    return restores, problems


def apply_undo(data, restores):
    done, errors = [], []
    for move in restores:
        try:
            move.target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.source), str(move.target))
            done.append(move)
        except OSError as exc:
            errors.append((move, exc))
    for directory in reversed(data.get("created_dirs", [])):
        try:
            Path(directory).rmdir()  # only succeeds if the folder is empty
        except OSError:
            pass
    return done, errors


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _display(path, root):
    return path.relative_to(root).as_posix() if _is_within(path, root) else str(path)


def format_plan(plan, show_reasons=False):
    groups = defaultdict(list)
    for move in plan.moves:
        groups[move.target.parent].append(move)
    lines = []
    for folder in sorted(groups, key=lambda p: str(p).casefold()):
        lines.append(f"{_display(folder, plan.dest)}/  ({len(groups[folder])})")
        for move in groups[folder]:
            line = f"    {_display(move.source, plan.source)}"
            if move.target.name != move.source.name:
                line += f"  ->  renamed {move.target.name} (name already taken)"
            if show_reasons and move.reason:
                line += f"   [{move.reason}]"
            lines.append(line)
    return "\n".join(lines)


def format_summary(plan):
    folders = len({m.target.parent for m in plan.moves})
    parts = [f"{len(plan.moves)} file(s) into {folders} folder(s)"]
    if plan.already_sorted:
        parts.append(f"{plan.already_sorted} already in place")
    if plan.skipped:
        detail = ", ".join(f"{reason}: {n}" for reason, n in sorted(plan.skipped.items()))
        parts.append(f"skipped {sum(plan.skipped.values())} ({detail})")
    return "; ".join(parts) + "."


def _script_hint():
    script = Path(__file__).resolve()
    try:
        return script.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(script)


def run_sort(args):
    plan = build_plan(
        args.folder, args.dest, args.by, args.recursive, args.include_hidden, args.duplicates
    )
    if not plan.moves:
        print(f"Nothing to sort in {plan.source}. {format_summary(plan)}")
        return 0
    print(format_plan(plan, show_reasons=args.by == "smart" or args.duplicates))
    print()
    print(format_summary(plan))
    if not args.apply:
        print("Preview only - nothing has been changed. Re-run with --apply to sort.")
        return 0

    done, errors, log_path = apply_plan(plan, copy=args.copy)
    print(f"{'Copied' if args.copy else 'Moved'} {len(done)} file(s) into {plan.dest}.")
    for move, exc in errors:
        print(f"  could not sort {move.source}: {exc}", file=sys.stderr)
    if log_path:
        print(f'To undo: python3 {_script_hint()} --undo "{log_path}" --apply')
    return 1 if errors else 0


def run_undo(args):
    data = load_log(args.undo)
    restores, problems = plan_undo(data)
    for path, reason in problems:
        print(f"  cannot restore {path}: {reason}", file=sys.stderr)
    if not restores:
        print("Nothing to restore.")
        return 1 if problems else 0
    for move in restores:
        print(f"    {move.source}  ->  {move.target}")
    print()
    if not args.apply:
        print(f"Preview: {len(restores)} file(s) would go back. Re-run with --apply to restore.")
        return 0
    done, errors = apply_undo(data, restores)
    print(f"Restored {len(done)} file(s) to where they were.")
    for move, exc in errors:
        print(f"  could not restore {move.source}: {exc}", file=sys.stderr)
    return 1 if errors or problems else 0


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Sort the files in a folder into subfolders, entirely offline. "
            "Shows a preview unless --apply is given. For a point-and-click "
            "window, run file_sorter_app.py instead."
        ),
    )
    parser.add_argument("folder", nargs="?", help="folder to sort, e.g. ~/Downloads")
    parser.add_argument(
        "--by", choices=SORT_MODES, default="smart",
        help="smart: by topic (Finance, Job Applications, ...) then kind (default); "
             "type: Documents/, Images/, ...; date: 2024/2024-03/ by last-modified "
             "time; extension: PDF/, DOCX/, ...",
    )
    parser.add_argument(
        "--duplicates", action="store_true",
        help="put exact copies of another file into a Duplicates folder "
             "(the oldest copy is sorted normally)",
    )
    parser.add_argument(
        "--dest", metavar="FOLDER",
        help="put the sorted subfolders here instead of inside FOLDER",
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true",
        help="also sort files in subfolders (hidden folders and code projects "
             "are always left alone)",
    )
    parser.add_argument(
        "--include-hidden", action="store_true",
        help="also sort hidden files (names starting with a dot)",
    )
    parser.add_argument(
        "--copy", action="store_true",
        help="copy files instead of moving them; the originals stay put",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually move the files (without it, only a preview is shown)",
    )
    parser.add_argument(
        "--undo", metavar="LOGFILE",
        help="put files back using the .sort_files_log_*.json written by an "
             "earlier --apply run (combine with --apply to restore)",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.undo:
            return run_undo(args)
        if not args.folder:
            parser.error("give a folder to sort, or --undo LOGFILE")
        return run_sort(args)
    except SortError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
