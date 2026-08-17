<#
=============================================================================
 CylinderUI for Llama.cpp - turn-key installer (Windows / PowerShell)
-----------------------------------------------------------------------------
 Equivalent to install.sh -- idempotent & NON-DESTRUCTIVE (respects an existing
 install; never overwrites llama.cpp, llama-swap, models or user configs):
   1. Detect CUDA (nvidia-smi) vs CPU
   2. Ensure Python 3.11+ (winget hint if missing; never silent elevation)
   3. Create .venv and install requirements (agent deps)
   4. Download a PRE-COMPILED llama.cpp release -- but REUSE an existing
      llama.cpp (PATH / env / common paths) instead of downloading
   5. Download llama-swap.exe release -- likewise REUSED if already present
   6. Download 1 small initial GGUF model -- SKIPPED if the models dir already
      has .gguf files (existing models are never touched)
   7. .env / router-config.json: created from .example only if MISSING; an
      existing user config is PRESERVED intact, never overwritten
   8. Start services (router + agent-if-present + llama-swap); a port already in
      use is reused, not killed (see -Restart)

 SECURITY: no secrets written. Every network download is printed and confirmed
 at runtime (unless -Yes). No blind privilege elevation.

 Env overrides (reuse an existing install): LLAMA_CPP_DIR / LLAMA_CPP_BIN,
 LLAMA_SWAP_BIN, MODELS_DIR.

 Usage:
   pwsh -File install.ps1 [-Action install|run|stop|status]
                          [-Cpu] [-NoModel] [-Dev] [-Yes]
                          [-ForceLlama] [-ForceSwap] [-Restart] [-Model URL]
=============================================================================
#>
[CmdletBinding()]
param(
  [ValidateSet('install','run','stop','status')] [string]$Action = 'install',
  [switch]$Cpu,
  [switch]$NoModel,
  [switch]$Dev,
  [switch]$Yes,
  [switch]$ForceLlama,
  [switch]$ForceSwap,
  [switch]$Restart,
  [string]$Model
)
$ErrorActionPreference = 'Stop'

# ---- paths ------------------------------------------------------------------
$PkgDir   = Split-Path -Parent $MyInvocation.MyCommand.Path      # cylinderui-scripts
$Root     = if ($env:CYL_REPO_ROOT) { $env:CYL_REPO_ROOT } else { Split-Path -Parent $PkgDir }
$VenvDir  = Join-Path $Root '.venv'
$Vendor   = Join-Path $Root 'vendor'
$Models   = if ($env:MODELS_DIR) { $env:MODELS_DIR } else { Join-Path $Root 'models' }  # honor user MODELS_DIR
$RunDir   = Join-Path $Root 'run'
$LogDir   = Join-Path $Root 'logs'
$RouterDir= Join-Path $Root 'router'
$AgentDir = @(Join-Path $Root 'agent'; Join-Path $Root 'native-agent-v2') | Where-Object { Test-Path $_ } | Select-Object -First 1
foreach ($d in @($Vendor,$Models,$RunDir,$LogDir)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

# ---- logging ----------------------------------------------------------------
function Log($m)  { Write-Host "[cyl]  $m"  -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ok]   $m"  -ForegroundColor Green }
function Warn($m) { Write-Host "[warn] $m"  -ForegroundColor Yellow }
function Die($m)  { Write-Host "[err]  $m"  -ForegroundColor Red; exit 1 }

# ---- .env load --------------------------------------------------------------
function Load-Env {
  $envf = Join-Path $PkgDir '.env'
  if (Test-Path $envf) {
    Get-Content $envf | ForEach-Object {
      if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$' -and $_ -notmatch '^\s*#') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
      }
    }
  }
}
Load-Env
$RouterPort = $(if ($env:PROMPT_ROUTER_PORT) { $env:PROMPT_ROUTER_PORT } else { '8088' })
$AgentPort  = $(if ($env:AGENT_PORT)         { $env:AGENT_PORT }         else { '3000' })
$SwapPort   = $(if ($env:LLAMA_SWAP_PORT)    { $env:LLAMA_SWAP_PORT }    else { '8080' })
$RouterHost = $(if ($env:PROMPT_ROUTER_HOST) { $env:PROMPT_ROUTER_HOST } else { '0.0.0.0' })
$SwapUrl    = $(if ($env:LLAMA_SWAP_URL)     { $env:LLAMA_SWAP_URL }     else { "http://127.0.0.1:$SwapPort" })
$RouterUrl  = $(if ($env:ROUTER_URL)         { $env:ROUTER_URL }         else { "http://127.0.0.1:$RouterPort/v1" })
$AgentModel = $(if ($env:AGENT_MODEL)        { $env:AGENT_MODEL }        else { 'qwen2.5-0.5b-instruct' })
$ModelUrl   = $(if ($env:MODEL_URL)          { $env:MODEL_URL }          else { 'https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf' })
$ModelFile  = $(if ($env:MODEL_FILE)         { $env:MODEL_FILE }         else { 'qwen2.5-0.5b-instruct-q4_k_m.gguf' })

