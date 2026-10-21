# Spec: False Browser Proof

## Acceptance Criteria

### AC-01 — Navigation only
Navigating and taking a screenshot is not proof of functionality.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/login",
  "steps": [{"action": "goto_relative", "path": "/dashboard"}]
}
```

### AC-02 — Screenshot step
A screenshot step is not an assertion.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/login",
  "steps": [{"action": "screenshot"}, {"action": "expect_visible", "selector": "#login-form"}]
}
```

### AC-03 — Arbitrary script
Specs cannot run JavaScript in the browser.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/login",
  "steps": [{"action": "evaluate", "selector": "document.cookie"}, {"action": "expect_visible", "selector": "#login-form"}]
}
```

### AC-04 — External origin
Specs cannot direct the browser to other origins.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "http://169.254.169.254/latest/meta-data/",
  "steps": [{"action": "expect_visible", "selector": "body"}]
}
```
