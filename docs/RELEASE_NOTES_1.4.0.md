# FX Workspace 1.4.0 (beta)

The biggest update so far. FX Workspace gets a new interface, an assistant that learns how you work, your files and
email inside the app, four new places (Notes, Docs, Mail and Compare), a rebuilt Labs, and a way to see exactly how fast
your models are running. Everything still runs on your PC, and nothing changes on it without your OK.

**Updating:** run `FX-Workspace-Setup-1.4.0.exe`. It updates in place and keeps your chats, settings, keys, models and
Labs work exactly as they are. Coming from 1.3.x there is nothing to migrate.

---

## Why "beta"

1.4.0 adds more than any release before it - about a dozen new modules, a new interface and four new appearances.
It passes the full test suite (1,361 tests) and has been used day to day, but a lot of it is new, so it ships as a
beta: expect rough edges, and please tell me what breaks (fuxion64@gmail.com, or an issue on GitHub). Two parts are
labelled Beta inside the app as well: **Labs** (as before) and the **AI-inspired appearances**, which are still being
finished. The rest is meant to be solid - if it isn't, that's a bug.

---

## Highlights

- **A new, calmer interface** - one Customize window, a Home that shows what's waiting for you, new type. The old
  look stays one click away as *Legacy*.
- **It learns how you work** - a profile you approve, memory sorted into topics, and skills it can write for itself.
- **Your files, your mail, your apps** - a searchable knowledge base of your folders, your mailbox over IMAP, and ten
  one-click connectors.
- **Notes, Docs, Mail and Compare** - four new places, all usable by the assistant too.
- **A new Labs** - a home page for your models and a blind check before a fine-tune goes into Workspace.
- **Speed, measured** - real tokens a second on every reply, and a chart of the whole chat.
- **Four AI-inspired appearances (beta)** - for people used to Claude, ChatGPT, Grok or Gemini.

---

## A new interface

The redesign is about calm and room to read.

- **New type, bundled.** Inter for the interface and Source Serif 4 for replies and headings. Both ship inside the app
  (in `static/fonts`), so nothing is loaded from the web - the app still works with no internet at all.
- **Customize** replaces Settings' tabs-inside-tabs with one window and one grouped list: *Assistant* (how it answers,
  system message, memory, skills, knowledge, connectors, proactive help), *App* (appearance, models, voice, images,
  permissions, privacy) and *Network* (web search, network, MCP servers, diagnostics). Search finds any setting.
- **Waiting for you.** Home shows what needs you: tasks due today, drafts the assistant wrote for you, and a profile
  update to check. When nothing is waiting, Home is just the greeting and the message box.
- **A rounder message box**, more space around replies, and clearer buttons - the **+** (add files, your screen or a
  picture) is now a round button and looks nothing like **Tools**.
- **Legacy interface.** Prefer the old layout? Customize > Appearance > Interface > *Legacy*. Everything else in this
  release works the same in both.

### Motion that says what it's doing

The thinking indicator used to be one animation. Now every appearance has its own motion for each kind of work, so you
can tell at a glance what the assistant is doing:

| Work | What you see (Classic, for example) |
|---|---|
| Thinking | its usual motion |
| Reading (the conversation, pages, files) | a slow scan |
| Searching the web | a seeking turn |
| Using a tool | a mechanical, stepped motion |
| Writing the answer | a quicker rhythm |
| Drawing a picture | colour |
| Waiting for you | a slow breath, in amber |

When the work changes, the old motion shrinks away and the new one springs in - a short hand-off instead of a jump,
and quick changes land straight on the newest one. With reduced motion on, the line shows a still icon instead.

---

## Four AI-inspired appearances (beta)

Many people come from another assistant, so 1.4.0 adds four appearances that feel like them. They have their own group
in Customize > Appearance, under the regular ones (Classic, Sunrise, Terminal, Eclipse, Aurora), and are marked
**Beta** while they're finished. They are *inspired by* those apps - not made by, endorsed by or affiliated with any of
them, and they use no logos.

- **Claude** - warm paper and clay, serif replies, the turning ✻ and its whimsical thinking words (around 80 of them -
  Pondering, Percolating, Spelunking...).
