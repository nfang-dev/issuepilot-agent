"""Lightweight, read-only source discovery and Python AST inspection.

This module does not execute repository code or attempt a full static call graph.
"""
from __future__ import annotations

import ast
import re
from typing import Any


def rank_file_paths(paths: list[str], query: str, path_prefix: str = "") -> list[str]:
    """Prioritize plausible source code locations without relying on GitHub code search auth."""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", query) if len(t) >= 3]
    prefix = path_prefix.casefold()

    def score(path: str) -> tuple[int, str]:
        lower = path.casefold()
        filename = path.rsplit("/", 1)[-1].casefold()
        relevance = sum(16 if token in filename else 6 for token in tokens if token in lower)
        if query.casefold() in lower:
            relevance += 40
        if path.endswith(".py"):
            relevance += 12
        if lower.startswith(("src/", "lib/", "internal/", "pkg/")):
            relevance += 12
        if lower.startswith(("tests/", "test/")) or "/tests/" in lower:
            relevance -= 6
        if lower.endswith((".md", ".json", ".yaml", ".yml")):
            relevance -= 8
        return (-relevance, path)

    return sorted((p for p in paths if p.casefold().startswith(prefix)), key=score)


def find_text_matches(source: str, query: str, url: str, path: str, limit: int = 4) -> list[dict]:
    """Return line-numbered literal text matches with short context snippets."""
    needle = query.strip().casefold()
    words = [w.casefold() for w in query.split() if len(w) >= 2]
    lines = source.splitlines()
    matches = []
    for i, line in enumerate(lines):
        text = line.casefold()
        if needle not in text and not (len(words) > 1 and all(w in text for w in words)):
            continue
        lo = max(0, i - 1)
        hi = min(len(lines), i + 2)
        snippet = "\n".join(f"{idx + 1}: {lines[idx][:220]}" for idx in range(lo, hi))
        matches.append({
            "path": path,
            "line": i + 1,
            "snippet": snippet[:800],
            "url": f"{url}#L{i + 1}" if url else "",
        })
        if len(matches) >= limit:
            break
    return matches


def inspect_python_symbols(source: str, path: str, symbol: str, url: str) -> dict[str, Any]:
    """Extract matching class/function signatures, relevant call names and class bases.

    These are syntactic observations. No import resolution or runtime dispatch occurs.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*){0,3}", symbol):
        return {"error": "symbol must be a Python identifier or dotted identifier"}
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return {"error": f"Cannot parse Python source: {exc.msg}"}
    lines = source.splitlines()
    candidates: list[tuple[str, Any, list[str]]] = []

    def walk(body: list, parents: list[str], bases: list[str]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                class_bases = [ast.unparse(b)[:100] for b in node.bases]
                qualified = ".".join(parents + [node.name])
                candidates.append((qualified, node, class_bases))
                walk(node.body, parents + [node.name], class_bases)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = ".".join(parents + [node.name])
                candidates.append((qualified, node, bases))
                # Nested helper functions are useful when tracing a public function.
                walk(node.body, parents + [node.name], bases)

    walk(tree.body, [], [])
    matches = []
    for qualified, node, bases in candidates:
        if symbol not in (qualified, qualified.rsplit(".", 1)[-1]):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        relevant_calls = []
        if not isinstance(node, ast.ClassDef):
            for item in ast.walk(node):
                if isinstance(item, ast.Call):
                    try:
                        target = ast.unparse(item.func)
                    except (ValueError, TypeError):
                        continue
                    if target not in relevant_calls:
                        relevant_calls.append(target[:120])
        snippet_to = min(end, start + 45)
        snippet = "\n".join(
            f"{line_no}: {lines[line_no - 1][:240]}"
            for line_no in range(start, min(snippet_to, len(lines)) + 1)
        )
        matches.append({
            "qualified_name": qualified,
            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
            "line_start": start,
            "line_end": end,
            "bases": bases,
            "calls": relevant_calls[:20],
            "excerpt": snippet[:7500],
            "url": f"{url}#L{start}-L{end}" if url else "",
        })
        if len(matches) == 6:
            break
    return {"path": path, "symbol": symbol, "matches": matches, "match_count": len(matches),
            "note": "Static AST observations only; calls and inheritance are not resolved across modules."}
