#Requires -Version 5.1
<#
.SYNOPSIS
    Launches the whole DecoAI stack: the SD2.1 NPU session server, the Amazon
    URL builder, the Geniex vision server and the OpenClaw gateway.

.DESCRIPTION
    Reads the layout that setup.ps1 resolved (decoai-setup.json) and the
    runtime environment (<workspace>\Skills\.env), then starts each service in
    dependency order, waits for it to accept connections, and tails the gateway
    log. Ctrl+C shuts everything back down.

    Services and ports:
      50002  SD2.1 session server   session_server.py on the Hexagon NPU
       8004  Amazon URL builder     node src/server.js
      18181  Geniex                 Qwen2.5-VL vision, --compute npu
      18789  OpenClaw gateway       the agent itself

.PARAMETER Config
    Path to an openclaw.json. Defaults to %USERPROFILE%\.openclaw\openclaw.json.

.PARAMETER StateFile
    Path to decoai-setup.json. Defaults to the copy next to this script.

.PARAMETER NoSd21
    Do not start the Stable Diffusion 2.1 session server.

.PARAMETER NoGeniex
    Do not start the Geniex vision server.

.PARAMETER NoAmazon
    Do not start the Amazon URL builder.

.PARAMETER SessionServerOnly
    Start only the SD2.1 session server and hold it in the foreground. Useful
    when the gateway is already running elsewhere.

.EXAMPLE
    .\start.ps1

.EXAMPLE
    .\start.ps1 -NoSd21 -NoGeniex
#>