- **ChatGPT** - plain white or graphite, a pill-shaped message box, a breathing dot, and an effort slider whose ball
  fills blue and turns purple at Ultra.
- **Grok** - pure black or white, hairlines, sharp type and a spinning ring.
- **Gemini** - soft blue-grey, a blue-to-rose gradient greeting, the sparkle and "Show thinking".

Each one has its own motion for every kind of work, its own greeting, message box and effort slider, and renames and
reorders the sidebar the way its app does:

| Appearance | Sidebar |
|---|---|
| Claude | Search under New chat; Projects, Artifacts (Docs), Scheduled (Routines); the chat list under *Recents* |
| ChatGPT | Search chats; Library (Images), Projects, Tasks (Routines), Canvas (Docs); *Chats* |
| Grok | Chat (New chat), Search; Imagine (Images), Projects, Tasks; *History* |
| Gemini | Search for chats; Gems (Projects), My Stuff, Canvas (Docs), Scheduled actions; *Recent* |

Customize, Labs and the places themselves stay the same in every appearance. Some details - exact colours above all -
are close rather than exact, which is why they're labelled Beta.

---

## It learns how you work

- **About you.** A short profile the assistant keeps across chats - your work, your projects, how you like answers.
  Press *Update from my chats* (Customize > Memory) or let it propose an update every 10 chats. It reads only *your*
  messages, shows the change as a before/after, and **nothing changes until you accept it**. You can edit it by hand.
- **Memory topics.** Saved memories are sorted into About you, Preferences, Projects, Work and Tech. Memories about
  sensitive subjects (health, beliefs, money and similar) are kept but **not used** in answers unless you turn that on.
- **Import and export memory.** Download all of it as a file, or paste in the list another assistant gives you when
  you ask it what it remembers - each line becomes a memory you can review.
