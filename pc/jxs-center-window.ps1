# Center the WSLg GStreamer player window on the primary monitor (screen 1).
# Wayland won't let the GStreamer client position itself, so we move it from the
# Windows side after it appears. Launched in the background by jxs-stream-view.sh.
#
# Usage: powershell -File jxs-center-window.ps1 [-Title "OpenGL Renderer (Ubuntu)"] [-W 1920] [-H 1080] [-TimeoutSec 15]
param(
  [string]$Title = "OpenGL Renderer (Ubuntu)",
  [int]$W = 1920,
  [int]$H = 1080,
  [int]$TimeoutSec = 15
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class W32 {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int ht, bool repaint);
  public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
}
"@

function Find-Window([string]$t) {
  # NB: avoid a var named $h here -- PowerShell is case-insensitive, so $h would
  # collide with the $H (height) param and corrupt the window size.
  $cb = [W32+EnumWindowsProc]{
    param($hwnd,$l)
    if ([W32]::IsWindowVisible($hwnd)) {
      $sb = New-Object System.Text.StringBuilder 512
      [void][W32]::GetWindowText($hwnd, $sb, 512)
      if ($sb.ToString() -eq $script:Title) { $script:found = $hwnd; return $false }
    }
    return $true
  }
  $script:found = [IntPtr]::Zero
  [void][W32]::EnumWindows($cb, [IntPtr]::Zero)
  return $script:found
}

$primary = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$x = $primary.X + [int](($primary.Width  - $W) / 2)
$y = $primary.Y + [int](($primary.Height - $H) / 2)

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
  $hwnd = Find-Window $Title
  if ($hwnd -ne [IntPtr]::Zero) {
    [void][W32]::MoveWindow($hwnd, $x, $y, $W, $H, $true)
    Write-Output ("centered '{0}' -> ({1},{2}) {3}x{4} on {5}" -f $Title, $x, $y, $W, $H, $primary.ToString())
    exit 0
  }
  Start-Sleep -Milliseconds 250
}
Write-Output ("window '{0}' not found within {1}s" -f $Title, $TimeoutSec)
exit 1
