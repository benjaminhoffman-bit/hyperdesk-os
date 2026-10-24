# setup.ps1 - installs or removes HyperDesk OS by SilvaDes for this Windows user. No admin: Python 3.12 and Ollama
# come through winget in user scope, the files go to %LOCALAPPDATA%\HyperDesk. Install.cmd and Uninstall.cmd run it:
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 [-Name Ann] [-Unattended] [-NoShortcuts] [-NoLaunch]
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -Uninstall [-Target <folder>] [-Unattended]
# Windows PowerShell 5.1 syntax, ASCII only (5.1 reads a file without BOM as ANSI).
param([string]$Source = $PSScriptRoot, [string]$Target = "$env:LOCALAPPDATA\HyperDesk", [string]$Name,
      [string]$Model = 'gemma4:12b-it-qat', [switch]$Uninstall, [switch]$NoShortcuts, [switch]$NoLaunch, [switch]$Unattended)
$ErrorActionPreference = 'Stop'
$start = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$utf8 = New-Object Text.UTF8Encoding $false  # Python's tomllib rejects a BOM

function Fail($why) {
  Write-Host "`n$why" -ForegroundColor Red
  if ($Uninstall -and -not $Unattended) { [void](Read-Host 'Press Enter to close') }  # Install.cmd pauses for an install
  exit 1
}
trap { Fail "Stopped: $($_.Exception.Message)" }
function Step($text) { Write-Host "`n$text" -ForegroundColor Cyan }
# Runs a native program silently and says whether it worked. Native programs do not throw; 5.1 turns redirected
# stderr into errors, so this one call runs with Continue.
function Ok([string]$exe) { $ErrorActionPreference = 'Continue'; & $exe @args *> $null; $LASTEXITCODE -eq 0 }
function Clean([string]$s) {  # letters, digits, space . - ' only: the name goes into a TOML string and model prompts
  $s = ($s -replace "[^\p{L}\p{M}\d .'-]", '').Trim()  # \p{M}: combining accents (a decomposed Rene keeps its e-acute)
  if ($s.Length -gt 40) { $s = $s.Substring(0, 40).Trim() }
  $s
}
function Find-Pythonw {
  foreach ($d in 'Python312', 'Python312-arm64') {
    $p = "$env:LOCALAPPDATA\Programs\Python\$d\pythonw.exe"
    if (Test-Path $p) { return $p }
  }
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $ErrorActionPreference = 'Continue'
    $exe = & py '-3.12' -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $exe) {
      $p = Join-Path (Split-Path "$exe".Trim()) 'pythonw.exe'
      if (Test-Path $p) { return $p }
    }
  }
}
function Find-Ollama {
  $p = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
  if (Test-Path $p) { return $p }
  $c = Get-Command ollama -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
}
function WingetInstall($id) {  # user scope, silent; whether it worked is checked by looking for the program again
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return }
  & winget install -e --id $id --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
}

