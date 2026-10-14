"""
shell_lint.py
=============
Catches POSIX-only shell constructs in a command BEFORE it is shown for
approval or run. This is Windows: `run_powershell` is Windows PowerShell 5.1,
`execute_command` is cmd.exe. A command written for bash fails there - late,
after the user already approved it - so the model gets a corrective hint
instead and makes a Windows-correct call.

lint(command, shell) -> list of {"construct", "hint"}; empty means OK.

Quoted strings are blanked out before matching and command names are only
matched where a command starts (line start, after | ; & && || ( ), so text
such as `Write-Output "use grep"` is not a false positive.
"""
from __future__ import annotations

import re

POWERSHELL = "powershell"
CMD = "cmd"


def _blank_quotes(command: str) -> str:
    """Replace the inside of '...' and "..." with spaces (keeping length)."""
    out, quote = [], None
    for ch in command:
        if quote:
            if ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append(" ")
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


_SEGMENT_SPLIT = re.compile(r"\|\||&&|[|;&(){}\n]")


def _command_words(blanked: str) -> list:
    """(first word, rest) for every command segment."""
    out = []
    for seg in _SEGMENT_SPLIT.split(blanked):
        seg = seg.strip()
        if not seg:
            continue
        parts = seg.split(None, 1)
        out.append((parts[0].lower(), parts[1] if len(parts) > 1 else ""))
    return out


def lint(command: str, shell: str = POWERSHELL) -> list:
    text = command or ""
    blanked = _blank_quotes(text)
    found = []

    def add(construct, hint):
        if not any(f["construct"] == construct for f in found):
            found.append({"construct": construct, "hint": hint})

    # Heredocs and shebangs: whole-command constructs. Matched on the text with
    # quoted strings blanked, so `python -c "print(1 << 3)"` is not a heredoc.
    if re.search(r"<<-?\s*(['\"]|\w)", blanked):
        add("heredoc (<<EOF)", "Write file contents with the write_file tool (append=true for further parts)"
            + (", or a PowerShell here-string: @'<newline>...<newline>'@." if shell == POWERSHELL else "."))
    if text.lstrip().startswith("#!"):
        add("shebang (#!/bin/...)", "There is no bash here; write a PowerShell command instead.")

    if shell == POWERSHELL and re.search(r"&&|\|\|", blanked):
        add("&& / ||", "Windows PowerShell 5.1 has no && or ||. Use `A; if ($?) { B }` to run B only if A "
            "succeeded, or `A; B` to run both.")
    if re.search(r"(?:^|\s)/dev/null\b", blanked):
        add("/dev/null", "Use `$null` in PowerShell (e.g. `> $null`) or `NUL` in cmd.")
    if re.search(r"(?:^|[\s=])/(tmp|usr|etc|home|var)/", blanked):
        add("POSIX path (/tmp, /usr, ...)", "Use Windows paths, e.g. $env:TEMP or C:\\Users\\<name>.")
    if re.match(r"^\s*[A-Za-z_]\w*=\S*\s+\S", blanked):
        add("inline VAR=value command", "Set the variable first: `$env:VAR = 'value'; command`.")
    if shell == CMD and re.search(r"\$\([^)]*\)", blanked):
        add("$(...) substitution", "cmd.exe has no $(...); use run_powershell instead.")

    for word, rest in _command_words(blanked):
        flags = rest.split()
        if word == "rm" and (shell == CMD or any(re.fullmatch(r"-[rRfvid]{2,3}|-f", f) for f in flags)):
            # Verified on Windows PowerShell 5.1: rm <path> and rm -r <path> work
            # (Remove-Item -Recurse); rm -rf fails ("no parameter 'rf'") and
            # rm -f is ambiguous (-Filter / -Force).
            add(f"rm {next((f for f in flags if f.startswith('-')), '')}".strip(),
                "Use the delete_path tool (undoable), or `Remove-Item -Recurse -Force <path>`."
                if shell == POWERSHELL else "Use the delete_path tool (undoable), or `rmdir /s /q` / `del`.")
        elif word == "printf":
            add("printf", "printf does not exist on Windows. To create a file use the write_file tool; "
                "to print text use `Write-Output`.")
        elif word == "export":
            add("export", "Use `$env:NAME = 'value'` (this process) or the set_env_var tool.")
        elif word == "touch":
            add("touch", "Use the write_file tool, or `New-Item -ItemType File <path>`.")
        elif word in ("ls", "dir") and any(f.startswith("-") and set(f[1:]) & set("laRh") and len(f) <= 4 for f in flags):
            add(f"{word} {flags[0]}", "Use the list_dir tool, or `Get-ChildItem -Force` (add -Recurse for subfolders).")
        elif word == "mkdir" and "-p" in flags and shell == CMD:
            # PowerShell 5.1's mkdir -p works (-p = -Path); cmd's mkdir makes a folder named "-p".
            add("mkdir -p", "cmd's mkdir already creates missing parents - drop -p, or use the make_dir tool.")
        elif word in ("grep", "egrep", "fgrep"):
            add(word, "Use `Select-String -Pattern <p> -Path <files>`, or the search_files tool.")
        elif word in ("sed", "awk"):
            add(word, "Use the edit_file / apply_patch tools for file edits, or PowerShell `-replace`.")
        elif word == "which":
            add("which", "Use `(Get-Command <name>).Source`.")
        elif word in ("chmod", "chown", "sudo", "apt", "apt-get", "brew", "yum"):
            add(word, f"{word} does not exist on Windows.")
        elif word == "ln" and "-s" in flags:
            add("ln -s", "Use `New-Item -ItemType SymbolicLink -Path <link> -Target <target>`.")
        elif word == "source" or (word == "." and rest.strip().startswith(("~/", "/"))):
            add("source", "There is no bash profile here; run the PowerShell commands directly.")
        elif word == "cat" and shell == CMD:
            add("cat", "Use the read_file tool, or `type <file>` in cmd.")
        elif word in ("head", "tail") and shell == POWERSHELL:
            add(word, "Use `Get-Content <file> -TotalCount N` / `-Tail N`, or the read_file tool.")
    return found


def hint_message(tool: str, findings: list, shell: str) -> str:
    where = "Windows PowerShell 5.1" if shell == POWERSHELL else "cmd.exe on Windows"
    lines = [f"NOT RUN - this command is written for a POSIX shell, but {tool} runs in {where}. "
             "Nothing was executed and nothing was shown to the user. Rewrite it:"]
    lines += [f"- {f['construct']}: {f['hint']}" for f in findings]
    return "\n".join(lines)
