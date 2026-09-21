<#
.SYNOPSIS
    在没有 Python 的电脑上，自动下载并静默安装官方 Python 3.11.9。

.DESCRIPTION
    由 start.bat 在「一个 Python 都找不到」时调用。装完就退出，不向调用方返回
    任何东西 —— 安装位置是固定的（当前用户目录下），start.bat 会自己到常见的
    安装目录里把它找回来。

    为什么固定装 3.11.9：它是 3.11 系列最后一个带 Windows 安装包的版本，
    也是本项目实际验证过的版本（tree-sitter 及其语法包在这版上有预编译轮子，
    更新的版本反而可能装不上）。

    安全上做三重校验，任何一条不过就删掉安装包并中止：
      1. 下载地址是硬编码的白名单（官方源 + 三个国内镜像），不接受命令行传入；
      2. SHA256 / MD5 必须等于写死的实测值（三个架构各一套）；
      3. 安装包必须带有效的 Authenticode 数字签名，且签名主体是
         "Python Software Foundation" —— 这一条比哈希更强，它由 Windows
         自带的信任链验证，能挡住「下载地址被劫持后换成一个别的程序」。
    校验通过后才去掉「来自 Internet」标记（Mark-of-the-Web）并执行安装。

    校验逻辑本身在 tools/lib/installer_check.ps1 里，那份是纯函数、可离线测试。

.PARAMETER DryRun
    下载并完成全部校验，但不执行安装程序。用来验证下载链路与校验逻辑。

.PARAMETER VerifyPath
    离线校验一个已经存在的安装包（配合 -Architecture 指定它属于哪个架构），
    不联网。给自动化测试用。

.PARAMETER Architecture
    Auto（默认，按当前系统判断）/ AMD64 / ARM64 / x86。

.PARAMETER KeepInstaller
    安装完成后保留安装包（默认装完就删，省磁盘空间）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\auto_install_python.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\auto_install_python.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$VerifyPath,
    [ValidateSet('Auto', 'AMD64', 'ARM64', 'x86')]
    [string]$Architecture = 'Auto',
    [switch]$KeepInstaller
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'lib\installer_check.ps1')

# 静默安装参数：按用户安装（不需要管理员权限、不污染其它账号），
# 顺便把 PATH 和 py 启动器配上，这样以后再运行 start.bat 能直接找到。
$InstallArgs = @(
    '/quiet'
    'InstallAllUsers=0'
    'PrependPath=1'
    'Include_pip=1'
    'Include_launcher=1'
    'InstallLauncherAllUsers=0'
    'Include_test=0'
    'Include_doc=0'
    'Shortcuts=0'
)


function Write-Warn {
    param([string]$Message)
    Write-Host "      [提醒] $Message" -ForegroundColor Yellow
}


function Get-FreeSpaceMB {
    param([string]$Path)
    try {
        $root = [System.IO.Path]::GetPathRoot((Resolve-Path $Path).Path)
        $drive = New-Object System.IO.DriveInfo $root
        return [math]::Floor($drive.AvailableFreeSpace / 1MB)
    } catch {
        return $null
    }
}


function Find-InstalledPython {
    <#
        安装完之后，到 Python 安装程序可能落脚的地方把 python.exe 找回来。
        我们的 cmd 会话拿不到刚写进注册表的新 PATH，所以必须自己找。
    #>
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python')
        $env:ProgramFiles
        ${env:ProgramFiles(x86)}
        'C:\'
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($root in $roots) {
        $dirs = Get-ChildItem -Path $root -Filter 'Python31*' -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending
        foreach ($dir in $dirs) {
            $exe = Join-Path $dir.FullName 'python.exe'
            if (Test-Path $exe) { return $exe }
        }
    }

    # 注册表兜底：装在自定义目录里的情况
    $keys = @(
        'HKCU:\SOFTWARE\Python\PythonCore\3.11\InstallPath'
        'HKLM:\SOFTWARE\Python\PythonCore\3.11\InstallPath'
        'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore\3.11\InstallPath'
    )
    foreach ($key in $keys) {
        if (-not (Test-Path $key)) { continue }
        $value = (Get-Item -Path $key).GetValue('')
        if ($value) {
            $exe = Join-Path $value 'python.exe'
            if (Test-Path $exe) { return $exe }
        }
    }
    return $null
}


