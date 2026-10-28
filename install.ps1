# FX Workspace - first-time setup (run install.bat, which runs this).
#
# 1. Python: finds Python 3.11 or 3.12 (offers to install 3.11 with winget when there is none).
# 2. .venv with the pinned packages from requirements.txt.
# 3. local\ (or AGENTIC_HOME): llama.cpp and models live here (paths.py).
# 4. llama.cpp: the newest Windows build for this PC - CUDA 12.4 with an NVIDIA GPU, else Vulkan, else CPU -
#    from github.com/ggml-org/llama.cpp, checked against GitHub's SHA-256. Asks first.
# 5. A starter chat model (Qwen3 4B, ~2.4 GB, from huggingface.co/Qwen), checked by SHA-256. Asks first.
# 6. models.ini from models.example.ini, with every .gguf in local\models added.
# 7. A desktop shortcut. Asks first.
# 8. Tidy up (update_cleanup.py): packages an older version needed and this one doesn't, __pycache__, old
#    models.ini backups. pip keeps no download cache (it grew by hundreds of MB with every update).
# Run it again any time: what's already there is kept.
#
# Unattended: install.ps1 -Yes [-SkipLlama] [-SkipModel] [-SkipShortcut] [-Images]  (answers every question itself;
#             the ~22 GB image models only with -Images)
# -Update (the setup wizard, updating): also brings the app's own llama.cpp (local\llama.cpp) to the newest build -
#             replaced in place, the old build's files removed; kept as it is if the new one doesn't start.
# -Quiet:     plain line-by-line output, no download progress bars (the setup wizard shows each line).
param([switch]$Yes, [switch]$SkipLlama, [switch]$SkipModel, [switch]$SkipShortcut, [switch]$Images,
      [switch]$Update, [switch]$Quiet)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PIP_NO_CACHE_DIR = '1'          # pip's cache is never reused here - it only grew with each update
$env:PIP_PROGRESS_BAR = if ($Quiet) { 'off' } else { 'on' }
$env:PYTHONUNBUFFERED = '1'          # each line shows as it happens, also on the wizard's progress page
Set-Location -LiteralPath $PSScriptRoot
$App = $PSScriptRoot
$HomeDir = if ($env:AGENTIC_HOME) { $env:AGENTIC_HOME } else { Join-Path $App 'local' }
$Headers = @{ 'User-Agent' = 'agentic-workspace-installer' }

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Ask($question, $default = 'Y', [switch]$Skip) {
    if ($Skip) { Say "$question - skipped"; return $false }
    if ($Yes) { Say "$question - yes"; return $true }
    $hint = if ($default -eq 'Y') { '[Y/n]' } else { '[y/N]' }
    $a = Read-Host "$question $hint"
    if ([string]::IsNullOrWhiteSpace($a)) { $a = $default }
    return $a.Trim().ToUpper().StartsWith('Y')
}
function Download($url, $dest, $sha256) {
    # curl.exe (in Windows 10 and 11) resumes a broken download and shows progress.
    Say "   $url"
    $show = @(if ($Quiet) { '-sS' })                 # an array, even of one: splatting a string passes its letters
    & curl.exe -L --fail --retry 3 @show -C - -o $dest $url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $url" }
    if ($sha256) {
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $dest).Hash.ToLower()
        if ($got -ne $sha256.ToLower()) { Remove-Item -LiteralPath $dest -Force; throw "Checksum mismatch for $dest (deleted)." }
        Say '   SHA-256 verified.' 'Green'
    }
}

Say 'FX Workspace - setup' 'White'

