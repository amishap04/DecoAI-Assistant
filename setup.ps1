#Requires -Version 5.1
<#
.SYNOPSIS
    End-to-end setup for the DecoAI Assistant on Windows / Snapdragon X Elite.

.DESCRIPTION
    Installs prerequisites, installs and configures OpenClaw into
    %USERPROFILE%\.openclaw, deploys the DecoAI skills and system prompts,
    builds the two Python virtual environments, and rewrites every
    machine-specific path so the skills find their scripts, models and data
    on THIS machine.

    Run it once. Then fill in .env and run start.ps1.

.PARAMETER DataRoot
    Directory that holds everything large or machine-local: the SD2.1 model
    package, generated images, Uno Q camera frames, the inventory database,
    both virtualenvs, the Hugging Face cache and the logs. Prompted for if
    omitted.
    Default: %USERPROFILE%\DecoAI

.PARAMETER OpenclawHome
    OpenClaw's config directory. Default: %USERPROFILE%\.openclaw

.PARAMETER ModelBins
    Path to an existing SD2.1 QNN model package (the folder containing
    text_encoder.onnx, unet.onnx, vae.onnx, metadata.json and the
    *_qairt_context.bin files). Copied into the data root. If omitted you are
    prompted; skipping leaves local NPU generation unavailable until you drop
    the files in yourself.

.PARAMETER Clean
    Move any existing %USERPROFILE%\.openclaw aside to a timestamped backup
    before installing, for a genuinely fresh start.

.PARAMETER SkipPrereqs
    Do not install Node, Python, adb or the global npm packages. Use when the
    machine is already provisioned.

.PARAMETER SkipVenvs
    Do not create the Python virtual environments or install requirements.

.PARAMETER SkipAdb
    Install everything except Android platform-tools.

.PARAMETER NonInteractive
    Never prompt. Missing answers fall back to their defaults.

.EXAMPLE
    .\setup.ps1

.EXAMPLE
    .\setup.ps1 -DataRoot D:\DecoAI -ModelBins D:\models\sd21_qnn -Clean
#>

