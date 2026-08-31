# Supervisor for the live run. Keeps the agent up across the whole scoring
# window without anyone watching it.
#
#     powershell -ExecutionPolicy Bypass -File keepalive.ps1            # DEV
#     powershell -ExecutionPolicy Bypass -File keepalive.ps1 -Comp      # SCORED
#
# run_forever already survives a bad cycle and sleeps through closed hours, so
# this is not about ordinary errors. It exists for the failures that kill the
# PROCESS rather than the cycle:
#
#   - the alpaca-mcp-server subprocess dies. That connection is opened outside
#     the agent's try/except, so every later call fails and the loop logs
#     "cycle raised" forever without reconnecting. A zombie that looks alive in
#     the log is worse than a crash, and only a restart clears it.
#   - the network drops long enough to break the stdio transport
#   - an unhandled exception escapes asyncio.run
#   - Windows restarts the machine overnight for an update
#
# Restarting is safe by construction. State is written atomically with a .bak,
# and every order carries a deterministic client_order_id that the executor
# checks for before placing anything, so a restart mid-order adopts the
# existing order instead of duplicating it. tools/scenarios.py covers exactly
# this path.

param(
    [switch]$Comp,
    # Thu 3 Sep 16:00 ET, in UTC. Past this nothing counts, so the supervisor
    # stops rather than restarting into a market that cannot score us.
    [datetime]$DeadlineUtc = [datetime]::ParseExact(
        "2026-09-03T20:00:00Z", "yyyy-MM-ddTHH:mm:ssZ",
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AdjustToUniversal)
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "no venv at $python -- run: uv venv .venv --python 3.11" }

$agentArgs = @("run.py")
if ($Comp) { $agentArgs += "--comp" }

New-Item -ItemType Directory -Force -Path "runs" | Out-Null
$superLog = "runs\supervisor.log"

function Write-Super($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $superLog -Value $line -Encoding utf8
}

$target = if ($Comp) { "COMPETITION (scored)" } else { "dev" }
Write-Super "supervisor starting -- account: $target"
Write-Super "will stop at $($DeadlineUtc.ToString('u')) (Thu 3 Sep 16:00 ET)"

# Backoff so a config error that fails instantly cannot spin the CPU for four
# days. Resets whenever a run survives long enough to have been doing work.
$delay = 5
$restarts = 0

while ([datetime]::UtcNow -lt $DeadlineUtc) {
    $started = Get-Date
    Write-Super "launching: python $($agentArgs -join ' ')"

    & $python @agentArgs
    $code = $LASTEXITCODE

    $ranFor = [int]((Get-Date) - $started).TotalSeconds
    if ([datetime]::UtcNow -ge $DeadlineUtc) {
        Write-Super "past the mark; not restarting (exit $code after ${ranFor}s)"
        break
    }

    # A clean exit after a long run is the agent stopping itself at the mark.
    # A clean exit after seconds is something refusing to start, and restarting
    # into it forever would just fill the log.
    if ($code -eq 0 -and $ranFor -gt 300) {
        Write-Super "agent exited cleanly after ${ranFor}s; treating as finished"
        break
    }

    $restarts++
    if ($ranFor -gt 120) { $delay = 5 } else { $delay = [Math]::Min($delay * 2, 300) }
    Write-Super "exit $code after ${ranFor}s -- restart #$restarts in ${delay}s"
    Start-Sleep -Seconds $delay
}

Write-Super "supervisor done after $restarts restart(s)"
