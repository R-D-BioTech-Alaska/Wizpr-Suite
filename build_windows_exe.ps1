param(
    [switch]$SkipTests,
    [switch]$KeepBuildEnvironment
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "This script must run on Windows."
}

$SourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Join-Path $env:LOCALAPPDATA "WizprSuiteBuild"
$ExcludedNames = @(
    ".git",
    ".venv-build",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist"
)

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host ""
    Write-Host "==> $Label"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Reset-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)

    Remove-Item -Recurse -Force $Path -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Invoke-PyInstallerBuild {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$SpecPath,
        [Parameter(Mandatory = $true)][string]$WorkPath,
        [Parameter(Mandatory = $true)][string]$DistPath,
        [Parameter(Mandatory = $true)][string]$CachePath
    )

    $Arguments = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--workpath", $WorkPath,
        "--distpath", $DistPath,
        $SpecPath
    )
    $MaximumAttempts = 3

    for ($Attempt = 1; $Attempt -le $MaximumAttempts; $Attempt++) {
        Write-Host ""
        Write-Host "==> Build self-contained Windows executable (attempt $Attempt of $MaximumAttempts)"
        Remove-Item -Recurse -Force $WorkPath -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $DistPath -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $CachePath -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $WorkPath, $DistPath, $CachePath | Out-Null

        & $PythonPath @Arguments
        $ExitCode = $LASTEXITCODE
        if ($ExitCode -eq 0) {
            return
        }

        if ($ExitCode -eq -1073741819 -or $ExitCode -eq 3221225477) {
            if ($Attempt -lt $MaximumAttempts) {
                Write-Warning "PyInstaller was terminated by Windows with access violation 0xC0000005. Clearing its workspace and retrying."
                Start-Sleep -Seconds (2 * $Attempt)
                continue
            }
        }

        throw "Build self-contained Windows executable failed with exit code $ExitCode."
    }

    throw "Build self-contained Windows executable failed after $MaximumAttempts attempts."
}

Write-Host ""
Write-Host "==> Create short isolated build workspace"
Reset-Directory $Workspace
Get-ChildItem -Path $SourceRoot -Force | Where-Object {
    $_.Name -notin $ExcludedNames
} | ForEach-Object {
    Copy-Item -Path $_.FullName -Destination $Workspace -Recurse -Force
}

$Root = $Workspace
Set-Location $Root

$Venv = Join-Path $Root ".venv-build"
$Python = Join-Path $Venv "Scripts\python.exe"
$PythonCheck = Join-Path $Root "tools\verify_build_python.py"
$QtCheck = Join-Path $Root "tools\verify_qt_runtime.py"
$PackagerCheck = Join-Path $Root "tools\verify_packager.py"
$NumpyCheck = Join-Path $Root "tools\verify_numpy_runtime.py"
$BuildTemp = Join-Path $Root ".build-temp"
$PyInstallerCache = Join-Path $Root ".pyinstaller-cache"

Write-Host ""
Write-Host "==> Reset isolated build environment"
Remove-Item -Recurse -Force $Venv -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "build") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "dist") -ErrorAction SilentlyContinue
Reset-Directory $BuildTemp
Reset-Directory $PyInstallerCache

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "64-bit Python 3.11 is required. Install it from python.org with the Python launcher enabled."
}

Invoke-Checked "Create Python 3.11 x64 build environment" "py" @("-3.11-64", "-m", "venv", $Venv)

if (-not (Test-Path $Python)) {
    throw "The build environment could not be created."
}

Invoke-Checked "Verify Python runtime" $Python @($PythonCheck)
Invoke-Checked "Upgrade packaging tools" $Python @("-m", "pip", "install", "--upgrade", "pip", "wheel")
Invoke-Checked "Install application and build dependencies" $Python @("-m", "pip", "install", "--only-binary=:all:", "-r", "requirements.txt", "-r", "build-requirements.txt")

$VenvConfigPath = Join-Path $Venv "pyvenv.cfg"
$HomeLine = Get-Content $VenvConfigPath | Where-Object { $_ -match "^home\s*=" } | Select-Object -First 1
if (-not $HomeLine) {
    throw "Could not determine the base Python installation from $VenvConfigPath."
}
$BasePython = ($HomeLine -split "=", 2)[1].Trim()

