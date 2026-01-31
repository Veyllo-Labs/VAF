$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile("D:\VAF\install.ps1", [ref]$tokens, [ref]$errors)
Write-Host "Lines:" (Get-Content "D:\VAF\install.ps1").Count "| Errors:" $errors.Count
if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Host " - Line $($_.Extent.StartLineNumber): $($_.Message)" -ForegroundColor Red } }
else { Write-Host "PowerShell syntax OK!" -ForegroundColor Green }
Remove-Item $MyInvocation.MyCommand.Path -Force
