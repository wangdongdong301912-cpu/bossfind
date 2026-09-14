$ErrorActionPreference = "Stop"

$programFiles = [Environment]::GetFolderPath("ProgramFiles")
$programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
$localAppData = [Environment]::GetFolderPath("LocalApplicationData")

$candidates = @(
  (Join-Path $programFiles "Google\Chrome\Application\chrome.exe"),
  (Join-Path $programFilesX86 "Google\Chrome\Application\chrome.exe"),
  (Join-Path $localAppData "Google\Chrome\Application\chrome.exe")
)

$chrome = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) {
  throw "chrome.exe was not found. Please install Google Chrome first."
}

$profileDir = Join-Path $PSScriptRoot "data\chrome-profile"
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

$arguments = @(
  "--remote-debugging-port=9222",
  "--user-data-dir=$profileDir",
  "--profile-directory=Default",
  "--no-first-run",
  "--new-window",
  "https://www.zhipin.com/web/geek/jobs?_security_check=1_1782462272045"
)

Start-Process -FilePath $chrome -ArgumentList $arguments
Write-Host "Started a BossFind Chrome window. Please finish BOSS login manually in Chrome."
