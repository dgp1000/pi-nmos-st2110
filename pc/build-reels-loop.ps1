# Build the Atoll "Test Reels" channel loop.
# The snapplings reels are short, vertical (1080x1920) phone clips. Streaming them as a folder
# playlist restarts the encoder every clip AND their odd timestamps break the file-paced udpsink
# (the stream never transmits). So concatenate them into ONE continuous 1280x720 file (portrait
# pillarboxed, timestamps reset, 30fps) -- the reels sender then feeds a stable stream like the
# other long-file channels. Re-run whenever the Test Reels folder changes.
#   powershell -ExecutionPolicy Bypass -File pc\build-reels-loop.ps1
$dir  = "C:\Users\dgper\OneDrive\Music\Test Reels"
$out  = "C:\Users\dgper\test-reels-loop.mp4"
$list = "C:\Users\dgper\reels-list.txt"
$clips = Get-ChildItem -LiteralPath $dir -Filter *.mp4 -ErrorAction SilentlyContinue | Sort-Object Name
if (-not $clips) { Write-Output "no .mp4 clips in $dir"; exit 1 }
($clips | ForEach-Object { "file " + [char]39 + $_.FullName + [char]39 }) | Set-Content -Path $list -Encoding ascii
Write-Output ("concatenating " + $clips.Count + " clips -> " + $out)
& ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i $list `
  -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,setpts=PTS-STARTPTS" `
  -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -ar 48000 $out
if (Test-Path $out) { Write-Output ("done: " + [math]::Round((Get-Item $out).Length/1MB,1) + " MB  (restart the reels sender / re-run launch-media.sh to pick it up)") }
