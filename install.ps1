# No CMD, em qualquer computador com internet:
# powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Gehrhardt9/Hypatiamat/main/install.ps1 | iex"

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$Dest = Join-Path $env:LOCALAPPDATA 'Hypatiamat'
$ZipUrl = 'https://github.com/Gehrhardt9/Hypatiamat/archive/refs/heads/main.zip'

function Write-Step([string]$Message) {
    Write-Host $Message
}

function Test-PythonExe([string]$Exe) {
    if (-not $Exe -or $Exe -match '\\WindowsApps\\' -or -not (Test-Path -LiteralPath $Exe)) {
        return $false
    }
    # A leitura de texto (rapidocr) nao existe para Python 3.13 nem 3.14.
    & $Exe -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 13) else 1)"
    return $LASTEXITCODE -eq 0
}

function Get-PythonExe {
    $candidates = @()
    foreach ($ver in @('312', '311', '310')) {
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$ver\python.exe")
        $candidates += "C:\Program Files\Python$ver\python.exe"
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py -and $py.Source -notmatch '\\WindowsApps\\') {
        foreach ($ver in @('-3.12', '-3.11', '-3.10')) {
            try {
                $printed = & py $ver -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $printed) {
                    $candidates += $printed.Trim()
                }
            } catch {
            }
        }
    }
    foreach ($exe in $candidates) {
        if (Test-PythonExe $exe) {
            return $exe
        }
    }
    return $null
}

function Install-Python {
    Write-Step 'A instalar Python 3.12 (a leitura de texto nao funciona no Python 3.13 ou 3.14)...'
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
        if (Get-PythonExe) {
            return
        }
    }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $setup = Join-Path $env:TEMP "python-3.12.8-$arch.exe"
    Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/3.12.8/python-3.12.8-$arch.exe" -OutFile $setup
    $proc = Start-Process -FilePath $setup -ArgumentList @(
        '/quiet',
        'InstallAllUsers=0',
        'PrependPath=1',
        'Include_launcher=1',
        'Include_tcltk=1',
        'Include_pip=1',
        'SimpleInstall=1'
    ) -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        throw "A instalacao do Python falhou (codigo $($proc.ExitCode))."
    }
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

Write-Step 'A descarregar o Hypatiamat...'
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$zip = Join-Path $env:TEMP 'Hypatiamat-main.zip'
$unpack = Join-Path $env:TEMP 'Hypatiamat-unpack'
if (Test-Path -LiteralPath $unpack) {
    Remove-Item -LiteralPath $unpack -Recurse -Force
}
Invoke-WebRequest -UseBasicParsing -Uri $ZipUrl -OutFile $zip
Expand-Archive -LiteralPath $zip -DestinationPath $unpack -Force
$src = Join-Path $unpack 'Hypatiamat-main'
if (-not (Test-Path -LiteralPath (Join-Path $src 'hypatyamat.py'))) {
    throw 'O download nao trouxe o programa.'
}
Copy-Item -Path (Join-Path $src '*') -Destination $Dest -Recurse -Force

$python = Get-PythonExe
if (-not $python) {
    Install-Python
    $python = Get-PythonExe
}
if (-not $python) {
    throw 'Nao consegui encontrar o Python depois de o instalar. Fecha o CMD, abre de novo e corre o comando outra vez.'
}

Write-Step 'A instalar as bibliotecas...'
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw 'Falhou a atualizacao do pip.'
}
& $python -m pip install -r (Join-Path $Dest 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    throw 'Falhou a instalacao das bibliotecas.'
}

& $python -c "import cv2, numpy, mss, rapidocr_onnxruntime, tkinter"
if ($LASTEXITCODE -ne 0) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Step 'A instalar o Visual C++ necessario para a leitura de texto...'
        & winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-package-agreements --accept-source-agreements
        & $python -c "import cv2, numpy, mss, rapidocr_onnxruntime, tkinter"
    }
}
if ($LASTEXITCODE -ne 0) {
    throw 'As bibliotecas instalaram, mas o programa ainda nao as consegue carregar.'
}

$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = $python
}
$script = Join-Path $Dest 'hypatyamat.py'
$menu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
New-Item -ItemType Directory -Force -Path $menu | Out-Null
$lnkPath = Join-Path $menu 'Hypatiamat.lnk'
$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = '"' + $script + '"'
$lnk.WorkingDirectory = $Dest
$lnk.Description = 'Hypatiamat'
$lnk.Save()

Write-Step ''
Write-Step 'Pronto. Abre o menu Iniciar e procura Hypatiamat.'
Write-Step "Pasta do programa: $Dest"
