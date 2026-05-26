"""
Claude PR Reviewer — Phase 3
Adds to Phase 2:
  - Call graph tool: Claude can ask "who calls this function?" before scoring severity
  - Severity boost: issues in functions on a critical call path are auto-promoted
  - Reads pre-built call_graph.json written by call_graph.py
"""

import os
import re
import json
from pathlib import Path
from dataclasses import dataclass

import anthropic
import requests
from tree_sitter import Language, Parser, Node
import tree_sitter_python as tspython

# Local module (must be in scripts/ alongside this file)
import sys
sys.path.insert(0, str(Path(__file__).parent))
from call_graph import build_call_graph, summarise_for_function, CallGraph

# ── Config ─────────────────────────────────────────────────────────────────────
MODEL       = "claude-haiku-4-5-20251001"   # swap to claude-sonnet-4-6 for richer output
MAX_TOKENS  = 2048
MAX_TOOLS   = 8
REPO_ROOT   = Path(os.environ.get("GITHUB_WORKSPACE", "."))
# ──────────────────────────────────────────────────────────────────────────────

PY_LANGUAGE = Language(tspython.language())
ts_parser   = Parser(PY_LANGUAGE)


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class FileDiff:
    filename: str
    changed_lines: list
    raw_diff: str


@dataclass
class ChangedFunction:
    file: str
    name: str
    start_line: int
    end_line: int
    source: str
    changed_lines: list


# ── Diff parser (unchanged from v2) ───────────────────────────────────────────

def parse_diff(diff_text: str) -> list:
    files, current_file, current_lines, current_diff_lines = [], None, [], []
    new_line_no = 0
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ b/"):
            if current_file:
                files.append(FileDiff(current_file, current_lines, "\n".join(current_diff_lines)))
            current_file, current_lines, current_diff_lines = raw_line[6:], [], [raw_line]
            new_line_no = 0
        elif raw_line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,\d+)?", raw_line)
            if m:
                new_line_no = int(m.group(1))
            current_diff_lines.append(raw_line)
        elif raw_line.startswith("+") and not raw_line.startswith("+++"):
            current_lines.append(new_line_no)
            new_line_no += 1
            current_diff_lines.append(raw_line)
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            current_diff_lines.append(raw_line)
        else:
            new_line_no += 1
            current_diff_lines.append(raw_line)
    if current_file:
        files.append(FileDiff(current_file, current_lines, "\n".join(current_diff_lines)))
    return [f for f in files if f.filename.endswith(".py")]


# ── Tree-sitter helpers (unchanged from v2) ───────────────────────────────────

def _node_name(node: Node, src: bytes) -> str:
    for child in node.children:
        if child.type == "identifier":
            return src[child.start_byte:child.end_byte].decode()
    return "unknown"


def find_changed_functions(file_diff: FileDiff) -> list:
    path = REPO_ROOT / file_diff.filename
    if not path.exists():
        return []
    src_bytes = path.read_bytes()
    tree      = ts_parser.parse(src_bytes)
    changed   = set(file_diff.changed_lines)
    results   = []

    def walk(node: Node):
        if node.type in ("function_definition", "class_definition"):
            start   = node.start_point[0] + 1
            end     = node.end_point[0]   + 1
            overlap = sorted(l for l in changed if start <= l <= end)
            if overlap:
                results.append(ChangedFunction(
                    file=file_diff.filename, name=_node_name(node, src_bytes),
                    start_line=start, end_line=end,
                    source=src_bytes[node.start_byte:node.end_byte].decode(),
                    changed_lines=overlap,
                ))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return results


# ── Tool implementations ───────────────────────────────────────────────────────

# Call graph is built once and reused by all tool calls
_call_graph: CallGraph | None = None

def _get_graph() -> CallGraph:
    global _call_graph
    if _call_graph is None:
        print("[claude-reviewer-v3] Building call graph ...")
        _call_graph = build_call_graph(REPO_ROOT)
        print(f"[claude-reviewer-v3] Call graph: {len(_call_graph.locations)} functions indexed")
    return _call_graph


def tool_get_call_graph(function_name: str) -> str:
    """Return caller/callee summary + critical-path hints for a function."""
    return summarise_for_function(_get_graph(), function_name)


