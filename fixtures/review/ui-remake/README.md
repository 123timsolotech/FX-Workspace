# Context and effort UI verification — 2026-10-12

## Current stationary pixel sliders — D60

- Online research and video scope: [slider-research.md](slider-research.md). The actual recording is
  Claude's mobile Ultracode row; the YouTube demo is terminal-only. Exact desktop footage not verified.
- Immediate fractional pointer tracking, 120ms release/keyboard settle, stable 3px markers. Square cells
  stay fixed and shimmer independently in five small opacity-only masked layers on Max/Workflow.
  The selected span contains every cell. No per-move sweep/flash or whole-texture translation.
- Fractional movement avoids rebuilding descriptions/limits unless the preview level changes; accessible
  availability updates immediately even if model capabilities change within the same preview level.
- **76 Node UI checks passed**, including **38 context/effort checks** and two new regressions.
- **468 fresh browser states passed**: six stops x 13 themes x Full/Reduced/Off x 390/1440px. Matching
  rail/range/caption geometry, shared header columns, contained handles/markers/effect, no overflow/errors.
- **Real trusted native drag:** fractional values 0.504 ... 3.975; confirmed Quick throughout input,
  Max on release. Pixel samples show different opacities while their transforms remain none.
- Evidence: `stationary-layouts.json`, `stationary-drag.json`, `stationary-motion.json`,
  `stationary-workflow.jpg`. HTML copies identical, CRLF/no BOM. Settings unchanged from task start:
  `0bf042637c34a41a5dbf67d46a53567362f6b32cd9a7c5e85e7ca65240dc5213`.

Refresh UI. Actual-app subjective mouse feel, hardware FPS and OS preference on hardware unmeasured.
App motion settings and trusted browser pointer behavior checked. Research video muted and paused.

## Historical audited continuous pixel sliders — D59

- Preserves D58's continuous pointer geometry and refined two-layer pixel field. No per-move flash,
  braided lines, label bounce or handle scaling. Release settles to the closest supported fixed stop.
- Shared header columns align selected names across both rows. Reserved status height replaces the old
  caption gap, so fallback no longer changes track position. Final 37px headers and identical track top
  measured for normal/fallback; 320px fallback has no overlap/overflow.
- Two audit fixes: late cancelled input cannot preview stale values, and older real async effort/prep
  replies/errors cannot overwrite the newer model/poll. Generation + mode/model checks guard responses.
- **74 Node checks passed**, including **36 context/effort checks**. Four new regression groups test real
  refresh code with deferred replies/errors and cancellation followed by late input/new gestures.
- **468 fresh browser states passed**: six stops x 13 themes x Full/Reduced/Off x 1440/390px, header value
  alignment, matching rail/native range/caption edges, contained stops/handles, no overflow/removed effects.
- **Real native pointer gesture rechecked after final CSS**: trusted fractional values throughout (0.324,
  0.665 ... 2.626), active Quick stays unchanged until release, then Reviewed. No browser errors recorded.
- Evidence: `aligned-continuous-layouts.json`, `aligned-pointer-drag.json`, `aligned-fallback-geometry.json`,
  `aligned-continuous-workflow.jpg`. Root/static HTML identical, CRLF/no BOM. Settings preserved from task start.

Refresh the app. **Unverified:** actual-app subjective mouse feel and OS reduced motion on hardware.
Real browser-native dragging and app Full/Reduced/Off settings are checked. No backend/model changes.

## Historical continuous pixel verification — D58

- Continuous native step=any: raw pointer position paints the handle/fill; labels preview a fixed level.
  Release selects the closest supported integer stop using raw distance; keyboard/cancel/model changes
  preserve correct settings behavior. Pending saves keep their target and explicit unconfirmed status.
- Restored two calm pixel texture layers on Max/Workflow. Removed braid, charge, travel sweep, label bounce
  and handle scaling. Dragging has no positional transition lag; release settles gently. Reduced/Off stop
  texture animation; no JS animation loop, new dependency or download.
- Rail/input/caption edges match. Native and decorative handles share 24px geometry; all stop centers use
  the same inset/travel equation. Endpoint handles/dots fully inside the rail. Header grid aligns controls.
- **70 Node tests passed**, **32 context/effort**: fractional Effort/Prep movement, polling, raw-distance
  closest supported choice and ties, keyboard, stale drag ownership, cancellation and deferred saves.
- **468 browser states passed**: all six stops x 13 themes x Full/Reduced/Off x 390/1440px. No outside dots,
  header overlap or horizontal overflow. Native/input widths, captions and all stop centers align within
  one pixel; quiet states have no pixel animation. No browser errors recorded.
