#!/bin/bash
# each step: 10s of 20ms pings, report % of RTTs > 2ms and max
m(){ ping -i 0.02 -c 500 10.10.10.1 2>/dev/null | grep -o "time=[0-9.]*" | cut -d= -f2 | awk -v l="$1" "{n++; if(\$1>2)h++; if(\$1>mx)mx=\$1} END{printf \"%-34s spikes>2ms=%4.1f%%  max=%.2fms\\n\", l, 100*h/n, mx}"; }
exec > /tmp/lat.txt 2>&1
m "a) baseline"
sudo iw dev wlan0 set power_save off; m "b) wifi power_save off"
for s in /sys/devices/system/cpu/cpu*/cpuidle/state*/disable; do echo 1 | sudo tee $s >/dev/null; done; m "c) +cpuidle deep states disabled"
sudo systemctl stop arduino-router arduino-app-cli; m "d) +arduino-router/app-cli stopped"
sudo systemctl stop lightdm; m "e) +lightdm stopped"
sudo nmcli radio wifi off; m "f) +wifi radio OFF"; sudo nmcli radio wifi on
sleep 8; m "g) wifi back on (all above still applied)"
for s in /sys/devices/system/cpu/cpu*/cpuidle/state*/disable; do echo 0 | sudo tee $s >/dev/null; done; sudo systemctl start arduino-router arduino-app-cli lightdm
m "h) restored (only power_save off kept)"
echo DONE
