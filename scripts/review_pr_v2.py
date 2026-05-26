"""
Claude PR Reviewer — Phase 2
- Tree-sitter AST: extracts full function bodies that contain changed lines
- Diff parser: maps line numbers to functions precisely
- Agentic loop: Claude can call tools (get_function_source, search_symbol,
  get_file_imports) before giving its final verdict
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

# ── Config ─────────────────────────────────────────────────────────────────────
MODEL       = "claude-haiku-4-5-20251001"  # swap to claude-sonnet-4-6 for richer reviews
MAX_TOKENS  = 2048
MAX_TOOLS   = 6   # max tool-call rounds before forcing a final answer
REPO_ROOT   = Path(os.environ.get("GITHUB_WORKSPACE", "."))
# ──────────────────────────────────────────────────────────────────────────────

# Initialise Tree-sitter Python parser (v0.22+ API)
PY_LANGUAGE = Language(tspython.language())
ts_parser   = Parser(PY_LANGUAGE)


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class FileDiff:
    filename: str
    changed_lines: list  # list[int]
    raw_diff: str


@dataclass
class ChangedFunction:
    file: str
    name: str
    start_line: int
    end_line: int
    source: str
    changed_lines: list  # list[int] — which of the changed lines sit inside this fn


# ── Diff parser ────────────────────────────────────────────────────────────────

def parse_diff(diff_text: str) -> list:
    """
    Parse a unified diff and return one FileDiff per changed .py file.
    Tracks new-file line numbers for every '+' line.
    """
    files = []
    current_file = None
    current_lines = []
    current_diff_lines = []
    new_line_no = 0

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ b/"):
            if current_file:
                files.append(FileDiff(
                    filename=current_file,
                    changed_lines=current_lines,
                    raw_diff="\n".join(current_diff_lines),
                ))
            current_file = raw_line[6:]
            current_lines = []
            current_diff_lines = [raw_line]
            new_line_no = 0

        elif raw_line.startswith("@@"):
            # e.g.  @@ -10,7 +12,9 @@
            m = re.search(r"\+(\d+)(?:,\d+)?", raw_line)
            if m:
                new_line_no = int(m.group(1))
            current_diff_lines.append(raw_line)

        elif raw_line.startswith("+") and not raw_line.startswith("+++"):
            # Added line — record the new line number
            current_lines.append(new_line_no)
            new_line_no += 1
            current_diff_lines.append(raw_line)

        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            # Removed line — no new line number increment
            current_diff_lines.append(raw_line)

        else:
            # Context line
            new_line_no += 1
            current_diff_lines.append(raw_line)

    if current_file:
        files.append(FileDiff(
            filename=current_file,
            changed_lines=current_lines,
            raw_diff="\n".join(current_diff_lines),
        ))

    # Only care about Python files
    return [f for f in files if f.filename.endswith(".py")]


# ── Tree-sitter helpers ────────────────────────────────────────────────────────

def _node_name(node: Node, src: bytes) -> str:
    for child in node.children:
        if child.type == "identifier":
            return src[child.start_byte:child.end_byte].decode()
    return "unknown"


def find_changed_functions(file_diff: FileDiff) -> list:
    """
    For a FileDiff, load the file from the checked-out repo, parse with
    Tree-sitter, and return every function/class whose line range overlaps
    with the changed lines.
    """
    path = REPO_ROOT / file_diff.filename
    if not path.exists():
        return []

    src_bytes = path.read_bytes()
    tree      = ts_parser.parse(src_bytes)
    changed   = set(file_diff.changed_lines)
    results   = []

    def walk(node: Node):
        if node.type in ("function_definition", "class_definition"):
            start = node.start_point[0] + 1   # tree-sitter is 0-indexed
            end   = node.end_point[0]  + 1
            overlap = sorted(l for l in changed if start <= l <= end)
            if overlap:
                results.append(ChangedFunction(
                    file         = file_diff.filename,
                    name         = _node_name(node, src_bytes),
                    start_line   = start,
                    end_line     = end,
                    source       = src_bytes[node.start_byte:node.end_byte].decode(),
                    changed_lines= overlap,
                ))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return results


# ── Tool implementations ───────────────────────────────────────────────────────

def tool_get_function_source(file: str, function_name: str) -> str:
    """Return the full source of a named function from any file in the repo."""
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

    result = find(tree.root_node)
    return result or f"Function '{function_name}' not found in {file}"


def tool_search_symbol(symbol: str) -> str:
    """Search every .py file for a function or class definition matching symbol."""
    hits = []
    for py_file in REPO_ROOT.rglob("*.py"):
        try:
            src_bytes = py_file.read_bytes()
            tree      = ts_parser.parse(src_bytes)

            def find_def(node: Node):
                if node.type in ("function_definition", "class_definition"):
                    if _node_name(node, src_bytes) == symbol:
                        line = node.start_point[0] + 1
                        hits.append(f"{py_file.relative_to(REPO_ROOT)}:{line} ({node.type})")
                for child in node.children:
                    find_def(child)

            find_def(tree.root_node)
        except Exception:
            pass

    return "\n".join(hits) if hits else f"No definition found for '{symbol}'"


def tool_get_file_imports(file: str) -> str:
    """Return all import statements from a Python file."""
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


# Tool definitions passed to the Claude API
TOOLS = [
    {
        "name": "get_function_source",
        "description": (
            "Get the complete source code of a Python function by name. "
            "Use this when you need to see the full implementation of a callee "
            "or helper that isn't in the diff."
        ),
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
        "description": (
            "Search the entire repo for where a function or class is defined. "
            "Useful for understanding call graphs and whether a changed function "
            "is used in critical paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Function or class name to look up"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_file_imports",
        "description": "Return all import statements from a file to understand its dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Relative path to the .py file"},
            },
            "required": ["file"],
        },
    },
]

SYSTEM_PROMPT = """You are an expert Python code reviewer.

