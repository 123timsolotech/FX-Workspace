# Builds installer\output\FX-Workspace-Setup-<version>.exe
#   1. the version: VERSION (one number for the app, the desktop app and the installer)
#   2. packages the desktop app (desktop\: npm install once, then electron-packager)
#   3. stages the files: what git would publish (tracked + new files that aren't ignored - files the assistant made
#      in your chats are listed in .git\info\exclude) - never data\, keys, .venv, local\ or models - plus the
#      packaged desktop app as desktop-app\, and .fx-manifest: the list of every file installed, so an update can
#      remove the ones a newer version no longer has (and nothing else)
#   4. compiles installer\FXWorkspace.iss with Inno Setup 6 (ISCC.exe)
# To release an update: raise VERSION, run this, publish the .exe on GitHub Releases (tag v<version>).
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Here = $PSScriptRoot
$Stage = Join-Path $Here 'stage\app'

function Find-Tool($names) { foreach ($n in $names) { if (Test-Path $n) { return $n } }; return $null }
$Git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $Git) { $Git = Get-ChildItem "$env:LOCALAPPDATA\GitHubDesktop" -Recurse -Filter git.exe -ErrorAction SilentlyContinue | Where-Object FullName -match 'cmd\\git.exe' | Select-Object -First 1 -ExpandProperty FullName }
$Iscc = Find-Tool @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe")
if (-not $Git) { throw 'git was not found (install Git or GitHub Desktop).' }
if (-not $Iscc) { throw 'Inno Setup 6 was not found: winget install JRSoftware.InnoSetup' }

$Version = (Get-Content (Join-Path $Root 'VERSION') -Raw).Trim()
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION must look like 1.4.0 (it is '$Version')." }
Write-Host "== FX Workspace $Version" -ForegroundColor White

Write-Host '== 1. Desktop app' -ForegroundColor Cyan
$pkgPath = Join-Path $Root 'desktop\package.json'
$pkg = Get-Content $pkgPath -Raw
$synced = $pkg -replace '"version":\s*"[^"]*"', "`"version`": `"$Version`""
if ($synced -ne $pkg) { [IO.File]::WriteAllText($pkgPath, $synced, (New-Object Text.UTF8Encoding $false)) }
Push-Location (Join-Path $Root 'desktop')
if (-not (Test-Path 'node_modules\electron\dist')) { npm install }
npm run pack:win
if ($LASTEXITCODE) { throw 'Packaging the desktop app failed.' }
Pop-Location
$Packed = Join-Path $Root 'desktop\dist\FX Workspace-win32-x64'

Write-Host '== 2. Staging' -ForegroundColor Cyan
if (Test-Path (Join-Path $Here 'stage')) { Remove-Item (Join-Path $Here 'stage') -Recurse -Force }
New-Item -ItemType Directory -Force $Stage | Out-Null
$tracked = @(& $Git -C $Root ls-files -c)
$new = @(& $Git -C $Root ls-files -o --exclude-standard)
# static/ ships whole: besides the page copy (rewritten at start) it holds scripts the page loads (system-messages.js)
# The test suite stays in the source only (1.4.2): ~120 files the app never runs. An update removes the ones older
# installs put there (.fx-manifest). So does the appearance audit (tools/appearance_audit.js, a developer's check).
$skip = '^(installer/|desktop/node_modules/|desktop/dist/|data/|local/|\.venv/|test_[\w]+\.py$|smoke_test\.py$|tools/appearance_audit\.js$)'
$n = 0
foreach ($f in ($tracked + $new | Sort-Object -Unique)) {
    if ($f -match $skip) { continue }
    $src = Join-Path $Root $f
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { continue }
    $dst = Join-Path $Stage $f
    New-Item -ItemType Directory -Force (Split-Path -Parent $dst) | Out-Null
    Copy-Item -LiteralPath $src -Destination $dst
    $n++
}
Copy-Item $Packed (Join-Path $Stage 'desktop-app') -Recurse
# Every script the page loads by itself (src="/name.js", served from static\) must ship - a missing one broke
# Settings on every page in the installed app.
$missing = Select-String -Path (Join-Path $Root 'index.html') -Pattern 'src="/([\w.\-]+\.js)"' -AllMatches |
    ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique |
    Where-Object { -not (Test-Path (Join-Path $Stage "static\$_")) }
