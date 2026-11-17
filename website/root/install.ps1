# Install Force on Windows (SPEC section 55). Force runs inside WSL, Windows' Linux; this installs it there and puts a
# `force` command into PowerShell, cmd and Windows Terminal. In PowerShell:
#
#   irm https://useforce.cc/install.ps1 | iex
#
# With options:
#
#   & ([scriptblock]::Create((irm https://useforce.cc/install.ps1))) -Distro Debian
#   & ([scriptblock]::Create((irm https://useforce.cc/install.ps1))) -Uninstall
#
# No admin rights, except once to install WSL itself when Windows does not have it yet.
param(
    [string]$Distro = $env:FORCE_WSL_DISTRO,     # the WSL distribution Force lives in (default: WSL's default one)
    [switch]$Uninstall
)

function Install-Force {
    param([string]$Distro, [bool]$Uninstall)
    # no 'Stop': Windows PowerShell 5.1 turns a native program's stderr into errors; exit codes are checked instead
    $env:WSL_UTF8 = '1'                          # wsl.exe's own messages in UTF-8, not UTF-16
    # the Linux installer comes from here; a test server or a mirror can stand in (as for install.sh)
    $base = if ($env:FORCE_DOWNLOAD_BASE) { $env:FORCE_DOWNLOAD_BASE.TrimEnd('/') } else { 'https://useforce.cc' }
    $bin = Join-Path $env:LOCALAPPDATA 'Programs\Force'
    $shim = Join-Path $bin 'force.cmd'

    function Say([string]$m) { Write-Host "force: $m" }
    function Wsl([string[]]$a) {                 # wsl.exe's output, NULs (UTF-16 leftovers) and blanks removed
        $out = & wsl.exe @a 2>&1
        (($out | Out-String) -replace "`0", '').Trim()
    }
    function Set-UserPath([string]$add, [string]$remove) {
        $p = [Environment]::GetEnvironmentVariable('Path', 'User')
        $parts = @(($p -split ';') | Where-Object { $_ -and $_ -ne $remove -and $_ -ne $add })
        if ($add) { $parts += $add }
        [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')
        $env:Path = (@(($env:Path -split ';') | Where-Object { $_ -and $_ -ne $remove -and $_ -ne $add }) +
                     @($add | Where-Object { $_ })) -join ';'
    }

    if ($Uninstall) {
        if (Test-Path $shim) {
            Say 'removing Force from WSL'
            & $shim uninstall --yes
        }
        Remove-Item -Recurse -Force $bin -ErrorAction SilentlyContinue
        Set-UserPath '' $bin
        Say 'removed the force command. Your sessions and settings inside WSL are gone with it; WSL itself stays.'
        return
    }

    # 1. WSL with a Linux distribution
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw 'force: this Windows has no WSL. Force needs Windows 10 (version 2004 or later) or Windows 11.'
    }
    if ($Distro) {
        & wsl.exe -d $Distro --exec true 2>$null
        if ($LASTEXITCODE -ne 0) { throw "force: there is no WSL distribution called $Distro (wsl -l lists them)" }
    } else {
        $Distro = Wsl @('--exec', 'sh', '-c', 'echo $WSL_DISTRO_NAME')
        if ($LASTEXITCODE -ne 0 -or -not $Distro -or $Distro -match '\s') {
            Say 'Force runs inside WSL, which needs a Linux distribution. Installing WSL with Ubuntu now:'
            Say 'Windows asks for permission, and may want a restart.'
            Start-Process wsl.exe -ArgumentList @('--install', '-d', 'Ubuntu-24.04') -Verb RunAs -Wait
            Say 'Next: open Ubuntu from the Start menu once and choose a user name and password,'
            Say 'then run this line again: irm https://useforce.cc/install.ps1 | iex'
            return
        }
    }
    Say "installing into WSL ($Distro)"

    # 2. a user, and curl
    if ((Wsl @('-d', $Distro, '--exec', 'id', '-u')) -eq '0') {
        throw ("force: $Distro logs in as root, and Force installs as a user. Make one and make it the default: " +
               "wsl -d $Distro -u root adduser NAME, then in $Distro /etc/wsl.conf: [user] default=NAME, then wsl --terminate $Distro")
    }
    & wsl.exe -d $Distro --exec sh -c 'command -v curl >/dev/null'
    if ($LASTEXITCODE -ne 0) { throw "force: $Distro has no curl: wsl -d $Distro -u root apt-get install -y curl" }

    # 3. the force command for PowerShell and cmd: runs Force inside WSL, from the folder you are in. Written
    #    before the Linux installer runs, so its Windows Terminal profile (SPEC section 45) is "Force", running it
    # the distro the command ran before (an earlier install.ps1): it gets its own profile back afterwards
    $previous = if (Test-Path $shim) { ((Get-Content -Raw $shim) | Select-String 'wsl\.exe -d (\S+) ').Matches |
                                       ForEach-Object { $_.Groups[1].Value } | Select-Object -First 1 }
    $home_ = Wsl @('-d', $Distro, '--exec', 'sh', '-c', 'printf %s $HOME')
    $run = "$home_/.local/share/force/app/share/windows/force-wsl"
    New-Item -ItemType Directory -Force $bin | Out-Null
    # %CD%\. : a folder ending in a backslash (C:\) would otherwise escape the closing quote
    Set-Content -Encoding Ascii -Path $shim -Value @(
        '@echo off',
        "rem Force (useforce.cc): runs Force inside WSL ($Distro), from the folder you are in. install.ps1 wrote it.",
        "wsl.exe -d $Distro --cd ""%CD%\."" --exec $run %*")

    # 4. Force itself, with its Linux installer (as on Linux; it pins the release's SHA-256)
    $env_ = if ($env:FORCE_DOWNLOAD_BASE) { "FORCE_DOWNLOAD_BASE='$base' " } else { '' }
    & wsl.exe -d $Distro --cd '~' --exec sh -c "curl -fsSL '$base/install.sh' | ${env_}sh"
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -Force $shim -ErrorAction SilentlyContinue
        throw "force: installing Force inside $Distro failed (above)"
    }
    & wsl.exe -d $Distro --exec test -x $run
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -Force $shim -ErrorAction SilentlyContinue
        throw "force: $run is missing: the installed Force is too old for Windows"
    }
    Set-UserPath $bin ''
    if ($previous -and $previous -ne $Distro) {
        # the "Force" profile is this distro's now: the other one's Force writes its "Force (<distro>)" again
        & wsl.exe -d $previous --exec sh -lc 'test -x ~/.local/bin/force && exec ~/.local/bin/force fonts' *> $null
        Say "force now runs Force in $Distro; Force in $previous keeps its Windows Terminal profile, ""Force ($previous)"""
    }

    Say "done. Type force in PowerShell, cmd or Windows Terminal (a new window or tab finds it)."
}

try {
    Install-Force -Distro $Distro -Uninstall $Uninstall.IsPresent
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $global:LASTEXITCODE = 1
}