$SitePackages = Join-Path $Venv "Lib\site-packages"
$QtRoot = Join-Path $SitePackages "PySide6"
$QtBin = Join-Path $QtRoot "Qt\bin"
$WindowsRoot = $env:SystemRoot
$SafePath = @(
    (Join-Path $Venv "Scripts"),
    $BasePython,
    (Join-Path $BasePython "DLLs"),
    $QtRoot,
    $QtBin,
    (Join-Path $WindowsRoot "System32"),
    $WindowsRoot,
    (Join-Path $WindowsRoot "System32\Wbem"),
    (Join-Path $WindowsRoot "System32\WindowsPowerShell\v1.0")
) | Where-Object { Test-Path $_ } | Select-Object -Unique
$env:PATH = $SafePath -join ";"
$env:QT_API = "pyside6"
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONHASHSEED = "0"
$env:PYTHONUTF8 = "1"
$env:PYINSTALLER_CONFIG_DIR = $PyInstallerCache
$env:TEMP = $BuildTemp
$env:TMP = $BuildTemp
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:QT_PLUGIN_PATH -ErrorAction SilentlyContinue
Remove-Item Env:QML2_IMPORT_PATH -ErrorAction SilentlyContinue
Remove-Item Env:QT_QPA_PLATFORM_PLUGIN_PATH -ErrorAction SilentlyContinue

Invoke-Checked "Verify pinned PyInstaller runtime" $Python @($PackagerCheck)
Invoke-Checked "Verify isolated PySide6 Qt runtime" $Python @($QtCheck)
Invoke-Checked "Verify pinned NumPy compiled runtime" $Python @($NumpyCheck)

if (-not $SkipTests) {
    Invoke-Checked "Run Ruff" $Python @("-m", "ruff", "check", "wizpr_suite", "tests", "tools\verify_build_python.py", "tools\verify_qt_runtime.py", "tools\verify_packager.py", "tools\verify_numpy_runtime.py", "wizpr_launcher.py")
    Invoke-Checked "Run tests" $Python @("-m", "pytest", "-q")
}

$Spec = Join-Path $Root "WizprSuite-onefile.spec"
$WorkPath = Join-Path $Root "build"
$DistPath = Join-Path $Root "dist"
Invoke-PyInstallerBuild -PythonPath $Python -SpecPath $Spec -WorkPath $WorkPath -DistPath $DistPath -CachePath $PyInstallerCache

$Exe = Join-Path $DistPath "WizprSuite.exe"
if (-not (Test-Path $Exe)) {
    throw "Build finished without producing $Exe."
}

if ((Get-Item $Exe).Length -lt 10000000) {
    throw "The generated self-contained executable is unexpectedly small and incomplete."
}

$UnexpectedRuntimeItems = Get-ChildItem -Path $DistPath -Force | Where-Object {
    $_.FullName -ne $Exe
}
if ($UnexpectedRuntimeItems) {
    $Names = ($UnexpectedRuntimeItems | ForEach-Object { $_.Name }) -join ", "
    throw "The build produced external runtime files: $Names"
}

$SelfTestReport = Join-Path $BuildTemp "WizprSuite-self-test.txt"
Remove-Item -Force $SelfTestReport -ErrorAction SilentlyContinue
$env:WIZPR_SELF_TEST_REPORT = $SelfTestReport
Write-Host ""
Write-Host "==> Run packaged executable self-test"
$Process = Start-Process -FilePath $Exe -ArgumentList "--self-test" -WorkingDirectory $Root -Wait -PassThru
if ($Process.ExitCode -ne 0) {
    if (Test-Path $SelfTestReport) {
        Get-Content $SelfTestReport | Write-Host
    }
    throw "The packaged executable failed its self-test with exit code $($Process.ExitCode)."
}
if (-not (Test-Path $SelfTestReport)) {
    throw "The executable exited without writing its self-test report."
}
$SelfTestText = Get-Content $SelfTestReport -Raw
if ($SelfTestText -notmatch "SELF TEST PASSED") {
    Write-Host $SelfTestText
    throw "The packaged executable did not pass its self-test."
}
Write-Host $SelfTestText

$FinalDist = Join-Path $SourceRoot "dist"
Remove-Item -Recurse -Force $FinalDist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $FinalDist | Out-Null
$FinalExe = Join-Path $FinalDist "WizprSuite.exe"
Copy-Item -Path $Exe -Destination $FinalExe -Force

$FinalItems = @(Get-ChildItem -Path $FinalDist -Force)
if ($FinalItems.Count -ne 1 -or $FinalItems[0].FullName -ne $FinalExe) {
    throw "The final dist folder must contain only WizprSuite.exe."
}

$Hash = (Get-FileHash -Algorithm SHA256 $FinalExe).Hash.ToLowerInvariant()

Write-Host ""
Write-Host "Build passed."
Write-Host "Standalone executable: $FinalExe"
Write-Host "The dist folder contains only WizprSuite.exe."
Write-Host "SHA-256: $Hash"

if ($KeepBuildEnvironment) {
    Write-Host "Build workspace retained at: $Workspace"
} else {
    Set-Location $SourceRoot
    Remove-Item -Recurse -Force $Workspace -ErrorAction SilentlyContinue
}
