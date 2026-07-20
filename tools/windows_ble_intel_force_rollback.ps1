$ErrorActionPreference = "Continue"
$IntelBluetoothId = "USB\VID_8087&PID_0033\5&218BFA3C&0&14"
$CurrentFailingInf = "oem234.inf"
$FallbackInf = "C:\Windows\INF\oem160.inf"
$LogPath = Join-Path $env:TEMP "wizpr_intel_bluetooth_force_rollback.log"

function Write-Log {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    $line | Tee-Object -FilePath $LogPath -Append
}

function Run-Step {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    Write-Log ""
    Write-Log "== $Label =="
    try {
        & $Command 2>&1 | Tee-Object -FilePath $LogPath -Append
    } catch {
        Write-Log "FAILED: $($_.Exception.Message)"
    }
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $isAdmin) {
    Write-Host "This script must run as Administrator." -ForegroundColor Red
    exit 1
}

"" | Out-File -FilePath $LogPath -Encoding utf8
Write-Log "Wizpr Intel Bluetooth force rollback started."
Write-Log "Target device: $IntelBluetoothId"
Write-Log "Removing current failing INF: $CurrentFailingInf"
Write-Log "Fallback INF expected: $FallbackInf"

Run-Step "Intel Bluetooth before force rollback" {
    Get-PnpDevice -InstanceId $IntelBluetoothId
    Get-PnpDeviceProperty -InstanceId $IntelBluetoothId `
        -KeyName DEVPKEY_Device_ProblemCode,DEVPKEY_Device_ProblemStatus,DEVPKEY_Device_DriverInfPath,DEVPKEY_Device_DriverProvider,DEVPKEY_Device_DriverVersion `
        -ErrorAction SilentlyContinue |
        Select-Object KeyName, Data |
        Format-List
}

Run-Step "Uninstall current failing Intel Bluetooth package" {
    pnputil /delete-driver $CurrentFailingInf /uninstall /force
}
Start-Sleep -Seconds 3

Run-Step "Ensure fallback package is present" {
    pnputil /add-driver $FallbackInf /install
}
Start-Sleep -Seconds 2

Run-Step "Scan for device changes" {
    pnputil /scan-devices
}
Start-Sleep -Seconds 5

Run-Step "Restart Intel Bluetooth adapter after force rollback" {
    pnputil /restart-device $IntelBluetoothId
}
Start-Sleep -Seconds 5

Run-Step "Intel Bluetooth after force rollback" {
    Get-PnpDevice -InstanceId $IntelBluetoothId
    Get-PnpDeviceProperty -InstanceId $IntelBluetoothId `
        -KeyName DEVPKEY_Device_ProblemCode,DEVPKEY_Device_ProblemStatus,DEVPKEY_Device_DriverInfPath,DEVPKEY_Device_DriverProvider,DEVPKEY_Device_DriverVersion `
        -ErrorAction SilentlyContinue |
        Select-Object KeyName, Data |
        Format-List
}

Run-Step "BLE Doctor after force rollback" {
    py -m wizpr_suite.tools.ble_doctor --all
}

Write-Log ""
Write-Log "Wizpr Intel Bluetooth force rollback finished."
Write-Host ""
Write-Host "Done. Log written to $LogPath"
