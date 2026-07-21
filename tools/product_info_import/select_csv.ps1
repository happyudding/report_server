# 기준정보 CSV 파일 선택 다이얼로그. run_import.bat 이 stdout 을 임시 파일로 받아 읽는다.
# 이 파일은 UTF-8 BOM + CRLF 로 저장할 것 (.gitattributes 가 eol 을 강제한다). BOM 이 없으면
# PowerShell 5.1 이 시스템 ANSI(cp949)로 읽어 한글이 깨지고 운 나쁘면 파스 에러가 난다.
Add-Type -AssemblyName System.Windows.Forms

$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = "기준정보 CSV 선택"
$dialog.Filter = "CSV files (*.csv)|*.csv|All files (*.*)|*.*"
$dialog.InitialDirectory = (Get-Location).Path

if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.FileName
}