param(
    [string]$DataRoot     = "",
    [string]$OpenclawHome = "",
    [string]$ModelBins    = "",
    [switch]$Clean,
    [switch]$SkipPrereqs,
    [switch]$SkipVenvs,
    [switch]$SkipAdb,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

$script:Warnings = New-Object System.Collections.ArrayList

function Write-Step { param([string]$m) Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-OK   { param([string]$m) Write-Host "    [OK]   $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "    .      $m" -ForegroundColor DarkGray }
function Write-Warn {
    param([string]$m)
    Write-Host "    [WARN] $m" -ForegroundColor Yellow
    [void]$script:Warnings.Add($m)
}
function Write-Fail { param([string]$m) Write-Host "    [FAIL] $m" -ForegroundColor Red }

function Stop-Setup {
    param([string]$m)
    Write-Host ""
    Write-Fail $m
    Write-Host ""
    exit 1
}

function Test-Cmd {
    param([string]$Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-CmdPath {
    param([string]$Name)
    $c = Get-Command $Name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return ""
}

# winget-installed tools land on the machine PATH but the current process still
# holds the PATH it started with, so re-read it after every install.
function Update-PathFromRegistry {
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ";"
}

# Runs a native tool, echoes its output, and returns ONLY the exit code.
#
# Two things this has to get right. First, npm, pip and winget all write
# progress and warnings to stderr; under $ErrorActionPreference='Stop' that
# surfaces as a terminating NativeCommandError and would abort the install on
# the first pip warning, so the preference is relaxed for the duration of the
# call. Second, the tool's stdout must not reach the success stream, or it
# would be returned alongside the exit code and every caller's `-ne 0` test
# would be comparing against an array.
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [string[]]$Arguments = @(),
        [string]$WorkDir = "",
        [switch]$IgnoreExitCode
    )
    $prevEap = $ErrorActionPreference
    $prevLoc = $null
    $code    = -1
    if ($WorkDir) { $prevLoc = (Get-Location).Path; Set-Location $WorkDir }
    $ErrorActionPreference = "Continue"
    try {
        & $File @Arguments 2>&1 | ForEach-Object { Write-Host "       $_" -ForegroundColor DarkGray }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
        if ($prevLoc) { Set-Location $prevLoc }
    }
    if (-not $IgnoreExitCode -and $code -ne 0) {
        throw "$File $($Arguments -join ' ') exited with code $code"
    }
    return $code
}

# Captures a short piece of stdout from a native tool without letting its
# stderr become a terminating error.
function Get-NativeOutput {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [string[]]$Arguments = @()
    )
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $File @Arguments 2>$null
        return [pscustomobject]@{
            Code = $LASTEXITCODE
            Text = (($out | Out-String).Trim())
        }
    } catch {
        return [pscustomobject]@{ Code = -1; Text = "" }
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

function Read-Answer {
    param([string]$Prompt, [string]$Default)
    if ($NonInteractive) { return $Default }
    if ($Default) {
        $a = Read-Host "$Prompt [$Default]"
    } else {
        $a = Read-Host $Prompt
    }
    if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
    return $a.Trim().Trim('"')
}

# Windows PowerShell 5.1 writes a BOM for -Encoding UTF8, and a BOM breaks
# Node's JSON.parse and Python's json.load on the files this script rewrites.
# Always go through .NET so the output is BOM-less on both 5.1 and 7.x.
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Set-TextFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    [System.IO.File]::WriteAllText($Path, $Text, $script:Utf8NoBom)
}

function New-Dir {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Copy-Tree {
    param([string]$Source, [string]$Destination)
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Dir (Split-Path -Parent $Destination)
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
}

# ---------------------------------------------------------------------------
# .env parsing / writing. Line-based so the template's comments survive.
# ---------------------------------------------------------------------------

function Read-EnvFile {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $map[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
    }
    return $map
}

function Write-EnvFile {
    param(
        [string]$TemplatePath,
        [string]$OutPath,
        [hashtable]$Values
    )
    $out  = New-Object System.Collections.ArrayList
    $seen = @{}
    foreach ($line in (Get-Content -LiteralPath $TemplatePath)) {
        $t = $line.Trim()
        if ($t -and -not $t.StartsWith("#") -and $t.Contains("=")) {
            $key = $t.Substring(0, $t.IndexOf("=")).Trim()
            if ($Values.ContainsKey($key)) {
                [void]$out.Add("$key=$($Values[$key])")
                $seen[$key] = $true
                continue
            }
        }
        [void]$out.Add($line)
    }
    $extra = $Values.Keys | Where-Object { -not $seen.ContainsKey($_) } | Sort-Object
    if ($extra) {
        [void]$out.Add("")
        [void]$out.Add("# Added by setup.ps1")
        foreach ($k in $extra) { [void]$out.Add("$k=$($Values[$k])") }
    }
    New-Dir (Split-Path -Parent $OutPath)
    Set-TextFile -Path $OutPath -Text (($out -join "`r`n") + "`r`n")
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

$ScriptDir  = $PSScriptRoot
$SrcSkills  = Join-Path $ScriptDir "Skills"
$SrcSystem  = Join-Path $ScriptDir "System"
$EnvTemplate = Join-Path $ScriptDir ".env.example"
$EnvLocal    = Join-Path $ScriptDir ".env"

foreach ($p in @($SrcSkills, $SrcSystem, $EnvTemplate)) {
    if (-not (Test-Path -LiteralPath $p)) {
        Stop-Setup "Required source not found: $p  (run setup.ps1 from the repository root, the folder holding Skills\ and System\)"
    }
}

if (-not $OpenclawHome) { $OpenclawHome = Join-Path $env:USERPROFILE ".openclaw" }
$Workspace     = Join-Path $OpenclawHome "workspace"
$WsSkills      = Join-Path $Workspace "Skills"
$RuntimeConfig = Join-Path $OpenclawHome "openclaw.json"

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Magenta
Write-Host "   DecoAI Assistant - Windows / Snapdragon X Elite setup" -ForegroundColor Magenta
Write-Host "  ============================================================" -ForegroundColor Magenta
Write-Host ""

# ---------------------------------------------------------------------------
# 1. Platform sanity
# ---------------------------------------------------------------------------

Write-Step "Checking platform"

$arch = $env:PROCESSOR_ARCHITECTURE
Write-Info "OS            : $([System.Environment]::OSVersion.VersionString)"
Write-Info "Architecture  : $arch"
Write-Info "PowerShell    : $($PSVersionTable.PSVersion)"

if ($arch -eq "ARM64") {
    Write-OK "Running natively on ARM64"
} else {
    Write-Warn "Architecture is '$arch', not ARM64. On a Snapdragon X Elite this usually means you launched an x64 PowerShell under emulation. The NPU pipeline needs native ARM64 tooling - close this and use the ARM64 PowerShell."
}

# ---------------------------------------------------------------------------
# 2. Where does the data live
# ---------------------------------------------------------------------------

Write-Step "Choosing the workspace data directory"

if (-not $DataRoot) {
    Write-Host ""
    Write-Host "  DecoAI needs a directory for everything large and machine-local:" -ForegroundColor White
    Write-Host "    - the Stable Diffusion 2.1 NPU model package (several GB)"
    Write-Host "    - generated concept images"
    Write-Host "    - the shared inventory database"
    Write-Host "    - both Python virtual environments"
    Write-Host "    - the Hugging Face cache and service logs"
    Write-Host ""
    Write-Host "  Pick a drive with plenty of free space. Avoid OneDrive-synced folders." -ForegroundColor DarkGray
    Write-Host ""
    $DataRoot = Read-Answer "  Workspace directory" (Join-Path $env:USERPROFILE "DecoAI")
}

try {
    $DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
} catch {
    Stop-Setup "Not a usable path: $DataRoot"
}

$DirModels  = Join-Path $DataRoot "models\stable-diffusion-2-1"
$DirModelBins = Join-Path $DirModels "Model_Bins"
$DirOutputs = Join-Path $DataRoot "outputs"
$DirFrames  = Join-Path $DataRoot "camera-frames"
$DirDb      = Join-Path $DataRoot "database"
$DirVenvs   = Join-Path $DataRoot "venvs"
$DirLogs    = Join-Path $DataRoot "logs"
$DirHfCache = Join-Path $DataRoot "hf-cache"
$VenvMain   = Join-Path $DirVenvs "decoai-py313"
$VenvSd21   = Join-Path $DirVenvs "sd21-py312"
$PyMain     = Join-Path $VenvMain "Scripts\python.exe"
$PySd21     = Join-Path $VenvSd21 "Scripts\python.exe"
$DbPath     = Join-Path $DirDb "decoai.sqlite"

foreach ($d in @($DataRoot, $DirModels, $DirModelBins, $DirOutputs, $DirFrames, $DirDb, $DirVenvs, $DirLogs, $DirHfCache)) {
    New-Dir $d
}

Write-OK "Data root      : $DataRoot"
Write-Info "models         : $DirModelBins"
Write-Info "outputs        : $DirOutputs"
Write-Info "camera frames  : $DirFrames"
Write-Info "database       : $DbPath"
Write-Info "virtualenvs    : $DirVenvs"
Write-Info "logs           : $DirLogs"
Write-OK "OpenClaw home  : $OpenclawHome"
Write-Info "workspace      : $Workspace"

# ---------------------------------------------------------------------------
# 3. Prerequisites
# ---------------------------------------------------------------------------

$PyLauncher = ""

if ($SkipPrereqs) {
    Write-Step "Skipping prerequisite installation (-SkipPrereqs)"
    if (Test-Cmd "py") { $PyLauncher = "py" }
} else {
    Write-Step "Installing prerequisites"

    $hasWinget = Test-Cmd "winget"
    if ($hasWinget) {
        Write-OK "winget found"
    } else {
        Write-Warn "winget not found - nothing can be auto-installed. Install App Installer from the Microsoft Store, or install Node 22+, Python 3.12, Python 3.13 and adb by hand and re-run with -SkipPrereqs."
    }

    function Install-WingetPackage {
        param([string]$Id, [string]$Label)
        if (-not $hasWinget) {
            Write-Warn "$Label missing and winget is unavailable - install it manually"
            return $false
        }
        Write-Info "winget install $Id"
        $code = Invoke-Native -File "winget" -IgnoreExitCode -Arguments @(
            "install", "--id", $Id, "-e",
            "--accept-source-agreements", "--accept-package-agreements",
            "--disable-interactivity"
        )
        Update-PathFromRegistry
        if ($code -eq 0) { Write-OK "$Label installed"; return $true }
        Write-Warn "$Label install returned exit code $code - continuing, verify manually"
        return $false
    }

    # --- Node.js 22+ ---------------------------------------------------------
    $nodeOk = $false
    if (Test-Cmd "node") {
        $nodeVer = (Get-NativeOutput -File "node" -Arguments @("-v")).Text
        if ($nodeVer -match "^v(\d+)\.") {
            $major = [int]$Matches[1]
            if ($major -ge 22) { Write-OK "Node $nodeVer"; $nodeOk = $true }
            else { Write-Warn "Node $nodeVer is older than the required v22" }
        }
    }
    if (-not $nodeOk) {
        Write-Info "Installing Node.js LTS"
        [void](Install-WingetPackage "OpenJS.NodeJS.LTS" "Node.js")
        if (Test-Cmd "node") { Write-OK "Node $((Get-NativeOutput -File 'node' -Arguments @('-v')).Text)" }
        else { Write-Warn "Node still not on PATH - open a new terminal and re-run" }
    }

    # --- Python 3.12 and 3.13 ------------------------------------------------
    if (-not (Test-Cmd "py")) {
        Write-Info "Python launcher not found; installing Python 3.12"
        [void](Install-WingetPackage "Python.Python.3.12" "Python 3.12")
    }
    $PyLauncher = if (Test-Cmd "py") { "py" } else { "" }

    function Test-PythonVersion {
        param([string]$Version)
        if (-not $PyLauncher) { return $false }
        return ((Get-NativeOutput -File $PyLauncher -Arguments @("-$Version", "-c", "import sys")).Code -eq 0)
    }

    foreach ($spec in @(
        @{ Ver = "3.12"; Id = "Python.Python.3.12"; Why = "Stable Diffusion 2.1 / QNN" },
        @{ Ver = "3.13"; Id = "Python.Python.3.13"; Why = "skill CLIs and Arduino shelf refresh" }
    )) {
        if (Test-PythonVersion $spec.Ver) {
            Write-OK "Python $($spec.Ver) present ($($spec.Why))"
        } else {
            Write-Info "Installing Python $($spec.Ver) for $($spec.Why)"
            [void](Install-WingetPackage $spec.Id "Python $($spec.Ver)")
            Update-PathFromRegistry
            if (-not $PyLauncher -and (Test-Cmd "py")) { $PyLauncher = "py" }
            if (-not (Test-PythonVersion $spec.Ver)) {
                Write-Warn "Python $($spec.Ver) is still not visible to the 'py' launcher. Open a new terminal and re-run, or install it from python.org (choose the ARM64 installer)."
            }
        }
    }

    # --- Git (optional) ------------------------------------------------------
    if (Test-Cmd "git") { Write-OK "git found" }
    else { [void](Install-WingetPackage "Git.Git" "Git") }

    # --- Android platform-tools (adb) ---------------------------------------
    if ($SkipAdb) {
        Write-Info "Skipping Android platform-tools (-SkipAdb)"
    } elseif (Test-Cmd "adb") {
        Write-OK "adb found: $(Get-CmdPath 'adb')"
    } else {
        Write-Info "Installing Android platform-tools (adb) for the Telegram phone node and Uno Q board"
        [void](Install-WingetPackage "Google.PlatformTools" "Android platform-tools")
        if (-not (Test-Cmd "adb")) {
            Write-Warn "adb is not on PATH. Install platform-tools from https://developer.android.com/tools/releases/platform-tools and add the folder to PATH."
        }
    }

    # --- Global npm packages -------------------------------------------------
    if (Test-Cmd "npm") {
        if (Test-Cmd "openclaw") {
            Write-OK "openclaw found: $(Get-CmdPath 'openclaw')"
        } else {
            Write-Info "npm install -g openclaw"
            $code = Invoke-Native -File "npm" -Arguments @("install", "-g", "openclaw") -IgnoreExitCode
            Update-PathFromRegistry
            if ($code -eq 0 -and (Test-Cmd "openclaw")) { Write-OK "openclaw installed" }
            else { Write-Warn "openclaw install failed (exit $code). Run 'npm install -g openclaw' manually." }
        }

        if (Test-Cmd "geniex") {
            Write-OK "geniex found: $(Get-CmdPath 'geniex')"
        } else {
            Write-Info "npm install -g geniex (local Qwen2.5-VL vision server)"
            $code = Invoke-Native -File "npm" -Arguments @("install", "-g", "geniex") -IgnoreExitCode
            Update-PathFromRegistry
            if ($code -eq 0 -and (Test-Cmd "geniex")) { Write-OK "geniex installed" }
            else { Write-Warn "geniex not installed - decoration photo analysis will fall back to a cloud model or mock data" }
        }
    } else {
        Write-Warn "npm not available - cannot install openclaw or geniex"
    }
}

if (-not $PyLauncher -and (Test-Cmd "py")) { $PyLauncher = "py" }

# ---------------------------------------------------------------------------
# 4. Back up / clean the existing OpenClaw install
# ---------------------------------------------------------------------------

Write-Step "Preparing $OpenclawHome"

if ($Clean -and (Test-Path -LiteralPath $OpenclawHome)) {
    $backup = "$OpenclawHome.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Move-Item -LiteralPath $OpenclawHome -Destination $backup -Force
    Write-OK "Existing install moved to $backup"
} elseif (Test-Path -LiteralPath $RuntimeConfig) {
    $backup = "$RuntimeConfig.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item -LiteralPath $RuntimeConfig -Destination $backup -Force
    Write-OK "Backed up existing openclaw.json to $(Split-Path -Leaf $backup)"
}

New-Dir $OpenclawHome
New-Dir $Workspace
New-Dir (Join-Path $Workspace "memory")

# ---------------------------------------------------------------------------
# 5. Deploy skills and system prompts
# ---------------------------------------------------------------------------

Write-Step "Deploying DecoAI skills into the workspace"

Copy-Tree -Source $SrcSkills -Destination $WsSkills
Write-OK "Skills -> $WsSkills"

# The bundled sample renders belong with the rest of the generated output, on
# the data drive. Move them across, then replace the folder with a junction so
# every workspace-relative "Skills/image-generation/output" path keeps working
# while the bytes actually land in the data root.
function New-OutputJunction {
    param([string]$LinkPath, [string]$TargetDir, [string]$Label)

    if (Test-Path -LiteralPath $LinkPath) {
        Get-ChildItem -LiteralPath $LinkPath -File -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $TargetDir $_.Name) -Force
        }
        Remove-Item -LiteralPath $LinkPath -Recurse -Force
    }
    try {
        New-Item -ItemType Junction -Path $LinkPath -Target $TargetDir -ErrorAction Stop | Out-Null
        Write-OK "$Label junction -> $TargetDir"
    } catch {
        New-Dir $LinkPath
        Write-Warn "Could not create the $Label junction ($($_.Exception.Message)). Files will be written inside the workspace instead of $TargetDir."
    }
}

New-OutputJunction -LinkPath (Join-Path $WsSkills "image-generation\output") `
    -TargetDir $DirOutputs -Label "image-generation/output"

# Camera frames from the Uno Q are generated output too, and the unoq SKILL.md
# embeds them by workspace-relative path, so the same junction trick applies.
New-OutputJunction -LinkPath (Join-Path $WsSkills "UnoQ-ESP32-VLM\output") `
    -TargetDir $DirFrames -Label "UnoQ-ESP32-VLM/output"

# The unoq skill drives the board over adb rather than a Python dependency, so
# the only thing to confirm here is that the board is actually reachable.
$UnoqScript = Join-Path $WsSkills "UnoQ-ESP32-VLM\scripts\shelf_counts.py"
if (Test-Path -LiteralPath $UnoqScript) {
    Write-OK "unoq skill installed (Uno Q + ESP32-CAM shelf counting)"
    if (Test-Cmd "adb") {
        $devices = (Get-NativeOutput -File "adb" -Arguments @("devices")).Text
        if ($devices -match "(?m)^\S+\s+device\s*$") {
            Write-OK "Uno Q reachable over adb"
        } else {
            Write-Info "No adb device attached - connect the Uno Q by USB and run 'adb shell ''bash ~/start_vlm.sh''' before counting the shelf. Shelf refresh falls back to ARDUINO_URL or dummy counts until then."
        }
    }
} else {
    Write-Warn "UnoQ-ESP32-VLM is missing from the bundle - camera shelf counting will be unavailable"
}

Write-Step "Deploying system prompt files"

foreach ($f in @("AGENTS.md", "BOOTSTRAP.md", "HEARTBEAT.md", "IDENTITY.md",
                 "SOUL.md", "TOOLS.md", "USER.md", "WORKFLOW-DECORATION-CONCEPT.md")) {
    $src = Join-Path $SrcSystem $f
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $Workspace $f) -Force
        Write-Info "$f"
    } else {
        Write-Warn "System\$f is missing from the bundle"
    }
}
Write-OK "System prompts -> $Workspace"