# ---- 0. A downloaded source folder: the setup wizard is the better way -------------------------
# install.bat + run.bat are for working on the code. Someone who downloaded the source zip gets the real app from
# the setup wizard: its own window (no browser), a Start-menu entry, uninstall, and Update now in Settings > About.
if (-not $Yes -and -not (Test-Path (Join-Path $App '.git')) -and -not (Test-Path (Join-Path $App 'desktop-app'))) {
    Say 'This is the source code. The setup wizard installs FX Workspace as a normal Windows app - its own window,' 'Yellow'
    Say 'a Start-menu entry and updates from inside the app.' 'Yellow'
    if (Ask 'Download the newest setup wizard from GitHub and run it instead (recommended)?') {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/fuxion64-ctrl/FX-Workspace/releases/latest' -Headers $Headers
        $a = $rel.assets | Where-Object { $_.name -match '^FX-Workspace-Setup-[\d.]+\.exe$' } | Select-Object -First 1
        if (-not $a -or $a.digest -notmatch '^sha256:(.+)$') { throw 'The newest release has no setup wizard with a checksum - get it from github.com/fuxion64-ctrl/FX-Workspace/releases.' }
        $setup = Join-Path $env:TEMP $a.name
        Download $a.browser_download_url $setup $Matches[1]
        Start-Process -FilePath $setup
        Say 'The setup wizard is open. You can delete this folder once it has finished.' 'Green'
        exit 0
    }
}

Say "App folder:   $App"
Say "Models/tools: $HomeDir  (set AGENTIC_HOME to use another drive)"

# ---- 1. Python ---------------------------------------------------------------------------------
Step 'Python'
$Py = $null
foreach ($v in '3.11', '3.12') {
    try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $Py = @('py', "-$v"); break } } catch {}
}
if (-not $Py) {
    try {
        $ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -in '3.11', '3.12') { $Py = @('python') }
    } catch {}
}
if (-not $Py) {
    Say 'Python 3.11 or 3.12 was not found.' 'Yellow'
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask 'Install Python 3.11 now with winget?')) {
        winget install -e --id Python.Python.3.11 --accept-package-agreements
        Say 'Python is installed. Close this window and run install.bat again.' 'Green'
    } else {
        Say 'Install Python 3.11 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run install.bat again.'
    }
    exit 1
}
Say "Using: $($Py -join ' ')" 'Green'

# ---- 2. The environment ------------------------------------------------------------------------
Step 'Python packages'
$VenvPy = Join-Path $App '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    Say 'Creating .venv ...'
    $PyArgs = @($Py | Select-Object -Skip 1)
    & $Py[0] @PyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
& $VenvPy -m pip install --disable-pip-version-check --upgrade pip | Out-Null
& $VenvPy -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Installing the packages failed (see above).' }
Say 'Packages installed.' 'Green'
& $VenvPy update_cleanup.py packages          # what an older version needed and this one doesn't

# ---- 3. Folders --------------------------------------------------------------------------------
$LlamaDir = Join-Path $HomeDir 'llama.cpp'
$ModelsDir = Join-Path $HomeDir 'models'
New-Item -ItemType Directory -Force -Path $LlamaDir, $ModelsDir | Out-Null

