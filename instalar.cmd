@echo off
setlocal
cd /d "%USERPROFILE%"
echo A descarregar o Hypatiamat...
curl.exe -fL --retry 3 -o "%TEMP%\Hypatiamat.zip" https://github.com/Gehrhardt9/Hypatiamat/archive/refs/heads/main.zip
if errorlevel 1 (
    echo O download falhou. Confirma que ha internet e tenta outra vez.
    exit /b 1
)
if exist "%TEMP%\Hypatiamat-src" rmdir /s /q "%TEMP%\Hypatiamat-src"
mkdir "%TEMP%\Hypatiamat-src"
tar.exe -xf "%TEMP%\Hypatiamat.zip" -C "%TEMP%\Hypatiamat-src"
if errorlevel 1 (
    echo Nao consegui abrir o ficheiro descarregado.
    exit /b 1
)
powershell.exe -NoProfile -Command "Get-ChildItem -LiteralPath $env:TEMP\Hypatiamat-src -Recurse -File | Unblock-File"
powershell.exe -NoProfile -File "%TEMP%\Hypatiamat-src\Hypatiamat-main\install.ps1"
exit /b %ERRORLEVEL%