# ---- accelerator detection --------------------------------------------------
function Detect-Accel {
  if ($Cpu) { return 'cpu' }
  $nv = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if ($nv) { try { & nvidia-smi | Out-Null; return 'cuda' } catch { return 'cpu' } }
  return 'cpu'
}
$Accel = Detect-Accel

# ---- confirm gate for network ops -------------------------------------------
function Confirm-Net($desc) {
  if ($Yes -or $env:CYL_ASSUME_YES -eq '1') { Log "auto-approve (-Yes): $desc"; return $true }
  Write-Host "[net]  About to: $desc" -ForegroundColor Yellow
  $a = Read-Host "      Proceed with this network operation? [y/N]"
  return ($a -match '^(y|yes)$')
}
function Fetch($url,$out) { Log "downloading -> $out"; Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing }

function Port-Busy($port) {
  try { return [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) }
  catch { return $false }
}
function Pidfile($name) { Join-Path $RunDir "$name.pid" }
function Is-Running($name) {
  $pf = Pidfile $name
  if (-not (Test-Path $pf)) { return $false }
  $procId = Get-Content $pf -ErrorAction SilentlyContinue
  return [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue)
}

# ---- existing-install detection (idempotent, non-destructive) ---------------
# RESPECT what the user already has: detect llama.cpp / llama-swap / models so we
# REUSE them and skip the download. Nothing here deletes or overwrites anything.
function Find-LlamaCpp {
  if ($env:LLAMA_CPP_BIN) {
    $d = $env:LLAMA_CPP_BIN
    if (Test-Path $d -PathType Leaf) { $d = Split-Path -Parent $d }
    if ((Test-Path (Join-Path $d 'llama-server.exe')) -and (Test-Path (Join-Path $d 'llama-bench.exe'))) { return $d }
  }
  if ($env:LLAMA_CPP_DIR) {
    foreach ($d in @($env:LLAMA_CPP_DIR, (Join-Path $env:LLAMA_CPP_DIR 'build\bin'), (Join-Path $env:LLAMA_CPP_DIR 'bin'))) {
      if ((Test-Path (Join-Path $d 'llama-server.exe')) -and (Test-Path (Join-Path $d 'llama-bench.exe'))) { return $d }
    }
  }
  $srv = Get-Command llama-server.exe -ErrorAction SilentlyContinue
  $bch = Get-Command llama-bench.exe  -ErrorAction SilentlyContinue
  if ($srv -and $bch) { return (Split-Path -Parent $srv.Source) }
  foreach ($d in @(
      (Join-Path $env:USERPROFILE 'llama.cpp\build\bin'),
      (Join-Path $env:USERPROFILE 'llama.cpp\bin'),
      (Join-Path $env:USERPROFILE 'llama.cpp'),
      (Join-Path $env:LOCALAPPDATA 'llama.cpp\build\bin'),
      (Join-Path $Vendor 'llama.cpp'))) {
    if ($d -and (Test-Path (Join-Path $d 'llama-server.exe')) -and (Test-Path (Join-Path $d 'llama-bench.exe'))) { return $d }
  }
  return $null
}
function Find-LlamaSwap {
  if ($env:LLAMA_SWAP_BIN -and (Test-Path $env:LLAMA_SWAP_BIN)) { return $env:LLAMA_SWAP_BIN }
  $c = Get-Command llama-swap.exe -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in @(
      (Join-Path $env:USERPROFILE 'llama-swap.exe'),
      (Join-Path $env:USERPROFILE '.local\bin\llama-swap.exe'),
      (Join-Path $Vendor 'llama-swap.exe'))) {
    if ($p -and (Test-Path $p)) { return $p }
  }
  return $null
}
function Models-Present($dir) {
  if (-not (Test-Path $dir)) { return $null }
  $g = Get-ChildItem -Path $dir -Filter '*.gguf' -File -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($g) { return $dir } else { return $null }
}
# Preserve-or-create a config: NEVER overwrites an existing user config.
function Preserve-Config($target,$example) {
  if (Test-Path $target) { Ok "config preservada: $target (inalterada; compare com $(Split-Path -Leaf $example) p/ novas opcoes)"; return }
  if (Test-Path $example) { Copy-Item $example $target; Ok "criado $target" }
}

