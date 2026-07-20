$ErrorActionPreference = "Continue"
$IntelBluetoothId = "USB\VID_8087&PID_0033\5&218BFA3C&0&14"
$LogPath = Join-Path $env:TEMP "wizpr_ble_stack_refresh.log"

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
Write-Log "Wizpr BLE stack refresh started."

Run-Step "Services before refresh" {
    Get-Service bthserv, RmSvc, BluetoothUserService* -ErrorAction SilentlyContinue |
        Select-Object Name, Status, StartType |
        Format-Table -AutoSize
}

Run-Step "Restart Bluetooth user services" {
    Get-Service BluetoothUserService* -ErrorAction SilentlyContinue |
        ForEach-Object {
            if ($_.Status -eq "Running") {
                Restart-Service -Name $_.Name -Force -ErrorAction Continue
            }
        }
}

Run-Step "Restart Bluetooth support service" {
    Restart-Service -Name bthserv -Force -ErrorAction Continue
}

Run-Step "Restart radio management service" {
    Restart-Service -Name RmSvc -Force -ErrorAction Continue
}

Run-Step "Restart Intel Bluetooth PnP device" {
    pnputil /restart-device $IntelBluetoothId
}

Start-Sleep -Seconds 5

Run-Step "Services after refresh" {
    Get-Service bthserv, RmSvc, BluetoothUserService* -ErrorAction SilentlyContinue |
        Select-Object Name, Status, StartType |
        Format-Table -AutoSize
}

Run-Step "Intel Bluetooth after refresh" {
    Get-PnpDevice -InstanceId $IntelBluetoothId
    Get-PnpDeviceProperty -InstanceId $IntelBluetoothId `
        -KeyName DEVPKEY_Device_ProblemCode,DEVPKEY_Device_ProblemStatus,DEVPKEY_Device_DriverInfPath,DEVPKEY_Device_DriverProvider,DEVPKEY_Device_DriverVersion `
        -ErrorAction SilentlyContinue |
        Select-Object KeyName, Data |
        Format-List
}

Run-Step "BLE Doctor after refresh" {
    py -m wizpr_suite.tools.ble_doctor --all
}

Write-Log ""
Write-Log "Wizpr BLE stack refresh finished."
Write-Host ""
Write-Host "Done. Log written to $LogPath"
