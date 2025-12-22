#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DDLファイルからデータベースメタデータを抽出するツール

DDLファイル（CREATE TABLE文）を解析し、テーブル/カラムの定義と
日本語コメントを抽出してJSONファイルを生成する。

対応DDL形式:
- MySQL / MariaDB
- Oracle
- SQL Server (T-SQL)
- PostgreSQL

使用方法:
    python extract_db_metadata.py schema.sql -o db_metadata.json
    python extract_db_metadata.py schema.sql -o db_metadata.json --dialect mysql
    python extract_db_metadata.py schema.sql -o db_metadata.json --md schema_doc.md
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# 低レベルヘルパー（コメント除去 / 文分割 / 括弧マッチング）
# =============================================================================

def remove_sql_comments(text: str) -> str:
    """MySQL形式のコメントを除去: -- <space>, #..., /* ... */ 文字列は保持"""
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

        # コメント開始（クォート内でない場合のみ）
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
                # MySQL: "-- " (空白/制御文字が必要)
                nxt2 = text[i + 2] if i + 2 < len(text) else ""
                if nxt2 in (" ", "\t", "\r", "\n"):
                    in_line_comment = True
                    i += 2
                    continue

        # クォートのトグル
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
    """';' で分割、文字列/バッククォート内は保持"""
    stmts: List[str] = []
    buf: List[str] = []
    i = 0
    in_single = in_double = in_backtick = in_bracket = False

    while i < len(text):
        ch = text[i]

        if ch == "'" and not (in_double or in_backtick or in_bracket):
            if in_single and i > 0 and text[i - 1] == "\\":
                buf.append(ch)
                i += 1
                continue
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue

        if ch == '"' and not (in_single or in_backtick or in_bracket):
            if in_double and i > 0 and text[i - 1] == "\\":
                buf.append(ch)
                i += 1
                continue
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue

        if ch == "`" and not (in_single or in_double or in_bracket):
            in_backtick = not in_backtick
            buf.append(ch)
            i += 1
            continue

        if ch == "[" and not (in_single or in_double or in_backtick):
            in_bracket = True
            buf.append(ch)
            i += 1
            continue

        if ch == "]" and not (in_single or in_double or in_backtick):
            in_bracket = False
            buf.append(ch)
            i += 1
            continue

        if ch == ";" and not (in_single or in_double or in_backtick or in_bracket):
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
    """MySQL シングルクォート文字列のアンクォート（\\', \\\\, '' を処理）"""
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        body = s[1:-1]
    elif len(s) >= 3 and s[0] == "N" and s[1] == "'" and s[-1] == "'":
        body = s[2:-1]
    else:
        body = s
    body = body.replace("\\'", "'").replace("\\\\", "\\")
    body = body.replace("''", "'")
    return body


def find_matching_paren(text: str, start_idx: int) -> int:
    """start_idx の '(' に対応する ')' を見つける（クォートを考慮）"""
    depth = 0
    i = start_idx
    in_single = in_double = in_backtick = in_bracket = False

    while i < len(text):
        ch = text[i]

        if ch == "'" and not (in_double or in_backtick or in_bracket):
            if in_single and i > 0 and text[i - 1] == "\\":
                i += 1
                continue
            in_single = not in_single
            i += 1
            continue

        if ch == '"' and not (in_single or in_backtick or in_bracket):
            if in_double and i > 0 and text[i - 1] == "\\":
                i += 1
                continue
            in_double = not in_double
            i += 1
            continue

        if ch == "`" and not (in_single or in_double or in_bracket):
            in_backtick = not in_backtick
            i += 1
            continue

        if ch == "[" and not (in_single or in_double or in_backtick):
            in_bracket = True
            i += 1
            continue

        if ch == "]" and not (in_single or in_double or in_backtick):
            in_bracket = False
            i += 1
            continue

        if in_single or in_double or in_backtick or in_bracket:
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
    """トップレベルでデリミタ分割（括弧/文字列/バッククォート内は無視）"""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    in_single = in_double = in_backtick = in_bracket = False
    i = 0

    while i < len(text):
        ch = text[i]

        if ch == "'" and not (in_double or in_backtick or in_bracket):
            if in_single and i > 0 and text[i - 1] == "\\":
                buf.append(ch)
                i += 1
                continue
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue

        if ch == '"' and not (in_single or in_backtick or in_bracket):
            if in_double and i > 0 and text[i - 1] == "\\":
                buf.append(ch)
                i += 1
                continue
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue

        if ch == "`" and not (in_single or in_double or in_bracket):
            in_backtick = not in_backtick
            buf.append(ch)
            i += 1
            continue

        if ch == "[" and not (in_single or in_double or in_backtick):
            in_bracket = True
            buf.append(ch)
            i += 1
            continue

        if ch == "]" and not (in_single or in_double or in_backtick):
            in_bracket = False
            buf.append(ch)
            i += 1
            continue

        if not (in_single or in_double or in_backtick or in_bracket):
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
    """カラムリストをパース: `a`,`b` または a, b"""
    cols: List[str] = []
    for c in split_top_level(text, ","):
        c = c.strip()
        if c.startswith("`") and c.endswith("`"):
            c = c[1:-1]
        elif c.startswith('"') and c.endswith('"'):
            c = c[1:-1]
        elif c.startswith("'") and c.endswith("'"):
            c = c[1:-1]
        elif c.startswith("[") and c.endswith("]"):
            c = c[1:-1]
        cols.append(c.strip())
    return cols