You receive a list of Python functions that were modified in a PR (extracted via Tree-sitter AST).
Each entry shows the function source and exactly which lines changed.

You also have three tools:
- get_function_source  — read any function in the repo for deeper context
- search_symbol        — find where a symbol is defined across the codebase
- get_file_imports     — see what a file imports

Review process:
1. Read every changed function carefully.
2. Call tools only when you genuinely need more context to make a confident call.
3. Focus on HIGH-CONFIDENCE issues only:
   - Bugs: off-by-one, missing null/zero checks, wrong logic
   - Security: hardcoded secrets, injection risks, unsafe eval/exec, path traversal
   - Performance: N+1 queries, unbounded loops, missing resource cleanup
   - Error handling: bare except, swallowed exceptions, missing finally/close
   - Naming: genuinely misleading names (not style preferences)

When done, output ONLY valid JSON — no markdown fences, no prose:
{
  "summary": "One sentence describing what this PR does",
  "issues": [
    {
      "severity": "high|medium|low",
      "file": "path/to/file.py",
      "line": 42,
      "function": "function_name",
      "issue": "Clear description of the problem",
      "suggestion": "Concrete actionable fix"
    }
  ]
}

If no real issues exist return an empty issues array. Do not invent problems."""


# ── Agent loop ─────────────────────────────────────────────────────────────────

def dispatch_tool(name: str, args: dict) -> str:
    if name == "get_function_source":
        return tool_get_function_source(**args)
    if name == "search_symbol":
        return tool_search_symbol(**args)
    if name == "get_file_imports":
        return tool_get_file_imports(**args)
    return f"Unknown tool: {name}"


def run_agent(changed_functions: list, client: anthropic.Anthropic) -> str:
    """Agentic review: Claude can call tools before giving its final answer."""
    if not changed_functions:
        return json.dumps({"summary": "No Python functions were changed in this PR.", "issues": []})

    # Build the initial user message
    parts = ["Here are the Python functions modified in this PR:\n"]
    for fn in changed_functions:
        parts.append(f"### `{fn.name}` in `{fn.file}` (lines {fn.start_line}–{fn.end_line})")
        parts.append(f"**Changed lines within this function:** {fn.changed_lines}")
        parts.append(f"```python\n{fn.source}\n```\n")

    messages = [{"role": "user", "content": "\n".join(parts)}]

    for round_no in range(MAX_TOOLS + 1):
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
                    print(f"[claude-reviewer-v2] tool={block.name} args={block.input}")
                    result = dispatch_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
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
        return f"## 🤖 Claude Code Review (v2)\n\n{raw}"

    lines = [
        "## 🤖 Claude Code Review",
        "> *Tree-sitter AST · Agentic analysis · Claude Haiku*\n",
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
            if issue.get("suggestion"):
                lines.append(f"  > 💡 {issue['suggestion']}")

    lines.append("\n---")
    lines.append("*False positive? Leave a 👎 on this comment.*")
    return "\n".join(lines)


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def get_pr_diff(repo: str, pr_number: str, token: str) -> str:
    url     = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3.diff"}
    resp    = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def post_pr_comment(repo: str, pr_number: str, body: str, token: str) -> int:
    url     = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    resp    = requests.post(url, headers=headers, json={"body": body}, timeout=30)
    return resp.status_code


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    github_token  = os.environ["GITHUB_TOKEN"]
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    repo          = os.environ["GITHUB_REPOSITORY"]
    pr_number     = os.environ["PR_NUMBER"]

    print(f"[claude-reviewer-v2] PR #{pr_number} in {repo}")
    client = anthropic.Anthropic(api_key=anthropic_key)

    # 1. Fetch diff
    diff = get_pr_diff(repo, pr_number, github_token)

    # 2. Parse diff → changed Python files + line numbers
    file_diffs = parse_diff(diff)
    print(f"[claude-reviewer-v2] Changed Python files: {[f.filename for f in file_diffs]}")

    # 3. Tree-sitter → extract functions that contain the changed lines
    changed_functions = []
    for fd in file_diffs:
        changed_functions.extend(find_changed_functions(fd))
    print(f"[claude-reviewer-v2] Changed functions: {[f.name for f in changed_functions]}")

    # 4. Agentic review
    raw_review = run_agent(changed_functions, client)

    # 5. Format + post
    comment = format_comment(raw_review, changed_functions)
    status  = post_pr_comment(repo, pr_number, comment, github_token)
    print(f"[claude-reviewer-v2] Posted comment — HTTP {status}")


if __name__ == "__main__":
    main()