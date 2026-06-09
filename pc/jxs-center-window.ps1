# Center the WSLg GStreamer player window on the primary monitor (screen 1).
# Wayland won't let the GStreamer client position itself, so we move it from the
# Windows side after it appears. Launched in the background by jxs-stream-view.sh.
#
# IMPORTANT: move only, never resize. Resizing the WSLg window freezes glimagesink
# (the external resize isn't propagated back to the Wayland GL surface, so it stops
# receiving frames and shows a still image). SetWindowPos with SWP_NOSIZE avoids it.
#
# Usage: powershell -File jxs-center-window.ps1 [-Title "OpenGL Renderer (Ubuntu)"] [-TimeoutSec 15]
# (-W/-H accepted for caller compatibility but unused: we center the window's own size.)
param(
  [string]$Title = "OpenGL Renderer (Ubuntu)",
  [int]$W = 0,
  [int]$H = 0,
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
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

function Find-Window {
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

$SWP_NOSIZE   = 0x0001
$SWP_NOZORDER = 0x0004
$primary = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
  $hwnd = Find-Window
  if ($hwnd -ne [IntPtr]::Zero) {
    $r = New-Object W32+RECT
    [void][W32]::GetWindowRect($hwnd, [ref]$r)
    $cw = $r.Right - $r.Left
    $ch = $r.Bottom - $r.Top
    $x = $primary.X + [int](($primary.Width  - $cw) / 2)
    $y = $primary.Y + [int](($primary.Height - $ch) / 2)
    [void][W32]::SetWindowPos($hwnd, [IntPtr]::Zero, $x, $y, 0, 0, ($SWP_NOSIZE -bor $SWP_NOZORDER))
    Write-Output ("centered '{0}' ({1}x{2}) -> ({3},{4}) on {5}" -f $Title, $cw, $ch, $x, $y, $primary.ToString())
    exit 0
  }
  Start-Sleep -Milliseconds 250
}
Write-Output ("window '{0}' not found within {1}s" -f $Title, $TimeoutSec)
exit 1
