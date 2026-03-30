$env:PYTHONUNBUFFERED = "1"
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "新闻抓取应用 - PowerShell启动器" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

& .\.venv\Scripts\python.exe -u launch.py
