# Agentic Workspace

A private, local-first desktop assistant for Windows: it drives your apps
through their real controls, asks before it changes anything, and - new in
this revision - notices what you're doing and offers help on its own, with its
reasoning shown. Roblox Studio is the first deeply integrated app.

Run `run.bat` (or the **Local AI** shortcut on the desktop). It creates a Python 3.11 environment, installs the pinned
dependencies, starts llama-server when available, and opens the UI in a window
of its own (Edge or Chrome in app mode - no tabs, its own taskbar button). It is
still a local page: http://127.0.0.1:8000 opens in any browser on this PC, and
`set APP_WINDOW=0` before run.bat opens a browser tab as before.

It starts without console windows: the **Local AI** shortcut runs `Agentic Workspace.vbs`, which starts
`launcher.pyw` with the environment's pythonw.exe (run.bat hands over to it too, after a brief flash). Only the app
window appears; closing it stops the server, and llama-server too when the launcher started it. Output goes to
`data\logs\server.log` and `data\logs\llama-server.log` (the run before: `*.prev.log`); problems show as a message
box. The very first start (no `.venv` yet) runs run.bat in a window so the setup can be seen. `run.bat console`
runs everything the old way, with the server's output in the window.

### Context window

The ring beside the message box shows how full the chat's context is. Click
it to see what uses the space and to **Compress now**. At 97% (change it in
the same popover or in Settings > Agent) older requests are automatically
replaced by a summary instead of being dropped. The last two requests, and
any task in progress, always stay word for word. The summary is saved with
the chat.

### Effort and Prompt Prep

The ⚡ button opens two sliders. Drag the knob, click anywhere on the bar, or
use the arrow keys: it glides there and settles on the nearest level the
current model can run. Effort is a continuous bar, Prompt Prep a stepped one;
both glow more the higher they go, and each has its own effect at the maximum.
A light also sweeps back from the knob and fades out - an energy comet on
Effort, a scan line on Prompt Prep - drawn in your theme's style.
Reduced motion (Windows animation setting) turns the motion off. When Prompt
Prep adds anything to a message, a small **Prompt Prep** pill appears under it;
open it to see exactly what was added (your own words are always sent
unchanged).

### Home, thinking and slash commands

