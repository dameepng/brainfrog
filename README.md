# BrainFrog (🐸) — Dual-System Coding Agent (Jev + Claude)

An agentic coding loop that writes code, runs tests, and drafts PRs — powered by a dual-system architecture inspired by OpenCode and Claude Code:

- **System 2 (Claude, via `ANTHROPIC_API_KEY`)** — the generative brain: planning changes, writing/editing code, diagnosing failures, drafting PR copy.
- **System 1 (Jev, via `TYPESAFE_API_KEY` or `--backend mock`)** — the fast, typed gatekeeper: scope matching, next-action evaluation, risk classification.

BrainFrog is packaged as a global CLI tool (`brainfrog` / `bf`) featuring an **Interactive REPL**, auto-workspace git detection, and slash commands.

## How the loop works

```
SCOPE GATE (Jev, before Claude sees anything):
  likely_domain  = which area of the codebase does this prompt match?
  change_type    = bug_investigation | feature_request | question_only | unclear
  -> confidence too low?      stop, ask the user to clarify (Claude not called)
  -> change_type=question_only? Claude reads only that domain's files and
     answers the question — no code is written, no PR
  -> otherwise: Claude's planner gets a file list scoped to the matched
     domain instead of the whole repo

plan (Claude)
  -> for each step:
       write code (Claude)
       run tests (real subprocess, your actual test command)
       ask Jev: next_action = open_pr | retry_fix | escalate_human | abandon
       branch on the answer + its confidence
         open_pr        -> go draft a PR
         retry_fix       -> Claude reads the failure, patches, loop again
         escalate_human  -> stop, print a report, wait for you
         abandon         -> stop, explain why
  -> draft PR title/body (Claude)
  -> ask Jev: diff_risk (low/med/high), safe_to_proceed (0-1 + confidence)
  -> if auto-pr enabled AND risk within your ceiling AND confidence clears
     the bar AND the domain isn't flagged sensitive -> actually branch,
     commit, push, `gh pr create`
     otherwise -> print the drafted PR for you to review/open by hand
```

A domain marked `"sensitive": true` in `modules.json` (auth, billing,
migrations, ...) **never** auto-opens a PR, no matter how high the
confidence or how permissive `--pr-risk-ceiling` is — it always stops for
a human.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then fill in ANTHROPIC_API_KEY
cp modules.example.json /path/to/your/repo/modules.json  # then edit for your actual domains
```

`modules.json` is what makes the scope gate useful — it's the vocabulary
Jev matches user prompts against. Without it, the tool still runs (falls
back to one undescribed "domain" per top-level directory), but Jev has
much less to go on and will ask for clarification more often. A few
short, accurate descriptions go a long way:

```json
{
  "auth": {
    "description": "Login, sessions, password reset, OAuth, tokens, permissions.",
    "paths": ["app/auth/", "app/middleware/session.py"],
    "sensitive": true
  }
}
```

## Usage

Dry run today, no Jev key needed:

```bash
python cli.py \
  --repo /path/to/your/repo \
  --task "Add a GET /health endpoint that returns 200 OK" \
  --test-cmd "pytest -q" \
  --backend mock
```

A vague, question-shaped prompt takes the diagnose-only path instead of
touching code:

```bash
python cli.py \
  --repo /path/to/your/repo \
  --task "ini kenapa saya gabisa login yah?" \
  --test-cmd "pytest -q" \
  --backend mock
```

Once your TypeSafe access arrives:

```bash
export TYPESAFE_API_KEY=sk-...
python cli.py \
  --repo /path/to/your/repo \
  --task "Add a GET /health endpoint that returns 200 OK" \
  --test-cmd "pytest -q" \
  --backend typesafe \
  --auto-pr --pr-risk-ceiling medium
```

`--backend auto` (the default) picks `typesafe` automatically if
`TYPESAFE_API_KEY` is set in the environment, else falls back to `mock`.

## Project layout

```
system1/            # Jev-shaped decision layer (fast, typed, cheap)
  base.py            interface: SystemOneClient, Choice/Score/Noul questions
  mock_client.py      free local heuristic backend (use until you have a key)
  typesafe_client.py  real backend, calls api.typesafe.ai/v1/systemone
system2/            # Claude-backed generation layer (slow, open-ended)
  claude_client.py    plan / write_code / review_and_fix / diagnose / draft_pr
modules.py           # domain registry loader + scope-gate helpers
modules.example.json # copy to <repo>/modules.json and edit
orchestrator.py     # the loop that wires system1 + system2 together
config.py           # picks which system1 backend to use
cli.py              # entrypoint
```

## Extending the gates

There are now three checkpoints, all defined as `ChoiceQuestion` /
`ScoreQuestion` / `NoulQuestion` objects near the top of
`orchestrator.py`:

1. **Scope gate** (`_scope_gate`, before Claude is called at all) —
   `likely_domain` + `change_type`, matched against `modules.json`.
2. **Loop gate** (`_run_step`, after every test run) — `next_action`:
   retry, open a PR, escalate, or abandon.
3. **PR gate** (`_finalize_pr`, once tests pass) — `diff_risk` +
   `safe_to_proceed`, plus the hard `sensitive` block from the matched
   domain.

Add more checkpoints the same way — e.g. a `flaky_test` Choice question
so a known-flaky test doesn't burn a retry, or a `touches_migration`
Noul check inside `_run_step` before a file write is even applied. Each
new question is one extra field in the `state` dict plus one entry in
the `questions` dict passed to `self.s1.decide(...)` — no orchestrator
rewrite needed.

## Notes / caveats

- `MockSystemOne` is a **placeholder for wiring, not for intelligence**.
  Its confidence numbers are illustrative, not calibrated. Don't tune
  production thresholds against it — swap to the real Jev backend
  first, then calibrate against real runs (TypeSafe's own docs
  recommend running Jev in shadow for a week before trusting cutoffs).
- `write_code` / `review_and_fix` return **full file contents**, not
  diffs — simplest thing that works reliably with an LLM. Fine for
  small/medium files; for very large files you may want to move to a
  proper patch format later.
- `--auto-pr` actually runs `git checkout -b`, `git commit`,
  `git push`, and `gh pr create`. Test with `--backend mock` (auto-pr
  off) on a throwaway repo first before pointing this at anything real.
