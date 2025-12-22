#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MySQL/MariaDB DDL parser (CREATE TABLE / ALTER TABLE ... FOREIGN KEY)
- Extract: table name + table comment (Japanese), columns + column comment (Japanese),
          primary keys, foreign keys
- Output: JSON (AI-friendly) and optional Markdown summary

Usage:
  python parse_mysql_ddl.py input.sql -o schema.json --md schema.md
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------
# Low-level helpers (comments / statements / splitting)
# ----------------------------

def remove_sql_comments(text: str) -> str:
    """Remove MySQL-style comments: -- <space>, #..., /* ... */ while preserving strings."""
    out: List[str] = []
    i = 0
    in_single = in_double = in_backtick = False
    in_line_comment = False
    in_block_comment = False

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        # comment start (only when not inside quotes)
        if not (in_single or in_double or in_backtick):
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch == "#":
                in_line_comment = True
                i += 1
                continue
            if ch == "-" and nxt == "-":
                # MySQL: "-- " (needs whitespace/control after)
                nxt2 = text[i + 2] if i + 2 < len(text) else ""
                if nxt2 in (" ", "\t", "\r", "\n"):
                    in_line_comment = True
                    i += 2
                    continue

        # quote toggles
        if ch == "'" and not (in_double or in_backtick):
            if in_single and i > 0 and text[i - 1] == "\\":
                out.append(ch)
                i += 1
                continue
            in_single = not in_single
            out.append(ch)
            i += 1
            continue

        if ch == '"' and not (in_single or in_backtick):
            if in_double and i > 0 and text[i - 1] == "\\":
                out.append(ch)
                i += 1
                continue
            in_double = not in_double
            out.append(ch)
            i += 1
            continue

        if ch == "`" and not (in_single or in_double):
            in_backtick = not in_backtick
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def split_sql_statements(text: str) -> List[str]:
    """Split by ';' while preserving strings/backticks."""
    stmts: List[str] = []
    buf: List[str] = []
    i = 0
    in_single = in_double = in_backtick = False

    while i < len(text):
        ch = text[i]

        if ch == "'" and not (in_double or in_backtick):
            if in_single and i > 0 and text[i - 1] == "\\":
                buf.append(ch)
                i += 1
                continue
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue

        if ch == '"' and not (in_single or in_backtick):
            if in_double and i > 0 and text[i - 1] == "\\":
                buf.append(ch)
                i += 1
                continue
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue

        if ch == "`" and not (in_single or in_double):
            in_backtick = not in_backtick
            buf.append(ch)
            i += 1
            continue

        if ch == ";" and not (in_single or in_double or in_backtick):
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    last = "".join(buf).strip()
    if last:
        stmts.append(last)
    return stmts


def unquote_mysql_string(s: str) -> str:
    """Unquote MySQL single-quoted string, handling \', \\, and ''."""
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        body = s[1:-1]
    else:
        body = s
    body = body.replace("\\'", "'").replace("\\\\", "\\")
    body = body.replace("''", "'")
    return body


def find_matching_paren(text: str, start_idx: int) -> int:
    """Find matching ')' for '(' at start_idx, respecting quotes."""
    depth = 0
    i = start_idx
    in_single = in_double = in_backtick = False

    while i < len(text):
        ch = text[i]

        if ch == "'" and not (in_double or in_backtick):
            if in_single and i > 0 and text[i - 1] == "\\":
                i += 1
                continue
            in_single = not in_single
            i += 1
            continue

        if ch == '"' and not (in_single or in_backtick):
            if in_double and i > 0 and text[i - 1] == "\\":
                i += 1
                continue
            in_double = not in_double
            i += 1
            continue

        if ch == "`" and not (in_single or in_double):
            in_backtick = not in_backtick
            i += 1
            continue

        if in_single or in_double or in_backtick:
            i += 1
            continue

        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i

        i += 1

    return -1