function Save-FileWithProgress {
    <#
        下载并实时报告进度。PS 5.1 里 Invoke-WebRequest 的进度条会把下载拖慢很多，
        所以这里自己读流、自己打印进度；同时学生也能看到「确实在动」，
        不会以为卡死了。
    #>
    param([string]$Url, [string]$OutFile)

    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.UserAgent = 'ai-code-tutor-agent/1.0'
    $request.Timeout = 60000
    $request.ReadWriteTimeout = 120000

    $response = $request.GetResponse()
    try {
        $total = $response.ContentLength
        $input = $response.GetResponseStream()
        $output = [System.IO.File]::Create($OutFile)
        try {
            $buffer = New-Object byte[] 131072
            $done = 0
            $lastReport = [DateTime]::UtcNow
            while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $output.Write($buffer, 0, $read)
                $done += $read
                if (([DateTime]::UtcNow - $lastReport).TotalMilliseconds -ge 500) {
                    $lastReport = [DateTime]::UtcNow
                    if ($total -gt 0) {
                        $pct = [int](100 * $done / $total)
                        Write-Host ("`r      正在下载 ... {0,3}%  ({1:N1} / {2:N1} MB)" -f `
                                $pct, ($done / 1MB), ($total / 1MB)) -NoNewline
                    } else {
                        Write-Host ("`r      正在下载 ... {0:N1} MB" -f ($done / 1MB)) -NoNewline
                    }
                }
            }
        } finally {
            $output.Close()
        }
        $input.Close()
        Write-Host ''
        return $done
    } finally {
        $response.Close()
    }
}


# ===========================================================================
#  分支 A：离线校验模式（自动化测试用，不联网）
# ===========================================================================
if ($VerifyPath) {
    $verifyArch = Resolve-TargetArchitecture -Requested $Architecture
    $verifyRelease = Get-InstallerRelease -Architecture $verifyArch

    if (-not (Test-Path $VerifyPath)) {
        Write-Host "找不到文件：$VerifyPath"
        exit 1
    }
    $problem = Test-InstallerHash -Path $VerifyPath -Release $verifyRelease
    if ($problem) {
        Write-Host "校验失败：$problem"
        exit 1
    }
    Write-Host "SHA256/MD5 校验通过（$verifyArch）"

    $sigProblem = Get-InstallerSignatureProblem -Path $VerifyPath
    if ($sigProblem -and $sigProblem.Fatal) {
        Write-Host "校验失败：$($sigProblem.Message)"
        exit 1
    }
    Write-Host '签名校验通过'
    exit 0
}


# ===========================================================================
#  主流程
# ===========================================================================
Write-Host ''
Write-Host '  ------------------------------------------------------------------'
Write-Host '   这台电脑上没有找到 Python，正在自动安装（只需一次）'
Write-Host '  ------------------------------------------------------------------'

$arch = Resolve-TargetArchitecture -Requested $Architecture
$release = Get-InstallerRelease -Architecture $arch
Write-Host "      目标版本：Python 3.11.9  $($release.Label)"
Write-Host "      安装包大小：约 $([math]::Round($release.Size / 1MB, 1)) MB"
Write-Host '      安装方式：只给当前用户安装，不需要管理员权限'
Write-Host ''

$workDir = Join-Path $env:TEMP 'ai-code-tutor-python'
New-Item -ItemType Directory -Force -Path $workDir | Out-Null
$installer = Join-Path $workDir $release.File

$freeMB = Get-FreeSpaceMB -Path $env:TEMP
if ($freeMB -and $freeMB -lt 800) {
    Write-Warn "临时目录所在磁盘剩余空间只有 $freeMB MB，Python 安装约需 150 MB，可能会失败。"
}

# --- 1) 下载（逐个换源，任何一个成功就停） ---
$downloaded = $false
foreach ($mirror in $InstallerMirrors) {
    $url = $mirror + $release.File
    $mirrorHost = ([Uri]$url).Host
    Write-Host "      正在从 $mirrorHost 下载 ..."
    try {
        if (Test-Path $installer) { Remove-Item $installer -Force }
        $bytes = Save-FileWithProgress -Url $url -OutFile $installer
        if ($bytes -ne $release.Size) {
            throw "文件大小不对：期望 $($release.Size) 字节，实际 $bytes 字节"
        }
        $downloaded = $true
        Write-Host "      下载完成（$([math]::Round($bytes / 1MB, 1)) MB）"
        break
    } catch {
        Write-Host ''
        Write-Warn "$mirrorHost 下载失败：$($_.Exception.Message)"
        if (Test-Path $installer) { Remove-Item $installer -Force }
    }
}