Write-Host ''
Write-Host '  HyperDesk OS by SilvaDes' -ForegroundColor Cyan
Write-Host '  ------------------------'
if ($Uninstall -and -not $PSBoundParameters.ContainsKey('Target')) { $Target = $PSScriptRoot }  # setup.ps1 sits in the install
$Source = [IO.Path]::GetFullPath($Source).TrimEnd('\')
$Target = [IO.Path]::GetFullPath($Target).TrimEnd('\')

if ($Uninstall) {
  # Never delete an arbitrary folder: only one with this install's layout (a developer copy has no Uninstall.cmd).
  if (-not ((Test-Path "$Target\AgentOS\hyperdesk.pyw") -and (Test-Path "$Target\Uninstall.cmd"))) {
    Fail "$Target does not look like a HyperDesk OS install. Nothing was removed."
  }
  if (-not (Test-Path "$Target\installed.txt")) { Fail 'This looks like the unzipped installer, not the install. Use Start menu, Uninstall HyperDesk.' }  # only the install step writes it
  if (-not $Unattended) {
    $a = Read-Host "Remove HyperDesk and everything in $Target, including your conversations, Knowledge and the files and videos it made in workspace? [y/N]"
    if ($a -notmatch '^\s*y') { Write-Host 'Nothing was removed.'; exit 0 }
  }
  Set-Location $env:TEMP
  Step '1. Stopping HyperDesk'
  $agent = "$Target\AgentOS".ToLower()
  $all = @(Get-CimInstance Win32_Process)
  $ours = @($all | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains($agent) } |
    ForEach-Object { $_.ProcessId })
  # plus only the ffmpeg they started (Quick edit holds its exports open), never other children (a browser it opened)
  $all | Where-Object { $ours -contains $_.ProcessId -or ($ours -contains $_.ParentProcessId -and $_.Name -match '^ff(mpeg|probe)\.exe$') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Step '2. Removing the shortcuts'
  $shell = New-Object -ComObject WScript.Shell
  foreach ($dir in @($start, [Environment]::GetFolderPath('Desktop'))) {
    foreach ($n in 'HyperDesk.lnk', 'HyperDesk Home Base.lnk', 'HyperDesk Game Mode.lnk', 'HyperDesk Lights.lnk', 'Uninstall HyperDesk.lnk') {
      $p = Join-Path $dir $n
      if (-not (Test-Path -LiteralPath $p)) { continue }
      $l = $shell.CreateShortcut($p)
      if ("$($l.TargetPath) $($l.Arguments)".ToLower().Contains("$Target\".ToLower())) { Remove-Item -LiteralPath $p -Force; "  removed $p" }
    }
  }
  Step "3. Removing $Target"
  $rm = @(if (Test-Path "$Target\models.txt") { Get-Content "$Target\models.txt" | Where-Object { $_.Trim() } | Select-Object -Unique })  # what this install added to Ollama
  try { Remove-Item -LiteralPath $Target -Recurse -Force } catch { Start-Sleep 2; Remove-Item -LiteralPath $Target -Recurse -Force }  # a stopped process may hold a file a moment longer
  Write-Host "`nHyperDesk is removed." -ForegroundColor Green
  Write-Host 'Ollama, Python and the models stay installed (other apps may use them).'
  if ($rm) { Write-Host "To remove the models this install added, run:`n  ollama rm $($rm -join ' ')" } else { Write-Host 'The models were already on this PC before HyperDesk: they stay.' }
  Write-Host 'If you installed Ollama and Python only for HyperDesk, you can remove them too:'
  Write-Host '  winget uninstall Ollama.Ollama'
  Write-Host '  winget uninstall Python.Python.3.12'
  if (-not $Unattended) { [void](Read-Host "`nPress Enter to close") }
  exit 0
}

Step '1. Python 3.12'
$pythonw = Find-Pythonw
if (-not $pythonw) {
  Write-Host '  Not found: installing it for this user with winget ...'
  WingetInstall 'Python.Python.3.12'
  $pythonw = Find-Pythonw
}
if (-not $pythonw) { Fail 'Python 3.12 is missing. Install it from python.org (Windows installer, 64-bit), then run Install.cmd again.' }
Write-Host "  $pythonw"

Step '2. Disk space'
$gb = [math]::Round((Get-PSDrive ($env:USERPROFILE.Substring(0, 1))).Free / 1GB, 1)  # Ollama keeps models in %USERPROFILE%\.ollama
Write-Host "  $gb GB free on $($env:USERPROFILE.Substring(0, 2))"
if ($gb -lt 15) {
  Write-Host '  Under 15 GB free: the model alone is about 7 GB.' -ForegroundColor Yellow
  if (-not $Unattended -and (Read-Host '  Continue anyway? [y/N]') -notmatch '^\s*y') { Fail 'Stopped: not enough free disk space.' }
}

Step "3. Copying HyperDesk to $Target"
$cfg = "$Target\AgentOS\config.toml"
$kept = ''  # a reinstall keeps the name; the new config.toml replaces the rest
if (Test-Path $cfg) { $m = [regex]::Match([IO.File]::ReadAllText($cfg), '(?m)^name = "([^"\r\n]*)"'); if ($m.Success) { $kept = $m.Groups[1].Value } }
if ($Source -ne $Target) {
  if (Test-Path $cfg) {  # the new config.toml replaces the old one (a newer zip may add required keys)
    Copy-Item -LiteralPath $cfg "$cfg.bak" -Force
    Write-Host "  Your old settings are in $cfg.bak: copy any changes you made (games, [free], [jobs]) back into config.toml."
  }
  & robocopy $Source $Target /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1
  if ($LASTEXITCODE -ge 8) { Fail "Copying the files failed (robocopy exit code $LASTEXITCODE)." }
  # Extract All marks every file as downloaded; unblock only the copied ones (not the user's own files in a reinstall)
  Get-ChildItem -LiteralPath $Source -Recurse -File | ForEach-Object { Unblock-File -LiteralPath "$Target$($_.FullName.Substring($Source.Length))" }
}
if (-not (Test-Path $cfg)) { Fail "$cfg is missing: run Install.cmd from the unzipped HyperDesk OS folder." }
[IO.File]::WriteAllText("$Target\installed.txt", "HyperDesk OS install`r`n", $utf8)  # never in the zip: -Uninstall refuses a folder without it
New-Item -ItemType Directory -Force "$Target\workspace\videos" | Out-Null  # Quick edit's exports: Open exports folder works at once

Step '4. Your name'
if (-not $Name) { $Name = $kept }
if (-not $Name -and -not $Unattended) { $Name = Read-Host "  What should HyperDesk call you? (Enter for $env:USERNAME)" }
$Name = Clean $Name
if (-not $Name) { $Name = Clean $env:USERNAME }
[IO.File]::WriteAllText($cfg, ([IO.File]::ReadAllText($cfg) -replace '(?m)^name = "[^"\r\n]*"', "name = `"$Name`""), $utf8)
Write-Host "  HyperDesk will call you $Name"

Step '5. Ollama (runs the models on this PC)'
$ollama = Find-Ollama
if (-not $ollama) {
  Write-Host '  Not found: installing it for this user with winget ...'
  WingetInstall 'Ollama.Ollama'
  $ollama = Find-Ollama
}
if (-not $ollama) { Fail 'Ollama is missing. Install it from ollama.com, then run Install.cmd again.' }
if (-not (Ok $ollama list)) {
  $app = Join-Path (Split-Path $ollama) 'ollama app.exe'
  $dir = Split-Path $ollama  # not this installer's folder: Ollama would hold it open and keep it from being deleted
  if (Test-Path $app) { Start-Process $app -WorkingDirectory $dir } else { Start-Process $ollama -ArgumentList 'serve' -WindowStyle Hidden -WorkingDirectory $dir }
  $up = $false
  for ($i = 0; $i -lt 45 -and -not $up; $i++) { Start-Sleep 2; $up = Ok $ollama list }  # up to 90 s
  if (-not $up) { Fail 'Ollama did not start. Open Ollama from the Start menu, then run Install.cmd again.' }
}
Write-Host "  $ollama"

Step '6. Graphics card'
$vram = 0
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  $line = @(& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits)[0]  # Select-Object -First stops it early: exit code -1
  if ($LASTEXITCODE -eq 0 -and "$line" -match '^(.+?),\s*(\d+)') { $vram = [int]$Matches[2]; Write-Host "  $($Matches[1]), $([math]::Round($vram / 1024)) GB VRAM" }
}
if ($vram -lt 7900) { Write-Host '  No NVIDIA card with 8 GB or more: HyperDesk works, but answers will be slower on this PC.' -ForegroundColor Yellow }  # 8 GB cards report a little under 8192 MiB

Step "7. The model ($Model, about 7 GB the first time)"
function Made($m) { Add-Content -LiteralPath "$Target\models.txt" $m }  # uninstall offers 'ollama rm' only for these (robocopy leaves the file alone)
if (-not (Ok $ollama show $Model)) {
  & $ollama pull $Model
  if ($LASTEXITCODE -ne 0) { Fail "Downloading $Model failed. If Ollama said it needs a newer version, update it (winget upgrade -e --id Ollama.Ollama, or ollama.com/download). Otherwise check the internet connection. Then run Install.cmd again." }
  Made $Model
}
$file = Join-Path $env:TEMP 'hyperdesk.Modelfile'
[IO.File]::WriteAllText($file, "FROM $Model`nPARAMETER num_ctx 16384`n", $utf8)
foreach ($alias in 'fast', 'code', 'daily', 'vision') {
  if (Ok $ollama show $alias) { Write-Host "  $alias exists already: kept as it is"; continue }  # never overwrite someone's model
  & $ollama create $alias -f $file
  if ($LASTEXITCODE -ne 0) { Fail "Making the model $alias failed." }
  Made $alias
}
Remove-Item $file -ErrorAction SilentlyContinue

if (-not $NoShortcuts) {
  Step '8. Shortcuts'
  $shell = New-Object -ComObject WScript.Shell
  $own = "$start\HyperDesk.lnk"  # another HyperDesk's shortcuts (and its AppUserModelID) stay its own; uninstall would delete ours
  if ((Test-Path -LiteralPath $own) -and -not $shell.CreateShortcut($own).Arguments.ToLower().Contains("$Target\".ToLower())) {
    Write-Host "  Another HyperDesk owns the HyperDesk shortcuts: kept them. Start this one from $Target\AgentOS\hyperdesk.pyw." -ForegroundColor Yellow
  } else {
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$Target\AgentOS\make-shortcuts.ps1" -Pythonw $pythonw
    if ($LASTEXITCODE -ne 0) { Fail 'Making the shortcuts failed.' }
    $linked = $true
  }
  $l = $shell.CreateShortcut("$start\Uninstall HyperDesk.lnk")
  $l.TargetPath = "$Target\Uninstall.cmd"
  $l.WorkingDirectory = $env:TEMP
  $l.IconLocation = "$Target\AgentOS\assets\hyperdesk.ico,0"
  $l.Description = 'Remove HyperDesk OS from this PC'
  $l.Save()
  "ok  $start\Uninstall HyperDesk.lnk"
}

if (-not $NoLaunch) {
  Step '9. Starting HyperDesk'
  Start-Process $pythonw "`"$Target\AgentOS\hyperdesk.pyw`"" -WorkingDirectory "$Target\AgentOS"
}

Write-Host "`nDone. HyperDesk OS is installed in $Target." -ForegroundColor Green
if ($linked) { Write-Host 'Open it from the Start menu (HyperDesk) or the desktop shortcut.' }
else { Write-Host "Open it with $Target\AgentOS\hyperdesk.pyw (no HyperDesk shortcuts were made for it)." }
Write-Host 'To remove it: Start menu, Uninstall HyperDesk (or run Uninstall.cmd in that folder).'
if (-not $Unattended) { [void](Read-Host "`nPress Enter to close") }
exit 0