def tool_get_function_source(file: str, function_name: str) -> str:
    path = REPO_ROOT / file
    if not path.exists():
        return f"File not found: {file}"
    src_bytes = path.read_bytes()
    tree      = ts_parser.parse(src_bytes)

    def find(node: Node):
        if node.type == "function_definition" and _node_name(node, src_bytes) == function_name:
            return src_bytes[node.start_byte:node.end_byte].decode()
        for child in node.children:
            r = find(child)
            if r:
                return r
        return None

    return find(tree.root_node) or f"Function '{function_name}' not found in {file}"


def tool_search_symbol(symbol: str) -> str:
    hits = []
    for py_file in REPO_ROOT.rglob("*.py"):
        try:
            src_bytes = py_file.read_bytes()
            tree      = ts_parser.parse(src_bytes)

            def find_def(node: Node):
                if node.type in ("function_definition", "class_definition"):
                    if _node_name(node, src_bytes) == symbol:
                        hits.append(f"{py_file.relative_to(REPO_ROOT)}:{node.start_point[0]+1} ({node.type})")
                for child in node.children:
                    find_def(child)

            find_def(tree.root_node)
        except Exception:
            pass
    return "\n".join(hits) if hits else f"No definition found for '{symbol}'"


def tool_get_file_imports(file: str) -> str:
    path = REPO_ROOT / file
    if not path.exists():
        return f"File not found: {file}"
    src_bytes = path.read_bytes()
    tree      = ts_parser.parse(src_bytes)
    imports   = []

    def find_imports(node: Node):
        if node.type in ("import_statement", "import_from_statement"):
            imports.append(src_bytes[node.start_byte:node.end_byte].decode())
        for child in node.children:
            find_imports(child)

    find_imports(tree.root_node)
    return "\n".join(imports) or "No imports found"


# ── Tool definitions for the API ───────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_call_graph",
        "description": (
            "Get the call graph context for a function: who calls it, what it calls, "
            "and whether it sits on a critical execution path. "
            "Use this to calibrate severity — a bug in a function called by the payment "
            "handler is more critical than one in a utility used only in tests."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "Exact function name to look up"},
            },
            "required": ["function_name"],
        },
    },
    {
        "name": "get_function_source",
        "description": "Get the full source code of any Python function in the repo by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file":          {"type": "string", "description": "Relative path to the .py file"},
                "function_name": {"type": "string", "description": "Exact function name"},
            },
            "required": ["file", "function_name"],
        },
    },
    {
        "name": "search_symbol",
        "description": "Search for where a function or class is defined across the entire codebase.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Function or class name"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_file_imports",
        "description": "Get all import statements from a file to understand its dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Relative path to the .py file"},
            },
            "required": ["file"],
        },
    },
]

SYSTEM_PROMPT = """You are an expert Python code reviewer with access to the repo's call graph.

You receive:
1. Python functions modified in this PR (via Tree-sitter AST)
2. The specific lines that changed inside each function
3. Four tools to explore the codebase further

Tools available:
- get_call_graph    — find who calls a function and what it calls; detects critical paths
- get_function_source — read any function's full source
- search_symbol     — find where a symbol is defined repo-wide
- get_file_imports  — see a file's dependencies

Review process:
1. Examine each changed function.
2. For any function with a potential security/bug issue, call get_call_graph to check
   whether it sits on a critical execution path (payment, auth, data persistence, etc.).
   A bug on a critical path should be "high"; same bug in a test helper is "low".
3. Use other tools only when you genuinely need more context.

Report ONLY high-confidence issues:
- Bugs: logic errors, missing null/zero checks, off-by-one
- Security: hardcoded secrets, injection, unsafe eval/exec, path traversal
- Performance: N+1, unbounded loops, resource leaks
- Error handling: bare except, swallowed exceptions, unclosed resources
- Naming: genuinely misleading (not minor style preferences)

Output ONLY valid JSON — no markdown, no prose:
{
  "summary": "One sentence: what does this PR do?",
  "issues": [
    {
      "severity": "high|medium|low",
      "file": "path/to/file.py",
      "line": 42,
      "function": "function_name",
      "issue": "Clear description",
      "suggestion": "Concrete fix",
      "call_path_note": "Optional: e.g. called by payment handler"
    }
  ]
}

If no real issues exist, return an empty issues array."""


# ── Agent loop ─────────────────────────────────────────────────────────────────

def dispatch_tool(name: str, args: dict) -> str:
    if name == "get_call_graph":       return tool_get_call_graph(**args)
    if name == "get_function_source":  return tool_get_function_source(**args)
    if name == "search_symbol":        return tool_search_symbol(**args)
    if name == "get_file_imports":     return tool_get_file_imports(**args)
    return f"Unknown tool: {name}"


