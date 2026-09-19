#requires -Version 5.1
<#
.SYNOPSIS
  一键启动：把五个服务按顺序拉起来，然后打开浏览器。

.DESCRIPTION
  端口分配（都只绑 127.0.0.1，不对外）：

      7860  界面（浏览器打开这个）
      8766  实时语音管线（WebSocket）
      8790  DSH 桥接 —— 大模型 + 长期记忆
      8791  OmniVoice —— 声音克隆
      8383  数字人引擎（跑在 WSL 里）

  数字人是**可选**的：WSL 发行版不在就跳过，降级成纯语音对话，其余照常。

  真正干活的是 app\start-ours.ps1，这里只负责环境变量、首次安装引导和错误提示。
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$SkipDigitalHuman,
    [string]$DistroName = "DuixDistro"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$bundleRoot   = $PSScriptRoot
$appRoot      = Join-Path $bundleRoot "app"
$marker       = Join-Path $bundleRoot ".installed.json"
$pythonHome   = Join-Path $bundleRoot "runtime\python311"
$ffmpegHome   = Join-Path $bundleRoot "runtime\ffmpeg"
$runtimePython= Join-Path $pythonHome "python.exe"
$sitePackages = Join-Path $appRoot ".venv\Lib\site-packages"
$s2sSrc       = Join-Path $appRoot "speech-to-speech\src"
$startScript  = Join-Path $appRoot "start-ours.ps1"

Write-Host ""
Write-Host "  AI GIRLFRIEND" -ForegroundColor DarkYellow
Write-Host ""

if (-not (Test-Path -LiteralPath $runtimePython)) {
    Write-Host "找不到 runtime\python311\python.exe —— 整合包没解压完整。" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $startScript)) {
    Write-Host "找不到 app\start-ours.ps1 —— 整合包没解压完整。" -ForegroundColor Red
    exit 1
}

# 自带的 ffmpeg 和 python 放在 PATH 最前面：用户机器上那些不一定是什么版本
$env:PATH               = "$ffmpegHome;$pythonHome;$env:PATH"
$env:PYTHONPATH         = "$sitePackages;$s2sSrc"
$env:PYTHONNOUSERSITE   = "1"
$env:PYTHONIOENCODING   = "utf-8"
# 本机服务间通信不应被系统 HTTP 代理转发。
$localBypass = @($env:NO_PROXY -split ',' | Where-Object { $_ }) + @('127.0.0.1', 'localhost', '::1')
$env:NO_PROXY = ($localBypass | Select-Object -Unique) -join ','
# 模型全部在包里，别让 transformers / hf 在启动时去联网找更新
$env:HF_HUB_OFFLINE     = "1"
$env:TRANSFORMERS_OFFLINE = "1"
$env:AI_GIRLFRIEND_PYTHON = $runtimePython

# ── 首次安装引导 ──────────────────────────────────────────
if (-not (Test-Path -LiteralPath $marker)) {
    Write-Host "还没有安装数字人引擎——这次直接以纯语音模式启动。" -ForegroundColor Yellow
    Write-Host "想要口型画面时，再双击「首次安装.cmd」即可。"
    $SkipDigitalHuman = $true
}

# ── 数字人在不在 ──────────────────────────────────────────
if (-not $SkipDigitalHuman) {
    $installed = @()
    try {
        $installed = @(& wsl.exe -l -q 2>$null | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
    } catch {
        $installed = @()
    }
    if ($installed -notcontains $DistroName) {
        Write-Host ("没有找到数字人引擎（WSL 发行版 {0}）—— 这次只用语音对话。" -f $DistroName) -ForegroundColor Yellow
        Write-Host "想要口型画面的话，跑一次「首次安装.cmd」。"
        $SkipDigitalHuman = $true
    }
}

# ── 交给真正的启动脚本 ────────────────────────────────────
$args = @{}
if ($NoBrowser)        { $args["NoBrowser"] = $true }
if ($SkipDigitalHuman) { $args["SkipDigitalHuman"] = $true }
$args["WslDistro"] = $DistroName

& $startScript @args
exit $LASTEXITCODE