def extract_logical_name(comment: Optional[str], physical_name: str) -> str:
    """コメントから論理名を抽出"""
    if comment:
        lines = comment.strip().split('\n')
        first_line = lines[0].strip()
        for sep in [':', '：', '-', '－', '（', '(', '　']:
            if sep in first_line:
                return first_line.split(sep)[0].strip()
        return first_line
    return physical_name


def read_text_with_fallback(path: str) -> str:
    """UTF-8を最初に試し、CP932、latin-1にフォールバック"""
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc-jp", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# =============================================================================
# パーサー（CREATE TABLE / カラム / FK）
# =============================================================================

# MySQL CREATE TABLE 正規表現
CREATE_TABLE_RE_MYSQL = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?:`(?P<schema_bt>[^`]+)`|(?P<schema>[^`.\s]+))\s*\.\s*)?"
    r"(?:`(?P<table_bt>[^`]+)`|(?P<table>[^`(\s]+))\s*\(",
    re.IGNORECASE | re.DOTALL,
)

# Oracle CREATE TABLE 正規表現
CREATE_TABLE_RE_ORACLE = re.compile(
    r"CREATE\s+TABLE\s+"
    r'(?:"?(?P<schema>[^".\s]+)"?\s*\.\s*)?'
    r'"?(?P<table>[^"(\s]+)"?\s*\(',
    re.IGNORECASE | re.DOTALL,
)

# SQL Server CREATE TABLE 正規表現
CREATE_TABLE_RE_SQLSERVER = re.compile(
    r"CREATE\s+TABLE\s+"
    r"(?:\[?(?P<schema>[^\].\s]+)\]?\s*\.\s*)?"
    r"\[?(?P<table>[^\](\s]+)\]?\s*\(",
    re.IGNORECASE | re.DOTALL,
)

# PostgreSQL CREATE TABLE 正規表現
CREATE_TABLE_RE_PGSQL = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?:"?(?P<schema>[^".\s]+)"?\s*\.\s*)?'
    r'"?(?P<table>[^"(\s]+)"?\s*\(',
    re.IGNORECASE | re.DOTALL,
)


def parse_column_def_mysql(part: str) -> Optional[Dict[str, Any]]:
    """MySQL カラム定義をパース"""
    m = re.match(
        r"\s*(?:`(?P<bt>[^`]+)`|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))\s+(?P<rest>.+)$",
        part.strip(),
        re.DOTALL,
    )
    if not m:
        return None

    name = m.group("bt") or m.group("plain")
    rest = m.group("rest").strip()

    # データ型: 最初のトークン (+ UNSIGNED/ZEROFILL)
    tokens = re.split(r"\s+", rest, maxsplit=5)
    col_type = tokens[0]
    if len(tokens) > 1 and tokens[1].upper() in ("UNSIGNED", "ZEROFILL"):
        col_type += " " + tokens[1]

    # 長さ/精度
    length = None
    precision = None
    scale = None
    type_match = re.match(r"(\w+)\s*\(([^)]+)\)", col_type)
    if type_match:
        col_type = type_match.group(1)
        params = type_match.group(2).split(",")
        if params:
            try:
                length = int(params[0].strip())
                precision = length
            except ValueError:
                pass
            if len(params) > 1:
                try:
                    scale = int(params[1].strip())
                except ValueError:
                    pass

    nullable = True
    if re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE):
        nullable = False

    comment_ja = None
    mcom = re.search(r"\bCOMMENT\s+(\'(?:\\.|\'\'|[^\'])*\')", rest, re.IGNORECASE)
    if mcom:
        comment_ja = unquote_mysql_string(mcom.group(1))

    is_pk = bool(re.search(r"\bPRIMARY\s+KEY\b", rest, re.IGNORECASE))

    default_value = None
    mdef = re.search(r"\bDEFAULT\s+(\S+)", rest, re.IGNORECASE)
    if mdef:
        default_value = mdef.group(1)

    return {
        "name": name,
        "logical_name": extract_logical_name(comment_ja, name),
        "data_type": col_type.upper(),
        "length": length,
        "precision": precision,
        "scale": scale,
        "nullable": nullable,
        "is_primary_key": is_pk,
        "is_foreign_key": False,
        "foreign_key_table": None,
        "default_value": default_value,
        "comment": comment_ja,
    }


