# BrainFrog TUI — Design Guidelines (v2)

> BrainFrog visual design guidelines. Written for human engineers OR injected into System 2 / Claude Code (e.g. via `BRAINFROG.md`) as mandatory reference whenever an agent touches UI code.

## 0. Changelog from Draft v1

- **Wordmark & splash screen proportions** are now derived from the OpenCode reference specification analysis (951×985px) — **verified programmatically** (see §2) that the ASCII block art aligns and accurately forms the 9 letters `BRAINFROG`, rather than relying on assumptions.
- **Color tokens updated** to values from reference documentation (`#0a0a0a` / `#1e1e1e` / `#00FF66`) — replacing the dimmer values in v1, as this reference comes from direct pixel measurements against visual benchmarks.
- **Footer reverted to pinned bottom-left / bottom-right pattern** (`~` bottom-left, version bottom-right) — v1 suggested merging into the shortcut hint line, which was mistaken and discarded. The pinned-corner pattern is a standard convention in TUIs (vim/tmux status lines) and matches references.
- **Added:** contrast constraints for `text.muted` (see §3.1) and notes on the philosophical tension between "single accent" vs semantic color requirements for errors (see §3.2) — both require deliberate architectural decisions, not guesswork.
- Sections NOT present in reference documentation (chat/history area, loading indicator, error/success banners, menu pickers) are retained from v1 with color tokens adjusted accordingly.

---

## 1. Design Principles

- **Single functional accent, not a rainbow.** Green (`#00FF66`) is the SOLE accent color for active/focus/cursor states. Do not introduce additional hues for "variety" — when visual differentiation is needed, leverage contrast levels or symbols rather than new hues.
- **Serene splash, information-dense history.** The splash screen may remain airy and minimalist; once conversation begins, priority shifts immediately to legibility and information density.
- **Graceful degradation.** Must maintain legibility on 16-color terminals, `NO_COLOR=1`, and non-Unicode terminal emulators. See §7.
- **Low contrast is a deliberate choice, not a default.** If an element is designed with low contrast (footer, placeholder), it MUST be because the element can be safely tuned out. If an element conveys critical information (errors, destructive confirmations), it must never use low-contrast tokens.

---

## 2. Wordmark & Identity — Splash Screen

### 2.1 ASCII Block Art (Verified)

```text
█▀▀▄ █▀▀▄ ▄▀▀▄ ▀█▀ █▄  █   █▀▀ █▀▀▄ ▄▀ ▀▄ ▄▀▀▀
█▀▀▄ █▀▀▄ █▀▀█  █  █ ▀▄█   █▀  █▀▀▄ █▄▄▄█ █ ▀█
▀▀▀  ▀  ▀ ▀  ▀ ▀▀▀ ▀   ▀   ▀   ▀  ▀ ▀   ▀ ▀▀▀▀
```

Programmatically verified: all 3 lines are exactly 46 characters wide. Splitting on empty columns across all three lines simultaneously yields EXACTLY 9 letter groups: `B R A I N` + `F R O G`. The frog eye notch in the letter `O` (group 8: `▄▀ ▀▄` on the top row, `█▄▄▄█` on the middle row) matches the design intent — giving the mark its distinct "FROG" character rather than generic geometric boxes.

**Legibility Note:** Letters `B` (group 1) and `R` (groups 2 & 7) appear nearly identical — differing by only 1 character on the bottom row (`▀▀▀ ` vs `▀  ▀`). While standard for compact block fonts, difference may blur on small terminal fonts. Not a blocker, but if this branding is scaled to smaller formats (favicons, CLI avatars), consider a more distinct variant.

### 2.2 Two-Tone Styling

- `BRAIN` → `color.text.secondary` (`#808080`)
- `FROG` → `color.text.primary` (`#eeeeee`), except for the eye notch in `O` which uses `color.accent.green` (`#00FF66`)

### 2.3 Non-Unicode Fallback

When terminal emulators lack support for box-drawing characters, degrade to:
```text
[ BRAINFROG ]
```
Plain text, preserving two-tone coloring where supported, horizontally centered.

---

## 3. Color System & Tokens

