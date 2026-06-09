# Allow the iPad (and other LAN hosts) to reach the NMOS tooling:
#   TCP 5000 = AMWA NMOS Testing Tool web UI
#   TCP 8080 = nmos-cpp registry/query API + admin browser
#   TCP 8090 = nmos-cpp virtnode API (handy for the testing tool / connection mgmt)
# Run in an ELEVATED PowerShell:
#   powershell -ExecutionPolicy Bypass -File pc\open-nmos-firewall.ps1
$ports = @{
  "NMOS Testing Tool 5000 (WSL)" = 5000
  "NMOS registry 8080 (WSL)"     = 8080
  "NMOS virtnode 8090 (WSL)"     = 8090
}
foreach ($name in $ports.Keys) {
  $port = $ports[$name]
  if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
      -Protocol TCP -LocalPort $port -Profile Any | Out-Null
    Write-Output "Added firewall rule: $name (TCP $port inbound)"
  } else {
    Write-Output "Firewall rule already present: $name"
  }
}
