<#
.SYNOPSIS
    Installs the Corpus2Skill Memory Provider plugin into a Windows-native
    Hermes install (User Provider tier: $HERMES_HOME\plugins\corpus2skill\).

.DESCRIPTION
    Run this on any PC that already has the Hermes Agent Windows app
    installed. It downloads the plugin source from the H-H-Agent GitHub
    fork, writes the Corpus2Skill API key into Hermes' .env, and sets
    memory.provider via Hermes' own `hermes config set` command (safe,
    idempotent YAML merge -- this script never hand-edits config.yaml).

    Background / design doc: docs/hh-agent/03_Architecture.md section 13
    in the Hermes-Hyper-Agent_HHAgent repo.

.PARAMETER ApiKey
    The Corpus2Skill Bearer token (same value as C2S_API_KEY on the
    Corpus2Skill side). If omitted, you will be prompted.

.PARAMETER HermesHome
    Path to the Hermes home directory. Defaults to $env:HERMES_HOME, or
    "$env:LOCALAPPDATA\hermes" if that is not set.

.EXAMPLE
    .\install_corpus2skill_plugin.ps1 -ApiKey "your-bearer-token"
#>

param(
    [string]$ApiKey,
    [string]$HermesHome
)

$ErrorActionPreference = "Stop"

$PluginRawBaseUrl = "https://raw.githubusercontent.com/hayashi0711-sketch/hermes-agent/hh-agent/.hermes/plugins/corpus2skill"
$PluginFiles = @("__init__.py", "plugin.yaml", "README.md")

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "OK: $Message" -ForegroundColor Green
}

function Write-Warn2 {
    param([string]$Message)
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
}

# --- Resolve HERMES_HOME ---
if (-not $HermesHome) {
    if ($env:HERMES_HOME) {
        $HermesHome = $env:HERMES_HOME
    } else {
        $HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
    }
}

if (-not (Test-Path $HermesHome)) {
    throw "Hermes home directory not found: $HermesHome`nInstall the Hermes Agent Windows app first, or pass -HermesHome explicitly."
}
Write-Step "Using Hermes home: $HermesHome"

# --- Resolve hermes.exe ---
$HermesExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\hermes.exe"
if (-not (Test-Path $HermesExe)) {
    $cmd = Get-Command hermes.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $HermesExe = $cmd.Source
    } else {
        throw "Could not find hermes.exe (looked in $HermesExe and PATH). Pass -HermesHome if your install layout differs."
    }
}
Write-Step "Using hermes.exe: $HermesExe"

# --- Prompt for API key if not supplied ---
if (-not $ApiKey) {
    $secure = Read-Host -Prompt "Enter the Corpus2Skill API key (Bearer token)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}
if (-not $ApiKey -or $ApiKey.Trim().Length -eq 0) {
    throw "No API key provided. Aborting."
}

# --- Download plugin files ---
$PluginDir = Join-Path $HermesHome "plugins\corpus2skill"
Write-Step "Installing plugin files to $PluginDir"
New-Item -ItemType Directory -Force -Path $PluginDir | Out-Null

foreach ($file in $PluginFiles) {
    $url = "$PluginRawBaseUrl/$file"
    $dest = Join-Path $PluginDir $file
    Write-Host "  downloading $file ..."
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}
Write-Ok "Plugin files installed."

# --- Update .env (idempotent) ---
$EnvPath = Join-Path $HermesHome ".env"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"

if (Test-Path $EnvPath) {
    $envContent = Get-Content -Path $EnvPath -Raw -Encoding UTF8
    Copy-Item -Path $EnvPath -Destination "$EnvPath.bak.$timestamp" -Force
    Write-Step "Backed up .env to .env.bak.$timestamp"
} else {
    $envContent = ""
}

if ($envContent -match "(?m)^CORPUS2SKILL_API_KEY=") {
    Write-Warn2 "CORPUS2SKILL_API_KEY already present in .env -- leaving it unchanged. Edit .env manually if you need to rotate the key."
} else {
    Add-Content -Path $EnvPath -Value "`nCORPUS2SKILL_API_KEY=$ApiKey" -Encoding UTF8
    Write-Ok "Added CORPUS2SKILL_API_KEY to .env"
}

# --- Set memory.provider via Hermes' own config command (safe YAML merge) ---
$ConfigPath = Join-Path $HermesHome "config.yaml"
if (Test-Path $ConfigPath) {
    Copy-Item -Path $ConfigPath -Destination "$ConfigPath.bak.$timestamp" -Force
    Write-Step "Backed up config.yaml to config.yaml.bak.$timestamp"
}

Write-Step "Setting memory.provider = corpus2skill"
& $HermesExe config set memory.provider corpus2skill
if ($LASTEXITCODE -ne 0) {
    throw "hermes config set failed with exit code $LASTEXITCODE"
}
Write-Ok "memory.provider set."

# --- Done ---
Write-Host ""
Write-Ok "Corpus2Skill Memory Provider plugin installed."
Write-Host "Next step: restart the Hermes gateway for the change to take effect:"
Write-Host "  & `"$HermesExe`" gateway restart"
Write-Host "(or fully quit and reopen the Hermes desktop app if you are not using the gateway service)"