| Token | Hex | ANSI 256 | 16-color fallback | Role / Usage |
|---|---|---|---|---|
| `color.bg.canvas` | `#0a0a0a` | 232 | black | Main background canvas |
| `color.surface.panel` | `#1e1e1e` | 234 | black (bright) | Input panel background |
| `color.accent.green` | `#00FF66` | 46 | bright green | Panel left accent border, cursor, active mode, eye notch on `O` |
| `color.text.primary` | `#eeeeee` | 255 | white | User input text, FROG wordmark, shortcut key labels |
| `color.text.secondary` | `#808080` | 244 | gray | BRAIN wordmark, shortcut descriptions, model name |
| `color.text.muted` | `#555555` | 240 | gray (dim) | Separator dot `·`, footer, empty placeholder — **see constraints in §3.1** |
| `color.error` | `#E5534B` | 167 | red | **New** — error message body. Not present in splash reference; mandatory for conversation states (§6) |
| `color.warning` | `#E3B341` | 179 | yellow | **New** — destructive action confirmations |

### 3.1 Constraints on `text.muted`

Contrast measurements: `#555555` against `#1e1e1e` (panel) = **2.24:1**; against `#0a0a0a` (canvas) = **2.66:1**. Both fall below the WCAG AA threshold (4.5:1 for normal text / 3:1 for large UI). **This is permissible and deliberate** for: footer paths, separator dots, empty placeholders. **STRICTLY FORBIDDEN** for: error messages, confirmations requiring user action, or any content whose omission leads to misunderstanding application state.

### 3.2 Tension: "Single Accent" vs Error Signaling

Principle §1 dictates "single functional accent". However, error/warning states lacking color differentiation risk users missing critical issues. Resolution adopted in this specification: **splash screens and idle states adhere strictly to monochrome + 1 green accent** (matching reference specifications), but **upon entering active conversation states (§6, State 2)**, `color.error` and `color.warning` are permitted — ALWAYS paired with unambiguous symbols (`✗`/`⚠`) rather than relying on color alone, preserving accessibility under 16-color/`NO_COLOR` environments.

---

## 4. Splash Screen Layout (State 0)

Proportions derived from reference analysis (scaled to active terminal dimensions rather than absolute pixels):

| Element | Y Position (from terminal height) | Width |
|---|---|---|
| Wordmark | 38–45% | ~37% canvas width, centered |
| Gap | 45–49% | — |
| Input Panel | 49–58% | 70% canvas width (`int(cols * 0.70)`), centered |
| Small Gap | 58–59% | — |
| Shortcut Bar | 59–61% | Left-aligned with input panel |
| Footer | 96–97% (last row) | Pinned bottom-left & bottom-right corners |

```text
┌──────────────────────────────────────────────────────────┐
│                                                            │
│                    (± 38% top whitespace)                 │
│                                                            │
│              █▀▀▄ █▀▀▄ ▄▀▀▄ ▀█▀ █▄  █   █▀▀ █▀▀▄ ▄▀ ▀▄ ▄▀▀▀ │
│              █▀▀▄ █▀▀▄ █▀▀█  █  █ ▀▄█   █▀  █▀▀▄ █▄▄▄█ █ ▀█ │
│              ▀▀▀  ▀  ▀ ▀  ▀ ▀▀▀ ▀   ▀   ▀   ▀  ▀ ▀   ▀ ▀▀▀▀ │
│                                                            │
│        ╭──────────────────────────────────────────╮       │
│        │▌ Ask BrainFrog… "Explain architecture..."  │       │ ← 70% width
│        │▌ agentic · claude-sonnet-5 · agentic_dev   │       │
│        ╰──────────────────────────────────────────╯       │
│        tab mode   /models pick   /? help   @ file          │
│                                                            │
│                    (flexible bottom spacing)               │
│ ~                                                   v0.1.0 │
└──────────────────────────────────────────────────────────┘
```

### Input Panel Specifications
- Left accent indicator: `▌` or `▎`, color `color.accent.green`, spanning panel height.
- Row 1: placeholder `Ask BrainFrog… "..."` in `color.text.secondary`; switches to `color.text.primary` as the user types.
- Row 2 (metadata): `mode` (accent green) `·` (muted) `model` (white) `·` (muted) `repo` (gray).