def parse_column_def_oracle(part: str) -> Optional[Dict[str, Any]]:
    """Oracle カラム定義をパース"""
    m = re.match(
        r'\s*"?(?P<name>[^"\s(]+)"?\s+(?P<rest>.+)$',
        part.strip(),
        re.DOTALL,
    )
    if not m:
        return None

    name = m.group("name")
    rest = m.group("rest").strip()

    # データ型
    type_match = re.match(r"(\w+)(?:\s*\(([^)]+)\))?", rest)
    col_type = type_match.group(1).upper() if type_match else "UNKNOWN"

    length = None
    precision = None
    scale = None
    if type_match and type_match.group(2):
        params = type_match.group(2).split(",")
        if params:
            try:
                length = int(params[0].strip())
                precision = length
            except ValueError:
                pass
            if len(params) > 1:
                try:
                    scale = int(params[1].strip())
                except ValueError:
                    pass

    nullable = not bool(re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE))
    is_pk = bool(re.search(r"\bPRIMARY\s+KEY\b", rest, re.IGNORECASE))

    default_value = None
    mdef = re.search(r"\bDEFAULT\s+(\S+)", rest, re.IGNORECASE)
    if mdef:
        default_value = mdef.group(1)

    return {
        "name": name,
        "logical_name": name,
        "data_type": col_type,
        "length": length,
        "precision": precision,
        "scale": scale,
        "nullable": nullable,
        "is_primary_key": is_pk,
        "is_foreign_key": False,
        "foreign_key_table": None,
        "default_value": default_value,
        "comment": None,
    }


def parse_column_def_sqlserver(part: str) -> Optional[Dict[str, Any]]:
    """SQL Server カラム定義をパース"""
    m = re.match(
        r'\s*\[?(?P<name>[^\]\s(]+)\]?\s+(?P<rest>.+)$',
        part.strip(),
        re.DOTALL,
    )
    if not m:
        return None

    name = m.group("name")
    rest = m.group("rest").strip()

    # データ型
    type_match = re.match(r"\[?(\w+)\]?(?:\s*\(([^)]+)\))?", rest)
    col_type = type_match.group(1).upper() if type_match else "UNKNOWN"

    length = None
    precision = None
    scale = None
    if type_match and type_match.group(2):
        params = type_match.group(2)
        if params.upper() == "MAX":
            length = -1
        else:
            param_list = params.split(",")
            if param_list:
                try:
                    length = int(param_list[0].strip())
                    precision = length
                except ValueError:
                    pass
                if len(param_list) > 1:
                    try:
                        scale = int(param_list[1].strip())
                    except ValueError:
                        pass

    nullable = not bool(re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE))
    is_pk = bool(re.search(r"\bPRIMARY\s+KEY\b", rest, re.IGNORECASE))

    if re.search(r"\bIDENTITY\b", rest, re.IGNORECASE):
        col_type += " IDENTITY"

    default_value = None
    mdef = re.search(r"\bDEFAULT\s+(\S+)", rest, re.IGNORECASE)
    if mdef:
        default_value = mdef.group(1)

    return {
        "name": name,
        "logical_name": name,
        "data_type": col_type,
        "length": length,
        "precision": precision,
        "scale": scale,
        "nullable": nullable,
        "is_primary_key": is_pk,
        "is_foreign_key": False,
        "foreign_key_table": None,
        "default_value": default_value,
        "comment": None,
    }


def parse_column_def_pgsql(part: str) -> Optional[Dict[str, Any]]:
    """PostgreSQL カラム定義をパース"""
    m = re.match(
        r'\s*"?(?P<name>[^"\s(]+)"?\s+(?P<rest>.+)$',
        part.strip(),
        re.DOTALL,
    )
    if not m:
        return None

    name = m.group("name")
    rest = m.group("rest").strip()

    type_match = re.match(r'"?(\w+)"?(?:\s*\(([^)]+)\))?', rest)
    col_type = type_match.group(1).upper() if type_match else "UNKNOWN"

    length = None
    precision = None
    scale = None
    if type_match and type_match.group(2):
        params = type_match.group(2).split(",")
        if params:
            try:
                length = int(params[0].strip())
                precision = length
            except ValueError:
                pass
            if len(params) > 1:
                try:
                    scale = int(params[1].strip())
                except ValueError:
                    pass

    nullable = not bool(re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE))
    is_pk = bool(re.search(r"\bPRIMARY\s+KEY\b", rest, re.IGNORECASE))

    default_value = None
    mdef = re.search(r"\bDEFAULT\s+(\S+)", rest, re.IGNORECASE)
    if mdef:
        default_value = mdef.group(1)

    return {
        "name": name,
        "logical_name": name,
        "data_type": col_type,
        "length": length,
        "precision": precision,
        "scale": scale,
        "nullable": nullable,
        "is_primary_key": is_pk,
        "is_foreign_key": False,
        "foreign_key_table": None,
        "default_value": default_value,
        "comment": None,
    }


