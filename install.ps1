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
# Run it again any time: what's already there is kept.
#
# Unattended: install.ps1 -Yes [-SkipLlama] [-SkipModel] [-SkipShortcut] [-Images]  (answers every question itself;
#             the ~22 GB image models only with -Images)
param([switch]$Yes, [switch]$SkipLlama, [switch]$SkipModel, [switch]$SkipShortcut, [switch]$Images)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
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
    & curl.exe -L --fail --retry 3 -C - -o $dest $url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $url" }
    if ($sha256) {
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $dest).Hash.ToLower()
        if ($got -ne $sha256.ToLower()) { Remove-Item -LiteralPath $dest -Force; throw "Checksum mismatch for $dest (deleted)." }
        Say '   SHA-256 verified.' 'Green'
    }
}

Say 'FX Workspace - setup' 'White'
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

# ---- 3. Folders --------------------------------------------------------------------------------
$LlamaDir = Join-Path $HomeDir 'llama.cpp'
$ModelsDir = Join-Path $HomeDir 'models'
New-Item -ItemType Directory -Force -Path $LlamaDir, $ModelsDir | Out-Null

# ---- 4. llama.cpp ------------------------------------------------------------------------------
Step 'llama.cpp (runs the chat models)'
if (Test-Path (Join-Path $LlamaDir 'llama-server.exe')) {
    Say "Already installed in $LlamaDir." 'Green'
} elseif (Ask 'Download the newest llama.cpp for this PC?' -Skip:$SkipLlama) {
    $nvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
    if ($nvidia) { & nvidia-smi -L 2>$null | Out-Null; $nvidia = ($LASTEXITCODE -eq 0) }
    $kind = if ($nvidia) { 'cuda-12.4' } else { 'vulkan' }
    Say ("GPU: " + $(if ($nvidia) { 'NVIDIA - the CUDA 12.4 build' } else { 'not NVIDIA - the Vulkan build (AMD, Intel, or any GPU)' }))
    $releases = Invoke-RestMethod -Uri 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15' -Headers $Headers
    $pick = $null
    foreach ($r in $releases) {
        $main = $r.assets | Where-Object { $_.name -match "^llama-b\d+-bin-win-$([regex]::Escape($kind))-x64\.zip$" } | Select-Object -First 1
        if ($main) { $pick = @{ release = $r; main = $main }; break }
    }
    if (-not $pick) { throw "No Windows $kind build found in the recent llama.cpp releases." }
    Say "Release: $($pick.release.tag_name)"
    $zips = @($pick.main)
    if ($nvidia) {
        $rt = $pick.release.assets | Where-Object { $_.name -eq 'cudart-llama-bin-win-cuda-12.4-x64.zip' } | Select-Object -First 1
        if ($rt) { $zips += $rt }
    }
    foreach ($a in $zips) {
        $zip = Join-Path $HomeDir $a.name
        $sha = if ($a.digest -match '^sha256:(.+)$') { $Matches[1] } else { $null }
        Download $a.browser_download_url $zip $sha
        Expand-Archive -LiteralPath $zip -DestinationPath $LlamaDir -Force
        Remove-Item -LiteralPath $zip -Force
    }
    # Some builds unpack into a subfolder: bring llama-server.exe and its DLLs up to local\llama.cpp.
    $exe = Get-ChildItem -LiteralPath $LlamaDir -Recurse -Filter 'llama-server.exe' | Select-Object -First 1
    if ($exe -and $exe.DirectoryName -ne $LlamaDir) { Get-ChildItem -LiteralPath $exe.DirectoryName | Move-Item -Destination $LlamaDir -Force }
    if (-not (Test-Path (Join-Path $LlamaDir 'llama-server.exe'))) { throw 'llama-server.exe was not in the download.' }
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

Write-Host ''
Say 'Done. Start the app with run.bat (or the desktop shortcut).' 'Green'
Say 'Optional: Settings > Voice has a one-click setup; images: .venv\Scripts\python.exe image_setup.py (see README).'
Say 'Cloud models: put API keys in keys.local.bat (created on the first start).'