### Footer Specifications
- Bottom-left: compact working directory (`~` or `~/tools/agentic_dev`), `color.text.muted`.
- Bottom-right: version string (`v0.1.0`), `color.text.muted`.
- **Keybinding Verification:** Reference documentation lists shortcuts `tab mode` / `/models pick` / `/? help`, whereas earlier project screenshots displayed `tab models` / `ctrl+p help`. Verify active bindings against implementation before finalizing display strings.

---

## 5. Typography & Iconography (All States)

- Box-drawing characters: `╭╮╰╯` (rounded) for standard panels; `┌┐└┘` (crisp sharp corners) for critical modals/overlays — providing an immediate visual distinction between routine panels and decisions requiring user attention.
- Status symbols (always paired with color, never standalone — see §3.2):
  `✓` success · `✗` error · `⚠` warning · `ℹ` info
- Loading spinner: braille frames `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`, colored with `color.accent.green`.
- Brand icon: 🐸 permitted sparingly in compact headers (§6, State 2+), never spammed repeatedly across body prose.

---

## 6. State Lifecycle

**State 0 — Splash/Empty:** Layout per §4. Displayed on initial launch or after `/clear`.

**State 1 — Typing:** Text entered into input panel styled with `text.primary`. Typing `@` triggers file autocomplete popup directly below the cursor. Typing `/` triggers command menu with sharp borders (§5) without displacing surrounding layout elements.

**State 2 — Streaming / Active Conversation:** Splash screen is NOT repeated on every turn. Wordmark collapses into a single-line compact header (`🐸 BrainFrog` or hidden). Layout shifts to top-anchored, scrolling/growing history area:
- User messages: prefixed with `›`, styled with `text.primary`.
- Assistant messages: prefixed with `🐸`, styled with `text.primary`; subordinate details (tool calls, diff blocks) indented 2 spaces in `text.secondary`.
- Loading: braille spinner + status string (`⠋ Reading files...`).
- Errors: bordered container with `color.error`, prefixed with `✗` — requiring color outside the green accent per §3.2:
  ```
  ╭─ ✗ Error ──────────────────────────╮
  │ Failed to connect to Claude API.   │
  │ Check ANTHROPIC_API_KEY in .env    │
  ╰────────────────────────────────────╯
  ```
- Warnings (destructive confirmations): bordered container with `color.warning`, prefixed with `⚠`.

**State 3 — Reset:** `/clear` purges conversation buffer and re-renders State 0 with updated runtime metadata (active model, mode, or repo may have changed).

---

## 7. Responsiveness & Fallbacks

| Condition | Strategy |
|---|---|
| ≥ 80 cols, ≥ 24 rows | Full composition — 3-line wordmark, 70% width panel. |
| 60–79 cols | Panel expands to 85% width (preventing metadata truncation), lateral padding reduced. |
| < 60 cols | Wordmark collapses to `[ BRAINFROG ]` plain text. Metadata condensed to `mode · model`. |
| 256-color / 16-color | `bg`→black, `panel`→234, `accent`→bright_green, `text`→white/gray. |
| Non-Unicode | Left accent line → `|`, separator → `.`, block letters → `#`/`=`/`-`. |
| `NO_COLOR=1` | All colors disabled; states MUST remain distinguishable via symbols (§5). |
| Terminal Resize (SIGWINCH) | Signal listener recalculates horizontal margins and panel width on refresh. |

---

## 8. Acceptance Checklist

**Splash Screen (State 0):**
- [ ] 9-letter wordmark aligns on all supported terminal widths.
- [ ] Input panel spans exactly 70% column width, horizontally centered.
- [ ] Metadata (mode/model/repo) reflects live runtime values, never hardcoded.
- [ ] Footer: cwd bottom-left, version bottom-right, styled in `text.muted`.
- [ ] Shortcut hints match genuinely active keybindings (cross-check §4 notes).

**General (All States):**
- [ ] No UI elements flush against screen edges without margins.
- [ ] All panels rendered with borders rather than raw text against canvas.
- [ ] `text.muted` restricted to decorative elements, never critical information (§3.1).
- [ ] Errors/warnings always combine symbol + color, never color alone (§3.2).
- [ ] Tested under `NO_COLOR=1` and 16-color terminals — all states remain distinguishable.
- [ ] Tested at widths < 60 columns — text wraps cleanly without truncation.
- [ ] Commands `@file`, `/models`, `/help` (or `/?`), `!shell`, and prompt-toolkit history operate smoothly — visual changes only, with zero behavioral disruption.