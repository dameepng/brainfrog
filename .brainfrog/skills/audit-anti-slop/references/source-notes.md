# Source notes and review criteria

## Provenance

- Inspiration: [miqdadbadjuber/anti-slop](https://github.com/miqdadbadjuber/anti-slop), reviewed 2026-09-24. The repository describes anti-slop as a filter rather than a style guide, distinguishes design direction from quality checks, and includes UI, copy, accessibility, responsive, and comment-focused skills.
- Primary text inspected: [README](https://github.com/miqdadbadjuber/anti-slop/blob/main/README.md), [antislop.md](https://github.com/miqdadbadjuber/anti-slop/blob/main/antislop.md), and [MIT license](https://github.com/miqdadbadjuber/anti-slop/blob/main/LICENSE). The repository's original rules and mandatory reporting protocol are not copied into this skill.

## Practical review lenses

| Lens | Ask | Evidence |
| --- | --- | --- |
| Direction | What makes this specific to the product? | Brief, brand guidance, content, real task |
| Purpose | What does each prominent device do? | Hierarchy, affordance, legibility |
| Honesty | Can factual claims be verified? | Source data, product behavior |
| Function | Can each apparent control be used? | Interaction test, link target |
| Resilience | Does it hold in realistic states? | Narrow viewport, keyboard, loading/error/empty |
| Language | Does the copy say something concrete? | Audience task and product specifics |

## Iconography and Badge Guidelines

Never strip icons or badges blindly under the guise of "anti-slop". Use this taxonomy to distinguish quality design from generic AI patterns:

| Element | Purposeful Design (DO KEEP & REFINE) | Generic AI Slop (DO AVOID & CLEAN) |
| --- | --- | --- |
| **Emojis & Emoticons** | **Strictly prohibited**: Zero unicode emojis or text emoticons. Always substitute with curated open-source vector SVG icons (Lucide, Heroicons, Feather, Tabler). | Raw unicode emojis (🍃, 🚀, ✨, 🔥, 🫖, 🍂) or emoticons (`:)`, `XD`) embedded in headings, buttons, badges, or brand marks. |
| **Icons** | Semantic actions (search, close, copy, cart), status states (success check, warning, info), domain-specific icons (tea leaves, clock, flame, location) that aid visual scanning. Ensure icon stroke weights and scale align with adjacent typography. | "Icon wallpaper": placing arbitrary, unrelated Lucide icons (rocket, zap, sparkles, diamond) in colorful glowing squares above every paragraph just to fill empty space. |
| **Badges / Chips** | Domain metadata: price tags (`Rp 45.000`), status indicators (`Tersedia`, `Musiman`), category tags (`White Tea`, `Tisane`), duration/specs (`45 Menit`, `85°C`), active filter pills, or one focal hero badge. | "Badge fatigue": an identical floating pill badge mechanically stamped above *every single* section title (`[Pill Badge] -> [Heading] -> [Subheading]` repeating across the entire page), or badges carrying empty hype words (`World-Class`, `Revolutionary`, `Next-Gen`). |
| **Cards & Surfaces** | Clear spatial groups with distinct purpose, generous hierarchy, readable contrast, and tactile surface feel (frosted glass, dark mode depth). | "Card-ception": nesting multiple cards with identical translucent borders and shadows inside each other, creating unnecessary visual noise without grouping real data. |
| **Visual Aesthetics** | Rich, curated design: modern typography, tailored HSL color harmonies, smooth glassmorphism, responsive balance, and subtle micro-interactions. | Over-purging all visual charm into a sterile, brutalist monochrome wireframe. Anti-slop elevates craft and character; it does not destroy beauty. |

Audit the rendered output first, then inspect source for causes. A design can be simple without being sterile; visual techniques are acceptable when tied to the product and task. If assessing code comments, remove only redundant narration and keep explanations of non-obvious decisions, invariants, and tradeoffs.

## Skill design constraints from the user

1. Description is the trigger: frontmatter says exactly which requests call for this skill.
2. Build from real expertise: inspect product evidence and source material; do not pose as having years of personal experience.
3. Spend context wisely: keep `SKILL.md` procedural and load this file only for deeper review.
4. Use deterministic scripts: use the local read-only scanner for repeatable candidate checks, then inspect each flag.
5. Vet before running: read scope and side effects of external skills and scripts before using them.

Keep each Markdown file under 500 lines. Put future expanded criteria in `references/`, linked from `SKILL.md`, rather than inflating the main file.