- **Actual native browser pointer gesture verified**: trusted fractional input values throughout the drag,
  with active Quick unchanged until release, then Reviewed. The hidden input-event trace belongs only to
  the isolated fixture and is kept in page memory; no production logging was added.
- Evidence: `continuous-pointer-drag.json`, `continuous-layouts.json`, `continuous-workflow-phone.jpg`.
  Root/static HTML identical, CRLF/no BOM. Live settings SHA256 preserved from this task's starting hash:
  `028d0a4d2f7ba645dda3291780bc666b8ec355449ed4ea2bc60fb56c0c4ddcae`.

Refresh the app. **Unverified:** dragging in the actual app rather than the isolated browser, OS-level
reduced motion on hardware, and subjective feel with the user's mouse. App motion and native pointer
gesture behavior are checked. No backend/model/effort budgets changed.

## Historical braided-effect verification — D57

- End dots inset 3px and rail clips decoration. Thumb/focus ring remain outside the clipping container.
- Two moving masked filaments and a travelling charge replace the dotted Claude-like pattern: slower
  warm Max, stronger themed Workflow. Small CSS transform/opacity layers; no canvas, RAF/timer, blur,
  new dependencies or downloads. Every changed Effort/Prep stop also has a finite directional impulse.
- Separate pending visual target prevents the save-confirmation bounce. Saving is explicit; actual state
  remains confirmed. Polling preserves the target, model switches ignore old targets and failures roll back.
- **66 Node checks passed**, including **28 context/effort checks**: deferred success/failure, active applied
  level on fallback, model changes, previews/cancellation, Auto, energy and Cross-check independence.
- **468 browser states passed**: six stops x 13 themes x Full/Reduced/Off x 390/1440px. All stop bounds
  inside the rail, clip=hidden; no horizontal overflow, header overlap or lost thumb. Max and Workflow
  run the new effect in Full; all new loops/impulses are none in Reduced/Off. No browser errors recorded.
- `energy-motion.json` records actual intermediate fill/thumb positions and changing filament transforms;
  `energy-layouts.json` records all states; `energy-max-phone.jpg` and `energy-workflow-phone.jpg` show them.
- HTML copies identical, CRLF/no BOM; live settings preserved. Refresh UI. Backend behavior unchanged.

**Unverified:** physical held-pointer dragging, OS-level reduced-motion preference and live hardware
performance. Deterministic native event lifecycle and app Full/Reduced/Off settings were checked.

## Historical polished effort verification — D56

- Claude-reference tracks, larger rounded thumbs, smooth fill/stop/press feedback, clearer purpose captions
  and matching Prompt Prep. Workflow alone has a CSS pixel wave/sweep. Classic lavender, themed elsewhere.
- **53 guarded Python checks passed**: all six profiles, settings API -> actual scripted pipeline limits and
  local model payloads, safe-boundary/session behavior, independent Cross-check, zero-reasoning override.
  The zero override previously became +2048 reply tokens; fixed. Workflow no longer advertises an enforced
  stage/delegation engine that was never implemented; real budgets and capability checks remain.
- **62 Node checks passed**, including **24 context/effort checks**. Feedback survives polls, cancels cleanly,
  activates only for supported fixed Workflow and stops on failed saves/fallback/Auto.
- **468 browser states passed**: six levels x 13 themes x Full/Reduced/Off x 1440/390px. No horizontal overflow
  or heading collision. Highest-label contrast >=5:1 in all themes. Computed pixel transforms/opacity
  actually change in Full; Reduced/Off animation name is none. Static highest-level pattern remains.
- Keyboard, failed save, unsupported fallback and expanded Cross-check On at 320x640 checked with actual
  controls in page memory. Tall menus scroll internally; no browser errors.
- `polished-effort-checks.json` records layouts/styles, `polished-effort-animation.json` records moving
  phases, `polished-workflow-phone.jpg` and `polished-balanced-phone.jpg` show the current controls.
  HTML copies identical, CRLF/no BOM. Protected-write attempts zero; settings unchanged during guarded tests.
  Current SHA256: `eadda3602316856d3a46907cb28cf15f191ede3ae9740531b8202e7684669e21`.
  This differs from historical D55: the live settings file was modified at 12:26:46 UTC, before checkpoint 24
  was created at 12:31:14 UTC. This pass did not write or restore live settings.

Restart backend and refresh UI. **Unverified:** live model quality/speed, real cloud reviews, OS reduced-motion
preference on actual hardware and physical held-pointer dragging. Reduced/Off styles and native event
lifecycles are verified. No stage/delegation engine, new dependency or downloads were added.

## Historical independent Cross-check verification — D55

- Max remains a fixed slider stop; Cross-check is a separate On/Off switch, default Off. It saves only its
  own setting and applies to new eligible Enhanced requests. Both Setup and Agent settings use it.
