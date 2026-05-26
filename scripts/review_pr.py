import os
import json
import anthropic
import requests

# ── Config ────────────────────────────────────────────────────────────────────
MAX_DIFF_CHARS = 8000   # keep token costs low with Haiku
MODEL           = "claude-haiku-4-5-20251001"
MAX_TOKENS      = 1024
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Python code reviewer. Analyse the PR diff and return ONLY valid JSON.

Rules:
- Only report HIGH-CONFIDENCE issues (bugs, security holes, major performance problems, broken error handling, bad naming)
- Skip minor style nits unless they break conventions badly
- Severity: "high" = bug/security, "medium" = logic/performance, "low" = naming/clarity

Return this exact JSON shape:
{
  "summary": "One sentence describing what this PR does",
  "issues": [
    {
      "severity": "high|medium|low",
      "file": "path/to/file.py",
      "line": 42,
      "issue": "Clear description of the problem",
      "suggestion": "Concrete fix"
    }
  ]
}

If no real issues exist, return an empty issues array."""


def get_pr_diff(repo: str, pr_number: str, token: str) -> str:
    """Fetch the unified diff for a PR from GitHub."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.diff",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def review_with_claude(diff: str, client: anthropic.Anthropic) -> str:
    """Send the diff to Claude Haiku and get a JSON review back."""
    # Truncate if too long
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n... (diff truncated to keep costs low)"

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"Review this Python PR diff:\n\n```diff\n{diff}\n```"}
        ],
    )
    return message.content[0].text


def format_comment(raw: str) -> str:
    """Turn Claude's JSON response into a readable GitHub Markdown comment."""
    try:
        review = json.loads(raw)
    except json.JSONDecodeError:
        # Claude didn't return valid JSON — just show it as-is
        return f"## 🤖 Claude Code Review\n\n{raw}"

    lines = ["## 🤖 Claude Code Review\n"]
    lines.append(f"**Summary:** {review.get('summary', '—')}\n")

    issues = review.get("issues", [])
    if not issues:
        lines.append("✅ **No significant issues found.** Looks good to merge!")
        return "\n".join(lines)

    severity_order = [
        ("high",   "🔴 High Severity"),
        ("medium", "🟡 Medium Severity"),
        ("low",    "🟢 Low Severity"),
    ]

    for key, heading in severity_order:
        bucket = [i for i in issues if i.get("severity") == key]
        if not bucket:
            continue
        lines.append(f"\n### {heading}\n")
        for issue in bucket:
            file_ref = f"`{issue.get('file', 'unknown')}`"
            if issue.get("line"):
                file_ref += f" (line {issue['line']})"
            lines.append(f"- **{file_ref}** — {issue.get('issue', '')}")
            if issue.get("suggestion"):
                lines.append(f"  > 💡 {issue['suggestion']}")

    lines.append("\n---")
    lines.append("*Reviewed by Claude Haiku · [false positives? leave a 👎 on this comment]*")
    return "\n".join(lines)


def post_pr_comment(repo: str, pr_number: str, body: str, token: str) -> int:
    """Post a comment on the PR (issue comments endpoint)."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.post(url, headers=headers, json={"body": body}, timeout=30)
    return resp.status_code


def main():
    # All injected by GitHub Actions environment
    github_token   = os.environ["GITHUB_TOKEN"]
    anthropic_key  = os.environ["ANTHROPIC_API_KEY"]
    repo           = os.environ["GITHUB_REPOSITORY"]          # e.g. "user/repo"
    pr_number      = os.environ["PR_NUMBER"]

    print(f"[claude-reviewer] Reviewing PR #{pr_number} in {repo}")

    client = anthropic.Anthropic(api_key=anthropic_key)

    diff       = get_pr_diff(repo, pr_number, github_token)
    raw_review = review_with_claude(diff, client)
    comment    = format_comment(raw_review)

    status = post_pr_comment(repo, pr_number, comment, github_token)
    print(f"[claude-reviewer] Comment posted — HTTP {status}")


if __name__ == "__main__":
    main()