def split_top_level(text: str, delimiter: str = ",") -> List[str]:
    """Split by delimiter at top-level (not inside parentheses/strings/backticks)."""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    in_single = in_double = in_backtick = False
    i = 0

    while i < len(text):
        ch = text[i]

        if ch == "'" and not (in_double or in_backtick):
            if in_single and i > 0 and text[i - 1] == "\\":
                buf.append(ch)
                i += 1
                continue
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue

        if ch == '"' and not (in_single or in_backtick):
            if in_double and i > 0 and text[i - 1] == "\\":
                buf.append(ch)
                i += 1
                continue
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue

        if ch == "`" and not (in_single or in_double):
            in_backtick = not in_backtick
            buf.append(ch)
            i += 1
            continue

        if not (in_single or in_double or in_backtick):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == delimiter and depth == 0:
                part = "".join(buf).strip()
                if part:
                    parts.append(part)
                buf = []
                i += 1
                continue

        buf.append(ch)
        i += 1

    last = "".join(buf).strip()
    if last:
        parts.append(last)
    return parts


def parse_identifier_list(text: str) -> List[str]:
    """Parse column list like `a`,`b` or a, b."""
    cols: List[str] = []
    for c in split_top_level(text, ","):
        c = c.strip()
        if c.startswith("`") and c.endswith("`"):
            c = c[1:-1]
        elif c.startswith('"') and c.endswith('"'):
            c = c[1:-1]
        elif c.startswith("'") and c.endswith("'"):
            c = c[1:-1]
        cols.append(c.strip())
    return cols


# ----------------------------
# Parsers (CREATE TABLE / column / FK)
# ----------------------------

CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?:`(?P<schema_bt>[^`]+)`|(?P<schema>[^`.\s]+))\s*\.\s*)?"
    r"(?:`(?P<table_bt>[^`]+)`|(?P<table>[^`(\s]+))\s*\(",
    re.IGNORECASE | re.DOTALL,
)


def parse_column_def(part: str) -> Optional[Dict[str, Any]]:
    m = re.match(
        r"\s*(?:`(?P<bt>[^`]+)`|(?P<plain>[A-Za-z0-9_]+))\s+(?P<rest>.+)$",
        part.strip(),
        re.DOTALL,
    )
    if not m:
        return None

    name = m.group("bt") or m.group("plain")
    rest = m.group("rest").strip()

    # type: first token (+ UNSIGNED/ZEROFILL if immediately following)
    tokens = re.split(r"\s+", rest, maxsplit=5)
    col_type = tokens[0]
    if len(tokens) > 1 and tokens[1].upper() in ("UNSIGNED", "ZEROFILL"):
        col_type += " " + tokens[1]

    nullable = True
    if re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE):
        nullable = False

    comment_ja = None
    mcom = re.search(r"\bCOMMENT\s+(\'(?:\\.|\'\'|[^\'])*\')", rest, re.IGNORECASE)
    if mcom:
        comment_ja = unquote_mysql_string(mcom.group(1))

    is_pk = bool(re.search(r"\bPRIMARY\s+KEY\b", rest, re.IGNORECASE))

    return {
        "name": name,
        "type": col_type,
        "nullable": nullable,
        "comment_ja": comment_ja,
        "is_primary_key": is_pk,
        "raw": rest,
    }