def run_agent(changed_functions: list, client: anthropic.Anthropic) -> str:
    if not changed_functions:
        return json.dumps({"summary": "No Python functions changed in this PR.", "issues": []})

    parts = ["Here are the Python functions modified in this PR:\n"]
    for fn in changed_functions:
        parts.append(f"### `{fn.name}` in `{fn.file}` (lines {fn.start_line}–{fn.end_line})")
        parts.append(f"**Changed lines:** {fn.changed_lines}")
        parts.append(f"```python\n{fn.source}\n```\n")

    messages = [{"role": "user", "content": "\n".join(parts)}]

    for _ in range(MAX_TOOLS + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[claude-reviewer-v3] tool={block.name} args={block.input}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": dispatch_tool(block.name, block.input),
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return json.dumps({"summary": "Review completed.", "issues": []})


# ── Comment formatter ──────────────────────────────────────────────────────────

def format_comment(raw: str, changed_functions: list) -> str:
    try:
        review = json.loads(raw)
    except json.JSONDecodeError:
        return f"## 🤖 Claude Code Review (v3)\n\n{raw}"

    lines = [
        "## 🤖 Claude Code Review",
        "> *Tree-sitter AST · Call graph · Agentic analysis · Claude Haiku*\n",
        f"**Summary:** {review.get('summary', '—')}\n",
    ]

    if changed_functions:
        fn_list = ", ".join(f"`{f.name}`" for f in changed_functions[:10])
        lines.append(f"**Functions analysed:** {fn_list}\n")

    issues = review.get("issues", [])
    if not issues:
        lines.append("✅ **No significant issues found.** Looks good to merge!")
        return "\n".join(lines)

    for key, heading in [("high", "🔴 High"), ("medium", "🟡 Medium"), ("low", "🟢 Low")]:
        bucket = [i for i in issues if i.get("severity") == key]
        if not bucket:
            continue
        lines.append(f"\n### {heading} Severity\n")
        for issue in bucket:
            fn_part  = f" · `{issue['function']}`" if issue.get("function") else ""
            location = f"`{issue.get('file','?')}`{fn_part} line {issue.get('line','?')}"
            lines.append(f"- **{location}** — {issue.get('issue','')}")
            if issue.get("call_path_note"):
                lines.append(f"  > 📍 *{issue['call_path_note']}*")
            if issue.get("suggestion"):
                lines.append(f"  > 💡 {issue['suggestion']}")

    lines.append("\n---")
    lines.append("*False positive? Leave a 👎 on this comment.*")
    return "\n".join(lines)


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def get_pr_diff(repo: str, pr_number: str, token: str) -> str:
    url  = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    resp = requests.get(url, headers={"Authorization": f"token {token}",
                                       "Accept": "application/vnd.github.v3.diff"}, timeout=30)
    resp.raise_for_status()
    return resp.text


def post_pr_comment(repo: str, pr_number: str, body: str, token: str) -> int:
    url  = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    resp = requests.post(url, headers={"Authorization": f"token {token}",
                                        "Accept": "application/vnd.github.v3+json"},
                         json={"body": body}, timeout=30)
    return resp.status_code


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    github_token  = os.environ["GITHUB_TOKEN"]
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    repo          = os.environ["GITHUB_REPOSITORY"]
    pr_number     = os.environ["PR_NUMBER"]

    print(f"[claude-reviewer-v3] PR #{pr_number} in {repo}")
    client = anthropic.Anthropic(api_key=anthropic_key)

    diff           = get_pr_diff(repo, pr_number, github_token)
    file_diffs     = parse_diff(diff)
    print(f"[claude-reviewer-v3] Changed Python files: {[f.filename for f in file_diffs]}")

    changed_functions = []
    for fd in file_diffs:
        changed_functions.extend(find_changed_functions(fd))
    print(f"[claude-reviewer-v3] Changed functions: {[f.name for f in changed_functions]}")

    # Pre-build call graph (used lazily by the tool)
    _get_graph()

    raw_review = run_agent(changed_functions, client)
    comment    = format_comment(raw_review, changed_functions)
    status     = post_pr_comment(repo, pr_number, comment, github_token)
    print(f"[claude-reviewer-v3] Posted comment — HTTP {status}")


if __name__ == "__main__":
    main()