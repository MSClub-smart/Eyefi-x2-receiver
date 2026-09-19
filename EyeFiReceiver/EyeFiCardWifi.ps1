<#
  EyeFiCardWifi.ps1 — 리더에 꽂힌 Eye-Fi X2 카드의 Wi-Fi 네트워크를 조회/테스트/등록/삭제.
  클라우드 불필요 — 공식 EyeFiCard.dll 만 사용 (시그니처는 공식 수신앱의 P/Invoke 선언에서 확정).

  사용:
    powershell -ExecutionPolicy Bypass -File EyeFiCardWifi.ps1 -Action list   [-Drive F:]
    powershell -ExecutionPolicy Bypass -File EyeFiCardWifi.ps1 -Action test   -Ssid Eyefi -Key pw [-Auth N]
    powershell -ExecutionPolicy Bypass -File EyeFiCardWifi.ps1 -Action add    -Ssid Eyefi -Key pw [-Auth N]
    powershell -ExecutionPolicy Bypass -File EyeFiCardWifi.ps1 -Action delete -Ssid old_ssid

  -Auth 생략 시: 카드가 스캔한 목록에서 해당 SSID 의 보안 flags 를 찾아 그대로 사용
  (공식앱과 동일한 방식 — IL 해독으로 확인). 스캔에 없으면 -Auth 지정 필요.
  SSID/키는 Ansi 마샬링(공식앱과 동일)이므로 ASCII 권장.

  출력: JSON 한 줄. 성공 {"ok":true, "action":.., "mac":.., "configured":[..], "scanned":[..], ...}
        실패 {"ok":false, "error":"사유"}
  주의: EyeFiCard.dll 은 32비트 → 자동으로 32비트 PowerShell 재실행.
        실행 전 공식 EyeFiX2Receiver 종료 필요(카드 접근 잠금).
#>
param(
  [ValidateSet('list','test','add','delete','info')][string]$Action = 'list',
  [string]$Ssid,
  [string]$Key = '',
  [int]$Auth = -1,
  [string]$Drive,
  [string]$InstallDir = 'C:\Program Files (x86)\Eye-Fi'
)

# --- 32비트로 자동 재실행 ---
if([Environment]::Is64BitProcess){
  $ps32 = "$env:WINDIR\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
  $argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath,
               '-Action',$Action,'-Auth',$Auth,'-InstallDir',$InstallDir)
  if($Ssid){ $argList += @('-Ssid',$Ssid) }
  if($Key){  $argList += @('-Key',$Key) }
  if($Drive){ $argList += @('-Drive',$Drive) }
  & $ps32 @argList
  exit $LASTEXITCODE
}

function Emit($obj, $code){
  [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 6))
  exit $code
}

if(-not (Test-Path (Join-Path $InstallDir 'EyeFiCard.dll'))){
  Emit @{ ok=$false; error="EyeFiCard.dll 없음: $InstallDir" } 10
}
if($Action -in @('test','add','delete') -and -not $Ssid){
  Emit @{ ok=$false; error="-Ssid 필요 (action=$Action)" } 11
}

$code = @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class EF {
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetDllDirectory(string p);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  public static extern bool GetVolumeInformationW(string root, StringBuilder v, int vs, out uint ser, out uint mcl, out uint fl, StringBuilder f, int fs);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Unicode)] public static extern IntPtr InitEyeFiCard(string path);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int EyeFiCardWrapper_Term(IntPtr c);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int EyeFiCardWrapper_GetToken(IntPtr c, byte t, IntPtr b, ref int n);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int EyeFiCardWrapper_CheckFATID(uint v);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int EyeFiCardWrapper_GetStatus(IntPtr c);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl)] public static extern IntPtr EyeFiCardWrapper_GetScannedNetworks(IntPtr c, ref int count);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl)] public static extern IntPtr EyeFiCardWrapper_GetConfiguredNetworks(IntPtr c, ref int count);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Ansi)] public static extern int EyeFiCardWrapper_TestNetworkSettings(IntPtr c, string ssid, string key, int authType);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Ansi)] public static extern int EyeFiCardWrapper_AddNetworkSettings(IntPtr c, string ssid, string key, int authType);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Ansi)] public static extern int EyeFiCardWrapper_DeleteNetwork(IntPtr c, string ssid);
  public static uint Ser(string r){ var v=new StringBuilder(261); var f=new StringBuilder(261); uint s,m,fl; GetVolumeInformationW(r,v,261,out s,out m,out fl,f,261); return s; }
  public static byte[] Tok(IntPtr c, byte t, out int rc, out int n){ byte[] b=new byte[256]; var h=GCHandle.Alloc(b,GCHandleType.Pinned); n=b.Length; rc=EyeFiCardWrapper_GetToken(c,t,h.AddrOfPinnedObject(),ref n); h.Free(); return b; }
  public static byte[] ReadMem(IntPtr p, int len){ byte[] b=new byte[len]; Marshal.Copy(p,b,0,len); return b; }
}
"@
Add-Type -TypeDefinition $code -Language CSharp
[EF]::SetDllDirectory($InstallDir) | Out-Null
[Environment]::CurrentDirectory = $InstallDir

