param(
    [switch]$DisableGenericRadio,
    [switch]$ReenableGenericAfter
)

$ErrorActionPreference = "Continue"
$IntelBluetoothId = "USB\VID_8087&PID_0033\5&218BFA3C&0&14"
$GenericBluetoothId = "USB\VID_0A12&PID_0001\5&218BFA3C&0&1"
$LogPath = Join-Path $env:TEMP "wizpr_ble_repair.log"

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

"" | Set-Content -Path $LogPath
Write-Log "Wizpr BLE repair started."
Write-Log "Log: $LogPath"

Run-Step "Bluetooth devices before repair" {
    Get-PnpDevice -Class Bluetooth |
        Sort-Object FriendlyName |
        Select-Object Status, FriendlyName, InstanceId |
        Format-Table -Wrap -AutoSize
}

Run-Step "Intel Bluetooth properties before repair" {
    Get-PnpDeviceProperty -InstanceId $IntelBluetoothId `
        -KeyName DEVPKEY_Device_ProblemCode,DEVPKEY_Device_ProblemStatus,DEVPKEY_Device_DriverInfPath,DEVPKEY_Device_DriverProvider,DEVPKEY_Device_DriverVersion `
        -ErrorAction SilentlyContinue |
        Select-Object KeyName, Data |
        Format-List
}

if ($DisableGenericRadio) {
    Run-Step "Temporarily disable Generic Bluetooth Radio" {
        pnputil /disable-device $GenericBluetoothId
    }
    Start-Sleep -Seconds 2
}

Run-Step "Restart Intel Bluetooth adapter" {
    pnputil /restart-device $IntelBluetoothId
}
Start-Sleep -Seconds 3

Run-Step "Scan for device changes" {
    pnputil /scan-devices
}
Start-Sleep -Seconds 3

Run-Step "BLE Doctor after repair" {
    py -m wizpr_suite.tools.ble_doctor --all
}

if ($DisableGenericRadio -and $ReenableGenericAfter) {
    Run-Step "Re-enable Generic Bluetooth Radio" {
        pnputil /enable-device $GenericBluetoothId
    }
    Start-Sleep -Seconds 2
    Run-Step "BLE Doctor after re-enabling Generic Bluetooth Radio" {
        py -m wizpr_suite.tools.ble_doctor --all
    }
}

Run-Step "Bluetooth devices after repair" {
    Get-PnpDevice -Class Bluetooth |
        Sort-Object FriendlyName |
        Select-Object Status, FriendlyName, InstanceId |
        Format-Table -Wrap -AutoSize
}

Write-Log ""
Write-Log "Wizpr BLE repair finished."
Write-Host ""
Write-Host "Done. Log written to $LogPath"