if (-not $downloaded) {
    Write-Host ''
    Write-Host '  [错误] 所有下载源都失败了，无法自动安装 Python。'
    Write-Host ''
    Write-Host '  这通常是网络问题（教室网限制访问外网、需要代理等）。可以改用下面的办法：'
    Write-Host '    1. 连上手机热点后重新双击 start.bat'
    Write-Host '    2. 或者手动安装：打开 https://www.python.org/downloads/'
    Write-Host '       下载 Python 3.11 的 Windows 安装包，安装时务必勾选'
    Write-Host '       最下方的 "Add Python to PATH"，装好后重新双击 start.bat'
    Write-Host ''
    exit 2
}

# --- 2) 校验哈希 ---
Write-Host '      正在校验安装包（SHA256 / MD5）...'
$problem = Test-InstallerHash -Path $installer -Release $release
if ($problem) {
    Remove-Item $installer -Force -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Host '  [错误] 安装包校验没通过，已删除，安装中止。'
    Write-Host "         $problem"
    Write-Host ''
    Write-Host '  这说明下载到的文件不是官方的那一份（下载被劫持或镜像缓存被污染）。'
    Write-Host '  请换一个网络重试，或按上面的说明手动安装 Python。'
    Write-Host ''
    exit 3
}

# --- 3) 校验数字签名 ---
Write-Host '      正在校验数字签名...'
$sigProblem = Get-InstallerSignatureProblem -Path $installer
if ($sigProblem) {
    if ($sigProblem.Fatal) {
        Remove-Item $installer -Force -ErrorAction SilentlyContinue
        Write-Host ''
        Write-Host '  [错误] 安装包的数字签名有问题，已删除，安装中止。'
        Write-Host "         $($sigProblem.Message)"
        Write-Host ''
        exit 4
    }
    Write-Warn $sigProblem.Message
}

# 校验都过了，去掉「来自 Internet」标记再执行，免得被 SmartScreen 拦下来
try { Unblock-File -Path $installer -ErrorAction SilentlyContinue } catch { }
Write-Host '      校验通过。'

if ($DryRun) {
    Write-Host ''
    Write-Host '      [DryRun] 已下载并通过全部校验，按要求不执行安装程序。'
    Write-Host "      安装包位置：$installer"
    Write-Host ''
    exit 0
}

# --- 4) 静默安装 ---
Write-Host ''
Write-Host '      正在安装，请耐心等待 1-3 分钟（窗口会停住不动，属正常现象）...'
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $proc = Start-Process -FilePath $installer -ArgumentList $InstallArgs -Wait -PassThru
    $code = $proc.ExitCode
} catch {
    Write-Host ''
    Write-Host '  [错误] 安装程序启动失败。'
    Write-Host "         $($_.Exception.Message)"
    Write-Host ''
    Write-Host '  可以手动安装（安装时勾选 "Add Python to PATH"）：'
    Write-Host "    $installer"
    Write-Host ''
    exit 5
}
$sw.Stop()

# 0 = 成功；3010 = 成功但建议重启（本项目不需要重启，照样继续）
if ($code -ne 0 -and $code -ne 3010) {
    Write-Host ''
    Write-Host "  [错误] 安装程序返回了错误码 $code，安装没有完成。"
    Write-Host ''
    Write-Host '  常见原因：'
    Write-Host '    * 安装过程被安全软件拦截 —— 暂时关闭后重试'
    Write-Host '    * 磁盘空间不足 —— 清理后重试'
    Write-Host '    * 系统里的 Python 安装被组策略禁用 —— 请老师协助'
    Write-Host ''
    Write-Host '  也可以手动安装（安装时勾选 "Add Python to PATH"）：'
    Write-Host "    $installer"
    Write-Host ''
    exit 6
}
Write-Host ("      安装完成，用时 {0:N0} 秒。" -f $sw.Elapsed.TotalSeconds)

# --- 5) 确认装到哪儿了 ---
$pythonExe = Find-InstalledPython
if (-not $pythonExe) {
    Write-Host ''
    Write-Host '  [警告] 安装程序报告成功，但没有找到 python.exe。'
    Write-Host '         请重新双击 start.bat；若仍然失败，请手动安装 Python 3.11。'
    Write-Host ''
    exit 7
}

Write-Host "      已安装到：$pythonExe"
Write-Host ''

if (-not $KeepInstaller) {
    Remove-Item $installer -Force -ErrorAction SilentlyContinue
}

exit 0