# The workspace copy of openclaw.json is inert reference material - OpenClaw
# reads the one in $OpenclawHome. Deployed to mirror the reference layout.
$sysCfg = Join-Path $SrcSystem "openclaw.json"
if (Test-Path -LiteralPath $sysCfg) {
    Copy-Item -LiteralPath $sysCfg -Destination (Join-Path $Workspace "openclaw.json") -Force
    Write-Info "openclaw.json (workspace reference copy)"
}

# ---------------------------------------------------------------------------
# 6. SD2.1 model package
# ---------------------------------------------------------------------------

Write-Step "Stable Diffusion 2.1 NPU model package"

$modelReady = Test-Path -LiteralPath (Join-Path $DirModelBins "metadata.json")

if ($modelReady) {
    Write-OK "Model package already present in $DirModelBins"
} else {
    # Look for a package the user already dropped somewhere obvious before
    # asking them to type a path. A folder counts only if it has metadata.json.
    if (-not $ModelBins) {
        $candidates = @(
            (Join-Path $Workspace "models\stable-diffusion-2-1\Model_Bins"),
            (Join-Path $Workspace "models\Model_Bins"),
            (Join-Path $Workspace "models"),
            (Join-Path $ScriptDir "models\stable-diffusion-2-1\Model_Bins"),
            (Join-Path $ScriptDir "models\Model_Bins"),
            (Join-Path $ScriptDir "models"),
            (Join-Path $ScriptDir "Model_Bins")
        )
        foreach ($c in $candidates) {
            if (Test-Path -LiteralPath (Join-Path $c "metadata.json")) {
                $ModelBins = $c
                Write-OK "Found an SD2.1 package at $c"
                break
            }
        }
    }

    if (-not $ModelBins -and -not $NonInteractive) {
        Write-Host ""
        Write-Host "  The precompiled SD2.1 QNN package is not distributed with this repo." -ForegroundColor White
        Write-Host "  It is the folder containing:" -ForegroundColor White
        Write-Host "    text_encoder.onnx  unet.onnx  vae.onnx  metadata.json"
        Write-Host ""
        Write-Host ""
        Write-Host "  Setup looks for it automatically in:" -ForegroundColor DarkGray
        Write-Host "    $(Join-Path $Workspace 'models')" -ForegroundColor DarkGray
        Write-Host "    $(Join-Path $ScriptDir 'models')" -ForegroundColor DarkGray
        Write-Host "  Drop it in either and re-run, or give the path here." -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "  Leave blank to skip - the cloud image path still works, and you can" -ForegroundColor DarkGray
        Write-Host "  drop the files into $DirModelBins later." -ForegroundColor DarkGray
        Write-Host ""
        $ModelBins = Read-Answer "  Path to the existing Model_Bins folder" ""
    }

    if ($ModelBins) {
        if (-not (Test-Path -LiteralPath $ModelBins)) {
            Write-Warn "Model path not found: $ModelBins - skipping"
        } elseif (-not (Test-Path -LiteralPath (Join-Path $ModelBins "metadata.json"))) {
            Write-Warn "$ModelBins has no metadata.json, so it is not an SD2.1 QNN package - skipping"
        } else {
            Write-Info "Copying model package (this takes a while - it is several GB)"
            Copy-Item -Path (Join-Path $ModelBins "*") -Destination $DirModelBins -Recurse -Force
            $modelReady = $true
            $mb = [math]::Round(((Get-ChildItem -LiteralPath $DirModelBins -Recurse -File |
                    Measure-Object -Property Length -Sum).Sum / 1MB), 1)
            Write-OK "Model package copied ($mb MB) -> $DirModelBins"
        }
    }

    if (-not $modelReady) {
        Write-Warn "No SD2.1 model package installed. Local NPU generation is disabled until you copy the package into $DirModelBins. Cloud generation via Cirrascale is unaffected."
    }
}