def parse_foreign_key(part: str) -> Dict[str, Any]:
    """外部キー定義をパース"""
    fk_name = None
    mname = re.search(r"\bCONSTRAINT\s+[`\"\[]?([^`\"\]\s]+)[`\"\]]?\s+", part, re.IGNORECASE)
    if mname:
        fk_name = mname.group(1)

    mfk = re.search(
        r"FOREIGN\s+KEY\s*\((?P<cols>[^)]+)\)\s*REFERENCES\s+"
        r"(?:(?:[`\"\[]?(?P<ref_schema>[^`\"\].\s]+)[`\"\]]?)\s*\.\s*)?"
        r"[`\"\[]?(?P<ref_table>[^`\"\](\s]+)[`\"\]]?\s*"
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
        ref_table = mfk.group("ref_table")
        ref_schema = mfk.group("ref_schema")

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
        "constraint_name": fk_name,
        "from_columns": cols,
        "to_schema": ref_schema,
        "to_table": ref_table,
        "to_columns": ref_cols,
        "on_delete": on_delete,
        "on_update": on_update,
    }


# =============================================================================
# 各方言のメイン解析関数
# =============================================================================

def parse_create_table_mysql(stmt: str) -> Optional[Dict[str, Any]]:
    """MySQL CREATE TABLE をパース"""
    m = CREATE_TABLE_RE_MYSQL.search(stmt)
    if not m:
        return None

    schema = m.group("schema_bt") or m.group("schema") or ""
    table = m.group("table_bt") or m.group("table")

    open_paren_idx = m.end() - 1
    close_paren_idx = find_matching_paren(stmt, open_paren_idx)
    if close_paren_idx == -1:
        return None

    inner = stmt[open_paren_idx + 1:close_paren_idx]
    tail = stmt[close_paren_idx + 1:]

    # テーブルコメント
    tcomment = None
    mcom = re.search(r"COMMENT\s*=\s*(\'(?:\\.|\'\'|[^\'])*\')", tail, re.IGNORECASE)
    if mcom:
        tcomment = unquote_mysql_string(mcom.group(1))

    table_obj: Dict[str, Any] = {
        "schema_name": schema,
        "table_name": table,
        "logical_name": extract_logical_name(tcomment, table),
        "table_type": "TABLE",
        "comment": tcomment,
        "columns": [],
        "primary_key": [],
        "foreign_keys": [],
    }

    parts = split_top_level(inner, ",")
    inline_pk: List[str] = []

    for part in parts:
        up = part.strip().upper()
        if not part.strip():
            continue

        # PRIMARY KEY
        if up.startswith("PRIMARY KEY"):
            mpk = re.search(r"PRIMARY\s+KEY\s*\((?P<cols>[^)]+)\)", part, re.IGNORECASE | re.DOTALL)
            if mpk:
                table_obj["primary_key"] = parse_identifier_list(mpk.group("cols"))
            continue

        # FOREIGN KEY
        if "FOREIGN KEY" in up:
            fk = parse_foreign_key(part)
            fk["from_table"] = table
            table_obj["foreign_keys"].append(fk)
            continue

        # インデックス（スキップ）
        if up.startswith(("UNIQUE KEY", "UNIQUE INDEX", "KEY", "INDEX", "FULLTEXT", "SPATIAL")):
            continue

        # CONSTRAINT（FK以外）
        if up.startswith("CONSTRAINT") and "FOREIGN KEY" not in up:
            continue

        # カラム定義
        col = parse_column_def_mysql(part)
        if col:
            table_obj["columns"].append(col)
            if col.get("is_primary_key"):
                inline_pk.append(col["name"])

    if not table_obj["primary_key"] and inline_pk:
        table_obj["primary_key"] = inline_pk

    # 主キーフラグを設定
    for col in table_obj["columns"]:
        if col["name"] in table_obj["primary_key"]:
            col["is_primary_key"] = True

    # 外部キーフラグを設定
    for fk in table_obj["foreign_keys"]:
        for col in table_obj["columns"]:
            if col["name"] in fk["from_columns"]:
                col["is_foreign_key"] = True
                col["foreign_key_table"] = fk["to_table"]

    return table_obj


def parse_create_table_oracle(stmt: str) -> Optional[Dict[str, Any]]:
    """Oracle CREATE TABLE をパース"""
    m = CREATE_TABLE_RE_ORACLE.search(stmt)
    if not m:
        return None

    schema = m.group("schema") or ""
    table = m.group("table")

    paren_start = stmt.find("(", m.end() - 1)
    if paren_start == -1:
        return None

    paren_end = find_matching_paren(stmt, paren_start)
    if paren_end == -1:
        return None

    inner = stmt[paren_start + 1:paren_end]

    table_obj: Dict[str, Any] = {
        "schema_name": schema,
        "table_name": table,
        "logical_name": table,
        "table_type": "TABLE",
        "comment": None,
        "columns": [],
        "primary_key": [],
        "foreign_keys": [],
    }

    parts = split_top_level(inner, ",")

    for part in parts:
        up = part.strip().upper()
        if not part.strip():
            continue

        if "PRIMARY KEY" in up:
            mpk = re.search(r"PRIMARY\s+KEY\s*\(([^)]+)\)", part, re.IGNORECASE)
            if mpk:
                table_obj["primary_key"] = parse_identifier_list(mpk.group(1))
            continue

        if "FOREIGN KEY" in up:
            fk = parse_foreign_key(part)
            fk["from_table"] = table
            table_obj["foreign_keys"].append(fk)
            continue

        if up.startswith("CONSTRAINT"):
            continue

        col = parse_column_def_oracle(part)
        if col:
            table_obj["columns"].append(col)

    for col in table_obj["columns"]:
        if col["name"].upper() in [pk.upper() for pk in table_obj["primary_key"]]:
            col["is_primary_key"] = True

    return table_obj