def parse_foreign_key(part: str) -> Dict[str, Any]:
    fk_name = None
    mname = re.search(r"\bCONSTRAINT\s+`?(?P<name>[^`\s]+)`?\s+", part, re.IGNORECASE)
    if mname:
        fk_name = mname.group("name")

    mfk = re.search(
        r"FOREIGN\s+KEY\s*\((?P<cols>[^)]+)\)\s*REFERENCES\s+"
        r"(?:(?:`(?P<ref_schema_bt>[^`]+)`|(?P<ref_schema>[^`.\s]+))\s*\.\s*)?"
        r"(?:`(?P<ref_table_bt>[^`]+)`|(?P<ref_table>[^`(\s]+))\s*"
        r"\((?P<ref_cols>[^)]+)\)",
        part,
        re.IGNORECASE | re.DOTALL,
    )

    cols: List[str] = []
    ref_cols: List[str] = []
    ref_table = None
    ref_schema = None
    if mfk:
        cols = parse_identifier_list(mfk.group("cols"))
        ref_cols = parse_identifier_list(mfk.group("ref_cols"))
        ref_table = mfk.group("ref_table_bt") or mfk.group("ref_table")
        ref_schema = mfk.group("ref_schema_bt") or mfk.group("ref_schema")

    on_delete = None
    on_update = None
    mdel = re.search(
        r"\bON\s+DELETE\s+(RESTRICT|CASCADE|SET\s+NULL|NO\s+ACTION|SET\s+DEFAULT)\b",
        part,
        re.IGNORECASE,
    )
    if mdel:
        on_delete = re.sub(r"\s+", " ", mdel.group(1).upper())

    mup = re.search(
        r"\bON\s+UPDATE\s+(RESTRICT|CASCADE|SET\s+NULL|NO\s+ACTION|SET\s+DEFAULT)\b",
        part,
        re.IGNORECASE,
    )
    if mup:
        on_update = re.sub(r"\s+", " ", mup.group(1).upper())

    return {
        "name": fk_name,
        "columns": cols,
        "ref_schema": ref_schema,
        "ref_table": ref_table,
        "ref_columns": ref_cols,
        "on_delete": on_delete,
        "on_update": on_update,
        "raw": part.strip(),
    }


def parse_create_table(stmt: str) -> Optional[Dict[str, Any]]:
    m = CREATE_TABLE_RE.search(stmt)
    if not m:
        return None

    schema = m.group("schema_bt") or m.group("schema")
    table = m.group("table_bt") or m.group("table")

    open_paren_idx = m.end() - 1  # pattern ends with '('
    close_paren_idx = find_matching_paren(stmt, open_paren_idx)
    if close_paren_idx == -1:
        return None

    inner = stmt[open_paren_idx + 1 : close_paren_idx]
    tail = stmt[close_paren_idx + 1 :]

    # table comment
    tcomment = None
    mcom = re.search(r"COMMENT\s*=\s*(\'(?:\\.|\'\'|[^\'])*\')", tail, re.IGNORECASE)
    if mcom:
        tcomment = unquote_mysql_string(mcom.group(1))

    table_obj: Dict[str, Any] = {
        "schema": schema,
        "name": table,
        "comment_ja": tcomment,
        "columns": [],
        "primary_key": [],
        "foreign_keys": [],
        "raw_options": tail.strip(),
    }

    parts = split_top_level(inner, ",")
    inline_pk: List[str] = []

    for part in parts:
        up = part.strip().upper()
        if not part.strip():
            continue

        # primary key
        if up.startswith("PRIMARY KEY"):
            mpk = re.search(r"PRIMARY\s+KEY\s*\((?P<cols>[^)]+)\)", part, re.IGNORECASE | re.DOTALL)
            if mpk:
                table_obj["primary_key"] = parse_identifier_list(mpk.group("cols"))
            continue

        # foreign key (in CREATE TABLE)
        if "FOREIGN KEY" in up:
            table_obj["foreign_keys"].append(parse_foreign_key(part))
            continue

        # ignore indexes
        if up.startswith(("UNIQUE KEY", "KEY", "INDEX", "FULLTEXT", "SPATIAL")):
            continue

        # column definition
        col = parse_column_def(part)
        if col:
            table_obj["columns"].append(col)
            if col.get("is_primary_key"):
                inline_pk.append(col["name"])

    if not table_obj["primary_key"] and inline_pk:
        table_obj["primary_key"] = inline_pk

    return table_obj


