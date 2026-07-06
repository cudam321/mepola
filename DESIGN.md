# Design System — MEPOLA [MEME · POWER · LAW]

**Source of truth:** `design_handoff_mepola_phosphor_crt/` (approved by the user 2026-07-03).
Read the handoff README + `theme/palette-and-rules.md` before ANY visual change.
The skin is "Vintage OS / Phosphor CRT". **Structure is sacred: reskin only, never recompose.**

## The four color roles (keeping them distinct is the whole game)
| Role | Value | Rule |
|---|---|---|
| Phosphor lime (chrome) | `#93C01F` (`#CBF14E` bright, `#6F8E38` muted, `#A6D63C` ink) | ALL frames, labels, idle glow |
| P&L green | `#3DDC84` | gains only — deliberately ≠ the chrome lime |
| P&L red | `#FF5147` | losses only |
| **Reserved incandescent** | `#F6FFE1` + lime bloom + 2.6s pulse | **≥10× winners ONLY, anywhere in the app. Nothing below 10× may ever use it.** |

Background `#060A05`; panels `rgba(9,15,6,.82)` w/ 1px `rgba(147,192,31,.34)` borders, 2px radius,
`┌`/`┘` corner glyphs; CRT scanlines + vignette + 5.5s flicker; `[ bracket ]` labels; `▍` title prefix.

## Typography
Departure Mono (self-hosted, `public/fonts/`) everywhere; fallback Pixelify Sans → ui-monospace.
`-webkit-font-smoothing: none`; all numbers `tabular-nums`. Tame wide tracking (~.02em; 0 at 8–9px).

## Logo / favicon (the power law AS a glyph)
The mark is the **dot-comet** (verbatim from the reference header, `App.jsx::LogoGlyph` + the favicon
data-URI in `index.html`): three small rising dots (`r` 3/4/5, opacity .35/.55/.78, `#CBF14E`) into
one haloed winner (`r13` halo @0.2 + solid `r8`). It sits before the block-ASCII MEPOLA wordmark.
**Not** the old pixel-M (7 rects) — that was wrong; if you see it, replace it. Header and favicon must
match.

## Text density — explanations go in `[?]` hints, not always-on subtitles
User (2026-07-04): the wall of muted subtitle text under every panel title was overwhelming. Rule:
each panel keeps its TITLE + numbers; its explanation collapses into a small phosphor `[?]`
(`InfoHint.jsx`) that reveals a tooltip on hover/focus. The tooltip is a **portal at `document.body`,
`position:fixed`** — appears instantly (native `title` has a ~1s delay that reads as broken) and never
clips inside `overflow-hidden`/`auto` panels. When adding a panel, prefer a `[?]` over a subtitle line.

## Never
Glassmorphism/blur · violet/purple · gradient buttons · rounded-soft cards · rewording the honest
caveat copy · using the incandescent below 10×.

## Always preserve
The dashed **ideal power-law ghost curve** on the hero (user-mandated) · the `break-even 1.0x` and
`tail ≥10x` lines · the honest caveats verbatim · red/green P&L instant readability.

Caveat placement (2026-07-04, user-mandated): the caveat strip and panel explainer subtitles are
REMOVED from the main chrome. The full caveat text lives verbatim in the controls modal under
"strategy (locked)" — it may move again, but it may never be reworded or deleted from the app.

## Decisions Log
| Date | Decision | Rationale |
|---|---|---|
| 2026-07-03 | Phosphor CRT (MEPOLA v1) adopted; ported natively in c84a073 | User-picked from claude.ai/design handoff after rejecting 3 prior skins; structure-sacred rule from user feedback |
| 2026-07-04 | Caveat strip + panel subtitles removed; caveat → controls modal | User overwhelmed by on-screen text |
| 2026-07-04 | Dot-comet logo + favicon (replaced pixel-M) | User: favicon still showed the M; the real design mark is the dot-comet |
| 2026-07-04 | Explanations → `[?]` portal tooltips (`InfoHint.jsx`) | Declutter; native `title` delay read as broken |