def parse_create_table_sqlserver(stmt: str) -> Optional[Dict[str, Any]]:
    """SQL Server CREATE TABLE をパース"""
    m = CREATE_TABLE_RE_SQLSERVER.search(stmt)
    if not m:
        return None

    schema = m.group("schema") or "dbo"
    table = m.group("table")

    paren_start = stmt.find("(", m.end() - 1)
    if paren_start == -1:
        return None

    paren_end = find_matching_paren(stmt, paren_start)
    if paren_end == -1:
        return None

    inner = stmt[paren_start + 1:paren_end]

    table_obj: Dict[str, Any] = {
        "schema_name": schema,
        "table_name": table,
        "logical_name": table,
        "table_type": "TABLE",
        "comment": None,
        "columns": [],
        "primary_key": [],
        "foreign_keys": [],
    }

    parts = split_top_level(inner, ",")

    for part in parts:
        up = part.strip().upper()
        if not part.strip():
            continue

        if "PRIMARY KEY" in up:
            mpk = re.search(r"PRIMARY\s+KEY[^(]*\(([^)]+)\)", part, re.IGNORECASE)
            if mpk:
                table_obj["primary_key"] = parse_identifier_list(mpk.group(1))
            continue

        if "FOREIGN KEY" in up:
            fk = parse_foreign_key(part)
            fk["from_table"] = table
            table_obj["foreign_keys"].append(fk)
            continue

        if up.startswith("CONSTRAINT"):
            continue

        col = parse_column_def_sqlserver(part)
        if col:
            table_obj["columns"].append(col)

    for col in table_obj["columns"]:
        if col["name"].upper() in [pk.upper() for pk in table_obj["primary_key"]]:
            col["is_primary_key"] = True

    return table_obj


def parse_create_table_pgsql(stmt: str) -> Optional[Dict[str, Any]]:
    """PostgreSQL CREATE TABLE をパース"""
    m = CREATE_TABLE_RE_PGSQL.search(stmt)
    if not m:
        return None

    schema = m.group("schema") or "public"
    table = m.group("table")

    paren_start = stmt.find("(", m.end() - 1)
    if paren_start == -1:
        return None

    paren_end = find_matching_paren(stmt, paren_start)
    if paren_end == -1:
        return None

    inner = stmt[paren_start + 1:paren_end]

    table_obj: Dict[str, Any] = {
        "schema_name": schema,
        "table_name": table,
        "logical_name": table,
        "table_type": "TABLE",
        "comment": None,
        "columns": [],
        "primary_key": [],
        "foreign_keys": [],
    }

    parts = split_top_level(inner, ",")

    for part in parts:
        up = part.strip().upper()
        if not part.strip():
            continue

        if "PRIMARY KEY" in up:
            mpk = re.search(r"PRIMARY\s+KEY\s*\(([^)]+)\)", part, re.IGNORECASE)
            if mpk:
                table_obj["primary_key"] = parse_identifier_list(mpk.group(1))
            continue

        if "FOREIGN KEY" in up:
            fk = parse_foreign_key(part)
            fk["from_table"] = table
            table_obj["foreign_keys"].append(fk)
            continue

        if up.startswith("CONSTRAINT"):
            continue

        col = parse_column_def_pgsql(part)
        if col:
            table_obj["columns"].append(col)

    for col in table_obj["columns"]:
        if col["name"].lower() in [pk.lower() for pk in table_obj["primary_key"]]:
            col["is_primary_key"] = True

    return table_obj


# =============================================================================
# ALTER TABLE / COMMENT ON 解析
# =============================================================================