# ---- 4. llama.cpp ------------------------------------------------------------------------------
Step 'llama.cpp (runs the chat models)'
# The newest Windows build of one kind (cuda-12.4 / vulkan): @{ release; zips; build = its b-number }.
function Find-LlamaBuild($kind) {
    $releases = Invoke-RestMethod -Uri 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15' -Headers $Headers
    foreach ($r in $releases) {
        $main = $r.assets | Where-Object { $_.name -match "^llama-b(\d+)-bin-win-$([regex]::Escape($kind))-x64\.zip$" } | Select-Object -First 1
        if (-not $main) { continue }
        $build = [int]($main.name -replace '^llama-b(\d+)-.*$', '$1')
        $zips = @($main)
        if ($kind -like 'cuda*') {
            $rt = $r.assets | Where-Object { $_.name -eq "cudart-llama-bin-win-$kind-x64.zip" } | Select-Object -First 1
            if ($rt) { $zips += $rt }
        }
        return @{ release = $r; zips = $zips; build = $build }
    }
    throw "No Windows $kind build found in the recent llama.cpp releases."
}
# Downloads and unpacks a build into $dest (empty), with llama-server.exe and its DLLs at its top level.
function Get-LlamaInto($pick, $dest) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    foreach ($a in $pick.zips) {
        $zip = Join-Path $HomeDir $a.name
        $sha = if ($a.digest -match '^sha256:(.+)$') { $Matches[1] } else { $null }
        Download $a.browser_download_url $zip $sha
        Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
        Remove-Item -LiteralPath $zip -Force
    }
    # Some builds unpack into a subfolder: bring llama-server.exe and its DLLs up.
    $exe = Get-ChildItem -LiteralPath $dest -Recurse -Filter 'llama-server.exe' | Select-Object -First 1
    if ($exe -and $exe.DirectoryName -ne $dest) { Get-ChildItem -LiteralPath $exe.DirectoryName | Move-Item -Destination $dest -Force }
    if (-not (Test-Path (Join-Path $dest 'llama-server.exe'))) { throw 'llama-server.exe was not in the download.' }
}
# The build number llama-server reports - "version: 0.6.0-dev (build 11541, commit ...)" now, "version: 6912
# (abc123)" in older builds - or 0.
function Get-LlamaVersion($exe) {
    $ErrorActionPreference = 'Continue'            # it prints to stderr, which 'Stop' would turn into an error
    try { $out = & $exe --version 2>&1 | ForEach-Object { "$_" } | Out-String } catch { return 0 }
    if ($out -match '\bbuild\s+(\d+)') { return [int]$Matches[1] }
    if ($out -match 'version:\s*(\d+)\s*\(') { return [int]$Matches[1] }
    return 0
}

$Server = Join-Path $LlamaDir 'llama-server.exe'
if (Test-Path $Server) {
    if ($Update -and -not $SkipLlama) {
        # Same kind as before (a CUDA build has ggml-cuda.dll beside it). Only the files at the top of
        # local\llama.cpp are the build's - PrismML's build in local\llama.cpp\prism stays as it is.
        $kind = if (Test-Path (Join-Path $LlamaDir 'ggml-cuda.dll')) { 'cuda-12.4' } else { 'vulkan' }
        $have = Get-LlamaVersion $Server
        try { $pick = Find-LlamaBuild $kind } catch { $pick = $null; Say "Couldn't check for a newer llama.cpp ($($_.Exception.Message)) - keeping build $have." 'Yellow' }
        if ($pick -and $have -and $pick.build -le $have) {
            Say "llama.cpp is up to date (build $have)." 'Green'
        } elseif ($pick) {
            Say "Updating llama.cpp: build $have -> $($pick.build)"
            $new = Join-Path $HomeDir 'llama.cpp.new'
            if (Test-Path $new) { Remove-Item -LiteralPath $new -Recurse -Force }
            try {
                Get-LlamaInto $pick $new
                if (-not (Get-LlamaVersion (Join-Path $new 'llama-server.exe'))) { throw 'the new build did not start' }
                Get-ChildItem -LiteralPath $LlamaDir -File | Remove-Item -Force
                Get-ChildItem -LiteralPath $new | Move-Item -Destination $LlamaDir -Force
                Say "llama.cpp updated to build $($pick.build); the old build's files were removed." 'Green'
            } catch {
                Say "llama.cpp wasn't updated ($($_.Exception.Message)) - build $have stays." 'Yellow'
            } finally {
                if (Test-Path $new) { Remove-Item -LiteralPath $new -Recurse -Force -ErrorAction SilentlyContinue }
            }
        }
    } else {
        Say "Already installed in $LlamaDir." 'Green'
    }
} elseif (Ask 'Download the newest llama.cpp for this PC?' -Skip:$SkipLlama) {
    $nvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
    if ($nvidia) { & nvidia-smi -L 2>$null | Out-Null; $nvidia = ($LASTEXITCODE -eq 0) }
    $kind = if ($nvidia) { 'cuda-12.4' } else { 'vulkan' }
    Say ("GPU: " + $(if ($nvidia) { 'NVIDIA - the CUDA 12.4 build' } else { 'not NVIDIA - the Vulkan build (AMD, Intel, or any GPU)' }))
    $pick = Find-LlamaBuild $kind
    Say "Release: $($pick.release.tag_name)"
    Get-LlamaInto $pick $LlamaDir
    Say "llama.cpp installed in $LlamaDir." 'Green'
} else {
    Say "Skipped. Put llama-server.exe (and its DLLs) in $LlamaDir later - or use Cloud mode with an API key."
}