if ($missing) { throw "The page loads scripts the installer wouldn't include: $($missing -join ', ')" }
# Tor (1.4.4): the Tor Project's Expert Bundle for Windows, its signature checked against the Tor Browser
# developers' pinned key (installer\verify_tor.py) before anything is staged - the build stops on a bad one. Cached in
# installer\cache\tor\<version>\ (the app run from source uses the same copy). Shipped: tor.exe, the GeoIP data,
# lyrebird (obfs4 and Snowflake bridges) and the bundle's licences - not the relay key tool or Conjure.
$TorVersion = (Get-Content (Join-Path $Here 'tor.version') -Raw).Trim()
$TorCache = Join-Path $Here "cache\tor\$TorVersion"
$TorUrl = "https://dist.torproject.org/torbrowser/$TorVersion/tor-expert-bundle-windows-x86_64-$TorVersion.tar.gz"
New-Item -ItemType Directory -Force $TorCache | Out-Null
$tgz = Join-Path $TorCache 'bundle.tar.gz'
if (-not (Test-Path "$TorCache\x\tor\tor.exe")) {
    Invoke-WebRequest $TorUrl -OutFile $tgz -UseBasicParsing
    Invoke-WebRequest "$TorUrl.asc" -OutFile "$tgz.asc" -UseBasicParsing
    Invoke-WebRequest 'https://keys.openpgp.org/vks/v1/by-fingerprint/EF6E286DDA85EA2A4BA7DE684E2C6E8793298290' -OutFile "$TorCache\signing-key.asc" -UseBasicParsing
    $Py = Find-Tool @((Join-Path $Root '.venv\Scripts\python.exe'))
    if (-not $Py) { $Py = (Get-Command python).Source }
    $PyLib = Join-Path $Here 'cache\pylib'
    if (-not (Test-Path "$PyLib\pgpy")) { & $Py -m pip install --no-cache-dir --quiet --disable-pip-version-check --target $PyLib pgpy }
    $env:PYTHONPATH = $PyLib
    $sha = & $Py (Join-Path $Here 'verify_tor.py') $tgz "$tgz.asc" "$TorCache\signing-key.asc"
    $env:PYTHONPATH = $null
    if ($LASTEXITCODE) { Remove-Item $TorCache -Recurse -Force; throw "The Tor bundle's signature did not verify - nothing was staged." }
    New-Item -ItemType Directory -Force "$TorCache\x" | Out-Null
    tar -xzf $tgz -C "$TorCache\x"
    if ($LASTEXITCODE) { throw 'Unpacking the Tor bundle failed.' }
    Set-Content "$TorCache\sha256.txt" $sha
}
$torFiles = @('tor\tor.exe', 'tor\pluggable_transports\lyrebird.exe', 'tor\pluggable_transports\pt_config.json',
              'data\geoip', 'data\geoip6', 'data\torrc-defaults') + (Get-ChildItem "$TorCache\x\docs" -Filter *.txt | ForEach-Object { "docs\$($_.Name)" })
foreach ($f in $torFiles) {
    $dst = Join-Path $Stage "tor\$f"
    New-Item -ItemType Directory -Force (Split-Path -Parent $dst) | Out-Null
    Copy-Item (Join-Path "$TorCache\x" $f) $dst
}
Set-Content (Join-Path $Stage 'tor\VERSION') $TorVersion
Write-Host "   Tor $TorVersion (signature verified, sha256 $((Get-Content "$TorCache\sha256.txt").Substring(0, 12))...)"
$notYet = $new | Where-Object { $_ -notmatch $skip }
if ($notYet) { Write-Host "   not committed yet (included): $($notYet -join ', ')" -ForegroundColor Yellow }
# The install's file list (Windows paths, relative to the app folder) - an update removes what drops out of it.
$manifest = Get-ChildItem $Stage -Recurse -File | ForEach-Object { $_.FullName.Substring($Stage.Length + 1) }
[IO.File]::WriteAllLines((Join-Path $Stage '.fx-manifest'), [string[]]($manifest + '.fx-manifest'))
Write-Host "   $n app files + the desktop app ($($manifest.Count) files in all)"

Write-Host '== 3. Setup wizard' -ForegroundColor Cyan
& $Iscc /Q "/DAppVersion=$Version" (Join-Path $Here 'FXWorkspace.iss')
if ($LASTEXITCODE) { throw 'Inno Setup failed.' }
Remove-Item (Join-Path $Here 'stage') -Recurse -Force          # its copies would look like stray code to the tests
$out = Join-Path $Here "output\FX-Workspace-Setup-$Version.exe"
Write-Host ("   {0}  ({1:N0} MB)" -f $out, ((Get-Item $out).Length / 1MB)) -ForegroundColor Green
