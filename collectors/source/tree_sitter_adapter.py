"""Tree-sitter 适配器：多语言 AST 提取函数/调用/依赖线索/配置读取。

输出结构化 Source Facts（函数实体、调用关系、Evidence），
不做跨文件数据流分析（方案 §2.1 的边界约束）。
"""
from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Language, Parser

from common.config import Config, git_commit
from common.models import Evidence
from collectors.source.semgrep_adapter import service_of_path

LANG_PACKS = {}
for _name, _mod in [("python", "tree_sitter_python"), ("go", "tree_sitter_go"),
                    ("java", "tree_sitter_java"), ("javascript", "tree_sitter_javascript"),
                    ("csharp", "tree_sitter_c_sharp")]:
    try:
        _m = __import__(_mod)
        LANG_PACKS[_name] = Language(_m.language())
    except Exception:
        pass

EXT_LANG = {".py": "python", ".go": "go", ".java": "java", ".js": "javascript",
            ".ts": "javascript", ".cs": "csharp"}

FUNC_QUERIES = {
    "python": "(function_definition name: (identifier) @func)",
    "go": "(function_declaration name: (identifier) @func) (method_declaration name: (field_identifier) @func)",
    "java": "(method_declaration name: (identifier) @func) (constructor_declaration name: (identifier) @func)",
    "javascript": "(function_declaration name: (identifier) @func) (method_definition name: (property_identifier) @func)",
    "csharp": "(method_declaration name: (identifier) @func)",
}
CALL_NODE_TYPES = {
    "python": {"call"}, "go": {"call_expression"}, "java": {"method_invocation"},
    "javascript": {"call_expression"}, "csharp": {"invocation_expression"},
}
FUNC_NODE_TYPES = {
    "python": {"function_definition"}, "go": {"function_declaration", "method_declaration"},
    "java": {"method_declaration", "constructor_declaration"},
    "javascript": {"function_declaration", "method_definition", "function_expression", "arrow_function"},
    "csharp": {"method_declaration"},
}
SKIP_DIRS = {"genproto", "node_modules", "tests", "test", ".git", "build", "dist",
             "gradle", "obj", "bin", "generated", "pb"}

ADDR_RE = re.compile(r"\b([a-z][a-z0-9-]*(?:service|cart)):(\d{2,5})\b")
ENV_RE = re.compile(
    r"""os\.Getenv\(\s*"([A-Z0-9_]+)"|os\.environ(?:\.get)?[\(\[]\s*["']([A-Z0-9_]+)["']"""
    r"""|process\.env\.([A-Z0-9_]+)|Environment\.GetEnvironmentVariable\(\s*"([A-Z0-9_]+)" """
    r"""|System\.getenv\(\s*"([A-Z0-9_]+)" """, re.X)


def _make_query(lang: Language, pattern: str):
    try:
        from tree_sitter import Query
        return Query(lang, pattern)
    except (ImportError, TypeError):
        return lang.query(pattern)


def _captures(query, node):
    try:
        from tree_sitter import QueryCursor
        return QueryCursor(query).captures(node)
    except ImportError:
        return query.captures(node)


def _node_name(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode(errors="replace")


def _walk_calls(node, lang_name, src, current_func, out, depth=0):
    if node.type in FUNC_NODE_TYPES[lang_name]:
        n = node.child_by_field_name("name")
        if n is not None:
            current_func = _node_name(n, src)
    if node.type in CALL_NODE_TYPES[lang_name]:
        f = node.child_by_field_name("function") or node.child_by_field_name("name")
        if f is not None:
            callee = _node_name(f, src).replace("\n", " ")[:120]
            out.append((current_func or "<module>", callee, node.start_point[0] + 1))
    for ch in node.children:
        _walk_calls(ch, lang_name, src, current_func, out, depth + 1)


def collect(cfg: Config, profile: dict) -> dict:
    repo = Path(cfg.source.repo_path)
    src_root = repo / profile.get("source_root", "src")
    commit = cfg.source.get("commit") or git_commit(str(repo))
    functions, calls, evidence = [], [], []
    seen_calls = set()
    parsers = {l: Parser(lang) for l, lang in LANG_PACKS.items()}
    queries = {l: _make_query(lang, FUNC_QUERIES[l]) for l, lang in LANG_PACKS.items()}

    for f in sorted(src_root.rglob("*")):
        if not f.is_file() or f.suffix not in EXT_LANG:
            continue
        if any(p in SKIP_DIRS for p in f.parts):
            continue
        rel = str(f.relative_to(repo))
        svc = service_of_path(rel, profile)
        if svc is None:
            continue
        lang_name = EXT_LANG[f.suffix]
        if lang_name not in parsers:
            continue
        try:
            text = f.read_bytes()
            tree = parsers[lang_name].parse(text)
        except Exception:
            continue

        for cap, nodes in _captures(queries[lang_name], tree.root_node).items():
            for n in (nodes if isinstance(nodes, list) else [nodes]):
                functions.append({"service": svc, "name": _node_name(n, text),
                                  "file": rel, "line": n.start_point[0] + 1,
                                  "language": lang_name})

        raw_calls = []
        _walk_calls(tree.root_node, lang_name, text, None, raw_calls)
        for caller, callee, line in raw_calls:
            key = (svc, caller, callee)
            if key in seen_calls:
                continue
            seen_calls.add(key)
            calls.append({"service": svc, "caller": caller, "callee": callee,
                          "file": rel, "line": line})

        body = text.decode(errors="replace")
        for m in ADDR_RE.finditer(body):
            line = body[:m.start()].count("\n") + 1
            evidence.append(Evidence(
                kind="service_dependency_hint", layer="source", subject=svc,
                attrs={"downstream_hint": m.group(1), "port": int(m.group(2)),
                       "note": "static address literal, needs runtime confirmation"},
                source="tree_sitter", method="address-literal extraction",
                location=f"{rel}:{line}", system_id=cfg.system_id, version=commit))
        env_names = set()
        for m in ENV_RE.finditer(body):
            name = next(g for g in m.groups() if g)
            if name not in env_names:
                env_names.add(name)
                line = body[:m.start()].count("\n") + 1
                evidence.append(Evidence(
                    kind="config_source", layer="source", subject=svc,
                    attrs={"env_var": name, "externalized": True},
                    source="tree_sitter", method="env-read extraction",
                    location=f"{rel}:{line}", system_id=cfg.system_id, version=commit))

    return {"functions": functions, "calls": calls, "evidence": evidence,
            "commit": commit}