# ---------------------------------------------------------------------------
# 7. Python virtual environments
# ---------------------------------------------------------------------------

$ReqSd21 = Join-Path $WsSkills "image-generation\Stable-Diffusion-2-1\requirements.txt"
$ReqMain = @(
    (Join-Path $WsSkills "inventory-management\requirements.txt"),
    (Join-Path $WsSkills "cost-estimation\requirements.txt"),
    (Join-Path $WsSkills "image-generation\requirements.txt")
)

function New-Venv {
    param([string]$PyVersion, [string]$Path, [string]$Label)

    if (Test-Path -LiteralPath (Join-Path $Path "Scripts\python.exe")) {
        Write-OK "$Label venv already exists at $Path"
        return $true
    }
    if (-not $PyLauncher) {
        Write-Warn "No 'py' launcher - cannot create the $Label venv"
        return $false
    }
    Write-Info "py -$PyVersion -m venv $Path"
    $code = Invoke-Native -File $PyLauncher -IgnoreExitCode -Arguments @("-$PyVersion", "-m", "venv", $Path)
    if ($code -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $Path "Scripts\python.exe"))) {
        Write-Warn "Could not create the $Label venv with Python $PyVersion (exit $code)"
        return $false
    }
    Write-OK "$Label venv created"
    return $true
}

