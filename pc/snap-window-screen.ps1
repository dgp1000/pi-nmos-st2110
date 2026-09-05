# Move-only snap for glimagesink output windows (titled "OpenGL Renderer (Ubuntu)") onto a target
# monitor. glimagesink RECREATES its window on any resize, so we NEVER resize -- the renderer opens
# the window at the monitor's native size already (4K render -> WSLg 1.5x DPI -> 2560x1440 window);
# here we only reposition it. Keeps re-snapping for TimeoutSec so a late-mapping / recreated window
# still lands. Title-scoped so it never touches other windows.
param([int]$Screen = 2, [int]$TimeoutSec = 12)
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;using System.Text;using System.Runtime.InteropServices;
public class SNP {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x,int y,int cx,int cy,uint f);
  public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left,Top,Right,Bottom; }
}
"@
$b = ([System.Windows.Forms.Screen]::AllScreens)[$Screen-1].Bounds
$SWP_NOSIZE = 0x0001; $SWP_NOZORDER = 0x0004
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
  $script:h = [IntPtr]::Zero; $script:w = 0; $script:ht = 0
  $cb = [SNP+EnumWindowsProc]{
    param($h, $l)
    if ([SNP]::IsWindowVisible($h)) {
      $sb = New-Object System.Text.StringBuilder 256; [void][SNP]::GetWindowText($h, $sb, 256)
      if ($sb.ToString() -like "*OpenGL Renderer*") {
        $r = New-Object SNP+RECT; [void][SNP]::GetWindowRect($h, [ref]$r)
        $script:h = $h; $script:w = $r.Right - $r.Left; $script:ht = $r.Bottom - $r.Top
      }
    }
    return $true
  }
  [void][SNP]::EnumWindows($cb, [IntPtr]::Zero)
  if ($script:h -ne [IntPtr]::Zero) {
    $x = $b.X + [int](($b.Width - $script:w) / 2); $y = $b.Y + [int](($b.Height - $script:ht) / 2)
    if ($x -lt $b.X) { $x = $b.X }; if ($y -lt $b.Y) { $y = $b.Y }
    [void][SNP]::SetWindowPos($script:h, [IntPtr]::Zero, $x, $y, 0, 0, ($SWP_NOSIZE -bor $SWP_NOZORDER))
  }
  Start-Sleep -Milliseconds 500
}
Write-Output ("snap done (screen " + $Screen + ")")
