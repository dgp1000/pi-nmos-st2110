# Capture what is actually on the PC's monitors, so the rendered multiview can be inspected
# exactly as it appears -- including anything the WSLg/waylandsink display path does to it, which a
# headless GStreamer render can never show.
#   powershell.exe -File grab-screen.ps1 -Screen 2 -Out C:\Users\perki\atoll-shot.png
param(
    [int]$Screen = 2,                                   # 1-based; 0 = all monitors stitched
    [string]$Out = "C:\Users\perki\atoll-shot.png"
)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$all = [System.Windows.Forms.Screen]::AllScreens
if ($Screen -le 0) {
    $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
} elseif ($Screen -le $all.Count) {
    $b = $all[$Screen - 1].Bounds
} else {
    Write-Output "SCREEN_OUT_OF_RANGE count=$($all.Count)"
    exit 1
}

$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "OK $Out $($b.Width)x$($b.Height) (monitor $Screen of $($all.Count))"