function Show-VenvArch {
    param([string]$PyExe, [string]$Label)
    $probe = "import platform,sys;print(platform.machine()+' '+'.'.join(map(str,sys.version_info[:3])))"
    $r = Get-NativeOutput -File $PyExe -Arguments @("-c", $probe)
    if ($r.Code -ne 0 -or -not $r.Text) {
        Write-Warn "Could not query the $Label interpreter"
        return
    }
    $parts   = $r.Text -split "\s+"
    $machine = $parts[0]
    $ver     = if ($parts.Count -gt 1) { $parts[1] } else { "?" }
    Write-Info "$Label -> Python $ver / $machine"
    if ($machine -ne "ARM64") {
        Write-Warn "$Label is $machine, not ARM64. onnxruntime-qnn needs native ARM64 Python to reach the Hexagon NPU at full speed."
    }
}

function Install-Requirements {
    param([string]$PyExe, [string[]]$Files, [string]$Label)
    Write-Info "Upgrading pip in $Label"
    [void](Invoke-Native -File $PyExe -IgnoreExitCode -Arguments @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel", "--quiet"))
    foreach ($f in $Files) {
        if (-not (Test-Path -LiteralPath $f)) { Write-Warn "requirements file missing: $f"; continue }
        Write-Info "pip install -r $(Split-Path -Leaf (Split-Path -Parent $f))\$(Split-Path -Leaf $f)"
        $code = Invoke-Native -File $PyExe -IgnoreExitCode -Arguments @("-m", "pip", "install", "-r", $f)
        if ($code -ne 0) { Write-Warn "pip install -r $f failed (exit $code)" }
    }
}

if ($SkipVenvs) {
    Write-Step "Skipping virtual environments (-SkipVenvs)"
} else {
    Write-Step "Creating the Python 3.13 environment (skill CLIs, Arduino shelf refresh)"
    if (New-Venv -PyVersion "3.13" -Path $VenvMain -Label "decoai-py313") {
        Show-VenvArch -PyExe $PyMain -Label "decoai-py313"
        Install-Requirements -PyExe $PyMain -Files $ReqMain -Label "decoai-py313"
        # The Uno Q shelf sensor speaks HTTP today; pyserial is here so the
        # documented USB/UART fallback in refresh_shelf.py works unmodified.
        Write-Info "pip install pyserial (Arduino serial fallback)"
        [void](Invoke-Native -File $PyMain -IgnoreExitCode -Arguments @("-m", "pip", "install", "pyserial", "--quiet"))
        Write-OK "decoai-py313 ready"
    }

    Write-Step "Creating the Python 3.12 environment (Stable Diffusion 2.1 on the NPU)"
    if (New-Venv -PyVersion "3.12" -Path $VenvSd21 -Label "sd21-py312") {
        Show-VenvArch -PyExe $PySd21 -Label "sd21-py312"
        Install-Requirements -PyExe $PySd21 -Files @($ReqSd21) -Label "sd21-py312"

        $htp = Get-NativeOutput -File $PySd21 -Arguments @("-c", "import onnxruntime_qnn as q;print(q.get_qnn_htp_path())")
        if ($htp.Code -eq 0 -and $htp.Text) {
            Write-OK "QNN HTP backend: $($htp.Text)"
        } else {
            Write-Warn "onnxruntime-qnn did not load. Local NPU generation will not work until it does - check that this venv is native ARM64."
        }
        Write-OK "sd21-py312 ready"
    }
}

# ---------------------------------------------------------------------------
# 8. Node dependencies for the Amazon URL builder
# ---------------------------------------------------------------------------

Write-Step "Installing the Amazon URL builder dependencies"

$amazonDir = Join-Path $WsSkills "amazon-url-builder"
if ((Test-Path -LiteralPath (Join-Path $amazonDir "package.json")) -and (Test-Cmd "npm")) {
    $code = Invoke-Native -File "npm" -Arguments @("install", "--no-audit", "--no-fund") -WorkDir $amazonDir -IgnoreExitCode
    if ($code -eq 0) { Write-OK "npm install complete in $amazonDir" }
    else { Write-Warn "npm install failed in $amazonDir (exit $code) - Amazon purchase links will not work" }
} else {
    Write-Warn "Skipping npm install for amazon-url-builder (npm or package.json missing)"
}

# ---------------------------------------------------------------------------
# 9. Skills/.env
# ---------------------------------------------------------------------------

Write-Step "Writing the runtime environment file"

if (-not (Test-Path -LiteralPath $EnvLocal)) {
    Copy-Item -LiteralPath $EnvTemplate -Destination $EnvLocal -Force
    Write-Warn "No .env in the bundle, so one was created from .env.example at $EnvLocal - it has no API keys in it yet."
}

$userEnv = Read-EnvFile $EnvLocal

$resolved = @{
    DECOAI_DB_PATH    = $DbPath
    DECOAI_OUTPUT_DIR = $DirOutputs
    SD21_MODEL_DIR    = $DirModelBins
    SD21_PYTHON       = $PySd21
    UNOQ_SCRIPT       = (Join-Path $WsSkills "UnoQ-ESP32-VLM\scripts\shelf_counts.py")
    HF_HOME           = $DirHfCache
}
foreach ($k in $userEnv.Keys) {
    # A value the user typed in wins over the path this script guessed.
    if ($resolved.ContainsKey($k) -and [string]::IsNullOrWhiteSpace($userEnv[$k])) { continue }
    $resolved[$k] = $userEnv[$k]
}

$SkillsEnv = Join-Path $WsSkills ".env"
Write-EnvFile -TemplatePath $EnvTemplate -OutPath $SkillsEnv -Values $resolved
Write-OK "Wrote $SkillsEnv"

$missing = @("ANTHROPIC_API_KEY", "CIRRASCALE_API_KEY") |
    Where-Object { [string]::IsNullOrWhiteSpace($resolved[$_]) }
if ($missing) {
    Write-Warn "These keys are still blank: $($missing -join ', '). Add them to $EnvLocal and re-run setup.ps1, or edit $SkillsEnv directly."
}

# ---------------------------------------------------------------------------
# 10. Rewrite machine-specific paths in the deployed files
# ---------------------------------------------------------------------------

Write-Step "Rewriting hardcoded paths for this machine"

# Paths baked in on the original build machines. Longest first, so the more
# specific model path is replaced before the generic workspace prefix.
$pathMap = [ordered]@{
    "C:\Users\qc_de\Desktop\DecoAI Assistant\DecoAI_Assistant-pragnya\DecoAI_Assistant-pragnya\Stable-Diffusion-2-1\Model_Bins" = $DirModelBins
    "C:\hackathon_code\compute_sd\ORT\stable_diffusion_v2_1-precompiled_qnn_onnx-w8a16-qualcomm_snapdragon_x_elite" = $DirModelBins
    "C:\hackathon_code\sd2.1_ort_qnn"      = (Join-Path $WsSkills "image-generation\Stable-Diffusion-2-1")
    "C:\Users\qc_de\.openclaw\workspace"   = $Workspace
    "C:\Users\HCKTest\.openclaw\workspace" = $Workspace
    "C:\Users\qc_de\.openclaw"             = $OpenclawHome
    "C:\Users\HCKTest\Downloads"           = (Join-Path $env:USERPROFILE "Downloads")
    "C:\Users\HCKTest\Pictures"            = (Join-Path $env:USERPROFILE "Pictures")
    "Skills\image-generation\Stable-Diffusion-2-1\venv\Scripts\python.exe" = $PySd21
}

$textExtensions = @(".md", ".json", ".ts", ".py", ".js", ".txt")
$targets = New-Object System.Collections.ArrayList
foreach ($root in @($Workspace)) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    Get-ChildItem -Path $root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            $textExtensions -contains $_.Extension.ToLower() -and
            $_.FullName -notlike "*\node_modules\*" -and
            $_.FullName -notlike "*\memory\*"
        } |
        ForEach-Object { [void]$targets.Add($_.FullName) }
}

