"""
The app's module folders (1.4.6). The modules used to sit loose in the app folder; now they're sorted into
folders by what they do, and those folders go on sys.path so every module still imports the others by its plain
name (`import image_gen`). Import this first in anything started directly: main.py, launcher.pyw, app_window.py,
image_setup.py, tests/__init__.py.

  base       paths, version, data files, the event log      chat       the assistant: context, turns, memory
  workspace  notes, docs, knowledge, projects, routines       llm        llama.cpp, the models, speed and benchmarks
  media      images, video, music                             speech     voice in and out
  web        search, pages, the network layer, Tor            connect    MCP, connectors, channels, mail, phone
  pc         Windows control, approvals, the tool runtime     roblox     Roblox Studio
  fxlabs     Fx Labs
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDERS = ("base", "chat", "workspace", "llm", "media", "speech", "web", "connect", "pc", "roblox", "fxlabs")

for _name in reversed(FOLDERS):
    _p = os.path.join(ROOT, _name)
    if _p not in sys.path:
        sys.path.insert(0, _p)
