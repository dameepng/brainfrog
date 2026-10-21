# Phase 1D — Browser Criteria Specification Syntax

Browser criteria extend the existing `spec.md` format. Nothing in the Phase 1B/1C format changed.

## Example

````markdown
### AC-03 — Invalid email validation
Invalid email must display an inline validation error and keep the user on the login page.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/login",
  "viewport": {"width": 1280, "height": 720},
  "steps": [
    {"action": "expect_hidden", "selector": "#email-error"},
    {"action": "fill", "selector": "#email", "value": "not-an-email"},
    {"action": "click", "selector": "#submit"},
    {"action": "expect_text", "selector": "#email-error", "contains": "Invalid email", "timeout_ms": 3000},
    {"action": "expect_url_path", "path": "/login"}
  ]
}
```
````

These fields are read from the Markdown:

- **`**Verification:** Browser`** marks the criterion as a browser criterion.
- **`**Browser Plan:**`** is followed by one fenced JSON block.
- **`**App Profile:**`** is optional. It takes an approved profile **ID** only; the default comes from the CLI's `--app-profile`, which defaults to `python-static`.

## Plan document

| Key | Type | Rule |
| :--- | :--- | :--- |
| `path` | string | Initial navigation; root-relative (see Paths). Default `/`. |
| `viewport` | object | `width` 320–3840 (int), `height` 320–2160 (int), `device_scale_factor` 1.0–3.0. Default 1280×720 @1. |
| `steps` | array | 1–50 step objects; executed in order |

Unknown keys are rejected at every level. The whole plan must be at most 64 KiB. The worst-case budget must be at most 120 s: 15 s for the initial navigation plus the sum of every step's `timeout_ms`.

## Steps

Every step object takes `action` (required), plus optional `timeout_ms` (100–15000, default 5000) and optional `id` (`[A-Za-z0-9_-]{1,64}`, unique).

### Actions (never counted as assertions)

| action | required keys | Behaviour |
| :--- | :--- | :--- |
| `goto_relative` | `path` | Navigate within the application origin |
| `fill` | `selector`, `value` (string ≤ 1000) | Type into exactly one element. The value is never recorded in evidence; only its length is. |
| `click` | `selector` | Click exactly one actionable element |
| `select_option` | `selector`, `value` (string) | Select an `<option>` by value |
| `check_checkbox` | `selector` | Check exactly one checkbox |

### Assertions (drive the verdict)

| action | required keys | PASS condition (polled until `timeout_ms`) |
| :--- | :--- | :--- |
| `expect_visible` | `selector` | Exactly one match, and it is visible |
| `expect_hidden` | `selector` | No visible matches (absent counts as hidden) |
| `expect_text` | `selector` + exactly one of `contains` (non-empty) / `equals` | Exactly one **visible** match; its `innerText` contains the value, or equals it after whitespace normalization. Hidden text never satisfies it. |
| `expect_count` | `selector`, `count` (0–1000) | Number of attached matches equals `count` |
| `expect_url_path` | `path` | Page is on the approved origin and the URL path equals `path` |
| `expect_no_horizontal_overflow` | — | `Page.getLayoutMetrics` (CDP): content width ≤ viewport client width (+0.5 px) |

A plan with **zero assertions is rejected** as a false browser proof, so the criterion is BLOCKED and never executed.

### Rejected actions

These are rejected with "forbidden action":

```text
evaluate, execute_script, javascript, eval, script, shell, python,
arbitrary_network_request, exec, download, upload, screenshot
```

Any other unknown action, including case variants such as `Click`, is also rejected.

Screenshots are evidence that BrainFrog captures itself. A screenshot is never a plan step and never an assertion.

## Selectors

Only **CSS** selectors are accepted, and they are passed to Playwright as `css=<selector>`. These are rejected:

- empty selectors, or more than 256 characters;
- control characters;
- `<`, `javascript:`, `eval(`;
- selector-engine prefixes (`text=`, `xpath=`, `internal:role=`, …);
- XPath-shaped selectors (`//…`, `..`, `(…`);
- `>>` chaining.

## Paths

Paths must:

- start with `/`, but not `//`;
- contain no `://`;
- use only URL path, query and fragment characters (no whitespace, backslashes or controls);
- contain no `..` segment (including `%2e%2e` forms) in the path part;
- be at most 512 characters.

Absolute URLs are never accepted. The runner resolves every path against the single fixed origin `http://bf-app:8080`, which is BrainFrog's choice, not the spec's.

## Not supported

The following are out of scope for this phase: arbitrary scripts, network requests, file upload or download, popups as targets, iframes as targets, cross-browser selection, hover and keyboard shortcuts, visual comparison, and AI judging.