# ---- 5. A starter model ------------------------------------------------------------------------
Step 'A chat model'
$have = @(Get-ChildItem -LiteralPath $ModelsDir -Filter '*.gguf' -ErrorAction SilentlyContinue)
if ($have.Count) {
    Say "Found $($have.Count) model file(s) in $ModelsDir." 'Green'
} elseif (Ask 'Download a starter model - Qwen3 4B (2.4 GB, runs on 4 GB+ of VRAM)?' -Skip:$SkipModel) {
    Download 'https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf' `
        (Join-Path $ModelsDir 'Qwen3-4B-Q4_K_M.gguf') '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'
} else {
    Say "Skipped. Put any .gguf model in $ModelsDir, then press Settings > Models > Scan for models."
}

# ---- 5b. Image generation (optional) -----------------------------------------------------------
Step 'Image generation (optional)'
$ImagesDir = Join-Path $HomeDir 'Images'
$gpu = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if (Test-Path (Join-Path $ImagesDir 'manifest.json')) {
    Say "Already set up in $ImagesDir." 'Green'
} elseif (-not $gpu) {
    Say 'Skipped: the image engine needs an NVIDIA GPU (CUDA).'
} elseif (Ask 'Set up local image generation - FLUX.2 klein, Z-Image-Turbo and Krea 2 (about 22 GB, every file checksum-verified)?' 'N' -Skip:($Yes -and -not $Images)) {
    New-Item -ItemType Directory -Force -Path $ImagesDir | Out-Null
    & $VenvPy image_setup.py $ImagesDir
    if ($LASTEXITCODE -ne 0) { Say 'Image setup finished with errors - run it again later: .venv\Scripts\python.exe image_setup.py' 'Yellow' }
} else {
    Say 'Skipped. Later: .venv\Scripts\python.exe image_setup.py   (Settings > Images explains the models)'
}

# ---- 6. models.ini -----------------------------------------------------------------------------
Step 'Model list (models.ini)'
& $VenvPy -c "import model_scan, json; p = model_scan.ensure_ini(); r = model_scan.scan(); print('models.ini:', p); print('added:', ', '.join(a['name'] for a in r['added']) or 'nothing new')"

# ---- 7. Shortcut -------------------------------------------------------------------------------
Step 'Shortcut'
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'FX Workspace.lnk'
if (-not (Test-Path $lnk) -and (Ask 'Put a shortcut on the desktop?' -Skip:$SkipShortcut)) {
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $App 'run.bat'
    $s.WorkingDirectory = $App
    $s.WindowStyle = 7
    $s.Save()
    Say 'Shortcut created.' 'Green'
}

# ---- 8. Tidy up --------------------------------------------------------------------------------
Step 'Tidying up'
& $VenvPy update_cleanup.py tidy

Write-Host ''
Say 'Done. Start the app with run.bat (or the desktop shortcut).' 'Green'
Say 'Optional: Settings > Voice has a one-click setup; images: .venv\Scripts\python.exe image_setup.py (see README).'
Say 'Cloud models: put API keys in keys.local.bat (created on the first start).'
exit 0          # the setup wizard reads the exit code; a probe that failed on the way (py, nvidia-smi) isn't a failure
