# Spec: Fixture Login Application

## Goal
Prove the fixture login application's acceptance criteria with real browser interactions in isolated containers.

## Non-goals
- Production authentication.

## Acceptance Criteria

### AC-01 — Login form visible
The login form must be visible.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/login",
  "viewport": {"width": 1280, "height": 720},
  "steps": [
    {"action": "expect_visible", "selector": "#login-form"},
    {"action": "expect_visible", "selector": "#email"},
    {"action": "expect_visible", "selector": "#submit"}
  ]
}
```

### AC-02 — Valid form submission
Valid input must produce the expected UI transition to the dashboard.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/login",
  "steps": [
    {"action": "fill", "selector": "#email", "value": "demo.user@example.test"},
    {"action": "fill", "selector": "#password", "value": "not-a-secret"},
    {"action": "click", "selector": "#submit"},
    {"action": "expect_url_path", "path": "/dashboard"},
    {"action": "expect_text", "selector": "#welcome-message", "contains": "Welcome"},
    {"action": "expect_count", "selector": "#widgets .widget", "count": 3}
  ]
}
```

### AC-03 — Invalid email validation
Invalid email must display an inline validation error and keep the user on the login page.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/login",
  "steps": [
    {"action": "expect_hidden", "selector": "#email-error"},
    {"action": "fill", "selector": "#email", "value": "not-an-email"},
    {"action": "click", "selector": "#submit"},
    {"action": "expect_text", "selector": "#email-error", "contains": "Invalid email", "timeout_ms": 3000},
    {"action": "expect_url_path", "path": "/login"}
  ]
}
```

### AC-04 — Mobile horizontal overflow
The page must not overflow horizontally at mobile width.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/login",
  "viewport": {"width": 375, "height": 667},
  "steps": [
    {"action": "expect_visible", "selector": "#login-form"},
    {"action": "expect_no_horizontal_overflow", "timeout_ms": 2000}
  ]
}
```

### AC-05 — Email validation unit tests
The email validation helper must accept valid and reject invalid addresses.

**Verification:** Test command
**Command:** python -m unittest discover -s tests

### AC-06 — Python sources compile
All Python sources must be syntactically valid.

**Verification:** Build check
**Command:** python -m compileall -q .
