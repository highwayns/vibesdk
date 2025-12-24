#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeneXus + WorkWithPlus 生成Javaコード向け解析スクリプト

GeneXus特有の命名規則に対応：
- WebPanel: *wp, *webpanel* → 画面
- Transaction: *trn*, *_bc → 画面（ビジネスコンポーネント）
- WorkWithPlus: *wwp*, *ww_* → 画面
- Procedure: *proc → バッチ
- DataProvider: *dp → バッチ
- Report: *report*, *rpt* → バッチ
- SDT: sdt*, type_* → データ構造（other）

使用方法：
    python parse_genexus.py /path/to/genexus/project -o output.json
"""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set

from tree_sitter import Parser, Language
import tree_sitter_java as tsjava


# ---------- Tree-sitter 初期化 ----------

JAVA_LANGUAGE = Language(tsjava.language())


def create_parser() -> Parser:
    parser = Parser()
    parser.language = JAVA_LANGUAGE
    return parser


# ---------- GeneXus機能タイプ検出設定 ----------

# 画面関連パターン（GeneXus/WorkWithPlus特有）
SCREEN_PATTERNS = {
    "class_name": [
        # WorkWithPlus
        r"(?i).*wwp.*", r"(?i).*wwpaux.*", r"(?i).*ww_.*", r"(?i).*workwith.*",
        # WebPanel
        r"(?i).*webpanel.*", r"(?i).*wp$", r"(?i).*_wp$", r"(?i).*_wp_.*",
        # WebComponent
        r"(?i).*wc$", r"(?i).*_wc$",
        # Transaction
        r"(?i).*trn$", r"(?i).*_trn$", r"(?i).*trn_.*", r"(?i).*transaction.*",
        # Business Component
        r"(?i).*_bc$", r"(?i).*bc_.*",
        # Prompt/Selection
        r"(?i).*prompt.*", r"(?i).*popup.*", r"(?i).*selection.*", r"(?i).*selector.*",
        # Services
        r"(?i).*_services$", r"(?i).*_service$", r"(?i).*servlet.*",
        # Panel/Screen
        r"(?i).*panel.*", r"(?i).*screen.*", r"(?i).*form.*", r"(?i).*view.*",
        # Navigation
        r"(?i).*menu.*", r"(?i).*masterpage.*", r"(?i).*master_page.*",
        r"(?i).*homepage.*", r"(?i).*home$", r"(?i).*_home$",
        r"(?i).*login.*", r"(?i).*logout.*",
        # CRUD Operations (UI)
        r"(?i).*grid.*", r"(?i).*list$", r"(?i).*_list$",
        r"(?i).*detail.*", r"(?i).*edit.*", r"(?i).*entry.*", r"(?i).*input.*",
        r"(?i).*inquiry.*", r"(?i).*inq$", r"(?i).*_inq$",
        r"(?i).*search.*", r"(?i).*srch.*",
    ],
    "package_name": [
        r"(?i).*\.web\.?.*", r"(?i).*\.wwp\.?.*", r"(?i).*\.workwithplus\.?.*",
        r"(?i).*\.ui\.?.*", r"(?i).*\.screen\.?.*", r"(?i).*\.panel\.?.*",
        r"(?i).*\.servlet\.?.*", r"(?i).*\.webpanel\.?.*",
        r"(?i).*\.transaction\.?.*", r"(?i).*\.trn\.?.*",
    ],
}

# バッチ処理パターン（GeneXus特有）
BATCH_PATTERNS = {
    "class_name": [
        # Procedure
        r"(?i).*proc$", r"(?i).*_proc$", r"(?i).*proc_.*", r"(?i).*procedure.*",
        # DataProvider
        r"(?i).*dp$", r"(?i).*_dp$", r"(?i).*dp_.*",
        r"(?i).*dataprovider.*", r"(?i).*data_provider.*",
        # Report
        r"(?i).*report.*", r"(?i).*rpt.*", r"(?i).*pdf.*",
        # Export/Import
        r"(?i).*export.*", r"(?i).*import.*",
        r"(?i).*exp$", r"(?i).*_exp$", r"(?i).*imp$", r"(?i).*_imp$",
        # Batch processing
        r"(?i).*batch.*", r"(?i).*job.*", r"(?i).*task.*",
        r"(?i).*scheduler.*", r"(?i).*cron.*", r"(?i).*timer.*",
        r"(?i).*daemon.*", r"(?i).*worker.*", r"(?i).*processor.*",
        # Data operations
        r"(?i).*generator.*", r"(?i).*loader.*", r"(?i).*extractor.*",
        r"(?i).*migrat.*", r"(?i).*sync.*", r"(?i).*transfer.*",
        # Calculation
        r"(?i).*calc.*", r"(?i).*compute.*", r"(?i).*aggregate.*",
        r"(?i).*summary.*", r"(?i).*consolidat.*", r"(?i).*closing.*",
        # Scheduled
        r"(?i).*nightly.*", r"(?i).*daily.*", r"(?i).*weekly.*", r"(?i).*monthly.*",
    ],
    "package_name": [
        r"(?i).*\.proc\.?.*", r"(?i).*\.procedure\.?.*", r"(?i).*\.batch\.?.*",
        r"(?i).*\.job\.?.*", r"(?i).*\.report\.?.*", r"(?i).*\.dp\.?.*",
        r"(?i).*\.dataprovider\.?.*", r"(?i).*\.export\.?.*",
        r"(?i).*\.import\.?.*", r"(?i).*\.task\.?.*", r"(?i).*\.scheduler\.?.*",
    ],
}

# GeneXusフレームワーク除外パターン
GENEXUS_FRAMEWORK_PATTERNS = [
    r"(?i)^gx[A-Z_].*", r"(?i)^GX[A-Z_].*",
    r"(?i).*_gxui.*", r"(?i).*_gxcommon.*", r"(?i).*gxwebsocket.*",
    r"(?i)^com\.genexus\..*", r"(?i)^com\.artech\..*",
]

# SDT（データ構造）パターン
SDT_PATTERNS = [
    r"(?i)^sdt.*", r"(?i).*_sdt$", r"(?i)^type_.*", r"(?i)^struct.*",
]


def compile_patterns(patterns: Dict[str, List[str]]) -> Dict[str, List[re.Pattern]]:
    """文字列パターンを正規表現オブジェクトにコンパイル"""
    compiled = {}
    for key, pattern_list in patterns.items():
        compiled[key] = [re.compile(p) for p in pattern_list]
    return compiled


COMPILED_SCREEN_PATTERNS = compile_patterns(SCREEN_PATTERNS)
COMPILED_BATCH_PATTERNS = compile_patterns(BATCH_PATTERNS)
COMPILED_FRAMEWORK_PATTERNS = [re.compile(p) for p in GENEXUS_FRAMEWORK_PATTERNS]
COMPILED_SDT_PATTERNS = [re.compile(p) for p in SDT_PATTERNS]


# ---------- ユーティリティ関数 ----------

def read_file_bytes(path: Path) -> bytes:
    return path.read_bytes()


def node_text(source_code: bytes, node) -> str:
    return source_code[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def node_line_range(node) -> Dict[str, int]:
    start_row, _ = node.start_point
    end_row, _ = node.end_point
    return {"start_line": start_row + 1, "end_line": end_row + 1}

# ---------- Step1最適化: メソッドコードの軽量化 ----------

_SQL_KEYWORDS_RE = re.compile(r"(?i)\b(select|from|join|insert|update|delete)\b")
_DB_HINT_LINE_RE = re.compile(r"(?i)(\bselect\b|\bfrom\b|\bjoin\b|\binsert\b|\bupdate\b|\bdelete\b|_bc\b|\.load\b|\.save\b|\.delete\b|cursor|cursordef)")


def _collect_string_literals(node, source_code: bytes, out: list[str]):
    """Collect string literals under a node (tree-sitter-java: string_literal)."""
    try:
        if node.type == 'string_literal':
            out.append(node_text(source_code, node))
    except Exception:
        pass
    for ch in getattr(node, 'children', []) or []:
        _collect_string_literals(ch, source_code, out)


def _normalize_string_literal(lit: str) -> str:
    """Normalize a tree-sitter string_literal to a compact plain string."""
    t = lit.strip()
    if len(t) >= 2 and ((t[0] == '"' and t[-1] == '"') or (t[0] == "'" and t[-1] == "'")):
        t = t[1:-1]
    # Best-effort unescape / whitespace normalize
    t = t.replace("\\\"", "\"").replace("\\'", "'")
    t = t.replace("\\n", " ").replace("\\t", " ")
    t = t.replace("\n", " ").replace("\t", " ")
    return " ".join(t.split())


def _count_formal_parameters(method_node) -> int:
    """Count formal parameters in a method_declaration node (best-effort)."""
    try:
        for ch in getattr(method_node, 'children', []) or []:
            if ch.type == 'formal_parameters':
                return len([c for c in getattr(ch, 'named_children', []) or [] if c.type == 'formal_parameter'])
    except Exception:
        pass
    return 0


def _collect_method_invocations(node, source_code: bytes, out: list[dict]):
    """Collect method invocation info from a node subtree.

    We rely on tree-sitter fields when available, and fall back to text parsing.
    Output items:
      {"name": str, "qualifier": str|None, "arg_count": int, "line": int, "text": str}
    """
    try:
        if node.type == 'method_invocation':
            name = None
            qualifier = None
            arg_count = -1

            # Field-based extraction (preferred)
            try:
                name_node = node.child_by_field_name('name')
                if name_node is not None:
                    name = node_text(source_code, name_node)
                obj_node = node.child_by_field_name('object')
                if obj_node is not None:
                    qualifier = node_text(source_code, obj_node)
                args_node = node.child_by_field_name('arguments')
                if args_node is not None:
                    # named_children are expressions (commas/parens excluded)
                    arg_count = len(getattr(args_node, 'named_children', []) or [])
            except Exception:
                pass

            # Text fallback
            text = node_text(source_code, node)
            if not name:
                prefix = text.split('(', 1)[0].strip()
                if '.' in prefix:
                    qualifier, name = prefix.rsplit('.', 1)
                    qualifier = qualifier.strip()
                    name = name.strip()
                else:
                    name = prefix
            if arg_count < 0:
                # best-effort count commas at top-level is hard; use simple heuristic
                inside = ''
                if '(' in text and ')' in text:
                    inside = text.split('(', 1)[1].rsplit(')', 1)[0].strip()
                if not inside:
                    arg_count = 0
                else:
                    arg_count = inside.count(',') + 1

            if name:
                start_row, _ = node.start_point
                out.append({
                    'name': name,
                    'qualifier': qualifier,
                    'arg_count': int(arg_count),
                    'line': int(start_row + 1),
                    'text': text[:200],
                })
    except Exception:
        pass

    for ch in getattr(node, 'children', []) or []:
        _collect_method_invocations(ch, source_code, out)

def _build_method_excerpt(method_text: str, max_lines: int = 80, window: int = 2) -> tuple[str, bool, bool]:
    """Build a compact excerpt: signature + lines around DB hints.

    Returns: (excerpt, has_hints, truncated)
    """
    lines = method_text.splitlines()
    if not lines:
        return '', False, False

    # signature line (first non-empty)
    sig_idx = 0
    for i, ln in enumerate(lines):
        if ln.strip():
            sig_idx = i
            break

    hint_idxs = [i for i, ln in enumerate(lines) if _DB_HINT_LINE_RE.search(ln)]
    has_hints = len(hint_idxs) > 0

    keep = set([sig_idx])
    for i in hint_idxs:
        for j in range(max(sig_idx, i - window), min(len(lines), i + window + 1)):
            keep.add(j)

    kept_lines = [lines[i].rstrip() for i in sorted(keep)]
    # Hard limits
    truncated = False
    if len(kept_lines) > max_lines:
        kept_lines = kept_lines[:max_lines]
        truncated = True

    excerpt = '\n'.join(kept_lines).strip()
    return excerpt, has_hints, truncated



def find_enclosing_type_name(node, source_code: bytes) -> Optional[str]:
    current = node
    while current is not None:
        if current.type in ("class_declaration", "interface_declaration", "enum_declaration"):
            for child in current.children:
                if child.type == "identifier":
                    return node_text(source_code, child)
        current = current.parent
    return None


def infer_package_name(source_code: bytes, root_node) -> str:
    for child in root_node.children:
        if child.type == "package_declaration":
            for c in child.children:
                if c.type in ("scoped_identifier", "identifier"):
                    return node_text(source_code, c)
    return ""


def extract_annotations(node, source_code: bytes) -> List[str]:
    annotations = []
    for child in node.children:
        if child.type == "modifiers":
            for mod_child in child.children:
                if mod_child.type in ("marker_annotation", "annotation"):
                    for ac in mod_child.children:
                        if ac.type == "identifier":
                            annotations.append(node_text(source_code, ac))
    return annotations


def is_genexus_framework_class(class_name: str, package_name: str) -> bool:
    """GeneXusフレームワーククラスかどうかを判定"""
    full_name = f"{package_name}.{class_name}" if package_name else class_name
    for pattern in COMPILED_FRAMEWORK_PATTERNS:
        if pattern.match(class_name) or pattern.match(full_name):
            return True
    return False


def is_sdt_class(class_name: str) -> bool:
    """SDT（Structured Data Type）クラスかどうかを判定"""
    for pattern in COMPILED_SDT_PATTERNS:
        if pattern.match(class_name):
            return True
    return False


def detect_genexus_object_type(class_name: str) -> Optional[str]:
    """GeneXusオブジェクトタイプを検出"""
    cl = class_name.lower()
    
    if cl.endswith("wp") or "_wp" in cl or cl.endswith("_wp"):
        return "WebPanel"
    if cl.endswith("wc") or "_wc" in cl:
        return "WebComponent"
    if cl.endswith("trn") or "_trn" in cl or "trn_" in cl:
        return "Transaction"
    if cl.endswith("_bc") or "bc_" in cl:
        return "BusinessComponent"
    if cl.endswith("proc") or "_proc" in cl or "proc_" in cl:
        return "Procedure"
    if cl.endswith("dp") or "_dp" in cl or "dp_" in cl:
        return "DataProvider"
    if "wwp" in cl or "workwith" in cl:
        return "WorkWithPlus"
    if "report" in cl or "rpt" in cl:
        return "Report"
    if cl.startswith("sdt") or cl.startswith("type_"):
        return "SDT"
    if "_services" in cl or cl.endswith("_service"):
        return "WebService"
    if "prompt" in cl or "selection" in cl:
        return "Prompt"
    return None


def check_pattern_match(class_name: str, package_name: str, 
                        compiled_patterns: Dict[str, List[re.Pattern]]) -> Tuple[bool, List[str]]:
    matches = []
    for pattern in compiled_patterns.get("class_name", []):
        if pattern.match(class_name):
            matches.append(f"class_name:{class_name}")
            break
    for pattern in compiled_patterns.get("package_name", []):
        if pattern.match(package_name):
            matches.append(f"package:{package_name}")
            break
    return len(matches) > 0, matches


def detect_function_type(class_name: str, package_name: str, 
                         annotations: List[str]) -> Tuple[str, List[str], Optional[str]]:
    """機能タイプを検出。戻り値: (機能タイプ, 理由リスト, GXオブジェクトタイプ)"""
    
    if is_genexus_framework_class(class_name, package_name):
        return "framework", ["genexus_framework"], "Framework"
    
    if is_sdt_class(class_name):
        return "other", ["sdt_data_structure"], "SDT"
    
    gx_type = detect_genexus_object_type(class_name)
    
    # GeneXusオブジェクトタイプによる判定
    if gx_type in ("WebPanel", "WebComponent", "Transaction", "BusinessComponent", 
                   "WorkWithPlus", "WebService", "Prompt"):
        return "screen", [f"genexus_type:{gx_type}"], gx_type
    
    if gx_type in ("Procedure", "DataProvider", "Report"):
        return "batch", [f"genexus_type:{gx_type}"], gx_type
    
    # パターンマッチング
    is_batch, batch_reasons = check_pattern_match(class_name, package_name, COMPILED_BATCH_PATTERNS)
    is_screen, screen_reasons = check_pattern_match(class_name, package_name, COMPILED_SCREEN_PATTERNS)
    
    if is_batch and is_screen:
        cl = class_name.lower()
        if "proc" in cl or "dp" in cl:
            return "batch", batch_reasons, gx_type
        return "screen", screen_reasons, gx_type
    elif is_batch:
        return "batch", batch_reasons, gx_type
    elif is_screen:
        return "screen", screen_reasons, gx_type
    
    return "other", [], gx_type


# ---------- 依存関係抽出 ----------

def extract_imports(root_node, source_code: bytes) -> List[Dict[str, str]]:
    imports = []
    for child in root_node.children:
        if child.type == "import_declaration":
            import_text = node_text(source_code, child).strip()
            is_static = "static" in import_text
            for c in child.children:
                if c.type in ("scoped_identifier", "identifier"):
                    full_path = node_text(source_code, c)
                    parts = full_path.split(".")
                    class_name = parts[-1] if parts else full_path
                    package_path = ".".join(parts[:-1]) if len(parts) > 1 else ""
                    imports.append({
                        "full_path": full_path,
                        "class_name": class_name,
                        "package": package_path,
                        "is_static": is_static,
                    })
                    break
    return imports


def extract_type_identifier(node, source_code: bytes) -> Optional[str]:
    if node is None:
        return None
    if node.type == "type_identifier":
        return node_text(source_code, node)
    elif node.type == "generic_type":
        for child in node.children:
            if child.type == "type_identifier":
                return node_text(source_code, child)
    elif node.type == "scoped_type_identifier":
        text = node_text(source_code, node)
        return text.split(".")[-1] if "." in text else text
    elif node.type == "array_type":
        for child in node.children:
            result = extract_type_identifier(child, source_code)
            if result:
                return result
    return None


def extract_all_type_references(node, source_code: bytes, collected: Set[str]):
    SKIP_TYPES = {"void", "int", "long", "double", "float", "boolean", "byte", "char", "short",
                  "String", "Object", "Class", "Integer", "Long", "Double", "Float", "Boolean",
                  "List", "Map", "Set", "Collection", "Optional", "Date", "Timestamp", 
                  "BigDecimal", "UUID", "Vector", "Hashtable", "ArrayList", "HashMap"}
    
    if node.type == "type_identifier":
        type_name = node_text(source_code, node)
        if type_name not in SKIP_TYPES:
            collected.add(type_name)
    elif node.type == "object_creation_expression":
        for child in node.children:
            if child.type == "type_identifier":
                collected.add(node_text(source_code, child))
            elif child.type == "scoped_type_identifier":
                text = node_text(source_code, child)
                collected.add(text.split(".")[-1])
    elif node.type == "method_invocation":
        for child in node.children:
            if child.type == "identifier":
                name = node_text(source_code, child)
                if name and len(name) > 1 and name[0].isupper():
                    collected.add(name)
                break
    elif node.type == "field_access":
        for child in node.children:
            if child.type == "identifier":
                name = node_text(source_code, child)
                if name and len(name) > 1 and name[0].isupper():
                    collected.add(name)
                break
    
    for child in node.children:
        extract_all_type_references(child, source_code, collected)


def extract_class_dependencies(class_node, source_code: bytes) -> Dict[str, Any]:
    dependencies = {
        "field_types": [], "method_params": [], "method_returns": [],
        "type_references": [], "extends": None, "implements": [],
    }
    all_refs: Set[str] = set()
    
    def walk_class(node):
        if node.type == "field_declaration":
            for child in node.children:
                type_name = extract_type_identifier(child, source_code)
                if type_name:
                    dependencies["field_types"].append(type_name)
                    all_refs.add(type_name)
        
        elif node.type == "method_declaration":
            for child in node.children:
                type_name = extract_type_identifier(child, source_code)
                if type_name:
                    dependencies["method_returns"].append(type_name)
                    all_refs.add(type_name)
                if child.type == "formal_parameters":
                    for param in child.children:
                        if param.type == "formal_parameter":
                            for pc in param.children:
                                ptype = extract_type_identifier(pc, source_code)
                                if ptype:
                                    dependencies["method_params"].append(ptype)
                                    all_refs.add(ptype)
                if child.type == "block":
                    extract_all_type_references(child, source_code, all_refs)
        
        elif node.type == "constructor_declaration":
            for child in node.children:
                if child.type == "formal_parameters":
                    for param in child.children:
                        if param.type == "formal_parameter":
                            for pc in param.children:
                                ptype = extract_type_identifier(pc, source_code)
                                if ptype:
                                    dependencies["method_params"].append(ptype)
                                    all_refs.add(ptype)
                if child.type == "constructor_body":
                    extract_all_type_references(child, source_code, all_refs)
        
        elif node.type == "superclass":
            for child in node.children:
                type_name = extract_type_identifier(child, source_code)
                if type_name:
                    dependencies["extends"] = type_name
                    all_refs.add(type_name)
        
        elif node.type == "super_interfaces":
            for child in node.children:
                if child.type == "type_list":
                    for tc in child.children:
                        type_name = extract_type_identifier(tc, source_code)
                        if type_name:
                            dependencies["implements"].append(type_name)
                            all_refs.add(type_name)
        
        for child in node.children:
            walk_class(child)
    
    walk_class(class_node)
    
    dependencies["field_types"] = list(set(dependencies["field_types"]))
    dependencies["method_params"] = list(set(dependencies["method_params"]))
    dependencies["method_returns"] = list(set(dependencies["method_returns"]))
    dependencies["type_references"] = list(all_refs)
    
    return dependencies


# ---------- コア解析ロジック ----------

def extract_from_file(path: Path, parser: Parser, method_filter: str = 'calls_or_sql', method_code_mode: str = 'excerpt', excerpt_max_lines: int = 80, excerpt_window: int = 2, sql_string_max: int = 20, sql_string_max_len: int = 500) -> Dict[str, Any]:
    source_code = read_file_bytes(path)
    tree = parser.parse(source_code)
    root = tree.root_node

    package_name = infer_package_name(source_code, root)
    imports = extract_imports(root, source_code)

    classes: Dict[int, Dict[str, Any]] = {}

    def walk_collect_classes(node):
        if node.type in ("class_declaration", "interface_declaration", "enum_declaration"):
            kind = {"class_declaration": "class", "interface_declaration": "interface",
                    "enum_declaration": "enum"}[node.type]

            class_name = None
            for child in node.children:
                if child.type == "identifier":
                    class_name = node_text(source_code, child)
                    break

            if class_name is None:
                class_name = "<anonymous>"

            annotations = extract_annotations(node, source_code)
            func_type, func_type_reasons, gx_type = detect_function_type(
                class_name, package_name, annotations
            )
            dependencies = extract_class_dependencies(node, source_code)

            info = {
                "name": class_name,
                "full_name": f"{package_name}.{class_name}" if package_name else class_name,
                "kind": kind,
                "package": package_name,
                "annotations": annotations,
                "function_type": func_type,
                "function_type_reasons": func_type_reasons,
                "genexus_type": gx_type,
                "dependencies": dependencies,
                **node_line_range(node),
                "methods": [],
            }
            classes[id(node)] = {"node": node, "info": info}

        for child in node.children:
            walk_collect_classes(child)

    walk_collect_classes(root)

    def walk_collect_methods(node):
        if node.type == "method_declaration":
            method_name = None
            for child in node.children:
                if child.type == "identifier":
                    method_name = node_text(source_code, child)
                    break

            if method_name is None:
                method_name = "<anonymous>"

            method_annotations = extract_annotations(node, source_code)
            method_text = node_text(source_code, node)

            # Collect static call info from AST (used by Step3 to build a call graph)
            calls: list[dict] = []
            _collect_method_invocations(node, source_code, calls)
            # Remove obvious self entries (the method declaration itself is not an invocation)
            # and keep a reasonable cap to avoid huge JSONs.
            if len(calls) > 500:
                calls = calls[:500]

            param_count = _count_formal_parameters(node)
            # Extract SQL-like string literals for downstream table mapping (compact)
            raw_strings: list[str] = []
            _collect_string_literals(node, source_code, raw_strings)
            sql_strings: list[str] = []
            seen = set()
            for lit in raw_strings:
                norm = _normalize_string_literal(lit)
                if not norm:
                    continue
                if _SQL_KEYWORDS_RE.search(norm):
                    if norm not in seen:
                        seen.add(norm)
                        sql_strings.append(norm[:sql_string_max_len])
                if len(sql_strings) >= sql_string_max:
                    break

            # --- Method filter: keep only methods that have calls or SQL (and optionally DB-hint patterns)
            keep_method = True
            if method_filter == 'all':
                keep_method = True
            elif method_filter == 'calls_or_sql':
                # Add has_db_hints judgment while scanning (cursor/BC ops etc.)
                has_line_hints = bool(_DB_HINT_LINE_RE.search(method_text))
                keep_method = (len(calls) > 0) or (len(sql_strings) > 0) or has_line_hints
            elif method_filter == 'calls_or_sql_or_hints':
                # DB hint: cursor / BC load/save/delete / SQL keywords in text lines
                has_line_hints = bool(_DB_HINT_LINE_RE.search(method_text))
                keep_method = (len(calls) > 0) or (len(sql_strings) > 0) or has_line_hints

            if not keep_method:
                # Skip heavy excerpt generation for irrelevant methods
                return

            if method_code_mode == 'full':
                code_out = method_text
                has_hints = True
                truncated = False
            elif method_code_mode == 'none':
                code_out = ''
                has_hints = len(sql_strings) > 0
                truncated = False
            else:
                code_out, has_hints, truncated = _build_method_excerpt(method_text, max_lines=excerpt_max_lines, window=excerpt_window)

            signature = ''
            for ln in method_text.splitlines():
                if ln.strip():
                    signature = ln.strip()
                    break

            method_info = {
                "name": method_name,
                "annotations": method_annotations,
                **node_line_range(node),
                # NOTE: downstream expects `code`; in excerpt mode this is compact
                "code": code_out,
                "code_mode": method_code_mode,
                "code_truncated": truncated,
                "has_db_hints": has_hints,
                "signature": signature,
                "sql_strings": sql_strings,
                "param_count": param_count,
                "calls": calls,
            }

            enclosing_name = find_enclosing_type_name(node, source_code)
            if enclosing_name is not None:
                for c in classes.values():
                    if c["info"]["name"] == enclosing_name:
                        c["info"]["methods"].append(method_info)
                        break

        for child in node.children:
            walk_collect_methods(child)

    walk_collect_methods(root)

    class_list = [c["info"] for c in classes.values()]

    # ファイルレベルの機能タイプ
    file_function_type = "other"
    if class_list:
        type_counts = {"screen": 0, "batch": 0, "other": 0, "framework": 0}
        for cls in class_list:
            ft = cls.get("function_type", "other")
            if ft in type_counts:
                type_counts[ft] += 1
        
        # frameworkを除いて判定
        if type_counts["screen"] > 0 or type_counts["batch"] > 0:
            if type_counts["screen"] >= type_counts["batch"]:
                file_function_type = "screen"
            else:
                file_function_type = "batch"

    return {
        "file": str(path),
        "package": package_name,
        "imports": imports,
        "function_type": file_function_type,
        "classes": class_list,
    }


def scan_project(root_dir: Path, exclude_framework: bool = True, method_filter: str = 'calls_or_sql', method_code_mode: str = 'excerpt', excerpt_max_lines: int = 80, excerpt_window: int = 2, sql_string_max: int = 20, sql_string_max_len: int = 500) -> List[Dict[str, Any]]:
    parser = create_parser()
    results: List[Dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirpath_p = Path(dirpath)
        for filename in filenames:
            if filename.endswith(".java"):
                file_path = dirpath_p / filename
                try:
                    file_result = extract_from_file(file_path, parser, method_filter=method_filter, method_code_mode=method_code_mode, excerpt_max_lines=excerpt_max_lines, excerpt_window=excerpt_window, sql_string_max=sql_string_max, sql_string_max_len=sql_string_max_len)
                    
                    # フレームワーククラスのみのファイルを除外
                    if exclude_framework:
                        non_fw_classes = [c for c in file_result.get("classes", []) 
                                         if c.get("function_type") != "framework"]
                        if not non_fw_classes:
                            continue
                    
                    results.append(file_result)
                except Exception as e:
                    print(f"[警告] 解析失敗 {file_path}: {e}")

    return results


# ---------- CLIエントリポイント ----------

def main():
    parser = argparse.ArgumentParser(
        description="GeneXus+WorkWithPlus生成Javaコードを解析し、機能タイプを検出してJSONに出力"
    )
    parser.add_argument("project_root", type=str, help="Javaプロジェクトのルートディレクトリ")
    parser.add_argument("-o", "--output", type=str, default="java_structure.json",
                        help="出力JSONファイルパス")
    parser.add_argument("--include-framework", action="store_true",
                        help="GeneXusフレームワーククラスも含める")

    parser.add_argument("--method-code", choices=["excerpt", "full", "none"], default="excerpt",
                        help="メソッド本文の出力モード: excerpt(既定・軽量)/full(従来互換)/none(本文なし)")

    parser.add_argument("--method-filter", choices=["all", "calls_or_sql", "calls_or_sql_or_hints"], default="calls_or_sql",
                        help="メソッド選別: all=全メソッド / calls_or_sql=呼び出しorSQL文字列のみ / calls_or_sql_or_hints=呼び出しorSQLorDBヒント")
    parser.add_argument("--excerpt-max-lines", type=int, default=80,
                        help="excerptモード時の最大行数")
    parser.add_argument("--excerpt-window", type=int, default=2,
                        help="DBヒント行の前後に含める行数")
    parser.add_argument("--sql-string-max", type=int, default=20,
                        help="SQL文字列（string literal）の最大保持数")
    parser.add_argument("--sql-string-max-len", type=int, default=500,
                        help="SQL文字列1件あたりの最大長")

    args = parser.parse_args()
    root_dir = Path(args.project_root).resolve()
    output_path = Path(args.output).resolve()

    if not root_dir.exists():
        raise SystemExit(f"プロジェクトディレクトリが存在しません: {root_dir}")

    print(f"[情報] プロジェクトをスキャン中: {root_dir}")
    results = scan_project(root_dir, exclude_framework=not args.include_framework, method_filter=args.method_filter, method_code_mode=args.method_code, excerpt_max_lines=args.excerpt_max_lines, excerpt_window=args.excerpt_window, sql_string_max=args.sql_string_max, sql_string_max_len=args.sql_string_max_len)

    # 統計
    stats = {"screen": 0, "batch": 0, "other": 0, "framework": 0}
    gx_type_stats = {}
    for r in results:
        for cls in r.get("classes", []):
            ft = cls.get("function_type", "other")
            if ft in stats:
                stats[ft] += 1
            gx_t = cls.get("genexus_type")
            if gx_t:
                gx_type_stats[gx_t] = gx_type_stats.get(gx_t, 0) + 1

    data = {
        "project_root": str(root_dir),
        "file_count": len(results),
        "function_type_stats": stats,
        "genexus_type_stats": gx_type_stats,
        "files": results,
    }

    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    print(f"[情報] 完了。JSONを保存しました: {output_path}")
    print(f"[情報] 機能タイプ統計: 画面={stats['screen']}, バッチ={stats['batch']}, その他={stats['other']}")
    print(f"[情報] GeneXusタイプ統計: {gx_type_stats}")


if __name__ == "__main__":
    main()