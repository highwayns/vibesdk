#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Markdown beautifier for tables produced by Excel→Markdown pipelines.

Main fixes:
- Normalize Markdown tables (pipes, separator row, column counts)
- Drop trailing/all-empty columns (safe mode: only when header is generic/blank)
- Optionally drop empty rows inside tables
- Clean up whitespace (trim trailing spaces, collapse excessive blank lines)
- Preserve fenced code blocks (``` ... ```), including mermaid/code snippets

Usage:
  python3 md_prettify.py input.md
  python3 md_prettify.py input.md -o pretty.md
  python3 md_prettify.py ./docs --recursive --in-place

No external dependencies.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


GENERIC_COL_RE = re.compile(r"^col\d+$", re.IGNORECASE)


def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def count_unescaped_pipes(line: str) -> int:
    # count pipes not escaped and not inside inline code (`...`)
    in_code = False
    esc = False
    cnt = 0
    for ch in line:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == "`":
            in_code = not in_code
            continue
        if ch == "|" and not in_code:
            cnt += 1
    return cnt


def split_table_row(line: str) -> List[str]:
    """
    Split a Markdown table row into cells.
    - Respects escaped pipes \|
    - Does not split pipes inside inline code spans (`...`)
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]

    cells: List[str] = []
    buf: List[str] = []
    in_code = False
    esc = False

    for ch in s:
        if esc:
            buf.append(ch)
            esc = False
            continue
        if ch == "\\":
            esc = True
            buf.append(ch)  # preserve backslash
            continue
        if ch == "`":
            in_code = not in_code
            buf.append(ch)
            continue
        if ch == "|" and not in_code:
            cells.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)

    cells.append("".join(buf).strip())
    return cells


def is_separator_row(cells: List[str]) -> bool:
    if not cells:
        return False
    for c in cells:
        t = c.strip()
        if not t:
            return False
        if not re.fullmatch(r":?-{3,}:?", t):
            return False
    return True


def is_table_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if count_unescaped_pipes(s) < 2:
        return False
    if s.startswith(("```", "+++", "---")):
        return False
    return True


def is_blank_cell(cell: str, treat_dash_as_blank: bool = True) -> bool:
    t = cell.strip()
    if t == "":
        return True
    if treat_dash_as_blank and t == "-":
        return True
    return False


@dataclass
class TableConfig:
    drop_empty_rows: bool = True
    drop_empty_cols: bool = True
    treat_dash_as_blank: bool = True
    aggressive_drop_cols: bool = False  # drop empty cols even if header meaningful


def format_table(block_lines: List[str], cfg: TableConfig) -> Tuple[List[str], bool]:
    raw = [ln.rstrip() for ln in block_lines]

    parsed: List[List[str]] = [split_table_row(ln) for ln in raw]

    # find separator row
    sep_idx = None
    for i, cells in enumerate(parsed[:10]):
        if is_separator_row(cells):
            sep_idx = i
            break
    if sep_idx is None:
        return raw, False

    # header row = nearest non-separator above sep
    header_idx = None
    for i in range(sep_idx - 1, -1, -1):
        if not is_separator_row(parsed[i]):
            header_idx = i
            break
    if header_idx is None:
        header_idx = 0

    header = parsed[header_idx]
    body = [r for j, r in enumerate(parsed) if j not in (header_idx, sep_idx) and not is_separator_row(r)]

    max_cols = max([len(header), len(parsed[sep_idx])] + [len(r) for r in body] + [1])

    def pad(row: List[str]) -> List[str]:
        return row + [""] * (max_cols - len(row))

    header = pad(header)
    body = [pad(r) for r in body]

    # drop empty rows
    if cfg.drop_empty_rows:
        body = [r for r in body if not all(is_blank_cell(c, cfg.treat_dash_as_blank) for c in r)]

    kept_cols = list(range(max_cols))
    if cfg.drop_empty_cols and max_cols > 1:
        keep = [True] * max_cols
        for ci in range(max_cols):
            body_all_blank = all(is_blank_cell(r[ci], cfg.treat_dash_as_blank) for r in body) if body else True
            if not body_all_blank:
                continue
            h = header[ci].strip()
            header_generic = (h == "" or GENERIC_COL_RE.fullmatch(h) is not None)
            if cfg.aggressive_drop_cols or header_generic:
                keep[ci] = False
        kept_cols = [i for i, ok in enumerate(keep) if ok] or [0]

    def pick(row: List[str]) -> List[str]:
        return [row[i] for i in kept_cols]

    header2 = pick(header)
    body2 = [pick(r) for r in body]

    # ensure header not all empty
    if all(h.strip() == "" for h in header2):
        header2 = [f"col{i+1}" for i in range(len(header2))]

    def norm_cell(c: str) -> str:
        # collapse internal whitespace a bit
        return re.sub(r"\s+", " ", c.strip())

    header2 = [norm_cell(c) for c in header2]
    body2 = [[norm_cell(c) for c in r] for r in body2]

    n = len(header2)
    out: List[str] = []
    out.append("| " + " | ".join(header2) + " |")
    out.append("|" + "|".join(["---"] * n) + "|")
    for r in body2:
        out.append("| " + " | ".join(r + [""] * (n - len(r))) + " |")

    return out, (out != raw)


def collapse_blank_lines(lines: List[str], max_blank: int = 2) -> List[str]:
    out: List[str] = []
    blanks = 0
    for ln in lines:
        if ln.strip() == "":
            blanks += 1
            if blanks <= max_blank:
                out.append("")
        else:
            blanks = 0
            out.append(ln.rstrip())
    while out and out[-1] == "":
        out.pop()
    out.append("")  # end with newline
    return out


def prettify_markdown_text(text: str, cfg: TableConfig, max_blank: int = 2) -> Tuple[str, dict]:
    text = normalize_newlines(text)
    lines = text.split("\n")

    out: List[str] = []
    tables_reformatted = 0
    in_fence = False
    fence_pat = re.compile(r"^\s*```")

    i = 0
    while i < len(lines):
        ln = lines[i]

        if fence_pat.match(ln):
            in_fence = not in_fence
            out.append(ln.rstrip())
            i += 1
            continue

        if in_fence:
            out.append(ln.rstrip())
            i += 1
            continue

        if is_table_line(ln):
            j = i
            block: List[str] = []
            while j < len(lines) and lines[j].strip() != "" and is_table_line(lines[j]):
                block.append(lines[j])
                j += 1
            formatted, changed = format_table(block, cfg)
            out.extend(formatted if changed else [b.rstrip() for b in block])
            if changed:
                tables_reformatted += 1
            i = j
            continue

        out.append(ln.rstrip())
        i += 1

    out2 = collapse_blank_lines(out, max_blank=max_blank)
    result = "\n".join(out2)
    report = {
        "tables_reformatted": tables_reformatted,
        "max_blank_lines": max_blank,
        "drop_empty_rows": cfg.drop_empty_rows,
        "drop_empty_cols": cfg.drop_empty_cols,
        "treat_dash_as_blank": cfg.treat_dash_as_blank,
        "aggressive_drop_cols": cfg.aggressive_drop_cols,
    }
    return result, report


def iter_input_files(path: Path, recursive: bool) -> List[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(str(path))
    pat = "**/*.md" if recursive else "*.md"
    return sorted(path.glob(pat))


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Beautify Markdown (especially tables).")
    p.add_argument("input", help="Input .md file or directory")
    p.add_argument("-o", "--output", default=None, help="Output file (file input only). Default: <name>_pretty.md")
    p.add_argument("--in-place", action="store_true", help="Overwrite input file(s)")
    p.add_argument("--recursive", action="store_true", help="When input is a directory, process recursively")
    p.add_argument("--max-blank", type=int, default=2, help="Max consecutive blank lines (default: 2)")

    p.add_argument("--keep-empty-rows", action="store_true", help="Do not drop empty rows inside tables")
    p.add_argument("--keep-empty-cols", action="store_true", help="Do not drop empty columns inside tables")
    p.add_argument("--keep-dash", action="store_true", help="Treat '-' as content (do not treat as blank)")
    p.add_argument("--aggressive-drop-cols", action="store_true", help="Drop empty columns even if header is meaningful")

    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logs")
    args = p.parse_args(argv)

    in_path = Path(args.input)
    files = iter_input_files(in_path, recursive=args.recursive)

    cfg = TableConfig(
        drop_empty_rows=not args.keep_empty_rows,
        drop_empty_cols=not args.keep_empty_cols,
        treat_dash_as_blank=not args.keep_dash,
        aggressive_drop_cols=args.aggressive_drop_cols,
    )

    for fp in files:
        txt = fp.read_text(encoding="utf-8", errors="replace")
        pretty, report = prettify_markdown_text(txt, cfg, max_blank=args.max_blank)

        if args.in_place:
            out_fp = fp
        else:
            if len(files) == 1 and fp.is_file() and args.output:
                out_fp = Path(args.output)
            else:
                out_fp = fp.with_name(fp.stem + "_pretty" + fp.suffix)

        out_fp.write_text(pretty, encoding="utf-8")

        if args.verbose:
            eprint(f"[OK] {fp} -> {out_fp}  (tables_reformatted={report['tables_reformatted']})")

    if not args.verbose:
        if len(files) == 1:
            if args.in_place:
                print(str(files[0]))
            else:
                if args.output:
                    print(str(Path(args.output)))
                else:
                    fp = files[0]
                    print(str(fp.with_name(fp.stem + "_pretty" + fp.suffix)))
        else:
            print(f"Processed {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
