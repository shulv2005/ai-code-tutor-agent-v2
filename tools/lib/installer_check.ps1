<#
    安装包校验的纯函数库 —— 被 tools/auto_install_python.ps1 点源（dot-source）使用，
    也被 tools/verify_bat.py 直接点源来做离线测试。

    单独拆出来的原因：这些函数是「决定要不要执行一个下载来的 exe」的唯一闸门，
    必须能脱离下载、脱离安装单独测。所有外部输入（系统架构、文件路径）都从参数进来，
    不从环境里偷偷读，测试才能构造各种情况。
#>

# Python 3.11.9 三个架构的安装包元数据。
# SHA256 是本项目实测出来的（同时核对了 python.org 公布的 MD5），不是抄来的。
$InstallerReleases = [ordered]@{
    'AMD64' = @{
        File   = 'python-3.11.9-amd64.exe'
        Sha256 = '5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde'
        Md5    = 'e8dcd502e34932eebcaf1be056d5cbcd'
        Size   = 26216840
        Label  = '64 位 (x64)'
    }
    'ARM64' = @{
        File   = 'python-3.11.9-arm64.exe'
        Sha256 = '58f3a4e91b63d5a680ecc77c1db4565a1e3966e8656d4c8b89200d58c1f5c4f3'
        Md5    = '328d93f71cb078965e4cfa2eb2663fa1'
        Size   = 25483288
        Label  = 'ARM64'
    }
    'x86'   = @{
        File   = 'python-3.11.9.exe'
        Sha256 = 'af19e5e2f03e715a822181f2cb7d4efef4eda13fa4a2db6da12e998e46f5cbf9'
        Md5    = '2a1d1ac2d8a0aa847515f9dd121ccbb7'
        Size   = 24933192
        Label  = '32 位 (x86)'
    }
}

# 下载源，按「国内快」到「官方稳」排序。全都镜像同一份文件，字节完全一致，
# 所以换源不影响校验结果 —— 校验本来就是用来兜住「源不可信」这件事的。
$InstallerMirrors = @(
    'https://mirrors.aliyun.com/python-release/windows/'
    'https://mirrors.huaweicloud.com/python/3.11.9/'
    'https://registry.npmmirror.com/-/binary/python/3.11.9/'
    'https://www.python.org/ftp/python/3.11.9/'
)


function Resolve-TargetArchitecture {
    <#
        决定该下哪个架构的安装包。

        参数全部显式传入（而不是直接读 $env:），这样测试可以构造
        「32 位 cmd 跑在 64 位系统上」这类环境，不用真的换机器。
    #>
    [CmdletBinding()]
    param(
        [ValidateSet('Auto', 'AMD64', 'ARM64', 'x86')]
        [string]$Requested = 'Auto',

        [string]$ProcessorArchitecture = $env:PROCESSOR_ARCHITECTURE,

        # 32 位的 cmd 跑在 64 位系统上时，cmd 自己报告 x86，
        # 真正的系统架构放在 PROCESSOR_ARCHITEW6432 里。
        [string]$ProcessorArchitectureW6432 = $env:PROCESSOR_ARCHITEW6432
    )

    if ($Requested -ne 'Auto') { return $Requested }

    $arch = $ProcessorArchitecture
    if ($ProcessorArchitectureW6432) { $arch = $ProcessorArchitectureW6432 }

    switch ($arch) {
        'ARM64' { return 'ARM64' }
        'AMD64' { return 'AMD64' }
        'x86' { return 'x86' }
        default { return 'AMD64' }
    }
}


function Get-InstallerRelease {
    <# 取某个架构对应的安装包元数据。 #>
    [CmdletBinding()]
    param(
        [ValidateSet('AMD64', 'ARM64', 'x86')]
        [string]$Architecture
    )
    return $InstallerReleases[$Architecture]
}


function Test-InstallerHash {
    <#
        校验 SHA256 与 MD5。通过返回 $null，否则返回给用户看的错误说明。
        MD5 是 python.org 页面上公布的那个值，用来兜住「下载被截断」；
        SHA256 是本项目实测的固定值，是主要防线。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Release
    )

    $sha = (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLower()
    if ($sha -ne $Release.Sha256) {
        return "SHA256 不匹配（文件与官方发布的不一致）`n        期望：$($Release.Sha256)`n        实际：$sha"
    }

    $md5 = (Get-FileHash -Path $Path -Algorithm MD5).Hash.ToLower()
    if ($md5 -ne $Release.Md5) {
        return "MD5 不匹配（文件可能不完整）`n        期望：$($Release.Md5)`n        实际：$md5"
    }

    return $null
}


function Get-InstallerSignatureProblem {
    <#
        校验 Authenticode 数字签名。返回 $null 表示没问题，否则返回
        @{ Fatal = $true/$false; Message = '...' }。

        分两类：
          * 硬伤 —— 没有签名，或签名主体不是 Python Software Foundation。
            必须拒绝：这说明文件不是官方那个安装包。
          * 软伤 —— 签名在、主体也对，只是本机证书链暂时验不过
            （例如系统根证书库太旧）。SHA256 已经证明文件没被动过，
            这种情况只提醒，不阻断安装，否则会在老系统上白白装不了。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $sig = Get-AuthenticodeSignature -FilePath $Path
    if (-not $sig.SignerCertificate) {
        return @{ Fatal = $true; Message = '安装包没有数字签名，已拒绝执行。' }
    }

    $subject = $sig.SignerCertificate.Subject
    if ($subject -notmatch 'Python Software Foundation') {
        return @{
            Fatal   = $true
            Message = "签名主体不是 Python Software Foundation，已拒绝执行。`n         实际主体：$subject"
        }
    }

    if ($sig.Status -ne 'Valid') {
        return @{
            Fatal   = $false
            Message = "数字签名状态为 $($sig.Status)（$($sig.StatusMessage)）。" +
                      'SHA256 已核对无误，继续安装。'
        }
    }

    return $null
}
