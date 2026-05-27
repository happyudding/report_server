@echo off
setlocal

if not defined PORT set "PORT=8000"

echo [terminate] Stopping server on port %PORT% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pids = @(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); " ^
  "if ($pids.Count -eq 0) { Write-Host '[terminate] No LISTENING process on port %PORT%.'; exit 0 }; " ^
  "foreach ($procId in $pids) { Write-Host ('[terminate] Killing PID ' + $procId); Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }; " ^
  "Write-Host '[terminate] Done.'"

endlocal
