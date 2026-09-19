[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [int]$MaxWorkers = 4
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

$liveActRepository = "Soul-AILab/LiveAct"
$liveActRevision = "1bcdef1d8941edf2a9a6e1d21f5657f0a69fcd37"
$wav2VecRepository = "TencentGameMate/chinese-wav2vec2-base"
$wav2VecRevision = "3991242c806928916fff4a8c0e4f76acf661b743"

$modelsRoot = Join-Path (Split-Path -Parent $ProjectRoot) "AI-Girlfriend-Models\liveact"
$checkpointRoot = Join-Path $modelsRoot "checkpoints"
$wav2VecRoot = Join-Path $modelsRoot "chinese-wav2vec2-base"
$manifestPath = Join-Path $modelsRoot "manifest.json"

New-Item -ItemType Directory -Path $modelsRoot -Force | Out-Null

$localHf = Join-Path $ProjectRoot ".venv\Scripts\hf.exe"
if (Test-Path -LiteralPath $localHf) {
    $hf = $localHf
} else {
    $hfCommand = Get-Command "hf.exe" -ErrorAction SilentlyContinue
    if ($null -eq $hfCommand) {
        throw "未找到 Hugging Face CLI。请先在项目虚拟环境中安装 huggingface_hub。"
    }
    $hf = $hfCommand.Source
}

Write-Host "正在下载 SoulX-LiveAct 官方 BF16 权重（约 51 GiB）……"
& $hf download $liveActRepository `
    --revision $liveActRevision `
    --local-dir $checkpointRoot `
    --max-workers $MaxWorkers `
    --exclude "assets/*" ".ipynb_checkpoints/*" ".DS_Store"
if ($LASTEXITCODE -ne 0) {
    throw "SoulX-LiveAct 权重下载失败，重新运行本脚本会断点续传。"
}

Write-Host "正在下载中文 Wav2Vec2 权重（约 1.4 GiB）……"
& $hf download $wav2VecRepository `
    --revision $wav2VecRevision `
    --local-dir $wav2VecRoot `
    --max-workers $MaxWorkers
if ($LASTEXITCODE -ne 0) {
    throw "中文 Wav2Vec2 权重下载失败，重新运行本脚本会断点续传。"
}

$manifest = [ordered]@{
    downloaded_at = (Get-Date).ToString("o")
    precision = "bf16"
    quantized_checkpoint = $false
    liveact = [ordered]@{
        repository = $liveActRepository
        revision = $liveActRevision
        path = "liveact/checkpoints"
    }
    wav2vec = [ordered]@{
        repository = $wav2VecRepository
        revision = $wav2VecRevision
        path = "liveact/chinese-wav2vec2-base"
    }
    note = "官方未提供独立量化权重。FP8 KV Cache 是运行时缓存压缩；FP4 GEMM 仅适用于 Blackwell/B 系列 GPU。"
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Host "LiveAct 模型下载完成：$modelsRoot" -ForegroundColor Green