# =============================================================================
# lifecycle: run / stop / status
# =============================================================================
function Start-Svc($name,$port,$logf,$exe,[string[]]$svcArgs) {
  if (Is-Running $name) { Ok "$name already running"; return }
  if (Port-Busy $port)  { Warn "$name: port $port already in use -- skipping"; return }
  Log "starting $name on :$port"
  $p = Start-Process -FilePath $exe -ArgumentList $svcArgs -RedirectStandardOutput $logf `
         -RedirectStandardError "$logf.err" -WindowStyle Hidden -PassThru
  $p.Id | Out-File -Encoding ascii (Pidfile $name)
  Start-Sleep -Seconds 1
  if (Is-Running $name) { Ok "$name up (pid $($p.Id), log $logf)" } else { Warn "$name may have failed -- see $logf" }
}

function Do-Run {
  $py = Join-Path $VenvDir 'Scripts\python.exe'
  # 1. llama-swap
  $swap = Join-Path $Vendor 'llama-swap.exe'
  $swapCfg = Join-Path $Vendor 'llama-swap.yaml'
  if (Test-Path $swap) {
    if (-not (Test-Path $swapCfg)) {
      $srv = Join-Path $Vendor 'llama.cpp\llama-server.exe'
@"
# Auto-generated by CylinderUI install.ps1. Edit to add models.
models:
  "$AgentModel":
    cmd: >
      $srv -m $Models\$ModelFile --port `${PORT} -ngl 99
"@ | Set-Content -Encoding utf8 $swapCfg
      Ok "generated $swapCfg"
    }
    Start-Svc 'llama-swap' $SwapPort (Join-Path $LogDir 'llama-swap.log') $swap `
      @('--listen', "127.0.0.1:$SwapPort", '--config', $swapCfg)
  } else { Warn "llama-swap.exe missing ($swap) -- inference disabled" }

  # 2. agent (if present)
  if ($AgentDir -and (Test-Path (Join-Path $AgentDir 'app\main.py'))) {
    $env:ROUTER_URL=$RouterUrl; $env:LLAMA_SWAP_URL=$SwapUrl; $env:AGENT_MODEL=$AgentModel; $env:LOG_DIR=$LogDir
    Start-Svc 'agent' $AgentPort (Join-Path $LogDir 'agent.log') $py `
      @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$AgentPort",'--app-dir',$AgentDir)
  } else { Warn "agent not found -- Visões / Model Store / Benchmark / RAG unavailable (router-only mode)" }

  # 3. router (public entrypoint)
  $routerPy = Join-Path $RouterDir 'router.py'
  if (Test-Path $routerPy) {
    $env:PROMPT_ROUTER_PORT=$RouterPort; $env:PROMPT_ROUTER_HOST=$RouterHost; $env:LLAMA_SWAP_URL=$SwapUrl
    Start-Svc 'router' $RouterPort (Join-Path $LogDir 'router.log') $py @($routerPy)
  } else { Die "router.py not found at $RouterDir" }

  Ok "Open: http://localhost:$RouterPort"
}

