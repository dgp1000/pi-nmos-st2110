# Park a WSLg GStreamer output window on a chosen Windows monitor, full-screen-sized.
# Wayland clients can't position themselves, so we move the window from the Windows side after it
# maps. Title-independent: snapshot existing windows, then grab the NEW one that appears and size+move
# it to the target screen. WSLg often repositions after the first frame, so we keep snapping back for
# TimeoutSec. Used for waylandsink windows (they tolerate resize). glimagesink ("OpenGL Renderer")
# windows are SKIPPED here -- they recreate on resize and are placed move-only by snap-window-screen.ps1.
#
# Usage: powershell -File move-window-screen.ps1 -Screen 2 [-TimeoutSec 12] [-MinSide 200]
param([int]$Screen = 2, [int]$TimeoutSec = 12, [int]$MinSide = 200)
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;using System.Text;using System.Runtime.InteropServices;
public class W32 {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
$screens = [System.Windows.Forms.Screen]::AllScreens
if ($Screen -lt 1 -or $Screen -gt $screens.Count) {
  Write-Output ("screen {0} out of range (have {1})" -f $Screen, $screens.Count); exit 1
}
$b = $screens[$Screen - 1].Bounds
$SWP_NOZORDER = 0x0004

function Get-VisWins {
  $list = New-Object System.Collections.ArrayList
  $script:list = $list
  $cb = [W32+EnumWindowsProc]{
    param($h, $l)
    if ([W32]::IsWindowVisible($h)) {
      $sb = New-Object System.Text.StringBuilder 256; [void][W32]::GetWindowText($h, $sb, 256)
      if ($sb.ToString() -notlike "*OpenGL Renderer*") {
        $r = New-Object W32+RECT
        [void][W32]::GetWindowRect($h, [ref]$r)
        $w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
        if ($w -ge $script:MinSide -and $ht -ge $script:MinSide) {
          [void]$script:list.Add(@{ h = $h; area = ($w * $ht) })
        }
      }
    }
    return $true
  }
  [void][W32]::EnumWindows($cb, [IntPtr]::Zero)
  return $list
}
$script:MinSide = $MinSide
$base = @(Get-VisWins | ForEach-Object { $_.h })
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$target = [IntPtr]::Zero
$placed = $false
while ((Get-Date) -lt $deadline) {
  if ($target -eq [IntPtr]::Zero) {
    $new = Get-VisWins | Where-Object { $base -notcontains $_.h } | Sort-Object { $_.area } -Descending
    if ($new) { $target = $new[0].h }
  }
  if ($target -ne [IntPtr]::Zero) {
    [void][W32]::SetWindowPos($target, [IntPtr]::Zero, $b.X, $b.Y, $b.Width, $b.Height, $SWP_NOZORDER)
    if (-not $placed) {
      Write-Output ("placed new window on screen {0} ({1})" -f $Screen, $b.ToString())
      $placed = $true
    }
  }
  Start-Sleep -Milliseconds 400
}
if (-not $placed) { Write-Output "no new window appeared to place"; exit 0 }
exit 0
