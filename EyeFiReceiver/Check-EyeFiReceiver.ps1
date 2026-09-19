<#
  Check-EyeFiReceiver.ps1 — 소크 기간 건강 점검 (더블클릭 또는 실행)
  누가 수신 중인지, 오늘 몇 장 받았는지, 최근 오류가 있는지 한눈에.
#>
$ErrorActionPreference = "SilentlyContinue"
Write-Host "============ Eye-Fi 수신기 상태 ============" -ForegroundColor Cyan

$py  = Get-Process pythonw -EA SilentlyContinue
$off = Get-Process EyeFiX2Receiver -EA SilentlyContinue
$l   = Get-NetTCPConnection -LocalPort 59278 -State Listen -EA SilentlyContinue
$owner = if ($l) { (Get-Process -Id $l.OwningProcess -EA SilentlyContinue).ProcessName } else { "(없음)" }

$whom = switch ($owner) {
  "pythonw"          { "우리 앱 (커스텀 수신기)" }
  "EyeFiX2Receiver"  { "공식 앱" }
  default            { $owner }
}
Write-Host ("59278 수신 중  : {0}" -f $whom) -ForegroundColor $(if($owner -ne '(없음)'){'Green'}else{'Red'})
Write-Host ("우리앱 pythonw : {0}" -f $(if($py){"실행중 (PID $($py.Id -join ','))"}else{"꺼짐"}))
Write-Host ("공식앱          : {0}" -f $(if($off){"실행중 (PID $($off.Id))"}else{"꺼짐"}))

$today = Join-Path "E:\Eye-Fi Down Folder" (Get-Date -Format "yyyy-MM-dd")
$cnt = if (Test-Path $today) { (Get-ChildItem $today -File -EA SilentlyContinue).Count } else { 0 }
Write-Host ("오늘 받은 사진 : {0}장  ({1})" -f $cnt, $today)

$log = "C:\EyeFiReceiver\EyeFiReceiver\receiver.log"
if (Test-Path $log) {
  $errs = Select-String -Path $log -Pattern "오류|실패" | Select-Object -Last 3
  Write-Host "`n최근 오류(있으면 시각 확인):"
  if ($errs) { $errs | ForEach-Object { Write-Host ("  " + $_.Line) -ForegroundColor Yellow } }
  else       { Write-Host "  없음 ✅" -ForegroundColor Green }
}
Write-Host "`n[되돌리기] 문제 시: 트레이 우클릭>종료 후 공식앱 실행" -ForegroundColor DarkGray
Write-Host '  Start-Process "C:\Program Files (x86)\Eye-Fi\EyeFiX2Receiver.exe"' -ForegroundColor DarkGray
Write-Host "===========================================" -ForegroundColor Cyan