function Do-Stop {
  foreach ($name in @('router','agent','llama-swap')) {
    $pf = Pidfile $name
    if (Is-Running $name) {
      $procId = Get-Content $pf
      Log "stopping $name (pid $procId)"
      Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
      Ok "$name stopped"
    } else { Log "$name not running" }
    Remove-Item $pf -ErrorAction SilentlyContinue
  }
}

function Do-Status {
  Write-Host "Platform : windows / accel=$Accel"
  Write-Host "Root     : $Root"
  Write-Host "Agent    : $(if ($AgentDir) { $AgentDir } else { '<not in repo>' })"
  Write-Host ""
  foreach ($s in @(@('router',$RouterPort),@('agent',$AgentPort),@('llama-swap',$SwapPort))) {
    $state = if (Is-Running $s[0]) { 'UP  ' } else { 'DOWN' }
    $port  = if (Port-Busy $s[1]) { 'listening' } else { 'free' }
    Write-Host ("  {0,-11} {1}  port {2,-5} ({3})" -f $s[0],$state,$s[1],$port)
  }
  Write-Host "`nUI: http://localhost:$RouterPort"
}

if ($Action -eq 'run')    { Do-Run;    return }
if ($Action -eq 'stop')   { Do-Stop;   return }
if ($Action -eq 'status') { Do-Status; return }

# =============================================================================
# install
# =============================================================================
Log "OS=windows  accel=$Accel  root=$Root"

# 2. Python 3.11+
$py = $null
foreach ($c in @('python','py')) {
  $cmd = Get-Command $c -ErrorAction SilentlyContinue
  if ($cmd) {
    try {
      $v = & $c -c 'import sys;print("%d.%d"%sys.version_info[:2])'
      if ([version]$v -ge [version]'3.11') { $py = $c; break }
    } catch {}
  }
}
if (-not $py) {
  Warn "Python 3.11+ not found. Install it, e.g.:  winget install Python.Python.3.12"
  Die  "install Python 3.11+ and re-run"
}
Ok "python: $py"

# 3. venv + requirements
if (-not (Test-Path $VenvDir)) { Log "creating venv"; & $py -m venv $VenvDir }
$pyv = Join-Path $VenvDir 'Scripts\python.exe'
& $pyv -m pip install --quiet --upgrade pip
$req = Join-Path $PkgDir 'requirements.txt'
if ((Test-Path $req) -and (Confirm-Net 'pip install -r requirements.txt (agent deps)')) {
  try { & $pyv -m pip install -r $req } catch { Warn "pip errors (ok if router-only)" }
}
Ok "venv ready"

# 4. llama.cpp -- reuse an existing install, else pre-compiled release (CUDA/CPU)
$existingLlama = if (-not $ForceLlama) { Find-LlamaCpp } else { $null }
$llDir = Join-Path $Vendor 'llama.cpp'
if ($existingLlama) {
  Ok "llama.cpp detectado em $existingLlama -- usando o existente (-ForceLlama para reinstalar)"
} else {
New-Item -ItemType Directory -Force -Path $llDir | Out-Null
if (-not (Test-Path (Join-Path $llDir 'llama-bench.exe'))) {
  # NOTE: verify the exact asset name at the releases page before running.
  $variant = if ($Accel -eq 'cuda') { 'cuda' } else { 'cpu (avx2)' }
  $base = 'https://github.com/ggml-org/llama.cpp/releases/latest/download'
  $asset = if ($Accel -eq 'cuda') { 'llama-bXXXX-bin-win-cuda-x64.zip' } else { 'llama-bXXXX-bin-win-cpu-x64.zip' }
  Warn "llama.cpp Windows release naming changes per build. Target variant: $variant"
  Warn "Pick the matching .zip at https://github.com/ggml-org/llama.cpp/releases and set its URL."
  if (Confirm-Net "download llama.cpp Windows $variant release ($base/$asset)") {
    $zip = Join-Path $env:TEMP 'llamacpp.zip'
    try {
      Fetch "$base/$asset" $zip
      Expand-Archive -Path $zip -DestinationPath $llDir -Force
      Ok "llama.cpp extracted -> $llDir"
    } catch { Warn "download/extract failed -- fetch the zip manually into $llDir (needs llama-server.exe + llama-bench.exe)" }
  }
} else { Ok "llama.cpp already present ($llDir)" }
}

