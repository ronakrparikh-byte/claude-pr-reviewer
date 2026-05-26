"""
call_graph.py — Lightweight Python call graph builder using the built-in ast module.

Produces a JSON file (call_graph.json) with:
  {
    "callers":  { "fn_name": ["fn_that_calls_it", ...] },
    "callees":  { "fn_name": ["fn_it_calls",       ...] },
    "locations":{ "fn_name": "path/to/file.py:line"    }
  }

This is the "Joern-lite" layer.  Full Joern adds inter-procedural taint
analysis and more precise data-flow tracking — see the NOTE at the bottom
if you want to upgrade later.

Usage (standalone):
    python scripts/call_graph.py --root . --out call_graph.json

Usage (from review_pr_v3.py):
    from call_graph import build_call_graph
    graph = build_call_graph(Path("."))
"""

import ast
import json
import argparse
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field, asdict


@dataclass
class CallGraph:
    # callers["fn"] = list of functions that call fn
    callers:   dict = field(default_factory=lambda: defaultdict(list))
    # callees["fn"] = list of functions that fn calls
    callees:   dict = field(default_factory=lambda: defaultdict(list))
    # locations["fn"] = "file.py:line"
    locations: dict = field(default_factory=dict)


class _CallGraphVisitor(ast.NodeVisitor):
    """Single-pass AST visitor that records definitions and call sites."""

    def __init__(self, filename: str, graph: CallGraph):
        self.filename  = filename
        self.graph     = graph
        self._scope_stack: list[str] = []   # nested function names

    # ── current scope ─────────────────────────────────────────────────────────
    @property
    def _current_scope(self) -> str | None:
        return self._scope_stack[-1] if self._scope_stack else None

    # ── function definition ────────────────────────────────────────────────────
    def visit_FunctionDef(self, node: ast.FunctionDef):
        name = node.name
        self.graph.locations[name] = f"{self.filename}:{node.lineno}"
        self._scope_stack.append(name)
        self.generic_visit(node)
        self._scope_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef   # treat async the same

    # ── call site ─────────────────────────────────────────────────────────────
    def visit_Call(self, node: ast.Call):
        callee_name = _extract_call_name(node.func)
        if callee_name and self._current_scope:
            caller = self._current_scope
            callee = callee_name
            # callees: caller → what it calls
            if callee not in self.graph.callees[caller]:
                self.graph.callees[caller].append(callee)
            # callers: callee ← who calls it
            if caller not in self.graph.callers[callee]:
                self.graph.callers[callee].append(caller)
        self.generic_visit(node)


def _extract_call_name(node: ast.expr) -> str | None:
    """Best-effort extraction of a called name from a call's func node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr   # e.g. self.save() → "save"
    return None


def build_call_graph(root: Path) -> CallGraph:
    """Walk all .py files under root and build the call graph."""
    graph = CallGraph()
    for py_file in sorted(root.rglob("*.py")):
        # Skip hidden dirs, venvs, __pycache__, etc.
        if any(part.startswith(".") or part in ("venv", "__pycache__", "node_modules")
               for part in py_file.parts):
            continue
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree   = ast.parse(source, filename=str(py_file))
            rel    = str(py_file.relative_to(root))
            visitor = _CallGraphVisitor(rel, graph)
            visitor.visit(tree)
        except SyntaxError:
            pass   # skip unparseable files silently
    return graph


def graph_to_dict(graph: CallGraph) -> dict:
    return {
        "callers":   dict(graph.callers),
        "callees":   dict(graph.callees),
        "locations": graph.locations,
    }


def summarise_for_function(graph: CallGraph, fn_name: str) -> str:
    """
    Return a human-readable summary of a function's call context.
    This is what gets handed to Claude as a tool result.
    """
    callers  = graph.callers.get(fn_name, [])
    callees  = graph.callees.get(fn_name, [])
    location = graph.locations.get(fn_name, "unknown location")

    lines = [f"Call graph for `{fn_name}` (defined at {location}):"]

    if callers:
        lines.append(f"  Called by ({len(callers)}): " + ", ".join(f"`{c}`" for c in callers[:10]))
    else:
        lines.append("  Called by: (nothing in this repo — may be an entry point or dead code)")

    if callees:
        lines.append(f"  Calls ({len(callees)}): " + ", ".join(f"`{c}`" for c in callees[:10]))
    else:
        lines.append("  Calls: (no other functions)")

    # Highlight critical-path heuristic: if callers include common entry points
    critical_hints = {"main", "run", "handle", "execute", "process",
                      "dispatch", "route", "view", "endpoint", "task"}
    critical = [c for c in callers if c.lower() in critical_hints
                or any(h in c.lower() for h in critical_hints)]
    if critical:
        lines.append(f"  ⚠️  Called from critical-path functions: {', '.join(critical)}")

    return "\n".join(lines)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a Python call graph")
    parser.add_argument("--root", default=".", help="Repo root directory")
    parser.add_argument("--out",  default="call_graph.json", help="Output JSON file")
    args = parser.parse_args()

    print(f"[call_graph] Scanning {args.root} ...")
    graph = build_call_graph(Path(args.root))
    data  = graph_to_dict(graph)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(data, indent=2))
    print(f"[call_graph] Wrote {len(data['locations'])} functions → {out_path}")


# ── NOTE: Upgrading to full Joern ──────────────────────────────────────────────
#
# This module gives you caller/callee edges from static AST analysis.
# Full Joern (https://joern.io) adds:
#   - Inter-procedural taint analysis  (e.g. user input reaches SQL query)
#   - Data-flow graphs (DFG)
#   - Control-flow graphs (CFG)
#   - Reachability queries in Scala via `cpg.method.name("fn").reachableBy(...)`
#
# To upgrade, install Joern (requires Java 11+):
#   curl -L https://github.com/joernio/joern/releases/latest/download/joern-install.sh | sh
#
# Then replace build_call_graph() with a subprocess call to joern-cli:
#   joern --script scripts/extract_cpg.sc --params "outFile=call_graph.json"
#
# Where extract_cpg.sc is a Scala script that queries the CPG and writes JSON.
# ──────────────────────────────────────────────────────────────────────────────