Add-Type -AssemblyName System.Windows.Forms

$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = "선례 CSV 파일 선택"
$dialog.Filter = "CSV files (*.csv)|*.csv|All files (*.*)|*.*"
$dialog.InitialDirectory = (Get-Location).Path

if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.FileName
}