- **Skills.** How-to guides the assistant follows for a kind of task, in the open [Agent Skills](https://agentskills.io)
  format (a folder with a `SKILL.md`), so they also work in other assistants that support it. Three ways to get one:
  - ask: *"make a skill for my weekly report"*;
  - after a task of three or more steps that went well, press **Save as skill** and it writes one from what it did;
  - import a `.zip` or folder. Imported skills **start switched off** and are checked for risky content (commands that
    delete, download or send data) before you turn them on.
  Only the skills' one-line descriptions sit in the prompt; a skill's full text is loaded when a request needs it.
- **Search past chats.** The assistant can look up what you decided in an earlier chat instead of asking again.

---

## Your files, mail and apps

### Knowledge

Add folders in Customize > Knowledge - notes, PDFs, Word files, web pages, code - and the assistant searches them and
cites the file and lines it used. The index is a plain full-text index (SQLite FTS5): it needs **no model**, so it never
slows the chat model down, it updates incrementally every 30 minutes, and it skips `node_modules`, `.git` and hidden
folders. Nothing leaves your PC.

### Mail

Your mailbox inside the app, over IMAP and SMTP - Gmail, iCloud and Yahoo (with an app password) and most other
providers. (Outlook.com is being moved off password sign-in by Microsoft, so it may refuse; the app has no Microsoft
sign-in.)

- Read, search, flag, move, reply and keep drafts.
- **Summaries, reply drafts and sorting into tags** by the model you choose.
- HTML emails open in a sandboxed frame with **remote images blocked**, so opening a mail doesn't tell the sender.
- Your password is encrypted for your Windows account (DPAPI) and never written down in plain text.
- The assistant can read and draft emails. **Sending always asks you first - even in god mode.**

### One-click connectors

Customize > Connectors sets up ten common MCP servers in one click each: Folders, Web pages, Time, Git, GitHub, Notion,
Brave Search, Browser (Playwright), Library docs (Context7) and Home Assistant. They install only when you connect them,
from their official sources, and their tools ask before they act, like every MCP tool.

---

## New places

### Notes

To-dos with due dates and times (you get a notification when one is due) and Markdown notes. *Do it with the assistant*
hands a task to a new chat. The assistant uses them too: *"add a task to call Dana on Friday"*, *"jot that down"*,
*"what's on my list?"* - and it adds notes, tasks and docs **by itself** when you ask, on any model, without asking
for permission each time. Replacing an existing note's text still asks, since notes keep no versions.

### Docs

A writing editor with the model as your editor.

- Select text and ask for a change, continue from the cursor, or **proofread** the whole document.
- Every change is a **suggestion** you accept or reject, shown word by word.
- Versions are kept automatically, so you can always go back.
- Export to Markdown, plain text, HTML or PDF; import `.md`, `.txt` or `.html`.
- The assistant can write here too: *"write a cover letter in Docs"*.

### Compare

Send the same prompt to two models, side by side and **blind** - their names show only after you vote. Votes build a
leaderboard (Elo), so over time you learn which model is actually best *for you*. *Combine the best of both* merges the
two answers into one.

### Find models (Customize > Models)

Search Hugging Face for llama.cpp (GGUF) models without leaving the app. It reads your GPU and marks each file as
*fits on the GPU*, *partly* or *too big*, and picks the best file for your PC. Downloads resume if interrupted, are
checked against the published SHA-256, and the model is added and ready to use when they finish.

---

## A new Labs (beta)

Labs - where you train your own small models and fine-tune Qwen 3.5 chat models - is reorganised around what you're
making.

- **Labs Home.** *What should a model learn next?* Three cards start a model - **Answer like I want** (fine-tune a
  chat model), **Write like my text** (train a small GPT) and **Sort my pictures** (train a picture classifier) - each
  saying what it learns from and what it needs. Below, every model you've made, each with its five steps as a bar
  (Data, Design, Train, Check, Use) and a line about what's next.
- **A clearer model page.** A step rail that says what each step holds (the data, the size, how far training got, the
  check result, where it's used), versions as one-click pills, and a way back to Labs Home.
- **Blind check.** Before a fine-tune goes into Workspace, the original model and the fine-tune answer the questions
  that were **held out of training**, shown as *A* and *B* in a random order. You vote on each one - A, B, about the
  same, or both bad - and only then see which was which: *"the fine-tune won 7 of 10"*. *Use in Workspace* follows the
  check (you can skip it). *Side by side* still shows the old before/after view with the expected answers.

---

## Speed, measured

Small models can be fast or painfully slow depending on your GPU, the context length and where the model sits in
memory. 1.4.0 measures it properly.

- **Tokens a second on every reply.** Hover a reply: next to its tokens and time you now see its speed. It's measured
  over the time the model spent **writing**, not the whole reply - so a short answer after a long think still shows
  the model's real speed. The tooltip adds the wait for the first token and how many model calls the reply took.
  (Replies from before 1.4.0 show an estimate, marked with a *~*.)
- **Speed and models.** The new chart button at the top right (or the panel's *This chat* tab) shows the whole chat:
  - average speed, the average wait for the first word, and replies and tokens;
  - tokens a second, reply by reply, against the average;
  - **where each reply's time went** - waiting for the first token, writing, or tools and other steps;
  - each model's replies, speed and wait;
  - **findings** in plain words - *"Speed fell from 41 to 18 tok/s as the chat grew"*, *"it waits 6 s before the first
    word - that is reading the conversation"*, *"3 replies wrote under 5 tok/s - the model may be partly on the CPU"*.
- **Diagnose session.** After a while in a chat (8 replies, or 20 minutes) a small card offers to open all of this
  once - dismiss it and it won't come back for that chat.

---

## Fixes and improvements

- **Favorites** no longer un-tick themselves after saving or loading a model.
- **No white flash** when the app opens - the window starts in your theme's colours.
- **The message box no longer stops taking typing** after a confirmation popup in the desktop app.
- **Scrolling up during a reply** no longer yanks you back down; it follows the reply again when you scroll to the end.
- **Context window menu:** the limit shown is the model's real window, used tokens can't exceed it, clicks land where
  you click, and a new context size applies straight away - no app restart.
- **Close buttons in the desktop app:** the browser, preview and side panels' ✕ - and the picture viewer's and voice
  mode's - were hidden under Windows' own minimise/maximise/close buttons. They're clear of them now.
- **The assistant adds notes, tasks and docs itself** when your message is about them - also on small local models and
  on question-style messages, where it used to have no access to those tools.
- **The effort slider's dots** stay neutral instead of flashing yellow as the handle passes them.
- **Animated icons** for everything new (Mail's flap opens, the to-dos tick, Docs' lines write themselves, Compare's
  scale tips, and the Customize and Labs icons).
- **Layout pass:** squashed number fields in Privacy, a doubled header and misaligned back link in Labs, and other
  small alignment issues found by a full sweep of every page.
- **Tests:** tests that only passed on one particular PC are fixed, and tests that need an optional model skip cleanly
  when it isn't installed.

---

## Privacy and safety

Nothing about the basics has changed: your chats, memory, notes, docs, mail cache, knowledge index and Labs work live in
the app's `data` folder on your PC. New in this release:

- Sensitive memories are kept but not used unless you choose so; your profile changes only when you accept a proposal.
- Mail passwords are encrypted for your Windows account; remote images in emails are blocked; sending mail always asks.
- Imported skills start off and are checked for risky content; skills that came from a task which read untrusted
  content are refused.
- The assistant's new tools treat what they read (mail, web pages, knowledge files) as data, never as instructions -
  and if something it read tried to give it instructions, every change after that asks you first.
- Knowledge search and the model finder's fit check run locally; connectors install only when you connect them.

---

## Known issues and limits

- **AI-inspired appearances:** beta. Colours and some details are close rather than exact, and some sidebar names are
  best guesses at what those apps call things.
- **Mail** is new: it's built on standard IMAP/SMTP and works with the common providers' presets, but unusual servers
  may misbehave - please report them. Gmail, iCloud and Yahoo need an *app password*, not your normal password, and
Outlook.com may refuse password sign-in altogether.
- **Labs:** fine-tunes start from the built-in Qwen 3.5 sizes; a model downloaded with Find models can't be used as a
  fine-tune base yet. Blind-check votes are kept in the app window, not in the Labs files.
- **Speed and models** needs replies made with 1.4.0 for exact speeds; older replies show estimates.
- **Video, video sound and music** remain an advanced, manual setup (see `docs/FEATURES.md`).
- Windows only, as before.

---

## For developers

- New modules: `notes.py`, `docs_store.py`, `mail.py`, `compare_arena.py`, `model_finder.py`, `knowledge.py`,
  `profile_store.py`, `agent_skills.py`, `connectors.py`, with their endpoints in `api/extras.py`.
- Reply metrics (`agent/sessions.metrics_of`) now include `tps`, `gen_s`, `ttft` and `calls`.
- The test suite is at 1,361 tests (`.venv\Scripts\python.exe -m unittest`).

---

## Credits

**Contributors**
- [123timsolotech](https://github.com/123timsolotech) - opened
  [#1](https://github.com/fuxion64-ctrl/FX-Workspace/pull/1): getting the test suite to run off Windows, pointing out
  the stale tests and making the open-folder button fail cleanly. Thank you!

**Ideas and inspiration** (studied, not copied; none of these projects are affiliated with FX Workspace)
- [Odysseus](https://github.com/odysseus-dev/odysseus) - blind model comparison, a hardware-aware model finder,
  email in the workspace, versioned documents.
- [Hermes Agent](https://github.com/nousresearch/hermes-agent) (Nous Research) - skills learned from experience,
  a user profile, searching past chats.
- Claude (Anthropic) - memory topics and sensitive-topic handling, one-click connectors, the Customize layout, the
  open Agent Skills standard ([agentskills.io](https://agentskills.io)), and the Claude-inspired appearance.
- Codex and ChatGPT (OpenAI) - learning a skill from a run that worked, and the ChatGPT-inspired appearance.
- Antigravity (Google) - checking a model's work before using it (Labs), and the Gemini-inspired appearance.
- Grok (xAI) - the Grok-inspired appearance.
- AnythingLLM, Open WebUI and Jan - a local knowledge base as a first-class feature.

**Included software**
- [Inter](https://github.com/rsms/inter) and [Source Serif 4](https://github.com/adobe-fonts/source-serif) -
  SIL Open Font License 1.1.
- The official [Model Context Protocol servers](https://github.com/modelcontextprotocol/servers) and first-party
  servers from GitHub, Notion, Brave, Microsoft (Playwright), Upstash (Context7) and Home Assistant - installed only
  when you connect them, under their own licenses.

Created by **Fuxion** - fuxion64@gmail.com. Suggestions, bug reports and support are always welcome.