$patched = 0
foreach ($file in $targets) {
    try {
        $text = Get-Content -LiteralPath $file -Raw -ErrorAction Stop
    } catch { continue }
    if (-not $text) { continue }
    $orig = $text

    foreach ($from in $pathMap.Keys) {
        $to = $pathMap[$from]
        if ($text.Contains($from)) {
            $text = $text.Replace($from, $to)
        }
        # Same path again, JSON/TypeScript escaped (C:\\Users\\...).
        $fromEsc = $from.Replace("\", "\\")
        if ($text.Contains($fromEsc)) {
            $text = $text.Replace($fromEsc, $to.Replace("\", "\\"))
        }
    }

    if ($text -ne $orig) {
        Set-TextFile -Path $file -Text $text
        $patched++
    }
}
Write-OK "Rewrote paths in $patched file(s)"

# The system prompts tell the agent to shell out to a bare `python`, which on
# this machine is either absent or the wrong interpreter. Point them at the
# 3.13 venv so `exec:` lines run against the installed dependencies.
Write-Step "Pointing agent exec commands at the project interpreter"

$pyQuoted = '"' + $PyMain + '"'
$promptFiles = @("AGENTS.md", "WORKFLOW-DECORATION-CONCEPT.md") |
    ForEach-Object { Join-Path $Workspace $_ } |
    Where-Object { Test-Path -LiteralPath $_ }

$rewritten = 0
foreach ($file in $promptFiles) {
    $text = Get-Content -LiteralPath $file -Raw
    $orig = $text
    # The prompts spell the invocation two ways, both with the leading `.\`.
    foreach ($prefix in @('python ".\Skills\', 'python ".\skills\')) {
        $tail = $prefix.Substring("python ".Length)
        $text = $text.Replace($prefix, "$pyQuoted $tail")
    }
    if ($text -ne $orig) {
        Set-TextFile -Path $file -Text $text
        $rewritten++
    }
}
Write-OK "Updated the interpreter in $rewritten prompt file(s)"

# ---------------------------------------------------------------------------
# 11. openclaw.json
# ---------------------------------------------------------------------------

Write-Step "Writing $RuntimeConfig"

$tplPath = Join-Path $SrcSystem "openclaw.runtime.json"
if (-not (Test-Path -LiteralPath $tplPath)) {
    Stop-Setup "System\openclaw.runtime.json is missing from the bundle"
}

$gatewayToken = ""
if (Test-Path -LiteralPath $RuntimeConfig) {
    try {
        $old = Get-Content -LiteralPath $RuntimeConfig -Raw | ConvertFrom-Json
        if ($old.PSObject.Properties.Name -contains "gateway" -and
            $old.gateway.PSObject.Properties.Name -contains "auth" -and
            $old.gateway.auth.PSObject.Properties.Name -contains "token") {
            $gatewayToken = [string]$old.gateway.auth.token
        }
    } catch { }
}
if ([string]::IsNullOrWhiteSpace($gatewayToken)) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $gatewayToken = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    Write-Info "Generated a fresh gateway auth token"
} else {
    Write-Info "Reusing the existing gateway auth token (paired phone nodes keep working)"
}

$json = Get-Content -LiteralPath $tplPath -Raw
$json = $json.Replace("__SKILLS_DIR__",    $WsSkills.Replace("\", "\\"))
$json = $json.Replace("__WORKSPACE__",     $Workspace.Replace("\", "\\"))
$json = $json.Replace("__GATEWAY_TOKEN__", $gatewayToken)
$cfg  = $json | ConvertFrom-Json
$cfg.PSObject.Properties.Remove('$comment')

# Telegram, only when a token is actually configured.
$tgToken = if ($resolved.ContainsKey("TELEGRAM_BOT_TOKEN")) { $resolved["TELEGRAM_BOT_TOKEN"] } else { "" }
$tgOwner = if ($resolved.ContainsKey("TELEGRAM_OWNER_CHAT_ID")) { $resolved["TELEGRAM_OWNER_CHAT_ID"] } else { "" }
if (-not [string]::IsNullOrWhiteSpace($tgToken)) {
    $cfg.channels.telegram.enabled  = $true
    $cfg.channels.telegram.botToken = $tgToken
    $cfg.plugins.entries.telegram.enabled = $true
    if (-not [string]::IsNullOrWhiteSpace($tgOwner)) {
        $cfg.commands.ownerAllowFrom = @("telegram:$tgOwner")
    }
    Write-OK "Telegram channel enabled"
} else {
    Write-Info "Telegram left disabled (no TELEGRAM_BOT_TOKEN in .env)"
}

# whisper-node-bridge, only when the plugin source is actually on this machine.
$bridge = Join-Path (Split-Path -Parent $ScriptDir) "Mobile_Telegram\openclaw-whisper-node-bridge"
if (Test-Path -LiteralPath $bridge) {
    $cfg.plugins.load.paths = @($bridge)
    $cfg.plugins.entries."whisper-node-bridge".enabled = $true
    Write-OK "whisper-node-bridge plugin found at $bridge"
    Write-Info "Pair your phone afterwards with Mobile_Telegram\setup-node.ps1"
} else {
    $cfg.plugins.load.paths = @()
    $cfg.tools.media.models = @()
    $cfg.tools.media.audio.enabled = $false
    Write-Info "whisper-node-bridge not present - voice transcription left disabled"
}

Set-TextFile -Path $RuntimeConfig -Text ($cfg | ConvertTo-Json -Depth 100)
Write-OK "Wrote $RuntimeConfig"
Write-Info "gateway port 18789, skills loaded from $WsSkills"

# ---------------------------------------------------------------------------
# 12. Initialise the inventory database
# ---------------------------------------------------------------------------

Write-Step "Initialising the inventory database"

if (Test-Path -LiteralPath $PyMain) {
    $env:DECOAI_DB_PATH = $DbPath
    $init = @"
import sys
sys.path.insert(0, r'$WsSkills')
from database.db import init_db, DB_PATH
init_db()
print(DB_PATH)
"@
    $tmp = Join-Path $env:TEMP "decoai_init_db.py"
    Set-TextFile -Path $tmp -Text $init
    $code = Invoke-Native -File $PyMain -Arguments @($tmp) -IgnoreExitCode
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    if ($code -eq 0) { Write-OK "Database ready at $DbPath" }
    else { Write-Warn "Database init failed (exit $code) - it will be created on first use" }
} else {
    Write-Warn "decoai-py313 not available - skipping database init"
}

# ---------------------------------------------------------------------------
# 13. Record the resolved layout for start.ps1
# ---------------------------------------------------------------------------

Write-Step "Saving the resolved layout"

$state = [ordered]@{
    generatedAt   = (Get-Date).ToString("o")
    dataRoot      = $DataRoot
    openclawHome  = $OpenclawHome
    workspace     = $Workspace
    skillsDir     = $WsSkills
    skillsEnv     = $SkillsEnv
    outputsDir    = $DirOutputs
    framesDir     = $DirFrames
    databasePath  = $DbPath
    modelDir      = $DirModelBins
    modelInstalled = $modelReady
    venvMain      = $VenvMain
    venvSd21      = $VenvSd21
    pythonMain    = $PyMain
    pythonSd21    = $PySd21
    logsDir       = $DirLogs
    hfHome        = $DirHfCache
    gatewayPort   = 18789
    sd21Port      = 50002
    geniexPort    = 18181
    amazonPort    = 8004
}
$statePath = Join-Path $ScriptDir "decoai-setup.json"
Set-TextFile -Path $statePath -Text ($state | ConvertTo-Json -Depth 10)
Write-OK "Wrote $statePath (start.ps1 reads this)"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Setup complete" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "   Data root       $DataRoot"
Write-Host "   Workspace       $Workspace"
Write-Host "   Skills          $WsSkills"
Write-Host "   Config          $RuntimeConfig"
Write-Host "   Environment     $SkillsEnv"
Write-Host "   Python 3.13     $PyMain"
Write-Host "   Python 3.12     $PySd21"
Write-Host "   SD2.1 model     $(if ($modelReady) { $DirModelBins } else { 'NOT INSTALLED' })"
Write-Host "   Camera frames   $DirFrames"
Write-Host ""

if ($script:Warnings.Count -gt 0) {
    Write-Host "   $($script:Warnings.Count) warning(s):" -ForegroundColor Yellow
    foreach ($w in $script:Warnings) { Write-Host "     - $w" -ForegroundColor Yellow }
    Write-Host ""
}

Write-Host "   Next steps:" -ForegroundColor Cyan
Write-Host "     1. Add your API keys to $EnvLocal"
Write-Host "        (ANTHROPIC_API_KEY and CIRRASCALE_API_KEY at minimum, plus"
Write-Host "         TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_CHAT_ID for Telegram)"
Write-Host "     2. Re-run .\setup.ps1 so the keys reach the workspace, or edit"
Write-Host "        $SkillsEnv directly"
Write-Host "     3. Start everything with .\start.ps1"
Write-Host ""
