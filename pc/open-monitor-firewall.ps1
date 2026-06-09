# Allow the iPad (and other LAN hosts) to reach the ST 2110 monitor on TCP 8096.
# Run in an ELEVATED PowerShell:
#   powershell -ExecutionPolicy Bypass -File pc\open-monitor-firewall.ps1
$rule = "ST2110 monitor 8096 (WSL)"
if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 8096 -Profile Any | Out-Null
  Write-Output "Added firewall rule: $rule (TCP 8096 inbound)"
} else {
  Write-Output "Firewall rule already present: $rule"
}
