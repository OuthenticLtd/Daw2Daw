param([Parameter(Mandatory=$true)][string]$src, [Parameter(Mandatory=$true)][string]$dst, [int]$rate = 48000, [int]$bits = 32, [int]$channels = 2)
# Decode a media file's audio to PCM WAV with Windows Media Foundation (the
# decoder REAPER uses for MP4/AAC on Windows), through WinRT's
# MediaTranscoder. No DAW involved; part of Windows 10/11.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Transcoding.MediaTranscoder, Windows.Media.Transcoding, ContentType = WindowsRuntime]
$null = [Windows.Media.MediaProperties.MediaEncodingProfile, Windows.Media.MediaProperties, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
$asTaskProgress = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncActionWithProgress`1' })[0]
function Await($op, [Type]$t) {
    $task = $asTaskGeneric.MakeGenericMethod($t).Invoke($null, @($op))
    $task.Wait() | Out-Null
    $task.Result
}

$srcFull = (Resolve-Path $src).Path
$dstDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($dst))
$dstName = Split-Path -Leaf $dst
$in = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($srcFull)) ([Windows.Storage.StorageFile])
$folder = Await ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync($dstDir)) ([Windows.Storage.StorageFolder])
$out = Await ($folder.CreateFileAsync($dstName, [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) ([Windows.Storage.StorageFile])

$profile = [Windows.Media.MediaProperties.MediaEncodingProfile]::CreateWav([Windows.Media.MediaProperties.AudioEncodingQuality]::High)
$profile.Audio = [Windows.Media.MediaProperties.AudioEncodingProperties]::CreatePcm($rate, $channels, $bits)
$profile.Video = $null
$tc = New-Object Windows.Media.Transcoding.MediaTranscoder
$prep = Await ($tc.PrepareFileTranscodeAsync($in, $out, $profile)) ([Windows.Media.Transcoding.PrepareTranscodeResult])
if (-not $prep.CanTranscode) { Write-Output "cannot transcode: $($prep.FailureReason)"; exit 2 }
$act = $prep.TranscodeAsync()
$t = $asTaskProgress.MakeGenericMethod([double]).Invoke($null, @($act))
$t.Wait() | Out-Null
Write-Output "decoded $srcFull -> $($out.Path)"