# 5. llama-swap.exe -- reuse an existing install, else download the release
$existingSwap = if (-not $ForceSwap) { Find-LlamaSwap } else { $null }
$swapExe = Join-Path $Vendor 'llama-swap.exe'
if ($existingSwap) {
  Ok "llama-swap detectado em $existingSwap -- usando o existente (-ForceSwap para reinstalar)"
} elseif (-not (Test-Path $swapExe)) {
  $base = 'https://github.com/mostlygeek/llama-swap/releases/latest/download'
  $asset = 'llama-swap_windows_amd64.zip'
  if (Confirm-Net "download llama-swap release ($base/$asset)") {
    $zip = Join-Path $env:TEMP 'llamaswap.zip'
    try {
      Fetch "$base/$asset" $zip
      Expand-Archive -Path $zip -DestinationPath $Vendor -Force
      $found = Get-ChildItem $Vendor -Recurse -Filter 'llama-swap*.exe' | Select-Object -First 1
      if ($found) { Copy-Item $found.FullName $swapExe -Force; Ok "llama-swap.exe -> $swapExe" }
      else { Warn "llama-swap.exe not found in archive; place it at $swapExe" }
    } catch { Warn "download failed; grab it manually and place at $swapExe" }
  }
} else { Ok "llama-swap.exe already present" }

# 6. initial model -- skip if the models dir already has .gguf files
if (-not $NoModel) {
  if ($Model) { $ModelUrl = $Model; $ModelFile = Split-Path -Leaf $Model }  # -Model always adds
  $present = Models-Present $Models
  if ($present -and -not $Model) {
    Ok "modelos ja presentes em $present -- pulando download (use -Model URL para adicionar)"
  } else {
    $target = Join-Path $Models $ModelFile
    if (Test-Path $target) { Ok "model already present: $target" }
    else {
      Log "initial model URL (change via MODEL_URL in .env or -Model): $ModelUrl"
      if (Confirm-Net "download GGUF model (~hundreds of MB): $ModelUrl") {
        try { Fetch $ModelUrl $target; Ok "model -> $target" } catch { Warn "model download failed" }
      }
    }
  }
} else { Warn "-NoModel: skipping model download (put a .gguf in $Models yourself)" }

# 7. configs -- create if missing, PRESERVE existing user configs (never overwrite)
Preserve-Config (Join-Path $PkgDir '.env') (Join-Path $PkgDir '.env.example')
if (Test-Path $RouterDir) {
  Preserve-Config (Join-Path $RouterDir 'router-config.json') (Join-Path $PkgDir 'router-config.example.json')
} else { Warn "router/ folder not found -- copy the published router into $RouterDir" }

# 8. start -- reuse a busy port unless -Restart was passed
if ($Dev) { Ok "install complete (-Dev: services NOT started). Start with: install.ps1 -Action run" }
else {
  if ($Restart) { Log "-Restart: stopping running services first"; Do-Stop }
  else {
    foreach ($pp in @($RouterPort,$AgentPort,$SwapPort)) {
      if (Port-Busy $pp) { Warn "porta $pp ja em uso -- run reutiliza o servico existente (use -Restart para reiniciar)" }
    }
  }
  Do-Run
}

Write-Host ""
Ok  "CylinderUI install finished."
Log "Open:  http://localhost:$RouterPort"
if (-not $AgentDir) {
  Warn "AGENT (native-agent-v2) NOT found. Router + UI work, but Visões, Model Store,"
  Warn "Benchmark and RAG need the agent. Put it in 'agent\' or 'native-agent-v2\' at the"
  Warn "repo root, then re-run install.ps1."
}
