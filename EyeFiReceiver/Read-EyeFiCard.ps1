<#
  Read-EyeFiCard.ps1  — 리더에 꽂힌 Eye-Fi X2 카드를 "읽기 전용"으로 판독해 JSON 출력.
  (Prep-EyeFiCard.ps1 의 판독부만 추출. Settings.xml/레지스트리는 절대 건드리지 않음.)

  사용:
    powershell -ExecutionPolicy Bypass -File Read-EyeFiCard.ps1 [-Drive F:]
    (-Drive 생략 시 이동식 드라이브를 자동 탐색)

  출력(성공): {"ok":true,"mac":"00-18-56-..","uploadkey":"..","actcode":"..","ssid":"..","raw":true,"drive":"F:"}
  출력(실패): {"ok":false,"error":"사유"}   (종료코드 != 0)

  주의: EyeFiCard.dll 은 32비트 → 자동으로 32비트 PowerShell 로 재실행됨.
        판독 중에는 공식 EyeFiX2Receiver 를 종료해야 카드 접근이 가능.
#>
param(
  [string]$Drive,
  [string]$InstallDir = 'C:\Program Files (x86)\Eye-Fi'
)

# --- 32비트로 자동 재실행 (자식 stdout 이 그대로 부모로 흐르게 함) ---
if([Environment]::Is64BitProcess){
  $ps32 = "$env:WINDIR\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
  $argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath,'-InstallDir',$InstallDir)
  if($Drive){ $argList += @('-Drive',$Drive) }   # 빈 드라이브면 생략 → 자동탐색
  & $ps32 @argList
  exit $LASTEXITCODE
}

function Emit($obj, $code){
  # JSON 한 줄만 stdout 으로 (파이썬이 파싱)
  [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress))
  exit $code
}

if(-not (Test-Path (Join-Path $InstallDir 'EyeFiCard.dll'))){
  Emit @{ ok=$false; error="EyeFiCard.dll 없음: $InstallDir" } 10
}

$code = @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class EF {
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetDllDirectory(string p);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  public static extern bool GetVolumeInformationW(string root, StringBuilder v, int vs, out uint ser, out uint mcl, out uint fl, StringBuilder f, int fs);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Unicode)] public static extern IntPtr InitEyeFiCard(string path);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Unicode)] public static extern int EyeFiCardWrapper_Term(IntPtr c);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Unicode)] public static extern int EyeFiCardWrapper_GetToken(IntPtr c, byte t, IntPtr b, ref int n);
  [DllImport("EyeFiCard.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Unicode)] public static extern int EyeFiCardWrapper_CheckFATID(uint v);
  public static uint Ser(string r){ var v=new StringBuilder(261); var f=new StringBuilder(261); uint s,m,fl; GetVolumeInformationW(r,v,261,out s,out m,out fl,f,261); return s; }
  public static byte[] Tok(IntPtr c, byte t, out int rc, out int n){ byte[] b=new byte[256]; var h=GCHandle.Alloc(b,GCHandleType.Pinned); n=b.Length; rc=EyeFiCardWrapper_GetToken(c,t,h.AddrOfPinnedObject(),ref n); h.Free(); return b; }
}
"@
Add-Type -TypeDefinition $code -Language CSharp
[EF]::SetDllDirectory($InstallDir) | Out-Null
[Environment]::CurrentDirectory = $InstallDir

# --- 대상 드라이브 결정 (지정 없으면 이동식 자동탐색) ---
$candidates = @()
if($Drive){ $candidates = @( ($Drive.TrimEnd('\',':')) + ':\' ) }
else {
  $candidates = Get-WmiObject Win32_LogicalDisk -Filter "DriveType=2" | ForEach-Object { $_.DeviceID + '\' }
}
if(-not $candidates -or $candidates.Count -eq 0){ Emit @{ ok=$false; error="이동식 드라이브를 찾지 못함. 카드를 리더에 꽂았는지 확인." } 2 }

$root = $null
foreach($c in $candidates){
  try { if([EF]::EyeFiCardWrapper_CheckFATID([EF]::Ser($c)) -eq 0){ $root = $c; break } } catch {}
}
if(-not $root){ Emit @{ ok=$false; error="Eye-Fi X2 카드를 인식하지 못함(CheckFATID 실패). X2 카드/리더 확인." } 3 }

Start-Sleep -Milliseconds 1200
$card = [EF]::InitEyeFiCard($root)
if($card -eq [IntPtr]::Zero){ Emit @{ ok=$false; error="카드 초기화 실패. 공식 EyeFiX2Receiver 를 종료 후 재시도." } 4 }

function Tok($type){ $rc=0;$n=0; $b=[EF]::Tok($card,[byte]$type,[ref]$rc,[ref]$n); if($rc -eq 0 -and $n -gt 0){ return ,@($b[0..($n-1)]) } return $null }
$macB=Tok 1; $keyB=Tok 253; $actB=Tok 35; $featB=Tok 15
[EF]::EyeFiCardWrapper_Term($card) | Out-Null

if(-not $macB -or -not $keyB){ Emit @{ ok=$false; error="MAC/UploadKey 판독 실패." } 5 }
$macHex = ($macB | ForEach-Object {'{0:X2}' -f $_}) -join ''
$macFmt = '00-18-56-' + $macHex.Substring(6,2).ToLower() + '-' + $macHex.Substring(8,2).ToLower() + '-' + $macHex.Substring(10,2).ToLower()
$upKey  = -join ($keyB | ForEach-Object {[char]$_})
$actCode= if($actB){ -join ($actB | ForEach-Object {[char]$_}) } else { '' }
$ssid   = 'Eye-Fi Card ' + $macHex.Substring(6).ToLower()
$isRaw  = [bool]($featB -and (($featB[0] -band 2) -ne 0))

Emit @{ ok=$true; mac=$macFmt; uploadkey=$upKey; actcode=$actCode; ssid=$ssid; raw=$isRaw; drive=$root.TrimEnd('\') } 0