def parse_alter_foreign_key(stmt: str, dialect: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY をパース"""
    if dialect == "mysql":
        m = re.search(
            r"ALTER\s+TABLE\s+"
            r"(?:(?:`([^`]+)`|([^`.\s]+))\s*\.\s*)?"
            r"(?:`([^`]+)`|([^`\s]+))",
            stmt, re.IGNORECASE
        )
    elif dialect == "sqlserver":
        m = re.search(
            r"ALTER\s+TABLE\s+"
            r"(?:\[?([^\].\s]+)\]?\s*\.\s*)?"
            r"\[?([^\]\s]+)\]?",
            stmt, re.IGNORECASE
        )
    else:  # oracle, postgresql
        m = re.search(
            r'ALTER\s+TABLE\s+(?:ONLY\s+)?'
            r'(?:"?([^".\s]+)"?\s*\.\s*)?'
            r'"?([^"\s]+)"?',
            stmt, re.IGNORECASE
        )

    if not m:
        return None, None

    if dialect == "mysql":
        schema = m.group(1) or m.group(2) or ""
        table = m.group(3) or m.group(4)
    else:
        schema = m.group(1) or ""
        table = m.group(2)

    key = f"{schema}.{table}" if schema else table
    fk = parse_foreign_key(stmt)
    fk["from_table"] = table
    return key, fk


def parse_comment_on_table(stmt: str, dialect: str) -> Optional[Tuple[str, str]]:
    """COMMENT ON TABLE をパース → (table_key, comment)"""
    if dialect == "oracle":
        m = re.search(
            r"COMMENT\s+ON\s+TABLE\s+"
            r'(?:"?([^".\s]+)"?\s*\.\s*)?'
            r'"?([^".\s]+)"?\s+IS\s+'
            r"(\'(?:\'\'|[^\'])*\'|N\'(?:\'\'|[^\'])*\')",
            stmt, re.IGNORECASE
        )
    elif dialect in ("postgresql", "pgsql"):
        m = re.search(
            r"COMMENT\s+ON\s+TABLE\s+"
            r'(?:"?([^".\s]+)"?\s*\.\s*)?'
            r'"?([^".\s]+)"?\s+IS\s+'
            r"(\'(?:\'\'|[^\'])*\'|E\'(?:\\.|[^\'])*\')",
            stmt, re.IGNORECASE
        )
    else:
        return None

    if m:
        schema = m.group(1) or ""
        table = m.group(2)
        comment = unquote_mysql_string(m.group(3))
        key = f"{schema}.{table}" if schema else table
        return key, comment

    return None


def parse_comment_on_column(stmt: str, dialect: str) -> Optional[Tuple[str, str, str]]:
    """COMMENT ON COLUMN をパース → (table_key, column_name, comment)"""
    if dialect == "oracle":
        m = re.search(
            r"COMMENT\s+ON\s+COLUMN\s+"
            r'(?:"?([^".\s]+)"?\s*\.\s*)?'
            r'"?([^".\s]+)"?\s*\.\s*'
            r'"?([^".\s]+)"?\s+IS\s+'
            r"(\'(?:\'\'|[^\'])*\'|N\'(?:\'\'|[^\'])*\')",
            stmt, re.IGNORECASE
        )
    elif dialect in ("postgresql", "pgsql"):
        m = re.search(
            r"COMMENT\s+ON\s+COLUMN\s+"
            r'(?:"?([^".\s]+)"?\s*\.\s*)?'
            r'"?([^".\s]+)"?\s*\.\s*'
            r'"?([^".\s]+)"?\s+IS\s+'
            r"(\'(?:\'\'|[^\'])*\'|E\'(?:\\.|[^\'])*\')",
            stmt, re.IGNORECASE
        )
    else:
        return None

    if m:
        schema = m.group(1) or ""
        table = m.group(2)
        column = m.group(3)
        comment = unquote_mysql_string(m.group(4))
        key = f"{schema}.{table}" if schema else table
        return key, column, comment

    return None


def parse_extended_property(stmt: str) -> Optional[Tuple[str, Optional[str], str]]:
    """SQL Server sp_addextendedproperty をパース → (table_key, column_name or None, description)"""
    if not re.search(r"sp_addextendedproperty", stmt, re.IGNORECASE):
        return None

    if not re.search(r"MS_Description", stmt, re.IGNORECASE):
        return None

    m_value = re.search(r"@value\s*=\s*N?(\'(?:\'\'|[^\'])*\')", stmt, re.IGNORECASE)
    if not m_value:
        return None
    description = unquote_mysql_string(m_value.group(1))

    m_table = re.search(r"@level1name\s*=\s*N?(\'(?:\'\'|[^\'])*\'|\[?[^\],\s]+\]?)", stmt, re.IGNORECASE)
    if not m_table:
        return None
    table_name = unquote_mysql_string(m_table.group(1).strip("[]'\"N"))

    schema_name = "dbo"
    m_schema = re.search(r"@level0name\s*=\s*N?(\'(?:\'\'|[^\'])*\'|\[?[^\],\s]+\]?)", stmt, re.IGNORECASE)
    if m_schema:
        schema_name = unquote_mysql_string(m_schema.group(1).strip("[]'\"N"))

    table_key = f"{schema_name}.{table_name}"

    column_name = None
    m_column = re.search(r"@level2name\s*=\s*N?(\'(?:\'\'|[^\'])*\'|\[?[^\],\s]+\]?)", stmt, re.IGNORECASE)
    if m_column:
        column_name = unquote_mysql_string(m_column.group(1).strip("[]'\"N"))

    return table_key, column_name, description


# =============================================================================
# メイン解析関数
# =============================================================================

def parse_ddl(sql_text: str, dialect: str) -> Dict[str, Any]:
    """DDLを解析してメタデータを抽出"""
    cleaned = remove_sql_comments(sql_text)
    stmts = split_sql_statements(cleaned)

    tables: Dict[str, Dict[str, Any]] = {}
    pending_fks: List[Tuple[str, Dict[str, Any]]] = []
    table_comments: Dict[str, str] = {}
    column_comments: Dict[str, Dict[str, str]] = {}

    # パーサー選択
    parse_create_table = {
        "mysql": parse_create_table_mysql,
        "oracle": parse_create_table_oracle,
        "sqlserver": parse_create_table_sqlserver,
        "postgresql": parse_create_table_pgsql,
        "pgsql": parse_create_table_pgsql,
    }.get(dialect, parse_create_table_mysql)

    for st in stmts:
        # CREATE TABLE
        ct = parse_create_table(st)
        if ct:
            key = f"{ct['schema_name']}.{ct['table_name']}" if ct['schema_name'] else ct['table_name']
            tables[key] = ct
            continue

        # ALTER TABLE ... FOREIGN KEY
        if re.search(r"\bALTER\s+TABLE\b", st, re.IGNORECASE) and \
           re.search(r"\bFOREIGN\s+KEY\b", st, re.IGNORECASE):
            key, fk = parse_alter_foreign_key(st, dialect)
            if key and fk:
                pending_fks.append((key, fk))
            continue

        # COMMENT ON TABLE (Oracle, PostgreSQL)
        if dialect in ("oracle", "postgresql", "pgsql"):
            result = parse_comment_on_table(st, dialect)
            if result:
                key, comment = result
                table_comments[key] = comment
                continue

            result = parse_comment_on_column(st, dialect)
            if result:
                key, column, comment = result
                if key not in column_comments:
                    column_comments[key] = {}
                col_key = column.upper() if dialect == "oracle" else column.lower()
                column_comments[key][col_key] = comment
                continue

        # sp_addextendedproperty (SQL Server)
        if dialect == "sqlserver":
            result = parse_extended_property(st)
            if result:
                key, column, description = result
                if column:
                    if key not in column_comments:
                        column_comments[key] = {}
                    column_comments[key][column.upper()] = description
                else:
                    table_comments[key] = description
                continue

    # ALTER TABLE の FK をテーブルに追加
    for key, fk in pending_fks:
        if key in tables:
            tables[key]["foreign_keys"].append(fk)
            for col in tables[key]["columns"]:
                if col["name"] in fk["from_columns"]:
                    col["is_foreign_key"] = True
                    col["foreign_key_table"] = fk["to_table"]

    # コメントをテーブルに適用
    for key, table in tables.items():
        if key in table_comments:
            table["comment"] = table_comments[key]
            table["logical_name"] = extract_logical_name(table_comments[key], table["table_name"])

        if key in column_comments:
            for col in table["columns"]:
                col_key = col["name"].upper() if dialect in ("oracle", "sqlserver") else col["name"].lower()
                if col_key in column_comments[key]:
                    col["comment"] = column_comments[key][col_key]
                    col["logical_name"] = extract_logical_name(col["comment"], col["name"])

    # 出力フォーマットに変換
    all_fks = []
    for table in tables.values():
        for fk in table.get("foreign_keys", []):
            all_fks.append({
                "constraint_name": fk.get("constraint_name"),
                "from_table": fk.get("from_table"),
                "from_columns": fk.get("from_columns", []),
                "to_table": fk.get("to_table"),
                "to_columns": fk.get("to_columns", []),
            })

    output_tables = []
    for table in tables.values():
        output_tables.append({
            "schema_name": table["schema_name"],
            "table_name": table["table_name"],
            "logical_name": table["logical_name"],
            "table_type": table.get("table_type", "TABLE"),
            "comment": table.get("comment"),
            "columns": table["columns"],
            "row_count": None,
        })

    return {
        "dialect": dialect,
        "database": "",
        "table_count": len(output_tables),
        "column_count": sum(len(t["columns"]) for t in output_tables),
        "tables": output_tables,
        "foreign_keys": all_fks,
    }


# =============================================================================
# 方言自動検出
# =============================================================================

def detect_dialect(sql_text: str) -> str:
    """DDL方言を自動検出"""
    # MySQL特有
    if re.search(r"\bENGINE\s*=\s*(InnoDB|MyISAM)", sql_text, re.IGNORECASE):
        return "mysql"
    if re.search(r"`[^`]+`", sql_text):
        return "mysql"

    # SQL Server特有
    if re.search(r"\[dbo\]", sql_text, re.IGNORECASE):
        return "sqlserver"
    if re.search(r"sp_addextendedproperty", sql_text, re.IGNORECASE):
        return "sqlserver"
    if re.search(r"\bIDENTITY\s*\(\d+\s*,\s*\d+\)", sql_text, re.IGNORECASE):
        return "sqlserver"

    # Oracle特有
    if re.search(r"\bNUMBER\s*\(\d+", sql_text, re.IGNORECASE):
        return "oracle"
    if re.search(r"\bVARCHAR2\b", sql_text, re.IGNORECASE):
        return "oracle"

    # PostgreSQL特有
    if re.search(r"\bSERIAL\b", sql_text, re.IGNORECASE):
        return "postgresql"
    if re.search(r"\bTEXT\b(?!\s+CHARACTER)", sql_text, re.IGNORECASE):
        return "postgresql"

    return "mysql"


# =============================================================================
# 出力ヘルパー
# =============================================================================

def to_markdown(doc: Dict[str, Any]) -> str:
    """Markdown形式で出力"""
    lines: List[str] = []
    lines.append("# データベーススキーマ定義書\n")

    lines.append("## 概要\n")
    lines.append(f"- テーブル数: {doc.get('table_count', 0)}")
    lines.append(f"- カラム総数: {doc.get('column_count', 0)}")
    lines.append(f"- 外部キー数: {len(doc.get('foreign_keys', []))}")
    lines.append("")

    lines.append("## テーブル一覧\n")
    lines.append("| No | テーブル名 | 論理名 | カラム数 | 説明 |")
    lines.append("|---:|------------|--------|----------|------|")
    for i, t in enumerate(doc.get("tables", []), 1):
        desc = (t.get("comment") or "")[:50].replace("\n", " ")
        lines.append(f"| {i} | {t['table_name']} | {t['logical_name']} | {len(t.get('columns', []))} | {desc} |")
    lines.append("")

    lines.append("## テーブル詳細\n")
    for t in doc.get("tables", []):
        full_name = f"{t['schema_name']}.{t['table_name']}" if t.get('schema_name') else t['table_name']
        lines.append(f"### {full_name}")
        if t['logical_name'] != t['table_name']:
            lines.append(f"**論理名**: {t['logical_name']}\n")
        if t.get("comment"):
            lines.append(f"**説明**: {t['comment']}\n")

        lines.append("| カラム名 | 論理名 | データ型 | NULL | PK | FK | 説明 |")
        lines.append("|----------|--------|----------|:----:|:--:|:--:|------|")

        for col in t.get("columns", []):
            nullable = "○" if col.get("nullable", True) else "×"
            pk = "●" if col.get("is_primary_key") else ""
            fk = f"→{col.get('foreign_key_table')}" if col.get("is_foreign_key") else ""
            desc = (col.get("comment") or "")[:30].replace("\n", " ")
            data_type = col.get("data_type", "")
            if col.get("length"):
                data_type += f"({col['length']})"
            lines.append(f"| {col['name']} | {col['logical_name']} | {data_type} | {nullable} | {pk} | {fk} | {desc} |")

        lines.append("")

    if doc.get("foreign_keys"):
        lines.append("## 外部キー一覧\n")
        lines.append("| 制約名 | テーブル | カラム | 参照先テーブル | 参照先カラム |")
        lines.append("|--------|----------|--------|----------------|--------------|")
        for fk in doc["foreign_keys"]:
            lines.append(
                f"| {fk.get('constraint_name') or '-'} | {fk.get('from_table')} | "
                f"{', '.join(fk.get('from_columns', []))} | {fk.get('to_table')} | "
                f"{', '.join(fk.get('to_columns', []))} |"
            )
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# メイン
# =============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="DDLファイルからデータベースメタデータを抽出"
    )
    ap.add_argument("ddl_file", help="入力DDLファイル (.sql)")
    ap.add_argument("-o", "--output", default="db_metadata.json", help="出力JSONファイルパス")
    ap.add_argument("--dialect", choices=["mysql", "oracle", "sqlserver", "postgresql", "auto"],
                    default="auto", help="DDL方言 (auto=自動検出)")
    ap.add_argument("--md", "--markdown", dest="markdown", default=None, help="Markdown出力ファイルパス")
    ap.add_argument("-q", "--quiet", action="store_true", help="サイレントモード")
    args = ap.parse_args()

    sql_text = read_text_with_fallback(args.ddl_file)

    if args.dialect == "auto":
        dialect = detect_dialect(sql_text)
        if not args.quiet:
            print(f"[情報] DDL方言を自動検出: {dialect}")
    else:
        dialect = args.dialect

    if not args.quiet:
        print(f"[情報] DDLファイルを解析中: {args.ddl_file}")

    doc = parse_ddl(sql_text, dialect)
    doc["source_file"] = args.ddl_file

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    if not args.quiet:
        print(f"[情報] メタデータを保存しました: {args.output}")
        print(f"[情報] テーブル数: {doc['table_count']}, カラム数: {doc['column_count']}, 外部キー数: {len(doc['foreign_keys'])}")

    if args.markdown:
        md = to_markdown(doc)
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(md)
        if not args.quiet:
            print(f"[情報] Markdownを保存しました: {args.markdown}")

    if not args.quiet and doc["tables"]:
        print(f"\n【テーブル一覧（先頭10件）】")
        for t in doc["tables"][:10]:
            print(f"  · {t['table_name']} ({t['logical_name']}) - {len(t['columns'])}カラム")
        if len(doc["tables"]) > 10:
            print(f"  ... 他 {len(doc['tables']) - 10} テーブル")


if __name__ == "__main__":
    main()
