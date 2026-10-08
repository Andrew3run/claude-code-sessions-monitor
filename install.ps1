<#
.SYNOPSIS
  Creates (or removes) a Desktop shortcut for Sessions Monitor for Claude Code.

.EXAMPLE
  .\install.ps1            # create the shortcut
  .\install.ps1 -Remove    # delete it

  The shortcut runs claude_monitor.pyw with pythonw (no console window). Nothing else on the
  system is changed: no registry, services or settings.
#>
param([switch]$Remove)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$link = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Sessions Monitor.lnk'

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed $link" } else { Write-Host 'Nothing to remove.' }
    return
}

$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { throw 'pythonw not found. Install Python 3.9+ from python.org (with tcl/tk) and retry.' }

$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + (Join-Path $root 'claude_monitor.pyw') + '"'
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = Join-Path $root 'assets\icon.ico'
$shortcut.Description = 'Sessions Monitor for Claude Code'
$shortcut.Save()
Write-Host "Shortcut created: $link"
Write-Host 'Optional: to show plan usage limits, see "Usage limits" in the README (statusline-usage.js).'
