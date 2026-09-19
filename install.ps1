#requires -Version 5.1
<#
.SYNOPSIS
  首次安装：检查 WSL2，导入数字人引擎，跑一遍自检。

.DESCRIPTION
  只需要跑一次。之后每次用「一键启动.cmd」就行。

  和旧版的区别：旧版走 Docker Desktop + HeyGem 镜像，要装 Docker、要管守护进程、
  还得占一份常驻内存。现在数字人引擎直接跑在 WSL2 发行版里，`wsl --import` 一条
  命令的事，不需要 Docker。

  语音、大模型、口型全部是本地进程或外部 API，安装阶段不联网、不下载。
#>
[CmdletBinding()]
param(
    [string]$DistroName = "DuixDistro",
    [string]$InstallRoot = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$bundleRoot = $PSScriptRoot
$marker = Join-Path $bundleRoot ".installed.json"
$rootfs = Join-Path $bundleRoot "engine\duix-rootfs.tar.gz"
$rootfsPlain = Join-Path $bundleRoot "engine\duix-rootfs.tar"

$step = 0
function Write-Step {
    param([string]$Text)
    $script:step++
    Write-Host ""
    Write-Host ("[{0}/4] {1}" -f $script:step, $Text) -ForegroundColor Cyan
}

function Fail {
    param([string]$Message, [string[]]$Hints = @())
    Write-Host ""
    Write-Host "安装中断：$Message" -ForegroundColor Red
    foreach ($hint in $Hints) { Write-Host "  · $hint" -ForegroundColor Yellow }
    exit 1
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-WslReady {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { return $false }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & wsl.exe --status *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
        # wsl.exe 存在不等于 WSL2 可用；老机器可能只开了 WSL1。
        # 显式把默认版本设为 2，失败就进入下面的自动启用/修复流程。
        & wsl.exe --set-default-version 2 *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Restart-Elevated {
    Write-Host "  Windows 需要管理员权限来启用 WSL，正在请求授权……" -ForegroundColor Yellow
    $elevationArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -DistroName `"$DistroName`""
    if ($InstallRoot) { $elevationArguments += " -InstallRoot `"$InstallRoot`"" }
    if ($Force) { $elevationArguments += " -Force" }
    try {
        $elevatedProcess = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru `
            -ArgumentList $elevationArguments
        exit $elevatedProcess.ExitCode
    } catch {
        Fail "没有取得管理员权限" @(
            "请在弹出的 Windows 授权窗口里点「是」",
            "或者右键「首次安装.cmd」，选择「以管理员身份运行」"
        )
    }
}

Write-Host ""
Write-Host "  AI GIRLFRIEND · 首次安装" -ForegroundColor DarkYellow
Write-Host "  ------------------------"

# ── 1. 包完整性 ───────────────────────────────────────────
Write-Step "检查整合包"
if ($bundleRoot -match '[^\x00-\x7F]' -or $bundleRoot -match '\s') {
    Fail "整合包所在路径含中文、特殊字符或空格" @(
        "请把整个文件夹移到纯英文、无空格的位置，例如 D:\AI-Girlfriend",
        "这是 WSL 数字人引擎的路径限制"
    )
}
$required = @(
    @{ Path = (Join-Path $bundleRoot "runtime\python311\python.exe"); Name = "Python 运行时" },
    @{ Path = (Join-Path $bundleRoot "runtime\ffmpeg\ffmpeg.exe");    Name = "ffmpeg" },
    @{ Path = (Join-Path $bundleRoot "app\.venv\Lib\site-packages");  Name = "依赖库" },
    @{ Path = (Join-Path $bundleRoot "app\.venv-omni-overlay");       Name = "语音依赖层" },
    @{ Path = (Join-Path $bundleRoot "AI-Girlfriend-Models\tts\omnivoice"); Name = "语音模型" },
    @{ Path = (Join-Path $bundleRoot "AI-Girlfriend-Models\stt\whisper-large-v3-turbo"); Name = "识别模型" }
)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_.Path) })
if ($missing.Count -gt 0) {
    Fail "整合包不完整，缺少：$(($missing | ForEach-Object { $_.Name }) -join '、')" @(
        "多半是解压中断了，或者网盘下载的分卷没凑齐",
        "重新完整解压一次再运行"
    )
}
Write-Host "  完整" -ForegroundColor Green

