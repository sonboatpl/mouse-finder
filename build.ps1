# Mouse Finder 빌드 스크립트
# 실행: .\build.ps1

Set-Location $PSScriptRoot

Write-Host "=== Mouse Finder 빌드 ===" -ForegroundColor Cyan

# 의존성 설치
Write-Host "`n[1/3] 패키지 설치 중..." -ForegroundColor Yellow
pip install -r requirements.txt

# PyInstaller로 단일 실행파일 생성
Write-Host "`n[2/3] 실행파일 패키징 중..." -ForegroundColor Yellow
pyinstaller `
    --onefile `
    --windowed `
    --name "MouseFinder" `
    --hidden-import "pynput.mouse._win32" `
    --hidden-import "pynput.keyboard._win32" `
    --hidden-import "pystray._win32" `
    main.py

Write-Host "`n[3/3] 완료!" -ForegroundColor Green
Write-Host "실행파일 위치: dist\MouseFinder.exe" -ForegroundColor Cyan
