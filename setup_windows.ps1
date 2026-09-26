$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Pause-Window {
    Write-Host ''
    Write-Host 'Press Enter to close this window...' -ForegroundColor Yellow
    [void](Read-Host)
}

try {
    Write-Host '========================================' -ForegroundColor Cyan
    Write-Host 'AI Router - Windows setup' -ForegroundColor Cyan
    Write-Host '========================================' -ForegroundColor Cyan
    Write-Host ("Folder: " + $PSScriptRoot)
    Write-Host ''

    $python = $null
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 --version
        if ($LASTEXITCODE -eq 0) { $python = 'py' }
    }
    if (-not $python -and (Get-Command python -ErrorAction SilentlyContinue)) {
        & python --version
        if ($LASTEXITCODE -eq 0) { $python = 'python' }
    }
    if (-not $python) {
        throw 'Python 3.11 or newer was not found. Install Python from python.org and enable Add python.exe to PATH.'
    }

    $venv = Join-Path $PSScriptRoot '.venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'

    if (-not (Test-Path $venvPython)) {
        Write-Host 'Creating virtual environment...' -ForegroundColor Cyan
        if ($python -eq 'py') { & py -3 -m venv $venv } else { & python -m venv $venv }
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create .venv.' }
    } else {
        Write-Host '.venv already exists - keeping it.' -ForegroundColor DarkGray
    }

    Write-Host 'Upgrading pip...' -ForegroundColor Cyan
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Failed to upgrade pip.' }

    Write-Host 'Installing Python dependencies...' -ForegroundColor Cyan
    & $venvPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install Python dependencies.' }

    if (Test-Path (Join-Path $PSScriptRoot 'package.json')) {
        if (Get-Command npm -ErrorAction SilentlyContinue) {
            Write-Host 'npm found. Installing Node dependencies...' -ForegroundColor Cyan
            Push-Location $PSScriptRoot
            try {
                & npm install
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning 'npm install failed. The GUI can still run; Node checks may be unavailable.'
                }
            } finally {
                Pop-Location
            }
        } else {
            Write-Host 'npm not found. Node/TypeScript checks will be unavailable until Node.js is installed.' -ForegroundColor Yellow
        }
    }

    Write-Host ''
    Write-Host 'SETUP COMPLETE.' -ForegroundColor Green
    Write-Host 'Run start.bat to launch AI Router.' -ForegroundColor Green
}
catch {
    Write-Host ''
    Write-Host 'SETUP FAILED:' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ''
    Write-Host 'If Python or Node is missing, install it and run this script again.' -ForegroundColor Yellow
}
finally {
    Pause-Window
}
