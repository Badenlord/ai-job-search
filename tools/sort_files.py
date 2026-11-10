#!/usr/bin/env python3
"""Sort a messy folder into tidy subfolders, entirely offline.

Groups files by type (Documents, Images, Archives, ...), by month
(2024/2024-03), or by extension (PDF, DOCX, ...). Uses only the Python
standard library and never touches the network.

Safe by default:
  * Shows a preview and changes nothing unless --apply is given.
  * Never overwrites: a name clash becomes "report (1).pdf".
  * Skips hidden files, symlinks, system files and unfinished downloads.
  * With --recursive, never descends into hidden folders or code projects
    (folders containing .git).
  * Every applied move is logged, so --undo can put everything back.

Examples:
  python3 tools/sort_files.py ~/Downloads                    # preview
  python3 tools/sort_files.py ~/Downloads --apply            # sort by type
  python3 tools/sort_files.py ~/Pictures --by date --apply   # 2024/2024-03/...
  python3 tools/sort_files.py ~/Downloads --undo ~/Downloads/.sort_files_log_<stamp>.json --apply
"""

import argparse
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Edit this table to change where file types go. Extensions are lowercase.
CATEGORIES = {
    "Documents": [
        ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".tex",
        ".pages", ".epub", ".mobi",
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
        ".py", ".ipynb", ".js", ".ts", ".html", ".css", ".json", ".xml",
        ".yaml", ".yml", ".sh", ".ps1", ".bat", ".c", ".cpp", ".h", ".java",
        ".go", ".rs", ".rb", ".php", ".sql", ".r", ".m",
    ],
    "Fonts": [".ttf", ".otf", ".woff", ".woff2"],
}
OTHER = "Other"
NO_EXTENSION = "No extension"

EXTENSION_TO_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
COMPOUND_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz")
UNFINISHED_DOWNLOAD_SUFFIXES = {
    ".crdownload", ".part", ".partial", ".download", ".opdownload", ".tmp",
}
SYSTEM_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}
LOG_PREFIX = ".sort_files_log_"
LOG_TOOL = "sort_files"
LOG_VERSION = 1


class SortError(Exception):
    """A problem the user must fix before anything can be sorted or restored."""


@dataclass(frozen=True)
class Move:
    source: Path
    target: Path


@dataclass
class Plan:
    source: Path
    dest: Path
    moves: list = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    already_sorted: int = 0


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


def folder_for(path, by):
    """The subfolder (relative to the destination) a file belongs in."""
    if by == "type":
        return Path(category_for(path.name))
    if by == "extension":
        ext = split_extension(path.name)[1]
        return Path(ext[1:].upper() if ext else NO_EXTENSION)
    if by == "date":
        stamp = datetime.fromtimestamp(path.stat().st_mtime)
        return Path(f"{stamp:%Y}", f"{stamp:%Y-%m}")
    raise ValueError(f"unknown sort mode: {by}")


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


def build_plan(source, dest=None, by="type", recursive=False, include_hidden=False):
    source = Path(source).expanduser().resolve()
    dest = Path(dest).expanduser().resolve() if dest else source
    check_folders(source, dest, recursive)

    plan = Plan(source=source, dest=dest)
    reserved = set()
    for path in iter_files(source, dest, recursive):
        if path.is_symlink():
            plan.skipped["shortcut/symlink"] += 1
            continue
        if not path.is_file():
            continue
        reason = skip_reason(path.name, include_hidden)
        if reason:
            plan.skipped[reason] += 1
            continue
        target_dir = dest / folder_for(path, by)
        if target_dir.is_dir() and target_dir.samefile(path.parent):
            plan.already_sorted += 1
            continue
        target = unique_target(target_dir / path.name, reserved)
        reserved.add(_key(target))
        plan.moves.append(Move(path, target))
    return plan


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
                done.append(Move(move.source, target))
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


def _display(path, root):
    return path.relative_to(root).as_posix() if _is_within(path, root) else str(path)


def format_plan(plan):
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
    plan = build_plan(args.folder, args.dest, args.by, args.recursive, args.include_hidden)
    if not plan.moves:
        print(f"Nothing to sort in {plan.source}. {format_summary(plan)}")
        return 0
    print(format_plan(plan))
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
            "Shows a preview unless --apply is given."
        ),
        epilog="Run with --help for details; the module docstring has more examples.",
    )
    parser.add_argument("folder", nargs="?", help="folder to sort, e.g. ~/Downloads")
    parser.add_argument(
        "--by", choices=("type", "date", "extension"), default="type",
        help="type: Documents/, Images/, ... (default); date: 2024/2024-03/ by "
             "last-modified time; extension: PDF/, DOCX/, ...",
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
