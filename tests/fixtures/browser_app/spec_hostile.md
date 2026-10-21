# Spec: Hostile Navigation Containment

## Acceptance Criteria

### AC-01 — Escape attempt is contained
A page that tries to navigate to an unapproved origin must not be reported as verified.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/escape",
  "steps": [{"action": "expect_visible", "selector": "#marker"}]
}
```

### AC-02 — Blocked subresources are recorded
Off-origin subresource requests are blocked and recorded; the page's own assertion decides the verdict.

**Verification:** Browser
**App Profile:** python-static
**Browser Plan:**
```json
{
  "path": "/subresources",
  "steps": [{"action": "expect_visible", "selector": "#marker"}]
}
```