- The local backend hosts one model at a time; the row explicitly says unavailable. Cloud review requires
  a different configured available model. Failed/invalid second reviews are never reported as checked.
- **52 guarded Python checks passed**, using the real API and pipeline with scripted model replies.
  All six slider choices reach real run limits and local thinking/token settings; session and safe-boundary
  effort changes, independent Cross-check, invalid/blocked second reviews and persistence failures pass.
  Protected-write attempts: **zero**; real settings SHA256 unchanged.
- **59 Node checks passed**, including **21 context/effort checks**. Independent toggle saves, duplicate/
  failed/unconfirmed responses, unavailable/Raw/older-backend descriptions are covered.
- **78 browser layouts**: 13 themes x Full/Reduced/Off x 1440/390px, no overflow/header overlap.
  Expanded On at 320px also fits. Keyboard effort changes leave Cross-check untouched; failed toggle saves
  preserve the choice. No browser errors recorded. All fixture state is in page memory.
- `cross-check-layouts.json` records dimensions/styles; `cross-check-phone.jpg` shows Max with the separate
  Off switch and the local availability message. Root/static HTML identical, CRLF/no BOM.

Restart the backend and refresh the page. Live settings were not changed. **Unverified:** actual cloud
review calls, real model quality/speed and physical held-pointer dragging. No second local runtime or
dedicated Max animation was added.

## Historical slider verification — D54

The user rejected the D53 effort design below. Its `effort.jpg` and slider-grid screenshots are historical,
not the current UI. Context screenshots and the D53 context verification remain applicable.

- Codex-style stepped rails replace the button grid. Budgets, sources, routing and session overrides are
  under More options. There are no dropdowns. Drag previews show the matching description.
- **56 Node checks passed**, including **18 context/effort checks**. New coverage checks failed/unconfirmed
  saves, duplicate requests, switching models while saving, Escape cancellation and Auto with unsupported stops.
- **80 guarded Python checks passed**; protected writes: zero. Live settings SHA256 remains
  `1f25521aafbcafc42595d90bbc8430b3fe4f89b996c071c3bb367a14ec127d54`.
- **78 isolated browser layouts**: 13 themes x Full/Reduced/Off x default desktop and 390px. No horizontal
  overflow or header overlap. Full 160ms, Reduced 60ms, Off 0ms interaction transitions. No slider idle loops.
  Keyboard, pointer click, Auto, unsupported effort and failed-save recovery were checked in page memory.
- `codex-slider-checks.json` records layout dimensions. `codex-sliders-desktop.jpg` and
  `codex-sliders-phone.jpg` show the earlier D54 UI before the separate Cross-check row. Root/static HTML match, CRLF/no BOM.

**Unverified:** physical held-pointer dragging in the live app. Deterministic events cover preview, commit,
cancel and polling. This pass changes no backend/model configuration and makes no performance claim.

## Historical D53 verification

The screenshots use the app's actual CSS, markup and controls in an isolated preview. Model/task state and settings are simulated in memory; the live app was not started or modified by these browser checks.

- **80 Python checks passed:** system-message persistence, turn snapshots, context accounting, modes/personas/effort regressions, and actual settings API disk-failure recovery for compression, effort and Prompt Prep. Temporary settings files only. A write guard recorded **zero attempts** under real `data/` or real settings. The live settings SHA256 stayed `1f25521aafbcafc42595d90bbc8430b3fe4f89b996c071c3bb367a14ec127d54`.
- **51 Node checks passed:** 13 new context/effort event and network regressions, plus the system-message editor and existing voice, globe and interactive-reply checks. Both HTML copies match byte for byte, with CRLF and no BOM; shipped JavaScript parses.
- **156 browser layouts checked:** context and effort panels × 13 palettes × Full/Reduced/Off × desktop and 390px widths. No horizontal overflow. Full interaction transitions are 160 ms, Reduced 60 ms, Off 0 ms. The new sliders have no canvas or idle animation loops.
- **Six extra context states checked at 320px:** compressing, summary present, model not loaded, long model name, busy task and failed save. Compression progress uses the full available row width.
- Browser interactions confirmed keyboard effort changes, Auto, unsupported-level skipping/fallback explanations, session overrides, and keeping the previous compression threshold on a failed save. No browser console errors were recorded.

`layout-checks.json` contains the measured dimensions and computed styles. `context.jpg` and `effort.jpg` show the controls with their surrounding preview.

**Unverified:** real model task quality after switching prompts, microphone behavior with the new prompt, and physical pointer dragging. Automated event tests cover drag preview/commit/cancel and poll races. No latency or model-generation speed improvement is claimed by this UI change.

Restart the backend and refresh the page to load all changes. The settings file was left unchanged; Compact becomes the default through normal settings loading when its new fields are absent.