def parse_alter_foreign_key(stmt: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ..."""
    m = re.search(
        r"ALTER\s+TABLE\s+"
        r"(?:(?:`(?P<schema_bt>[^`]+)`|(?P<schema>[^`.\s]+))\s*\.\s*)?"
        r"(?:`(?P<table_bt>[^`]+)`|(?P<table>[^`\s]+))\s+.*FOREIGN\s+KEY",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None, None

    schema = m.group("schema_bt") or m.group("schema")
    table = m.group("table_bt") or m.group("table")
    key = f"{schema + '.' if schema else ''}{table}"
    fk = parse_foreign_key(stmt)
    return key, fk


def parse_mysql_ddl(sql_text: str) -> Dict[str, Any]:
    cleaned = remove_sql_comments(sql_text)
    stmts = split_sql_statements(cleaned)

    tables: Dict[str, Dict[str, Any]] = {}
    pending_fks: List[Tuple[str, Dict[str, Any]]] = []

    for st in stmts:
        ct = parse_create_table(st)
        if ct:
            key = f"{ct['schema'] + '.' if ct['schema'] else ''}{ct['name']}"
            tables[key] = ct
            continue

        if re.search(r"\bALTER\s+TABLE\b", st, re.IGNORECASE) and re.search(r"\bFOREIGN\s+KEY\b", st, re.IGNORECASE):
            key, fk = parse_alter_foreign_key(st)
            if key and fk:
                pending_fks.append((key, fk))

    # attach ALTER TABLE FKs
    for key, fk in pending_fks:
        if key in tables:
            tables[key]["foreign_keys"].append(fk)

    return {
        "dialect": "mysql",
        "format_version": 1,
        "tables": list(tables.values()),
    }


# ----------------------------
# Output helpers
# ----------------------------

def to_markdown(doc: Dict[str, Any]) -> str:
    lines: List[str] = []
    for t in doc.get("tables", []):
        schema = t.get("schema")
        tname = t.get("name")
        full = f"{schema}.{tname}" if schema else tname
        lines.append(f"## {full}（{t.get('comment_ja') or ''}）".rstrip("（）"))
        lines.append("")
        lines.append("### Columns")
        for c in t.get("columns", []):
            lines.append(f"- `{c['name']}` : {c.get('type','')} / nullable={c.get('nullable')} / ja=`{c.get('comment_ja') or ''}`")
        lines.append("")
        lines.append(f"### Primary Key\n- {t.get('primary_key', [])}")
        lines.append("")
        lines.append("### Foreign Keys")
        if not t.get("foreign_keys"):
            lines.append("- (none)")
        else:
            for fk in t["foreign_keys"]:
                ref = f"{(fk.get('ref_schema') + '.') if fk.get('ref_schema') else ''}{fk.get('ref_table')}"
                lines.append(f"- {fk.get('name') or '(no_name)'} : {fk.get('columns')} -> {ref}{fk.get('ref_columns')}"
                             f" (ON DELETE={fk.get('on_delete')}, ON UPDATE={fk.get('on_update')})")
        lines.append("\n---\n")
    return "\n".join(lines).strip() + "\n"


def read_text_with_fallback(path: str) -> str:
    """Try UTF-8 first, then CP932 (Windows Japanese), then latin-1 as last resort."""
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    # final fallback (replace)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sql_file", help="Input .sql file (MySQL/MariaDB DDL)")
    ap.add_argument("-o", "--out", default="schema.json", help="Output JSON path")
    ap.add_argument("--md", default=None, help="Optional output Markdown path")
    args = ap.parse_args()

    sql_text = read_text_with_fallback(args.sql_file)
    doc = parse_mysql_ddl(sql_text)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    if args.md:
        md = to_markdown(doc)
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(md)

    print(f"[OK] JSON: {args.out}")
    if args.md:
        print(f"[OK] Markdown: {args.md}")


if __name__ == "__main__":
    main()
