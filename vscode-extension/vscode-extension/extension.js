/**
 * Claude Code Reviewer — VS Code Extension
 *
 * What it does:
 *   - Adds a command "Claude: Review This File"  (Ctrl/Cmd+Shift+R)
 *   - Calls the Anthropic API with the current Python file's source
 *   - Converts Claude's JSON response to VS Code Diagnostics
 *     (squiggly underlines + Problems panel entries)
 *   - Optionally auto-reviews on file save
 */

const vscode = require("vscode");
const https  = require("https");

// ── Diagnostic collection (persists across reviews) ───────────────────────────
let diagnosticCollection;

// ── Activate ──────────────────────────────────────────────────────────────────
function activate(context) {
  diagnosticCollection = vscode.languages.createDiagnosticCollection("claude-reviewer");
  context.subscriptions.push(diagnosticCollection);

  // Command: review current file
  context.subscriptions.push(
    vscode.commands.registerCommand("claude-reviewer.reviewFile", reviewCurrentFile)
  );

  // Command: clear diagnostics
  context.subscriptions.push(
    vscode.commands.registerCommand("claude-reviewer.clearDiagnostics", () => {
      diagnosticCollection.clear();
      vscode.window.showInformationMessage("Claude review results cleared.");
    })
  );

  // Auto-review on save (if enabled in settings)
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument((doc) => {
      const cfg = vscode.workspace.getConfiguration("claude-reviewer");
      if (cfg.get("autoReviewOnSave") && doc.languageId === "python") {
        runReview(doc);
      }
    })
  );

  console.log("[claude-reviewer] Extension activated");
}

// ── Review the active editor's document ───────────────────────────────────────
async function reviewCurrentFile() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage("No active editor.");
    return;
  }
  if (editor.document.languageId !== "python") {
    vscode.window.showWarningMessage("Claude reviewer only supports Python files.");
    return;
  }
  await runReview(editor.document);
}

// ── Core review logic ─────────────────────────────────────────────────────────
async function runReview(document) {
  const cfg    = vscode.workspace.getConfiguration("claude-reviewer");
  const apiKey = cfg.get("apiKey");
  const model  = cfg.get("model") || "claude-haiku-4-5-20251001";

  if (!apiKey) {
    const action = await vscode.window.showErrorMessage(
      "Claude reviewer: API key not set.",
      "Open Settings"
    );
    if (action === "Open Settings") {
      vscode.commands.executeCommand(
        "workbench.action.openSettings",
        "claude-reviewer.apiKey"
      );
    }
    return;
  }

  const fileName = document.fileName.split(/[\\/]/).pop();

  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: `Claude reviewing ${fileName}...`,
      cancellable: false,
    },
    async () => {
      try {
        const source = document.getText();
        const issues = await callClaude(source, fileName, apiKey, model);
        applyDiagnostics(document, issues);

        const count = issues.length;
        if (count === 0) {
          vscode.window.showInformationMessage(`✅ Claude: no issues found in ${fileName}`);
        } else {
          vscode.window.showWarningMessage(
            `Claude found ${count} issue${count > 1 ? "s" : ""} in ${fileName} — see Problems panel`
          );
        }
      } catch (err) {
        vscode.window.showErrorMessage(`Claude reviewer error: ${err.message}`);
        console.error("[claude-reviewer]", err);
      }
    }
  );
}

// ── Call Anthropic API ─────────────────────────────────────────────────────────
function callClaude(source, fileName, apiKey, model) {
  return new Promise((resolve, reject) => {
    const systemPrompt = `You are an expert Python code reviewer.
Analyse the file and return ONLY valid JSON — no markdown, no prose.

Report ONLY high-confidence issues:
- Bugs: logic errors, missing null/zero checks, off-by-one
- Security: hardcoded secrets, injection, unsafe eval/exec, path traversal
- Performance: N+1, unbounded loops, unclosed resources
- Error handling: bare except, swallowed exceptions

JSON format:
{
  "summary": "One sentence describing the file's purpose",
  "issues": [
    {
      "severity": "high|medium|low",
      "line": 42,
      "function": "function_name_or_null",
      "issue": "Clear description",
      "suggestion": "Concrete fix"
    }
  ]
}
If no real issues exist return an empty issues array.`;

    const body = JSON.stringify({
      model,
      max_tokens: 1024,
      system: systemPrompt,
      messages: [
        {
          role: "user",
          content: `Review this Python file (${fileName}):\n\n\`\`\`python\n${source}\n\`\`\``,
        },
      ],
    });

    const options = {
      hostname: "api.anthropic.com",
      path:     "/v1/messages",
      method:   "POST",
      headers:  {
        "Content-Type":      "application/json",
        "x-api-key":         apiKey,
        "anthropic-version": "2023-06-01",
        "Content-Length":    Buffer.byteLength(body),
      },
    };

    const req = https.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          const parsed = JSON.parse(data);
          if (parsed.error) {
            reject(new Error(parsed.error.message || "Anthropic API error"));
            return;
          }
          const text   = parsed.content?.[0]?.text || "{}";
          const review = JSON.parse(text);
          resolve(review.issues || []);
        } catch (e) {
          reject(new Error("Failed to parse Claude response: " + e.message));
        }
      });
    });

    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

// ── Convert issues → VS Code Diagnostics ──────────────────────────────────────
function applyDiagnostics(document, issues) {
  // Clear previous diagnostics for this file
  diagnosticCollection.delete(document.uri);

  if (!issues || issues.length === 0) return;

  const diagnostics = issues.map((issue) => {
    // Line numbers from Claude are 1-indexed; VS Code is 0-indexed
    const lineIndex = Math.max(0, (issue.line || 1) - 1);
    const line      = document.lineAt(Math.min(lineIndex, document.lineCount - 1));
    const range     = new vscode.Range(
      line.lineNumber, line.firstNonWhitespaceCharacterIndex,
      line.lineNumber, line.text.length
    );

    const severity = severityMap(issue.severity);
    const message  = issue.function
      ? `[${issue.function}] ${issue.issue} — 💡 ${issue.suggestion}`
      : `${issue.issue} — 💡 ${issue.suggestion}`;

    const diag   = new vscode.Diagnostic(range, message, severity);
    diag.source  = "Claude Reviewer";
    diag.code    = issue.severity?.toUpperCase();
    return diag;
  });

  diagnosticCollection.set(document.uri, diagnostics);
}

function severityMap(s) {
  if (s === "high")   return vscode.DiagnosticSeverity.Error;
  if (s === "medium") return vscode.DiagnosticSeverity.Warning;
  return vscode.DiagnosticSeverity.Information;
}

// ── Deactivate ────────────────────────────────────────────────────────────────
function deactivate() {
  if (diagnosticCollection) diagnosticCollection.dispose();
}

module.exports = { activate, deactivate };