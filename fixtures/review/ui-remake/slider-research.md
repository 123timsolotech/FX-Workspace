# Slider motion research — 2026-10-12

## What was checked

- [Apple slider HIG](https://developer.apple.com/design/human-interface-guidelines/sliders): keep the
  direction familiar, use clear marks/labels and give live feedback as the value changes.
- [Material motion](https://m3.material.io/styles/motion): small controls use fast motion; standard
  motion minimizes bounce; color/opacity effects do not need overshoot. The 120ms settle is our own
  design choice, not a measured Claude timing or an exact Material token.
- [Google CSS animation guide](https://web.dev/articles/animations-guide): favor transform/opacity
  for continuous animation, avoid expensive painting/layout and apply layer promotion sparingly.
- [W3C slider pattern](https://www.w3.org/WAI/ARIA/apg/patterns/slider/): preserve keyboard arrows,
  Home/End, a clear label and meaningful accessible value text.

## Claude recordings and reference limits

- [Claude Ultracode mobile interaction recording](https://60fps.design/shots/claude-ultracode-effort-selection-easter-egg-tap-interaction):
  opened the recording and inspected frames of the dark purple square grid and pixel mascot bursts.
  This is the mobile effort row/easter egg, not the desktop slider. No video downloaded. Muted and
  paused the player at the user's request; muted=true confirmed in the media element.
- [Tristen O'Brien video, effort menu at 00:34](https://www.youtube.com/watch?v=IgIlIWqeT-I&t=34s):
  opened and watched by the research agent. It shows the terminal /effort menu, not the desktop pixel
  slider. It is not evidence of the desktop animation's timing or behavior.
- [Independent desktop-inspired recreation](https://github.com/zanwei/claude-model-selector):
  source reviewed for stationary grid cells, independent light/color phases and top-tier texture.
  This is an unaffiliated experiment, not Anthropic's source. Its magnetic dragging/canvas loop were
  not adopted. Our CSS masks and behavior refinements are generated locally; no code copied.

## Implemented direction

Pointer tracking stays immediate and continuous. Only release/keyboard settles for 120ms. Stable
markers remove the extra pop at every preview boundary. High levels use fixed square cell coordinates,
with five independently phased opacity groups instead of translated/breathing circular wallpaper.
The effect is contained by the selected span, and no sweep or flash triggers on a normal level change.
Same-level fractional input avoids redundant description/limit rebuilding, while accessible text stays
current. This direction is a design inference from the references and the user's feedback.

Exact desktop Claude animation footage was not verified. Hardware FPS and subjective feel in the live
application remain unmeasured. Browser checks verify geometry, motion settings and native dragging.
