<#
=============================================================================
 CylinderUI - cross-platform llama-bench wrapper (Windows / PowerShell)
-----------------------------------------------------------------------------
 Detects CUDA vs CPU and builds the RIGHT llama-bench.exe command line, then
 prints prompt (pp) and generation (tg) tok/s. Mirrors the agent model_bench.py.

 Usage:
   pwsh -File scripts\bench.ps1 <MODEL.gguf> [rapido|medio|detalhado] [-Json] [-Threads N] [-LlamaBench PATH]

 Profiles:
   rapido    : -p 512 -n 128                                  (1 run)
   medio     : thread sweep -t 4,8,... up to NUMBER_OF_PROCESSORS
   detalhado : thread sweep x prompt-length sweep (512, 2048)

 Command lines:
   Windows CUDA : llama-bench.exe -m M -ngl 99 -p 512 -n 128
   Windows CPU  : llama-bench.exe -m M -ngl 0  -t <NUMBER_OF_PROCESSORS> -p 512 -n 128
=============================================================================
#>
[CmdletBinding()]
param(
  [Parameter(Position=0)] [string]$Model,
  [Parameter(Position=1)] [ValidateSet('rapido','medio','detalhado')] [string]$Preset = 'rapido',
  [switch]$Json,
  [int]$Threads = 0,
  [string]$LlamaBench = '',
  [switch]$Cpu
)
$ErrorActionPreference = 'Stop'
function Log($m) { Write-Host "[cyl]  $m" -ForegroundColor Cyan }
function Ok($m)  { Write-Host "[ok]   $m" -ForegroundColor Green }
function Die($m) { Write-Host "[err]  $m" -ForegroundColor Red; exit 1 }

$ScriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PkgDir = Split-Path -Parent $ScriptsDir
$Root   = if ($env:CYL_REPO_ROOT) { $env:CYL_REPO_ROOT } else { Split-Path -Parent $PkgDir }
$Vendor = Join-Path $Root 'vendor'
$Models = Join-Path $Root 'models'

# ---- accelerator ------------------------------------------------------------
$Accel = 'cpu'
if (-not $Cpu) {
  $nv = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if ($nv) { try { & nvidia-smi | Out-Null; $Accel = 'cuda' } catch {} }
}
$Ngl = if ($Accel -eq 'cuda') { 99 } else { 0 }
$Ncpu = [int]$env:NUMBER_OF_PROCESSORS
if ($Ncpu -le 0) { $Ncpu = 4 }
if ($Threads -le 0) { $Threads = $Ncpu }

# ---- model ------------------------------------------------------------------
if (-not $Model) {
  $mf = if ($env:MODEL_FILE) { $env:MODEL_FILE } else { 'qwen2.5-0.5b-instruct-q4_k_m.gguf' }
  $Model = Join-Path $Models $mf
}
if (-not (Test-Path $Model)) {
  $alt = Join-Path $Models $Model
  if (Test-Path $alt) { $Model = $alt } else { Die "model not found: $Model" }
}

# ---- llama-bench ------------------------------------------------------------
if (-not $LlamaBench) {
  foreach ($c in @((Join-Path $Vendor 'llama.cpp\llama-bench.exe'), (Join-Path $Vendor 'llama-bench.exe'))) {
    if (Test-Path $c) { $LlamaBench = $c; break }
  }
  if (-not $LlamaBench) {
    $g = Get-Command llama-bench.exe -ErrorAction SilentlyContinue
    if ($g) { $LlamaBench = $g.Source }
  }
}
if (-not $LlamaBench -or -not (Test-Path $LlamaBench)) { Die "llama-bench.exe not found; run install.ps1 or pass -LlamaBench" }

# ---- thread sweep -----------------------------------------------------------
function Thread-List {
  $out = @(); $t = 4
  while ($t -lt $Ncpu) { $out += $t; $t *= 2 }
  $out += $Ncpu
  return ($out | Select-Object -Unique) -join ','
}

# ---- build args -------------------------------------------------------------
$Targ = @(); $Parg = '512'; $Narg = '128'
switch ($Preset) {
  'rapido'    { if ($Ngl -eq 0) { $Targ = @('-t', "$Threads") } }
  'medio'     { if ($Ngl -eq 0) { $Targ = @('-t', (Thread-List)) } else { $Targ = @('-t', "$Threads") } }
  'detalhado' { if ($Ngl -eq 0) { $Targ = @('-t', (Thread-List)) } else { $Targ = @('-t', "$Threads") }; $Parg = '512,2048' }
}

$argsList = @('-m', $Model, '-ngl', "$Ngl") + $Targ + @('-p', $Parg, '-n', $Narg)
if ($Json) { $argsList += @('-o','json') }

Log "model=$(Split-Path -Leaf $Model)  accel=$Accel  ngl=$Ngl  ncpu=$Ncpu  profile=$Preset"
Log "command: $LlamaBench $($argsList -join ' ')"
Write-Host ""
& $LlamaBench @argsList
if (-not $Json) { Write-Host ""; Ok "Columns: 'pp' = prompt tok/s, 'tg' = generation tok/s." }