# ── 2. WSL2 ───────────────────────────────────────────────
Write-Step "检查 WSL2"
if (-not (Test-WslReady)) {
    if (-not (Test-Administrator)) { Restart-Elevated }

    Write-Host "  这台电脑还没有可用的 WSL2，正在启用 Windows 组件……" -ForegroundColor Yellow
    $wslCommand = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if ($wslCommand) {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & wsl.exe --install --no-distribution
            $wslInstallExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($wslInstallExitCode -ne 0) {
            Fail "Windows 没能自动启用 WSL（退出码 $wslInstallExitCode）" @(
                "确认 Windows 已完成系统更新",
                "确认 BIOS 里的 CPU 虚拟化已开启",
                "也可以在管理员 PowerShell 里手动执行：wsl --install --no-distribution"
            )
        }
    } else {
        try {
            Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -All -NoRestart | Out-Null
            Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All -NoRestart | Out-Null
        } catch {
            Fail "这版 Windows 无法自动启用 WSL" @(
                "先完成 Windows 系统更新",
                "然后在管理员 PowerShell 里执行：wsl --install --no-distribution"
            )
        }
    }

    Write-Host ""
    Write-Host "WSL2 已启用，但 Windows 必须重启一次才能继续。" -ForegroundColor Green
    Write-Host "重启电脑后，再双击一次「首次安装.cmd」，它会继续导入数字人引擎。"
    exit 2
}
# `wsl -l -q` 在没有任何发行版时返回非零，这不算错误，所以不用 ErrorAction 拦
$distros = @()
try {
    $distros = @(& wsl.exe -l -q 2>$null | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
} catch {
    $distros = @()
}
Write-Host "  WSL 可用" -ForegroundColor Green

# ── 3. 数字人引擎 ─────────────────────────────────────────
Write-Step "准备数字人引擎"
$alreadyThere = $distros -contains $DistroName
if ($alreadyThere -and -not $Force) {
    Write-Host ("  发行版 {0} 已存在，跳过导入" -f $DistroName) -ForegroundColor Green
} else {
    $source = if (Test-Path -LiteralPath $rootfs) { $rootfs }
              elseif (Test-Path -LiteralPath $rootfsPlain) { $rootfsPlain }
              else { $null }
    if (-not $source) {
        Fail "找不到数字人引擎镜像" @(
            "应该在：engine\duix-rootfs.tar.gz",
            "没有它也能用，但只有纯语音对话，不会有口型画面",
            "想跳过数字人：直接运行 一键启动.cmd，它会自己降级"
        )
    }

    $target = if ($InstallRoot) { $InstallRoot } else { Join-Path $bundleRoot "engine\wsl" }
    $targetFullPath = [IO.Path]::GetFullPath($target)
    $targetDriveName = [IO.Path]::GetPathRoot($targetFullPath).TrimEnd('\').TrimEnd(':')
    $targetDrive = Get-PSDrive -Name $targetDriveName -ErrorAction SilentlyContinue
    if ($targetDrive -and $targetDrive.Free -lt 20GB) {
        Fail "数字人引擎所在磁盘空间不足" @(
            "当前可用：$([Math]::Round($targetDrive.Free / 1GB, 1)) GB，至少需要 20 GB",
            "清理空间后重试，或者用 -InstallRoot 指定其他英文路径"
        )
    }
    New-Item -ItemType Directory -Force -Path $target | Out-Null

    $sizeGb = (Get-Item -LiteralPath $source).Length / 1GB
    Write-Host ("  正在导入 {0:N1} GB 的镜像，解压需要几分钟，别关窗口……" -f $sizeGb)
    if ($alreadyThere) {
        Write-Host "  （--Force：先注销同名发行版）" -ForegroundColor Yellow
        & wsl.exe --unregister $DistroName | Out-Null
    }
    & wsl.exe --import $DistroName $target $source --version 2
    if ($LASTEXITCODE -ne 0) {
        Fail "导入失败（退出码 $LASTEXITCODE）" @(
            "确认 BIOS 里虚拟化已开启",
            "确认磁盘剩余空间大于 20 GB",
            "在 PowerShell 里执行 wsl --status 看看 WSL 本身是否正常"
        )
    }
    Write-Host "  导入完成" -ForegroundColor Green
}

# ── 4. 自检 ───────────────────────────────────────────────
Write-Step "自检"
$engineOk = $false
# 脚本顶上是 ErrorActionPreference = Stop，而 wsl.exe 会往 stderr 写一行
# localhost 代理的警告——那会被当成终止错误直接吞进 catch，探测永远失败。
# 所以这一段临时放开。
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    # WSL 那行警告也可能混进 stdout，所以不能整体比较，只看有没有那个标记。
    $probe = & wsl.exe -d $DistroName -e bash -lc "test -f /code/app_local.py && echo DUIX_OK" 2>$null
    $engineOk = (($probe -replace "`0", "") -join "`n") -match "DUIX_OK"
} catch {
    $engineOk = $false
} finally {
    $ErrorActionPreference = $prevEap
}
if ($engineOk) {
    Write-Host "  数字人引擎就位" -ForegroundColor Green
} else {
    Write-Host "  数字人引擎没响应 —— 启动时会自动降级成纯语音对话" -ForegroundColor Yellow
}

@{
    installed_at = (Get-Date).ToString("s")
    distro       = $DistroName
    engine_ok    = $engineOk
} | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host "接下来双击「一键启动.cmd」，浏览器会自己打开。"
Write-Host ""
Write-Host "第一次打开界面后，右上角「设置」里填一个语言模型 API Key，就可以先聊天。" -ForegroundColor Cyan
Write-Host "包里已有演示角色和默认声音；想换成自己的，再上传角色视频和参考录音。"
Write-Host ""
exit 0
