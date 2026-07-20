$ErrorActionPreference = "Continue"
$IntelBluetoothId = "USB\VID_8087&PID_0033\5&218BFA3C&0&14"
$RollbackInf = "C:\Windows\INF\oem160.inf"
$LogPath = Join-Path $env:TEMP "wizpr_intel_bluetooth_rollback.log"

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
Write-Log "Wizpr Intel Bluetooth rollback started."
Write-Log "Target device: $IntelBluetoothId"
Write-Log "Rollback INF: $RollbackInf"

Run-Step "Intel Bluetooth before rollback" {
    Get-PnpDevice -InstanceId $IntelBluetoothId
    Get-PnpDeviceProperty -InstanceId $IntelBluetoothId `
        -KeyName DEVPKEY_Device_ProblemCode,DEVPKEY_Device_ProblemStatus,DEVPKEY_Device_DriverInfPath,DEVPKEY_Device_DriverProvider,DEVPKEY_Device_DriverVersion `
        -ErrorAction SilentlyContinue |
        Select-Object KeyName, Data |
        Format-List
}

Run-Step "Install older matching Intel Bluetooth driver package" {
    pnputil /add-driver $RollbackInf /install
}

Run-Step "Restart Intel Bluetooth adapter" {
    pnputil /restart-device $IntelBluetoothId
}
Start-Sleep -Seconds 3

Run-Step "Scan for device changes" {
    pnputil /scan-devices
}
Start-Sleep -Seconds 3

Run-Step "Intel Bluetooth after rollback" {
    Get-PnpDevice -InstanceId $IntelBluetoothId
    Get-PnpDeviceProperty -InstanceId $IntelBluetoothId `
        -KeyName DEVPKEY_Device_ProblemCode,DEVPKEY_Device_ProblemStatus,DEVPKEY_Device_DriverInfPath,DEVPKEY_Device_DriverProvider,DEVPKEY_Device_DriverVersion `
        -ErrorAction SilentlyContinue |
        Select-Object KeyName, Data |
        Format-List
}

Run-Step "BLE Doctor after rollback" {
    py -m wizpr_suite.tools.ble_doctor --all
}

Write-Log ""
Write-Log "Wizpr Intel Bluetooth rollback finished."
Write-Host ""
Write-Host "Done. Log written to $LogPath"