# --- 카드 드라이브 탐색 (Read-EyeFiCard.ps1 과 동일) ---
$candidates = @()
if($Drive){ $candidates = @( ($Drive.TrimEnd('\',':')) + ':\' ) }
else { $candidates = Get-WmiObject Win32_LogicalDisk -Filter "DriveType=2" | ForEach-Object { $_.DeviceID + '\' } }
if(-not $candidates -or $candidates.Count -eq 0){ Emit @{ ok=$false; error="이동식 드라이브 없음. 카드를 리더에 꽂았는지 확인." } 2 }

$root = $null
foreach($c in $candidates){
  try { if([EF]::EyeFiCardWrapper_CheckFATID([EF]::Ser($c)) -eq 0){ $root = $c; break } } catch {}
}
if(-not $root){ Emit @{ ok=$false; error="Eye-Fi X2 카드 인식 실패(CheckFATID). 공식앱 종료 후 재시도." } 3 }

Start-Sleep -Milliseconds 1200
$card = [EF]::InitEyeFiCard($root)
if($card -eq [IntPtr]::Zero){ Emit @{ ok=$false; error="카드 초기화 실패. 공식 EyeFiX2Receiver 종료 후 재시도." } 4 }

# 이후 어떤 경로로 나가든 Term 이 반드시 불리도록 감싼다
$result = $null
$exitCode = 0
try {
  function Get-Mac {
    $rc=0;$n=0; $b=[EF]::Tok($card,[byte]1,[ref]$rc,[ref]$n)
    if($rc -ne 0 -or $n -lt 6){ return $null }
    $hex = ($b[0..5] | ForEach-Object {'{0:x2}' -f $_}) -join ''
    return '00-18-56-' + $hex.Substring(6,2) + '-' + $hex.Substring(8,2) + '-' + $hex.Substring(10,2)
  }
  function Get-UploadKey {
    $rc=0;$n=0; $b=[EF]::Tok($card,[byte]253,[ref]$rc,[ref]$n)
    if($rc -ne 0 -or $n -le 0){ return $null }
    return -join ($b[0..($n-1)] | ForEach-Object {[char]$_})
  }
  function Read-CString([byte[]]$bytes){
    $z = [Array]::IndexOf($bytes, [byte]0)
    if($z -lt 0){ $z = $bytes.Length }
    if($z -eq 0){ return '' }
    return [Text.Encoding]::ASCII.GetString($bytes, 0, $z)
  }
  # 스캔망 레코드: SSID[33] + RSSI(1) + flags(1) = 35바이트 (공식앱 구조체 확정)
  function Get-Scanned {
    $count = 0
    $p = [EF]::EyeFiCardWrapper_GetScannedNetworks($card, [ref]$count)
    $nets = @()
    if($p -ne [IntPtr]::Zero -and $count -gt 0 -and $count -lt 128){
      $raw = [EF]::ReadMem($p, 35 * $count)
      for($i = 0; $i -lt $count; $i++){
        $rec = $raw[(35*$i)..(35*$i+34)]
        $nets += @{ ssid = (Read-CString $rec[0..32]); rssi = [int]$rec[33]; flags = [int]$rec[34] }
      }
    }
    return ,$nets
  }
  # 설정망 레코드: SSID[33] = 33바이트
  function Get-Configured {
    $count = 0
    $p = [EF]::EyeFiCardWrapper_GetConfiguredNetworks($card, [ref]$count)
    $nets = @()
    if($p -ne [IntPtr]::Zero -and $count -gt 0 -and $count -lt 128){
      $raw = [EF]::ReadMem($p, 33 * $count)
      for($i = 0; $i -lt $count; $i++){
        $s = Read-CString $raw[(33*$i)..(33*$i+32)]
        if($s){ $nets += @{ ssid = $s } }
      }
    }
    return ,$nets
  }
  function Resolve-Auth([string]$ssid, [object[]]$scanned){
    if($Auth -ge 0){ return $Auth }
    $hit = $scanned | Where-Object { $_.ssid -eq $ssid } | Select-Object -First 1
    if(-not $hit){ $hit = $scanned | Where-Object { $_.ssid -ieq $ssid } | Select-Object -First 1 }
    if($hit){ return [int]$hit.flags }
    return -1
  }

  $mac = Get-Mac
  $status = $null
  try { $status = [EF]::EyeFiCardWrapper_GetStatus($card) } catch {}

  switch($Action){
    'info' {
      $result = @{ ok=$true; action='info'; drive=$root.TrimEnd('\'); mac=$mac; status=$status
                   uploadkey=(Get-UploadKey) }
    }
    'list' {
      $scanned = Get-Scanned
      $configured = Get-Configured
      $result = @{ ok=$true; action='list'; drive=$root.TrimEnd('\'); mac=$mac; status=$status
                   configured=$configured; scanned=$scanned }
    }
    'test' {
      $scanned = Get-Scanned
      $auth = Resolve-Auth $Ssid $scanned
      if($auth -lt 0){
        $result = @{ ok=$false; error="'$Ssid' 가 카드 스캔에 없음 — 카드를 AP 가까이 두거나 -Auth 지정"
                     scanned=$scanned; mac=$mac }
        $exitCode = 5
      } else {
        $rc = [EF]::EyeFiCardWrapper_TestNetworkSettings($card, $Ssid, $Key, $auth)
        $result = @{ ok=$true; action='test'; drive=$root.TrimEnd('\'); mac=$mac; ssid=$Ssid
                     auth_used=$auth; rc=$rc; status_after=[EF]::EyeFiCardWrapper_GetStatus($card)
                     scanned=$scanned }
      }
    }
    'add' {
      $scanned = Get-Scanned
      $auth = Resolve-Auth $Ssid $scanned
      if($auth -lt 0){
        $result = @{ ok=$false; error="'$Ssid' 가 카드 스캔에 없음 — 카드를 AP 가까이 두거나 -Auth 지정"
                     scanned=$scanned; mac=$mac }
        $exitCode = 5
      } else {
        $rc = [EF]::EyeFiCardWrapper_AddNetworkSettings($card, $Ssid, $Key, $auth)
        $configured = Get-Configured   # 등록 직후 재조회로 반영 확인
        $registered = [bool]($configured | Where-Object { $_.ssid -eq $Ssid })
        $result = @{ ok=$true; action='add'; drive=$root.TrimEnd('\'); mac=$mac; ssid=$Ssid
                     auth_used=$auth; rc=$rc; registered=$registered; configured=$configured }
      }
    }
    'delete' {
      $rc = [EF]::EyeFiCardWrapper_DeleteNetwork($card, $Ssid)
      $configured = Get-Configured
      $stillThere = [bool]($configured | Where-Object { $_.ssid -eq $Ssid })
      $result = @{ ok=$true; action='delete'; drive=$root.TrimEnd('\'); mac=$mac; ssid=$Ssid
                   rc=$rc; removed=(-not $stillThere); configured=$configured }
    }
  }
}
catch {
  $result = @{ ok=$false; error=("예외: " + $_.Exception.Message) }
  $exitCode = 9
}
finally {
  [EF]::EyeFiCardWrapper_Term($card) | Out-Null
}
Emit $result $exitCode