param(
    [string]$Config    = "",
    [string]$StateFile = "",
    [switch]$NoSd21,
    [switch]$NoGeniex,
    [switch]$NoAmazon,
    [switch]$SessionServerOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step { param([string]$m) Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-OK   { param([string]$m) Write-Host "    [OK]   $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "    .      $m" -ForegroundColor DarkGray }
function Write-Warn { param([string]$m) Write-Host "    [WARN] $m" -ForegroundColor Yellow }
function Write-Fail { param([string]$m) Write-Host "    [FAIL] $m" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# Layout, from what setup.ps1 resolved
# ---------------------------------------------------------------------------

$ScriptDir = $PSScriptRoot
if (-not $StateFile) { $StateFile = Join-Path $ScriptDir "decoai-setup.json" }

if (-not (Test-Path -LiteralPath $StateFile)) {
    Write-Host ""
    Write-Fail "decoai-setup.json not found at $StateFile"
    Write-Host "    Run .\setup.ps1 first - it records where everything was installed."
    Write-Host ""
    exit 1
}

$state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json

$Workspace  = $state.workspace
$SkillsDir  = $state.skillsDir
$SkillsEnv  = $state.skillsEnv
$LogsDir    = $state.logsDir
$PyMain     = $state.pythonMain
$PySd21     = $state.pythonSd21
$ModelDir   = $state.modelDir
$GatewayPort = [int]$state.gatewayPort
$Sd21Port    = [int]$state.sd21Port
$GeniexPort  = [int]$state.geniexPort
$AmazonPort  = [int]$state.amazonPort

if (-not (Test-Path -LiteralPath $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

$GatewayLog = Join-Path $LogsDir "openclaw-gateway.log"
$Sd21Log    = Join-Path $LogsDir "sd21-session-server.log"
$GeniexLog  = Join-Path $LogsDir "geniex-server.log"
$AmazonLog  = Join-Path $LogsDir "amazon-url-builder.log"

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Magenta
Write-Host "   DecoAI Assistant - start" -ForegroundColor Magenta
Write-Host "  ============================================================" -ForegroundColor Magenta
Write-Host ""
Write-Host "   workspace  $Workspace"
Write-Host "   logs       $LogsDir"
Write-Host ""

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

Write-Step "Loading environment"

$loaded = 0
if (Test-Path -LiteralPath $SkillsEnv) {
    foreach ($line in (Get-Content -LiteralPath $SkillsEnv)) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        if ($v) {
            [System.Environment]::SetEnvironmentVariable($k, $v, "Process")
            $loaded++
        }
    }
    Write-OK "Loaded $loaded value(s) from $SkillsEnv"
} else {
    Write-Warn "$SkillsEnv not found - services will fall back to their defaults"
}

# Child processes inherit these, so the skills and the SD2.1 pipeline resolve
# the same paths the gateway does. SD21_PYTHON is what lets generate_cli.py
# reach the 3.12 interpreter, which lives on the data drive rather than inside
# the skill folder it looks in by default.
[System.Environment]::SetEnvironmentVariable("SD21_MODEL_DIR", $ModelDir, "Process")
[System.Environment]::SetEnvironmentVariable("SD21_PYTHON", $PySd21, "Process")
[System.Environment]::SetEnvironmentVariable("PYTHONUNBUFFERED", "1", "Process")

foreach ($k in @("ANTHROPIC_API_KEY", "CIRRASCALE_API_KEY")) {
    if ([string]::IsNullOrWhiteSpace([System.Environment]::GetEnvironmentVariable($k))) {
        Write-Warn "$k is not set - the features that need it will fail at runtime"
    }
}

# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

$script:Started = New-Object System.Collections.ArrayList

function Test-Port {
    param([string]$TargetHost = "127.0.0.1", [int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# Everything is launched through cmd.exe so stdout and stderr can share one
# log file - Start-Process refuses to redirect both streams to the same path.
function Start-Service-Process {
    param(
        [string]$Name,
        [string]$CommandLine,
        [string]$LogFile,
        [string]$WorkDir = ""
    )
    [System.IO.File]::WriteAllText($LogFile, "", (New-Object System.Text.UTF8Encoding($false)))
    # /s plus one pair of outer quotes: cmd strips exactly the first and last
    # quote and runs the rest verbatim. Without it, a command line that opens
    # with a quoted interpreter path gets mangled into "The system cannot find
    # the path specified" before the program is ever launched.
    $cmdArgs = "/s /c `"$CommandLine >> `"$LogFile`" 2>&1`""
    $splat = @{
        FilePath     = "cmd.exe"
        ArgumentList = $cmdArgs
        NoNewWindow  = $true
        PassThru     = $true
    }
    if ($WorkDir) { $splat["WorkingDirectory"] = $WorkDir }
    $p = Start-Process @splat
    [void]$script:Started.Add([pscustomobject]@{ Name = $Name; Proc = $p; Log = $LogFile })
    return $p
}

# Start-Process -PassThru hands back a process object whose ExitCode is often
# blank until it is refreshed, which turns a real failure into "(code )".
function Get-ExitCode {
    param($Proc)
    try {
        $Proc.Refresh()
        if ($null -ne $Proc.ExitCode) { return $Proc.ExitCode }
    } catch { }
    return "unknown"
}

function Wait-ForPort {
    param([string]$Name, [int]$Port, [int]$TimeoutSeconds, $Proc)
    Write-Info "waiting for $Name on port $Port (up to ${TimeoutSeconds}s)"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Proc -and $Proc.HasExited) {
            Write-Fail "$Name exited early (code $(Get-ExitCode $Proc)) - see $($script:Started | Where-Object { $_.Proc.Id -eq $Proc.Id } | ForEach-Object { $_.Log })"
            return $false
        }
        if (Test-Port -Port $Port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-All {
    if ($script:Started.Count -eq 0) { return }
    Write-Host ""
    Write-Step "Shutting down"
    # Reverse start order, so the gateway goes down before the model servers
    # it talks to.
    $items = $script:Started.ToArray()
    [array]::Reverse($items)
    $prevEap = $ErrorActionPreference
    # taskkill writes to stderr when a pid has already gone, which would be a
    # terminating error while the preference is Stop.
    $ErrorActionPreference = "Continue"
    foreach ($s in $items) {
        try {
            if (-not $s.Proc.HasExited) {
                & taskkill /F /T /PID $s.Proc.Id 2>&1 | Out-Null
                Write-OK "$($s.Name) stopped"
            }
            $s.Proc.Dispose()
        } catch {
            Write-Warn "Could not stop $($s.Name): $($_.Exception.Message)"
        }
    }
    $ErrorActionPreference = $prevEap
    $script:Started.Clear()
}

# ---------------------------------------------------------------------------
# 1. Stable Diffusion 2.1 session server
# ---------------------------------------------------------------------------

$sessionScript = Join-Path $SkillsDir "image-generation\Stable-Diffusion-2-1\session_server.py"

# setup.ps1 records where it put the package, but the model is often dropped in
# afterwards. Fall back to the usual spots so that works without a re-run.
if (-not (Test-Path -LiteralPath (Join-Path $ModelDir "metadata.json"))) {
    foreach ($c in @(
        (Join-Path $Workspace "models\stable-diffusion-2-1\Model_Bins"),
        (Join-Path $Workspace "models\Model_Bins"),
        (Join-Path $Workspace "models"),
        (Join-Path $SkillsDir "image-generation\Stable-Diffusion-2-1\Model_Bins")
    )) {
        if (Test-Path -LiteralPath (Join-Path $c "metadata.json")) {
            $ModelDir = $c
            [System.Environment]::SetEnvironmentVariable("SD21_MODEL_DIR", $ModelDir, "Process")
            Write-Info "using the SD2.1 package found at $ModelDir"
            break
        }
    }
}

if ($NoSd21) {
    Write-Step "Skipping the SD2.1 session server (-NoSd21)"
} elseif (-not (Test-Path -LiteralPath $ModelDir) -or
          -not (Test-Path -LiteralPath (Join-Path $ModelDir "metadata.json"))) {
    Write-Step "SD2.1 session server"
    Write-Warn "No model package in $ModelDir - local NPU generation is unavailable. Cloud generation still works."
} elseif (-not (Test-Path -LiteralPath $PySd21)) {
    Write-Step "SD2.1 session server"
    Write-Warn "Python 3.12 environment missing at $PySd21 - re-run setup.ps1"
} elseif (Test-Port -Port $Sd21Port) {
    Write-Step "SD2.1 session server"
    Write-OK "Already listening on port $Sd21Port - reusing it"
} else {
    Write-Step "Starting the SD2.1 session server on the NPU"
    Write-Info "loading QNN context binaries - the first start takes a minute or two"

    $cmd = "`"$PySd21`" `"$sessionScript`" --model_dir `"$ModelDir`""
    $p = Start-Service-Process -Name "sd21-session-server" -CommandLine $cmd -LogFile $Sd21Log `
        -WorkDir (Split-Path -Parent $sessionScript)

    if (Wait-ForPort -Name "sd21-session-server" -Port $Sd21Port -TimeoutSeconds 300 -Proc $p) {
        Write-OK "SD2.1 session server ready on 127.0.0.1:$Sd21Port"
        Write-Info "log: $Sd21Log"
    } else {
        Write-Warn "SD2.1 session server did not come up. See $Sd21Log. Continuing without local NPU generation."
    }
}

if ($SessionServerOnly) {
    Write-Host ""
    Write-Host "  Session server running. Press Ctrl+C to stop." -ForegroundColor Cyan
    Write-Host ""
    try {
        Get-Content -LiteralPath $Sd21Log -Wait -Encoding UTF8 | ForEach-Object { Write-Host "  $_" }
    } finally {
        Stop-All
    }
    exit 0
}

# ---------------------------------------------------------------------------
# 2. Amazon URL builder
# ---------------------------------------------------------------------------

$amazonDir = Join-Path $SkillsDir "amazon-url-builder"

if ($NoAmazon) {
    Write-Step "Skipping the Amazon URL builder (-NoAmazon)"
} elseif (Test-Port -Port $AmazonPort) {
    Write-Step "Amazon URL builder"
    Write-OK "Already listening on port $AmazonPort - reusing it"
} elseif (-not (Test-Path -LiteralPath (Join-Path $amazonDir "src\server.js"))) {
    Write-Step "Amazon URL builder"
    Write-Warn "src\server.js not found in $amazonDir"
} elseif (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Step "Amazon URL builder"
    Write-Warn "node is not on PATH - purchase links will not be generated"
} else {
    Write-Step "Starting the Amazon URL builder"
    [System.Environment]::SetEnvironmentVariable("PORT", "$AmazonPort", "Process")
    $p = Start-Service-Process -Name "amazon-url-builder" -CommandLine "node src\server.js" `
        -LogFile $AmazonLog -WorkDir $amazonDir
    if (Wait-ForPort -Name "amazon-url-builder" -Port $AmazonPort -TimeoutSeconds 20 -Proc $p) {
        Write-OK "Amazon URL builder ready on http://127.0.0.1:$AmazonPort"
    } else {
        Write-Warn "Amazon URL builder did not come up. See $AmazonLog"
    }
}

# ---------------------------------------------------------------------------
# 3. Geniex vision server
# ---------------------------------------------------------------------------

if ($NoGeniex) {
    Write-Step "Skipping Geniex (-NoGeniex)"
} elseif (Test-Port -Port $GeniexPort) {
    Write-Step "Geniex vision server"
    Write-OK "Already listening on port $GeniexPort - reusing it"
} elseif (-not (Get-Command geniex -ErrorAction SilentlyContinue)) {
    Write-Step "Geniex vision server"
    Write-Warn "geniex is not on PATH - decoration photo analysis falls back to a cloud model or mock data"
} else {
    Write-Step "Starting the Geniex vision server (Qwen2.5-VL on the NPU)"
    $p = Start-Service-Process -Name "geniex" `
        -CommandLine "geniex serve --host 127.0.0.1:$GeniexPort --compute npu" -LogFile $GeniexLog
    if (Wait-ForPort -Name "geniex" -Port $GeniexPort -TimeoutSeconds 120 -Proc $p) {
        Write-OK "Geniex ready on http://127.0.0.1:$GeniexPort"
        if ([string]::IsNullOrWhiteSpace([System.Environment]::GetEnvironmentVariable("IMAGE_READ_MODEL_URL"))) {
            Write-Info "set IMAGE_READ_MODEL_URL=http://127.0.0.1:$GeniexPort/v1 in Skills\.env to route photo analysis here"
        }
    } else {
        Write-Warn "Geniex did not come up. See $GeniexLog"
    }
}

# ---------------------------------------------------------------------------
# 4. OpenClaw gateway
# ---------------------------------------------------------------------------

Write-Step "Starting the OpenClaw gateway"

if (-not (Get-Command openclaw -ErrorAction SilentlyContinue)) {
    Write-Fail "openclaw is not on PATH - install it with: npm install -g openclaw"
    Stop-All
    exit 1
}
Write-OK "openclaw: $((Get-Command openclaw).Source)"

if (-not $Config) { $Config = Join-Path $state.openclawHome "openclaw.json" }
if (Test-Path -LiteralPath $Config) {
    Write-OK "config: $Config"
} else {
    Write-Warn "No config at $Config - openclaw will fall back to its defaults"
}

if (Test-Port -Port $GatewayPort) {
    Write-Fail "Port $GatewayPort is already in use - another gateway is running. Stop it first."
    Stop-All
    exit 1
}

# No --config flag: openclaw rejects it and exits. It reads the config from its
# own home directory, which is where setup.ps1 writes openclaw.json.
$gwCmd = "openclaw gateway run"
if ($Config -and (Test-Path -LiteralPath $Config) -and
    $Config -ne (Join-Path $state.openclawHome "openclaw.json")) {
    Write-Warn "openclaw has no --config option, so $Config will be ignored. Copy it to $(Join-Path $state.openclawHome 'openclaw.json') to use it."
}

$gateway = Start-Service-Process -Name "openclaw-gateway" -CommandLine $gwCmd -LogFile $GatewayLog -WorkDir $Workspace

if (Wait-ForPort -Name "openclaw-gateway" -Port $GatewayPort -TimeoutSeconds 60 -Proc $gateway) {
    Write-OK "Gateway ready on http://127.0.0.1:$GatewayPort"
} else {
    Write-Warn "Gateway did not open port $GatewayPort in time - tailing the log anyway"
}

# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   DecoAI is running" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
foreach ($s in $script:Started) {
    Write-Host ("   {0,-22} pid {1,-8} {2}" -f $s.Name, $s.Proc.Id, $s.Log)
}
Write-Host ""
Write-Host "   Control UI   http://127.0.0.1:$GatewayPort" -ForegroundColor Cyan
Write-Host "   Press Ctrl+C to stop everything." -ForegroundColor Cyan
Write-Host ""
Write-Host "   Tailing $GatewayLog" -ForegroundColor DarkGray
Write-Host ""

try {
    Get-Content -LiteralPath $GatewayLog -Wait -Encoding UTF8 | ForEach-Object {
        Write-Host "  $_"
        if ($gateway.HasExited) {
            Write-Warn "Gateway exited (code $(Get-ExitCode $gateway))"
            break
        }
    }
} finally {
    Stop-All
    Write-Host ""
    Write-OK "All services stopped"
    Write-Host ""
}