* **Home** is where the app opens: the message box, a **Continue** card for the
  chat you had open, and your recent chats, projects and routines (with each
  routine's next run and last result). Chats, Projects and Routines all load at
  start in the sidebar, expanded; a section you fold stays folded. Reloading a
  tab while in a chat stays in that chat.
* **Thinking.** Above each reply, while the model thinks, a phrase in your
  theme's voice changes every few seconds (`THINKING_PHRASES` in `index.html`);
  click it to read the reasoning as it streams. Afterwards it reads
  "Thought for Xs" and still opens - the reasoning is saved with the reply.
* **Copy** is on your own messages too (hover a message).
* **`/plan <request>`** - the model writes a numbered plan and nothing runs until
  you **Approve** it. **Edit** changes the steps (one per line) and the edited
  plan is what runs; **Reject** ends it with nothing run.
* **`/design <request>`** - a visual-design answer (HTML with inline CSS/SVG, no
  computer tools). It renders live in a preview beside the chat with
  Preview / Code, Copy and Open in a new tab. The preview is sandboxed and
  offline: it can't reach this app or the network. Any HTML, SVG or CSS code
  block in a reply has a **Preview** button too.

### Web search and reading

Search works with no setup (DuckDuckGo). `search_engine.py` tries providers in
order and moves on when one fails: **Brave** and **Tavily** first when you add a
key (Settings > Web Search - keys are kept in `data/search_keys.json` or the
`BRAVE_API_KEY` / `TAVILY_API_KEY` environment variables, never shown or
logged), your **SearXNG** instance when chosen, then DuckDuckGo, then
DuckDuckGo's instant answers. Searches are paced, retried, cached for 15
minutes, and stop when you press Stop.

* **`web_search`** takes up to 4 phrasings at once, `recency` (set by itself for
  news-like questions), `site` and `region`. Results are merged when they are
  the same page (tracking parameters, `www.`/`m.`/AMP mirrors), and ranked:
  official docs and primary sources up, content farms down, fresh pages up
  for time-sensitive questions.
* **`fetch_page`** reads up to 6 pages in parallel - HTML (the main text:
  headings, lists, tables, code) and PDFs (`pdf_text.py`, no extra library) -
  and keeps the passages about the question when a page is long. A paywall,
  bot check or sign-in is reported as the reason, not guessed around.
* **Citations.** Every result and page gets a number; the model cites claims as
  `[n]`. In the chat `[n]` is a link with a hover card (site, favicon, date,
  title, the passage read) and a **Sources** list sits under the reply. Both
  are saved with the message. The model is told to date time-sensitive facts,
  show both sides when sources disagree, and say so when it found nothing
  reliable.
* **How well confirmed** (`corroboration.py`): sources are counted as independent
  origins, not links - a Yahoo page carrying a Reuters story counts as Reuters (the
  Sources list says "via Reuters"), bbc.com and bbc.co.uk are one newsroom, and copied
  text counts once. Research findings are tagged single source / 2 independent /
  confirmed by N, the report presents single-source claims as "according to…", and
  every report ends with a **How well confirmed** section listing the claims only one
  origin supports.
* **The search row** above a reply shows "Searching “…”", "Reading 5 pages…"
  and the sites as they are read, in your theme's working style; open it to
  see every query and page.
* Questions (not only actions) may search - turn that off with *Search the web
  when a question needs it*. Some sites refuse scripts: then the assistant
  reads or searches in its own browser window (a temporary tab), or uses the
  Wayback Machine's copy.

### Tools, presets and files (the Tools button beside Screen)

Each tool you turn on shows as a chip above the message box and stays on:
**Grounded answers** (search and cite first), **Bias check** (searches both
sides of a claim equally, labels each source's kind - social media, news
outlet, fact-checker, government... - rates how far to trust it and why,
double-checks unusual claims, looks past mainstream outlets when a conflict
looks one-sided, keeps the raw findings and ends with a verdict; with Research
the report gets a "Sources and trust" table - `composer_tools.BIAS_RULES`),
**Research** (a plan you approve,
then 12-48 sources by effort, a structured report with citations, Markdown/PDF
export - `agent/research.py`), **Summarize a link**, **Code runner** (Python
runs on this PC in a temp folder after asking once; JavaScript in a sandboxed
frame), **Canvas** (HTML/SVG opens beside the chat), **Critique** (reviews and
revises; switch between the revision, the first draft and the changes) and
**Compare** (a second model's answer side by side). Attach CSV, PDF, image or
text files (menu, drag and drop, or paste): CSVs get column statistics and
charts (a ```` ```chart ```` block the chat draws). **Presets** save tools,
instructions, model and effort. **Memory** switches on and off in the same menu.

**Persona** (the button beside Tools): who the assistant is and how it talks to you -
a role ("a patient maths tutor") or a manner ("blunt and brief"), as a system message
for every message while it's chosen. **Custom** is the default (empty: its normal
self; Edit to write your own), and **New persona…** makes more, with starting points
(Teacher, Straight talker, Casual friend, Coach, Critic, Senior engineer). A persona
sets tone, role and style - never safety, honesty or what you ask (`personas.py`).

The hidden theme stays hidden: it never appears in Settings, even once found - six
quick clicks on the logo turn it on and off, and its button in the top bar holds its
effects and music. By day it is Judaism Mode; the light/dark button turns it into
**MossadGPT** after dark - an operations console on the Terminal skin (deep navy, a
faint tactical grid, a radar sweep that turns only while the assistant works, a
status strip reading STANDBY / ACTIVE / AWAITING GO with local time and UTC, and a
reticle for the logo) - with the music carried over. While it's on, its own persona
takes Custom's place: IsraelGPT by day, MossadGPT after dark (briefing style: bottom
line first, terse, 24-hour time; it never claims to be or speak for a real agency
and never invents classified information). Neither is ever listed or used outside
the theme.

**MossadGPT's world monitor** (Phase 1, `osint/`): live public data collected on this PC while
MossadGPT is on screen - aircraft around your watched areas (adsb.lol), military aircraft worldwide
(adsb.lol), a worldwide flight snapshot every 20 minutes (OpenSky), 11,000+ satellites including the
ISS and Starlink (orbit data from CelesTrak, positions and passes computed here with `sgp4`),
earthquakes (USGS), space weather and the aurora oval (NOAA), and conflict/protest events coded from
world news (GDELT). No keys, no middleman: each feed is read from its origin through the app's network
route, and kept in `data/osint/osint.db` (7 days, 2 GB by default; `osint_*` settings).
* **The globe** (hand-drawn, no map library; Natural Earth coastlines and GeoNames places are bundled in
  `geo/`): behind the chat it turns slowly inside the radar; the **Ops view** (the globe button in the top
  bar, or Ctrl+Alt+G) makes it the main view - drag, zoom, click a dot for its card (with a link to its
  source and an "Ask" button), a layers panel with each feed's state and age, and a place search. The
  chat moves to a column on the right.
* **The assistant** gets a `world_monitor` tool on MossadGPT's turns only: what's near a place now,
  events in a region over the last hours, tracking a flight or satellite, satellite passes over a place,
  feed status, and turning the globe. Places work in English, Hebrew and Russian. Results are numbered
  sources to cite; GDELT events are reported as single-outlet until confirmed.
* **Voice beside the globe**: in MossadGPT, voice mode's buttons (top right) dock it on the left, on
  the right or along the bottom, or shrink it to a pill in the corner - the globe takes the rest of the
  screen. As each sentence of the reply is spoken, the globe flies to what it names - cities and regions
  (English, Hebrew, Russian), tracked aircraft by callsign or registration, the ISS - with a label, one
  after another. An action waiting for your OK opens the pill back into a panel. The choice is remembered.
* Coming in later phases: the Middle East desk (Home Front Command alerts, news feeds, GPS jamming),
  official public cameras, weather and hazards, ships and infrastructure.

**Claim timelines** (Tools): after a sourced answer or research report, who reported
each claim first and who confirmed it later, in date order - copies of one story are
marked "repeats Reuters - not independent" (`corroboration.timelines`).

### Messages, commands and shortcuts

* Hover a message: **Copy**, **Edit & resend**, **Retry**, **Retry with** another
  model or effort, **Branch** (a new chat from here), **Pin**, **Delete** - the
  assistant's memory of the chat changes with it. Replies show their tokens and
  time; pinned messages are under the pin at the top.
* **Ctrl+F** finds in the chat, **Ctrl+Shift+F** searches all chats, the export
  button saves Markdown, HTML or PDF with sources. Each chat keeps its own draft;
  pressing Enter while a task runs queues the message.
* Slash commands (one list, `SLASH_COMMANDS` in `index.html`; the server's are in
  `agent/tasks.SLASH_COMMANDS`): `/plan`, `/design`, `/research`, `/search`,
  `/summarize`, `/biascheck`, `/explain`, `/fix`, `/translate`, `/tldr`, `/new`, `/clear`,
  `/model`, `/effort`, `/export`, `/branch`.
* **Ctrl+K** opens the command palette; **?** lists every shortcut. **Esc** stops a
  running task when nothing else is open.

### Reliability and organisation

* Your message is saved before its turn runs; if the app stops mid-answer, the
  chat offers to send it again. Failed requests retry with backoff and say so.
* **Usage** (Workspace panel): tokens, searches, pages read and cloud cost for the
  chat, today and the last 7 days (`usage.py`, `data/usage.json`).
* Chats: right-click (or …) to pin, rename, move, archive or delete; drag a chat
  onto a project to move it. Projects can pin files whose text goes with every
  chat in the project. The sidebar edge drags to resize.
* Routines can also be a **request** answered afresh each run (+ in Routines) -
  with web search, e.g. a daily news digest with sources. The routine keeps its
  run history, and you get a notification when a run finishes.

### Appearance and looks (Settings > Appearance)

* **Appearances** come in a light and a dark version: **Classic** (Light / Dark),
  **Sunrise** (Dawn / Dusk), **Terminal** (Lite Light / Lite Dark - mono type,
  prompts and stepped motion, no scanlines or glow; *Phosphor at night* brings back
  the original bright green on black), **Eclipse** (a solar corona on a pale violet
  sky by day, a black one at night) and **Aurora** (frosted glass and pale teal
  light by day, polar night after).
* **The sun/moon button** in the top bar (and **Ctrl+Alt+L**) flips between the two
  halves of the appearance you chose - no need to pick another theme. *Light or dark*
  in Settings sets Light, Dark or **Follow Windows**. Settings saved before this move
  over on their own (`appearance`, `color_mode`, `terminal_phosphor` in
  settings.json; `core/config.clean_appearance`).
* **Looks** (how the workspace is arranged): **Studio** (everyday), **Focus**
  (reading and research: one calm column with a **reading rail** beside a long reply -
  its contents from the headings, how far you are, and the sources it cites with
  "via Reuters" and how many independent origins they come from; narrow the window
  and it sits beside another app, like Roblox Studio, with a live window edge),
  **Operator** (long automations: activity, plan and charts lead; its own console
  colors in Classic, graphite at night and cool grey by day) and **Command** (Studio
  and Operator in one: your chats and the chat, with the live panel - activity, plan,
  load chart, every section open - and Operator's denser text). Companion is gone:
  Focus does its narrow-window job, and a saved Companion opens as Focus.
* **The message bar fits its box** in every look and width (MossadGPT's Ops column,
  a narrow window, a phone): as it narrows, labels give way to icons, and at the
  narrowest the buttons take a second row with the model and Send on the right.

### Live model activity and Diagnostics

* Under the Thinking row, a strip shows what the model is doing right now: the
  model, where it sits (**on the GPU**, **~N% on the CPU**, or **N GB in system
  RAM** when Windows lent RAM to the GPU - slow), and its stage: *loading into
  memory · Ns* (the Thinking row says so too), *reading the conversation*,
  *thinking/writing · N tok/s* or *writing a tool call*. The model menu shows
  the same placement beside the loaded model.
* **Settings > Diagnostics** (runs at startup and on demand) checks the GPU,
  RAM, disks, llama-server and every model in `models.ini`: what each needs in
  VRAM at its context size (weights, image projector, KV cache, working memory -
  read from the GGUF header, `gguf_meta.py`), what would make it fit, and fixes
  them - **Apply all** writes the plan to `models.ini` (the old file is kept as
  `models.ini.bak-<time>`). The dot on Settings means it found a problem.
* The plan: `[*] ngl = auto` with a 256 MB `fit-target`, so llama.cpp's `--fit`
  places each model at load time against the VRAM free right then - everything
  on the GPU when it fits, only the overflow on the CPU, never spilling into
  shared memory; each model gets the largest context (up to what it was trained
  on, at least 16K) that fits with an f16 or q8_0 KV cache; a MoE model too big
  for the card keeps only as many layers' experts in RAM as needed (`n-cpu-moe`),
  read into RAM when it loads (`load-mode = none`). Hybrid models (Qwen 3.5,
  Bonsai 2) stay whole on the GPU (`ngl = 99`): split between GPU and CPU they
  crawl. Context sizes are planned for other apps using up to ~1.1 GB of VRAM.
* Measured on this PC (RTX 3060 Ti 8 GB), tokens/s before -> after: Mistral-7B
  42.5 -> 63.5, DeepSeek-Coder-V2 7.7 -> 18.8 (first reply 115 s -> 13 s),
  Ternary-Bonsai-2-27B 17.5 -> 28.1, Huihui-Qwen3.5-9B 26.2 -> 35.4.
* Tip it shows: NVIDIA Control Panel > Manage 3D settings > llama-server.exe >
  CUDA - Sysmem Fallback Policy: *Prefer No Sysmem Fallback*.
* GPU memory per process is read from Windows' performance counters in-process
  (under a millisecond; it used to start PowerShell every 1.5 s).

### Privacy: temporary chats, the network route and search modes

* **Temporary chat** (sidebar, Home, or `/temp`): not saved to history, a
  project, memory, usage, past runs or the connection log, and never restored
  after a restart or crash. Its files live in one folder under %TEMP% that is
  deleted when it closes, when the app exits, and at the next start. Everything
  else works inside it; **Save this chat** keeps it only when you click.
  Actions it takes outside the chat (a file written, an app changed) still
  happen - the banner says so.
* **Settings > Web search and network › Network** - one route for everything this app sends: Direct,
  the system proxy, your SOCKS5/HTTP proxy (names resolved by the proxy), or Tor
  (9050 Expert Bundle / 9150 Tor Browser), with per-component overrides for
  search, cloud models and the rest. **Kill switch:** if the route goes down,
  traffic is blocked and a red banner says so - it never falls back to direct by
  itself (you can go direct for one search or the session). **Require VPN**
  blocks traffic while no VPN is detected (the app can't switch a VPN on). On
  demand: your public IP through the route, a **leak test** (IP, which DNS
  servers answer, WebRTC, what listens beyond 127.0.0.1) and a speed test.
  The connection log (host, route, size - never the page or query) stays in
  memory unless you choose to save it.
* **Settings > Web search and network › Web search** - modes, each saying what it protects and what
  still leaves this PC: **Standard**, **Private** (no cookies, Referer, tracking
  parameters or personal details in the query; a generic browser signature),
  **Maximum Privacy** (your SearXNG instance through a proxy or Tor; asks before
  anything less private is used), **Local Only** (no network at all - searches
  pages read before, history if on, the project and memory) and **Disabled**.
  Also: a per-message mode in the Tools menu, a preview to edit or cancel each
  query, **Test connection** (reports the route really used), optional history
  and **Clear search history & cache**. No mode is "completely private".
* **Speed indicator** (top bar): download/upload speed for this app or the whole
  PC, the route and search mode, and latency from requests already made; red
  when the kill switch is blocking. It updates once a second only while the
  window is visible; the speed test runs only when you click.

### Voice mode

* The **Talk** orb on Home, the microphone by the message box, or **Ctrl+Alt+V**
  opens voice mode: talk, and it answers out loud. Speech recognition and speech
  playback run on this PC. Transcripts follow your selected Local or Cloud model mode
  and are part of the chat history (unless using a temporary chat). The first time,
  **Download and set up** fetches what the
  chosen engines need (Settings > Voice).
* **What I heard** always shows your recognized words below the orb. With the live
  listener installed, it updates while you speak; the final transcript replaces the
  preview. Reply captions are optional. The orb has a flowing surface, softer motion,
  and a speech level meter. Reduced-motion preferences are respected.
* **Change voices while talking:** open the voice name at the top left. Installed
  voices, speaking speed, volume, reply captions, and hands-free listening are right
  there. Expand **Language & microphone** for language, pause length, sensitivity,
  interruption, and sounds. The phrase already playing finishes; unplayed phrases use
  your new voice or speed. Language changes restart listening and affect new turns;
  replies already underway keep their original language. Uninstalled voices require
  explicit setup in the full settings. The bottom bar lets you type, mute, toggle
  hands-free listening, or return to the chat.
* **Approving by voice**: an action waiting for your OK appears just above the bar.
  Tap Allow or Deny, press **Enter** or **Esc**, or say **"approve"** / **"cancel"**.
  Saying it can approve only low-risk requests - opening or showing something (the
  "benign" tier, plus media keys and arranging windows), up to three actions, with no
  warning on them. Anything that types, clicks, runs code, changes or deletes files,
  sends, downloads or is an MCP tool needs a tap or a key; the server checks this
  again (`approval_broker.voice_ok`), so a misheard word can't approve it. Only the
  whole phrase counts ("I approve of this" doesn't), an "approve" said over its own
  voice doesn't count (it could be the echo), and it never says the word itself.
  "Cancel" always works.
* **Natural voice** (the default): Kokoro, the open voice model closest to the big
  assistants in blind listening tests, with all 28 of its English voices, best first
  (Heart and Bella sound best). Pause between sentences and its own volume are settings.
  Before speaking, text is turned into how a person says it ("$215.94B" -> "215.94
  billion dollars", "+5%" -> "5 percent", dates, units, "e.g."), the first words go
  out after the first clause, later text in longer pieces for natural intonation, and
  the pieces play gaplessly with a breath between them. **Fast** (Piper) stays as an
  option. On this PC: a first sentence in about 2 s, then about 3x faster than real time.
* **Listening**: **Accurate** (NVIDIA Parakeet, ~0.3 s for a sentence, with punctuation)
  or Whisper. **Quick replies** - a second, small streaming listener - notices when you
  finish a sentence, so it answers sooner (English). **Wait after you stop** (short /
  medium / long) and **Microphone sensitivity** tune when it thinks you've started and
  finished. A soft chime marks when it's listening.
* **Languages** (Settings > Voice > Language): English, a language of your own, or
  **Detect** (English plus the languages you pick). Another language is heard by a
  multilingual Whisper model (Small by default, ~1.5 s for a short phrase here) and
  answered in that language by its own Piper voice; in Detect mode the reply comes back
  in whichever you spoke. Spanish, French, German, Italian, Portuguese, Russian,
  Ukrainian, Polish, Dutch, Turkish, Arabic, Hindi and Chinese have voices. **Hebrew** now uses the local Phonikud diacritics/IPA model and Shaul ONNX voice;
  **Russian** uses the Irina Piper voice. Choose **Auto: English · Русский · עברית**
  in the in-call voice menu to detect each utterance and answer aloud in that language.
  The recognized transcript stays visible under the orb, with right-to-left Hebrew support.
  Speech runs on the CPU, leaving GPU memory for the assistant. Short phrases and technical
  names can still be misheard; choose a fixed language when automatic detection is ambiguous.
  Hebrew assets are downloaded under `data/voice/hebrew`, including the tokenizer, so speaking
  does not download anything at runtime. Requirements pin the tested Phonikud/Piper adapters.
  Third-party Hebrew speech assets: https://github.com/phonikud/phonikud-tts (non-commercial license).
  Korean and Japanese have no voice that runs here.
* It hears where you stop talking, starts speaking while the reply is still being
  written (sentence by sentence; code, tables and links are skipped), and in
  hands-free mode listens again when it's done. Talk over it to interrupt. Mute,
  hands-free, speed, language and "read replies aloud" are in Settings > Voice; you
  can also type into voice mode and still hear the answer.
* The orb follows your theme: a glowing sphere (Light/Dark), a radial equaliser
  (Terminal), a corona (Eclipse, Dawn, Dusk), a living blob of light (Aurora), a
  turning star (the hidden theme). It reacts to your voice while listening and to
  its own while speaking. Closing voice mode releases the microphone, including a
  permission request that finishes late. Raw audio is not saved in chat history.

### Local images (Settings > Images)

* Ask for a picture ("draw a lighthouse at dusk", "צייר חתול", "нарисуй кота"), turn on
  the **Image** chip, or type `/image ...`. The image is made on this PC by
  [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) (`sd-server`,
  CUDA 12) and appears under the reply; nothing is sent anywhere. Made images are in
  `data/images/` (a `.json` beside each keeps the prompt, model and seed).
* Three qualities (the assistant picks one, or you set the default):

  | Quality | Model | Licence | Warm 1024x1024 on an RTX 3060 Ti |
  |---|---|---|---|
  | Fast | FLUX.2 klein 4B (Q8_0) - also **edits an attached image** | Apache-2.0 | ~14 s |
  | Balanced | Z-Image-Turbo (Q6_K) - photoreal, readable text | Apache-2.0 | ~27-34 s |
  | Best | Krea 2 Turbo 12B (Q4_K_M, community quantization) | Krea 2 Community License | ~55 s |

* **Files:** `E:\ClankerLocalCpp\Images` (engine + models, ~22 GB). `image_setup.py`
  downloads them from the published sources and checks every file's SHA-256 (a
  mismatch is deleted, never used); `manifest.json` and `setup.log` there list what
  was verified. E: is a hard disk: the first image after a restart waits ~1.5-2 min
  while the model is read; after that Windows keeps it cached. Moving the folder to
  the NVMe drive (C:) and changing *Image folder* would make that ~15 s.
* **The GPU (8 GB) holds one model at a time:** with *Free the GPU* on (the
  default), the chat model is unloaded while an image renders and comes back with the
  next message; the image engine lets go before that message. *Keep ready* sets how
  long the engine stays loaded when idle (2 / 5 / 15 / 60 min); **Unload now** frees it.
* **Filter:** sexual images involving minors are always refused. The *Image filter*
  switch (on by default; Krea 2's licence asks for one) also refuses sexual images of
  real, named people and sexual violence. It reads only the words of the prompt - it
  never touches the picture or its quality.
* Settings > Images has the speed options (warm-up, step cache, the VRAM reserve), the files and
  **Move to another drive…**; the pictures themselves are in the **Images** place in the sidebar.

### The workspace after the revamp

* **Places** in the sidebar: Home, **Images** (make, browse, favourite, edit, compare - drop a picture on the page to
  edit it), Projects and Routines. Alt+1-4 jump between them; **This PC** shows the chat model, the image engine and
  Roblox Studio at a glance.
* **The message box:** **+** adds files, your screen or a picture from Images (attached pictures show as
  thumbnails); **Tools** is searchable (Ctrl+/); **Setup** picks the model, effort and persona (Ctrl+Alt+M). The line
  under it says where the message goes - stays on this PC, out to web searches, or to cloud models.
* **Picks its own tools:** with *Choose tools automatically* (Settings > Agent > Honesty and tools) a question about
  facts turns on grounded web answers, numbers turn on charts, "draw ..." turns on Images - never anything that
  changes your PC. The reply says what it turned on and why.
* **Charts and statistics:** averages, medians, growth, correlation and 8 chart types from numbers in the chat or an
  attached CSV, each with a "Show the numbers" table.
* **Context window:** aligned token counts, a separate model name, automatic compression thresholds and
  readable progress below **Compress now**. Older messages can become a summary while the last two stay in full.
* **Effort and Prompt Prep** (Setup > Effort): continuous pointer sliders with a rounded thumb and a clear selected level; release settles on a fixed choice.
  **Auto** picks effort per request; model defaults and **This session only** remain available. Limits and
  routing are under **More options**. Changes become selected only after the server confirms the save.
  **Max** and **Workflow** have stationary shimmering square pixels in Full motion and static patterns in Reduced/Off.
  The handle follows the pointer immediately between stops, with a quick 120ms settle on release.
  Pixels stay inside the selected track and markers keep their size. A pending save stays at your chosen stop with a
  **Saving** label; failed saves return to the active level.
  **Workflow** offers the largest run budget for sustained multi-step work; all levels show their purpose and real limits.
  **Max** increases the run budget and primary-model review. **Cross-check** is a separate switch (also in
  Settings > Agent), default Off: a different configured cloud model reviews eligible new Enhanced requests.
  Availability is shown; the current local backend runs one model at a time. Failed/unavailable second
  reviews are reported. Changing the slider never enables or disables Cross-check.
  Slider rows share aligned columns; fallback status keeps the track in place. Delayed model polls and
  cancelled pointer events cannot repaint a stale choice.
* **System message** (Settings > Assistant > System message): Compact is the shorter default; Detailed adds
  guidance for longer tasks; Original restores the previous instructions; Custom lets you write your own.
  Preview a preset or **Edit a copy**, set the assistant's name with {NAME}, then **Save changes**. Applies to
  new text and voice turns in Enhanced mode. Your persona and the app's permission checks still apply.
* **Careful mode** (on by default, works with every persona): it says when it doesn't know, fact-checks instead of
  guessing, removes citations that point at nothing and marks answers from memory with *Check it*.
* **The top bar's ⋯** renames, finds, branches, shows pins and exports the chat. **?** lists every shortcut.
* **Errors** say what happened and what to do (Compress this chat, New chat, Try again); the raw text is under
  *Details*. **Approval cards** say what kind of action it is (Looks only / Types or clicks / Changes things).
* **Motion:** each theme's indicator changes with what the assistant is doing - thinking, reading, searching, using a
  tool, writing, drawing or waiting for you. Settings > Appearance > Motion: Full, Reduced (the action's icon, still)
  or Off; text size and high contrast are there too. **Notifications** tell you when a reply is ready while the
  window is in the background.
* **Settings > Diagnostics > Storage** shows what the app keeps on disk, folder by folder, and how full the drives are.
* **Images, faster:** Fast ~13 s, Balanced ~34 s, Best ~39 s warm at 1024x1024 (RTX 3060 Ti); *Move to another
  drive...* copies the models to an SSD and checks every file before switching.

### Research depth, Investment research and Market data

* **Research** now has a depth (Tools menu, or Settings > Web Search): **Quick**
  ~8 sources, **Standard** ~25, **Extensive** ~80, **Ultra-deep** 300-400 (1-3
  hours on a local model). Deep runs keep short notes instead of whole pages, merge
  duplicates, stop a question when new pages only repeat it, cap pages per site, and
  write the report section by section - so a local model's context is never the
  limit. Progress is saved: if it stops, send **resume** with Research on.
* **Investment research** (Tools, or `/invest`): the company's numbers first -
  five years of reported financials, filings, price history, returns, volatility,
  drawdown, P/E and margins, the economy (Treasury yields, inflation, jobs) - then
  web research with every source and finding labelled **FACT** (official),
  **MARKET**, **COMPUTED**, **REPORTED** (news), **ESTIMATE** (forecasts, targets),
  **OPINION**, **SPECULATION** or **PROMOTIONAL**. The report keeps them apart, gives
  a bull and a bear case, the risks, what to watch and how much rests on facts versus
  forecasts versus opinion. It never tells you to buy or sell.
* **Market data** (Tools, or `/market`): quick answers with live figures from the
  same sources (`market_data` tool). Free, no key: SEC EDGAR, Yahoo Finance and Nasdaq,
  US Treasury, BLS, World Bank, ECB. Optional free keys (Settings > Web Search): FRED,
  Alpha Vantage and Finnhub (analyst estimates).

### Roblox Studio: asking it to build

* **Two ways in, both optional.** The app's own **bridge** (the Agentic Bridge
  plugin: `roblox_*` tools - script edits with diffs and undo, a fixed set of
  commands, no arbitrary code) and **Roblox's own Studio MCP** (Settings > MCP >
  `roblox_studio`, running `%LOCALAPPDATA%\Roblox\mcp.bat`, which Roblox installs
  with Studio): `execute_luau`, `multi_edit`, `insert_asset` and more. With one
  Studio window open, the app fills in the `studio_id` and `datamodel_type` every
  Studio MCP call needs, so the model's first call can be the one doing the work.
* **Repeated work is one call and one approval.** "Stack 10 parts" is one
  `execute_luau` script with a loop (the approval card shows the whole script) -
  or, through the bridge alone, one `roblox_batch` call (`repeat: 10` with
  `step: {"Position": [0, 1, 0]}`), never ten calls. Prompt Prep's Expand and
  Plan cards write repeat-style requests the same way ("one script, one loop").
  New things go in Workspace, in a Folder named for them, unless you say where.
* **"Allow all like this"** on an approval card lets the same tools run without
  asking again until your current request is finished; your next message asks
  again. Never for form fills, sending messages, lockdown mode, or a request that
  read content trying to instruct the assistant.
* **Every effort level can change things.** Effort sets how hard it thinks, its
  step and tool-call limits and whether it plans and reviews - never which tools
  it gets or what the system prompt says (a test checks every level). Requests
  like "stack 10 parts", "do it", "no, you run the script" or a short answer to
  its own question now get the tools that change things.
* **Small local models:** the tool list fits the model's window (at most 55% of
  it for the prompt and tools). When everything doesn't fit, Roblox's Studio MCP
  replaces the bridge tools it overlaps, its core tools come first and the rest
  are shortened or left out - Activity says which.

### Studio Overlay and the overlay window

* Settings > Appearance > **Studio layout: Overlay** puts small live panels over
  the Studio look: the model (what it's doing, tokens/s, effort, the tool in
  progress), Roblox Studio (connected, Edit/Play, the selection, the latest
  errors), the network and the session. Drag a panel by its title
  (double-click docks it back), collapse or hide it, choose which ones show,
  the opacity and click-through; **Ctrl+Alt+O** hides or shows them all.
* **Always on top of Roblox Studio** opens the same panels as a small window
  that stays above other windows, starts over Studio's top-right corner and
  moves with it (Windows).
* There is also a hidden theme to find (ARCHITECTURE.md D38 says how).

### Ternary Bonsai 2 27B (PrismML)

`Ternary-Bonsai-2-27B` in `models.ini` points at
`E:\ClankerLocalCpp\Clankers\Ternary-Bonsai-2-27B-PTQ1_0.gguf`. This format
only loads on **PrismML's llama.cpp fork**; stock llama.cpp rejects it. To install:

1. Download `llama-prism-<build>-bin-win-cuda-12.4-x64.zip` from
   https://github.com/PrismML-Eng/llama.cpp/releases.
2. Extract it to `E:\ClankerLocalCpp\prism\`, then copy `cudart64_12.dll`,
   `cublas64_12.dll` and `cublasLt64_12.dll` from `E:\ClankerLocalCpp\` next to it.
3. That's all: `run.bat` starts the newest stock llama.cpp (`E:\ClankerLocalCpp`),
   and picking Ternary Bonsai makes the app restart llama-server on the fork (a
   few seconds); picking another model moves it back, so the rest keep the newest
   llama.cpp. Settings > Models > llama.cpp: Auto, Newest or PrismML (ARCHITECTURE.md D51).

It runs with a 16K window (flash attention, q8_0 KV cache) to fit an 8 GB GPU,
text only (no mmproj installed). See ARCHITECTURE.md D29.

## Architecture

| Module | Responsibility |
| --- | --- |
| `main.py` | Entrypoint only (`uvicorn main:app`); see the packages below and `ARCHITECTURE.md` |
| `api/server.py` | FastAPI app: middleware and every HTTP route, static UI |
| `agent/` | `models.py` model calls (local streamed + constrained, cloud), `loop.py` the agent loop, `tasks.py` turn handling / desktop routes / the orchestrator, `parsing.py` tool-call parsing, `prompts.py` system prompts |
| `tools/` | `schemas.py` tool schemas + per-turn authority, `dispatch.py` every tool's implementation, `runtime.py` registry + executor + `execute_tool_guarded`, `approvals.py` approval path, `files.py` undo journal / atomic writes |
| `core/`, `events.py`, `routing/` | settings + shared state + awareness glue; the audit log and UI event stream; model registry + handoff |
| `effort.py`, `model_caps.py`, `llm_stream.py`, `stream_hub.py`, `undo_store.py`, `approval_view.py`, `shell_lint.py`, `patch_edit.py`, `recorder.py`, `replay_harness.py` | Effort levels as data; detected model capabilities; streaming; the undo trail; approval views; POSIX-command lint; patch engine; run recorder; replay harness |
| `turns.py` | Runs each chat request on a background thread. `POST /api/chat` returns a turn id at once; `GET /api/turn/{id}?wait=20` long-polls for the result; `/api/state` carries the current turn so a reloaded page reattaches to a running task or picks up an answer it missed |
| `run_control.py` | Pause / resume / stop checkpoints before every model call and tool action |
| `history.py` | Fits the conversation into the model's window: demote old screenshots, drop whole old turns, then shorten older tool output inside a long task |
| `model_output.py` | Sets aside `<think>` reasoning so it is never shown or parsed as a tool call; detects replies cut off mid tool call |
| `llama_local.py` | Local model catalog: server reachability (cached, non-blocking for the UI), `models.ini` (cached by mtime), vision capability from `mmproj`, per-model context limits and file sizes |
| `system_metrics.py` | CPU / RAM / GPU telemetry; the UI reads it without blocking while it refreshes in the background |
| `orchestrator.py` | The run pipeline: `TaskState` (immutable original request), effort profiles, `PromptPreparer`, `Planner`, `Reviewer`, and `AgentOrchestrator`, which wraps the existing engine (`chat_with_agent`) as its Executor stage and runs bounded revisions |
| `tool_registry.py` | Typed tool catalogue: input/output schema, tier, read-only, retryable, idempotent, timeout, cooldown, permissions (`GET /api/tools`) |
| `tool_executor.py` | Validates arguments, classifies results (success / retryable / permanent / approval required / cancelled / unavailable), bounded retries with backoff, cooldowns, per-task tool budget, no blind repeats of a failed call, enforced timeouts for isolated tools |
| `model_router.py` | Model registry (capabilities, context, speed, memory, availability) and the controlled `request_model_handoff` operation with validation, loop/limit/cooldown checks and fallback (`GET /api/models/registry`) |
| `messaging.py` | Messaging provider interface and the read / draft / send tools (no provider ships; the in-memory fake used by tests is in `testing_fakes.py`) |
| `window_state.py` | Keeps inspection observational: captures window placement + foreground and restores them; never un-maximizes |

### Agent settings (Settings > Agent)
* **Effort** - Low (direct, no planning, no revisions), Normal (plans multi-step tasks, one revision), High (model plan + model review, two revisions, reasoning models think), Max (larger budgets and up to three revisions). Cross-check is an independent option, requiring a different available cloud model. Each level also sets tool, time and handoff limits and the runtime's native reasoning effort. A clearly complex task at Normal is raised to High; greetings and short questions skip planning and review.
* **Prompt preparation** - Off / Minimal (rules) / Standard / Detailed (model). The original request is always kept verbatim and wins over any prepared notes.
* **Revisions after review** - Auto (effort decides), 0, 1 or 2. An identical set of findings twice stops revising early.
* **Automatic model routing** - Off: the chosen model is never switched (a screenshot is withheld from a text-only model). On: the agent may request a handoff; each one needs approval, is validated, and falls back to the previous model if loading fails.

### Messaging
Read, draft and send are separate tools with separate permissions (Settings > Permissions > Messaging). Drafting never sends; sending takes a saved draft id and can only be "Ask" or "Never". No real provider ships yet - implement `messaging.MessagingProvider` and register it in `messaging.hub_from_env()`.

**Local-only access.** The API rejects requests whose `Host` isn't `127.0.0.1`/`localhost` (DNS rebinding) and state-changing requests from another site's `Origin` (cross-site posts). Add hosts with `AGENTIC_ALLOWED_HOSTS`.

**Local models.** A preset with an `mmproj` line in `models.ini` is treated as vision-capable whatever its name. History is budgeted against the model that will actually run (after any text-to-vision escalation), local replies get 2048 tokens, and a reply cut off mid tool call is retried once with more room.

## What's new in this revision

### Proactive help (`awareness.py`)
The assistant watches a few cheap, local signals every 2 seconds - the app in
focus, input idle time, the open-window list, CPU/RAM/VRAM, free disk space,
new files in Downloads, and the context the Roblox Studio plugin pushes. No
screenshots, no keylogging, no network. When something is worth mentioning it
creates a **suggestion**: a card with a title, one line of detail, the exact
signals behind it (**Why?**), and one to three actions.

| Detector | Fires when | Offers |
| --- | --- | --- |
| Roblox Studio errors | New errors appear in Studio's Output | Diagnose & fix / Just explain |
| Studio bridge not connected | You work in Studio but the plugin is off | Setup steps |
| Frozen apps | A window shows "(Not Responding)" for 12 s | Force close / Wait |
| Error dialogs | An error-looking window comes to the front | Read & explain it |
| Memory pressure | RAM >= 92% or VRAM >= 97% for 40 s | What's using it? / Resource settings |
| Low disk space | A drive has < 10 GB or < 7% free | Scan it with WizTree |
| New downloads | A download finishes | Open / Show folder |
| Window juggling | 8+ switches between two apps in 3 min | Snap them side by side |
| Break reminders | 90 min of continuous activity | Remind me in 10 min |
| Welcome back | You return after 15+ min away | Resume the task / Reopen the app |
| Routines | You usually open an app at this hour | Open it |
| Copied errors (opt-in) | You copy a stack trace | Explain it |

Rules it follows:

* **Suggest, never act.** Nothing runs until you click, and that click still
  passes the normal permission gate. Prompt actions become an ordinary chat
  message, so the "current message grants authority" rule is unchanged.
* **Respectful.** Quiet / Balanced / Eager caps suggestions at 3 / 6 / 12 an
  hour. Duplicates are suppressed, cards expire after 15 minutes, and quiet
  hours silence Windows notifications.
* **Learns from feedback.** Dismissing a kind of suggestion lowers its weight,
  using it raises it, and "Don't suggest this" mutes it (unmute in Settings).
* **Private.** Window titles stay in memory. Only app-usage counts (for
  routines) and feedback weights are saved, in `data/awareness.json`.
  Settings > Proactive help > Clear activity data wipes them.
* **Context for the model.** The live state block now includes a line such as
  "User activity: working in Roblox Studio for 47 min ... editing
  ServerScriptService/KillBrick", so "fix this" means the right thing.

### Roblox Studio
* **The plugin now ships with the app**: `roblox_plugin/AgenticBridge.server.lua`.
  Copy it into Studio's Plugins folder (Plugins tab > Plugins Folder), restart
  Studio, click **Bridge** once and allow HTTP to 127.0.0.1. The on/off choice
  is remembered, so it reconnects on every place load.
* New tools: `roblox_read_script`, `roblox_set_script_source`, `roblox_find`,
  `roblox_get_output`, and a richer `roblox_status` (place, open script,
  selection, playtest state, error count).
* Every change is one ChangeHistory recording: **one Ctrl+Z undoes it**.
* Property values accept friendly JSON (`{"x":0,"y":5,"z":0}`, `"#ff0000"`,
  `"Neon"`, `"Enum.Material.Neon"`, paths for Instance references).
* During a playtest the server/client copies of the plugin only report errors;
  commands always run in the edit DataModel.
* The Roblox tools join the small local-model tool set **only while Studio is
  connected**, so they cost no context otherwise.
* `roblox_places.py` - imported by the tool dispatcher and `desktop_router.py` but
  missing, which stopped the server from starting - now exists. "open Test1"
  resolves `.rbxl`/`.rbxlx` files in Documents/ROBLOX, Desktop, Downloads and
  any folder in `ROBLOX_PLACES_DIRS`.
* The system prompt now carries Luau conventions (GetService, task.*,
  server/client split, RemoteEvents, never trust the client).

### Interface
* New design: warm paper/charcoal palette, serif headings and replies, a quiet
  sidebar, a centred composer with a greeting on the home screen, and a
  collapsible side panel (Progress, Suggestions, Context, System, Audit log).
* **Live steps.** Each tool the assistant runs appears in the reply as it
  happens ("Read script ServerScriptService/KillBrick"), with failures and
  unverified results marked. Expand a step to see its output.
* **Inline approvals.** When the window is focused, approvals are an
  Allow / Deny card in the chat (Enter / Esc). When it isn't, the desktop
  popup appears beside the mouse as before; if you leave the tab with a card
  open, the popup takes over after 15 s.
* Contextual quick actions under the composer follow the app you're in
  (Review the open script, Explain selection, Fix Output errors ...).
* Fully offline: no Tailwind/marked/Google Fonts CDNs. Markdown, code blocks
  with copy buttons, and tables are rendered locally.
* Settings reorganised: General, Appearance (System/Light/Dark, accent,
  density), Model, Resources, Proactive help, Permissions, Privacy.
* The transcript survives a page refresh; if the server restarted, the UI
  says so instead of pretending the model still remembers.

### Privacy and defaults
* **Local mode is the default** for new sessions.
* **Cloud models no longer receive screenshots** unless you turn on
  Settings > Privacy > Send screenshots to cloud models.

### Fixes
* Server failed to start: missing `roblox_places` module.
* Requests like "Add a red part", "Fix my script", "Build ...", "Resume ..."
  were classified as conversation, so no tools were offered. The turn
  classifier now recognises building/fixing verbs, and questions about
  "my script / this place / the selection" get read-only Roblox tools.
* "open C:\path\file.pdf" was treated as an app name; paths and URLs now go
  to the agent (open_file_in_os / open_url).
* `approval.py` crashed on Python builds without Tk; it now degrades to web
  approvals.
* The clarification repair was hard-coded to Proton; it now uses any product
  family defined in `intent_router.PRODUCT_FAMILIES`.
* `test_task_flow` had two tests that could never pass (missing global).
* The UI is served straight from `index.html` (no stale `static/` copy).

### New modules
| File | Purpose |
| --- | --- |
| `awareness.py` | Proactive engine: signals, timeline, detectors, rate limits, feedback learning |
| `win_signals.py` | Foreground app/exe, idle time, side-by-side window snapping (Win32) |
| `approval_broker.py` | Routes approvals to the web card or the desktop popup |
| `roblox_places.py` | Resolves place names to `.rbxl`/`.rbxlx` files |
| `roblox_plugin/AgenticBridge.server.lua` | The Studio plugin |
| `test_awareness.py`, `test_roblox_and_approvals.py` | Offline tests for all of the above |
| `search_engine.py` | Search providers and their fallback, ranking, keys; the `web_search` / `fetch_page` tools |
| `sources.py` | Numbers a turn's results and pages for `[n]` citations |
| `pdf_text.py` | PDF text extraction without extra dependencies |
| `composer_tools.py` | The composer's Tools: turn instructions, attachments, Bias check, Critique, Compare |
| `gguf_meta.py` | Reads a GGUF model's header (architecture, layers, heads, trained context) without loading it |
| `diagnostics.py` | Settings > Diagnostics: VRAM estimates per model, the fit plan for `models.ini`, placement of the loaded model |
| `agent/research.py` | Research: plan card, iterative sources, the report |
| `usage.py` | Tokens, searches, pages read and cloud cost per chat and per day |
| `test_search.py`, `test_composer.py`, `test_messages.py`, `test_polish.py`, `test_diagnostics.py` | Their tests (no network) |

Endpoints added with them: `GET|POST /api/search/keys`, `GET /api/favicon/{domain}`,
`POST /api/files`, `GET /api/files/{id}`, `POST /api/code/run`, `POST /api/chats/{id}/messages`,
`GET /api/usage`, `GET /api/diagnostics?run=1`, `POST /api/diagnostics/fix` (`{"id": "all" | "fit-policy" |
"fit:<model>" | "remove:<model>" | "cleanup"}`). `POST /api/chats/{id}` also takes `pinned` and `archived`;
`POST /api/routines` takes `prompt` and `tools` for a request routine. `GET /api/state` also returns
`placement` (where the loaded model sits) and `diagnostics` (problem counts). Set
`AGENTIC_DISABLE_DIAGNOSTICS=1` to skip the startup check.

Privacy and windows (D38; tests `test_temp_chat.py`, `test_netlayer.py`, `test_search_privacy.py`,
`test_overlay_theme.py`): `POST /api/chats/temporary`, `POST /api/chats/temporary/save`,
`GET /api/search/modes`, `POST /api/search/test`, `POST /api/search/clear`, `GET /api/network`,
`GET /api/network/stats`, `GET|DELETE /api/network/log`, `POST /api/network/session-direct`,
`GET /api/network/vpn`, `POST /api/network/ip`, `POST /api/network/leak-test`, `POST /api/network/speed-test`,
`GET /api/app-window`, `POST /api/app-window/open`. `/api/chat` also takes `temporary_chat` and `search_mode`.
`AGENTIC_DISABLE_NETGUARD=1` skips the socket guard; `AGENTIC_DISABLE_APP_WINDOW=1` keeps the overlay window
from reopening at start.

Voice, research depth and finance (D39; tests `test_voice.py`, `test_research_finance.py`): `GET /api/voice`,
`POST /api/voice/setup`, `POST /api/voice/transcribe` (16 kHz WAV body), `POST /api/voice/speak`,
`POST /api/voice/stop`. `/api/chat` also takes `voice` and `research_depth`. The `market_data` tool (finance.py).
Since D41 (`test_personas.py`): `/api/chat` takes `voice_lang` and `persona`; `/api/voice/speak` takes `lang`;
`/api/voice/transcribe` returns `lang`; `POST /api/approval/{id}` takes `via: "voice"` (low-risk requests only);
`pending_approval` carries `voice_ok`.
Since D42 (`test_studio_tools.py`): `POST /api/approval/{id}` takes `scope: "request"` ("Allow all like this");
`pending_approval` carries `request_ok`. New tool `roblox_batch`; `studio_mcp.py` for Roblox's Studio MCP.

New endpoints: `GET /api/suggestions`, `POST /api/suggestions/{id}/accept|dismiss|snooze`,
`GET /api/awareness/detectors`, `POST /api/awareness/clear`, `POST /api/approval/{id}`,
`POST /api/roblox/context`. `GET /api/state` now also returns `events`,
`pending_approval`, `suggestions`, `awareness` and `roblox`.

Set `AGENTIC_DISABLE_AWARENESS=1` to run without the background observer.

## Undo, backups and recordings

* **Every file change the assistant makes can be undone** — writes, edits, patches,
  copies, moves, renames, deletes, zips, downloads and saved screenshots. Before
  the change, the original is backed up; deleted files and overwritten
  destinations are *moved* into the backup area instead of destroyed. Use
  **Undo** on a step in the Activity panel, or **Undo last change**. If the file
  was changed again afterwards, Undo asks before replacing that newer version.
* **Where:** `data\undo\<change id>\` (backups) and `data\undo\manifest.json`
  (what each change did).
* **How long:** the newest 300 changes, for up to 14 days, up to 2 GB in total —
  the oldest go first. The most recent change is always kept.
* A tool that fails or is stopped halfway is rolled back at once.
* Shell commands (`run_powershell`, `execute_command`, `run_python`) are **not
  undoable**; their approval card says so.
* **Recordings (off by default):** with `record_model_io` on, local runs are saved
  to `data\recordings\` in the replay format (see `fixtures\replay\README.md`).
  They contain your conversation and stay on this computer.

## Tests

```
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

One file: `.venv\Scripts\python.exe -m unittest tests.test_images`. Run them from the repo root.

These mock Windows, media sessions, Studio and the models; `test_replay.py`
replays recorded/reconstructed model replies through the real pipeline
(`fixtures\replay\`). They do not replace a live run on Windows.

## Earlier behaviour (still true)

- **The current message controls tool authority.** Each turn is classified as
  conversation, read-only inspection, or an explicit action. Conversation gets
  no computer tools, inspection gets only read-only tools, and mutation tools
  are exposed only for an explicit action in the current message. Old requests
  remain context but cannot authorize new work. A second host-side gate blocks
  any out-of-scope tool proposal before an approval window can appear.
- **Greetings cannot resume stale work.** `Hi`, `hello`, thanks, and similar
  messages clear pending task/clarification state and receive a direct reply;
  no model, plan, approval, or computer tool is invoked. The compact local
  prompt no longer contains concrete VMware/VirtualBox examples that a small
  model could copy as if they were the current task. Every model call ends with
  the exact current request and its permitted tool mode.
- **Media transport uses the app's background session.** `Play music on
  tidal` and `Stop music in tidal` read TIDAL's own Windows media session,
  leave an already-correct state untouched, and send one explicit Play, Stop,
  or Pause command. The result is re-read without focusing an existing window.
  `Previous song` and `Next song` target the one session that is currently
  playing and never open or focus a player; if multiple sessions are playing,
  no command is sent. A named form such as `Next song in TIDAL` targets only
  that app's session.
  `Skip song` is compiled as `Next`, while `Pause music` targets that same
  unique playing session; neither command falls back to screen inspection or
  asks a playback question.
  Play launches the named app only when no matching media session exists, then
  retries the app-specific session; it never uses a global media toggle.
  Missing/ambiguous sessions, rejected commands, and unchanged state are
  reported as failures; another player's session is never substituted.
  Restart `run.bat` to install the new Windows media bindings.
  This uses Microsoft's [media-session API](https://learn.microsoft.com/en-us/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession)
  through [PyWinRT](https://pywinrt.readthedocs.io/en/latest/types.html).
- **Generic playback resumes only the current queue.** Named song or playlist
  requests still go to the model. A generic Play request does not invent a
  playlist, choose a track, or send a second toggle after an uncertain result.
- **Existing state is reused.** An open focused app is neither relaunched nor
  refocused. An open background app is focused once; a focus failure stops
  input. Connected VPNs and active music do not receive another Connect/Play
  click. Completed/skipped steps remain finished as the plan advances.

- **Requests are compiled before anything opens.** Commas, `and`, and `then`
  split imperative tasks before installed-app lookup. Each compiled task stores
  its action, exact app, and arguments such as `G:`. A malformed compound app
  target cannot be auto-launched through fuzzy matching; fuzzy matches are
  suggestions that require a choice. `Play music on tidal, scan drive G in
  wiztree and establish vpn connection in proton` compiles to three independent
  host actions. Mentioning a VPN connection resolves Proton VPN directly.
- **Multi-part requests keep separate results.** `Stop music in tidal, scan
  drive C in wiztree and connect to proton` retains each result and proceeds past an
  independent music/scan failure. Answering the Proton question resumes only
  that task. The final summary preserves a completed scan even if music failed.
  Explicit `then` chains, a rejected action, and a failed VPN prerequisite stop
  later actions. `Connect to proton
  and scan C with wiztree` preserves the scan when Proton is clarified, then
  proceeds to WizTree after a successful connection. `Open wiztree and scan C`
  carries the app context into the scan task.
- **WizTree scanning has a deterministic driver.** The host opens or reuses
  WizTree, selects the requested drive through its real ComboBox, invokes one
  exact Scan button, and polls live controls for the requested drive and
  completed results. It reports failure if drive selection, the Scan control,
  or completion cannot be verified. No model is involved in this path, and
  model prose cannot mark a scan completed. A bare command such as `Scan drive
  G` is compiled directly to WizTree instead of being handed to the model.
  After a selector action it re-enumerates the live ComboBox before every
  further attempt, first selecting WizTree's actual drive label (for example
  `[G:] System`) and only then expanding to choose one exact live option.

- **Explicit app requests are deterministic.** `Open Proton VPN and connect`
  is resolved against installed Windows apps before any model call. The host
  opens Proton VPN, invokes Connect/Quick Connect through UI Automation, and
  checks for a connected state. It never substitutes a browser search or an
  unrelated app.
- **Ambiguity pauses execution.** `Open Proton` displays Proton VPN, Mail,
  and Drive buttons plus a custom-answer field. Models can use
  the same UI through the `ask_user` tool for other ambiguous tasks.
- **Clarifications follow your attention.** Choices stay in the conversation
  while the workspace is active. If its tab or window is no longer focused,
  the same choices appear in a native popup beside the mouse, safely clamped
  to the current monitor.
- **Clarifications resolve once.** `Connect to proton` asks one question, then
  preserves the original connect action when an option is chosen. Repeated
  questions and malformed character-by-character option lists are blocked.
  Two repeated/unrelated model clarification attempts end the current task
  with an error, instead of exhausting the whole turn in a question loop.
- **VPN state is verified without substring mistakes.** `Not connected` and
  `Disconnected` can never count as success. Proton receives one native UIA
  action, then one physical Quick Connect fallback if UIA caused no change;
  success requires an observed `Connected`/`Disconnect` state. If Proton is
  closed while connecting, the attempt is reported as interrupted; the host
  does not reopen it, click again, or request an irrelevant screenshot.
  `Check VPN status` and `Disconnect from VPN` are direct Proton VPN actions,
  not model prompts; the former reads only a live Connected/Disconnect state
  and the latter sends one native Disconnect command only when connected.
- **Specific models can be forced into memory.** Open **Settings → Local
  model** and select **Load now** beside any model. This loads and pins that
  exact preset. The Resources tab retains its force-load control as well.
- **Live resource use is always visible.** The strip under the header refreshes
  CPU, system RAM, GPU load, total VRAM, model VRAM, and system/other VRAM,
  each with a percentage bar and exact memory amounts where applicable. Direct
  NVIDIA process accounting is preferred; on Windows/WDDM the app next reads
  Windows' dedicated-GPU process counter, then falls back to a measured
  before/after model-load delta (marked with `~`) only if both direct sources
  are unavailable.
  Missing telemetry displays `Unavailable`, never a misleading `0%`.
- **LiteLLM failure is isolated.** Calls go through `llm_compat.py`; local
  llama.cpp has a direct OpenAI-compatible fallback if LiteLLM does not expose
  `completion()`.

Use `python -m unittest smoke_test test_task_flow test_media_session -v` for
offline regression checks. These tests mock Windows execution, media sessions,
and model responses; they do not replace live Windows/TIDAL/Proton/WizTree
integration testing.


### Local tools and multilingual update

`Gemma-4-E4B-Tools` uses Google's standard E4B QAT Q4 GGUF (about 5.15 GB),
registered in `models.ini` with a 16K context. The existing `Qwen-3.5-4B-Q4_K_M`
is the lighter option. Gemma's native tool calls work with llama.cpp's
`--jinja`; no text-token parser workaround is required. Vision remains
available through the existing Gemma E2B vision presets. Missing Qwen3 8B and
Llama 11B registrations were removed; no existing weight files were deleted.

Connected MCP tools are no longer silently dropped after the first eight. Local
prompts name the actual callable tools and distinguish tool execution from spoken
conversation. Explicit Russian/Hebrew action requests get the same tool permissions
as English requests; questions and negations do not grant mutation permission.
Normal application approval checks still apply. Capability caches refresh when the
server's chat template changes, as well as when its build changes.
