#requires -Version 5.1
<#
.SYNOPSIS
  删除当前固定运行方案不会使用的第三方演示、评测和旧 TTS 文件。

.DESCRIPTION
  当前发行包固定使用 Whisper + OmniVoice + DSH + Duix。上游库仍保留多后端源码，
  但发行环境无需附带 Qwen3-TTS 的 Python 实现、OmniVoice 的 Gradio 演示与 WER
  评测依赖，也无需附带 Silero 仓库的示例和测试资料。

  不删除 PyTorch、Transformers、OmniVoice 主体、Whisper、E5、Silero 运行源码、
  DSH runtime 或 Duix rootfs。
#>
[CmdletBinding()]
param([switch]$Apply)

$ErrorActionPreference = "Stop"
$bundleRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$bundlePrefix = $bundleRoot.TrimEnd('\') + '\'

$patterns = @(
    "AI-Girlfriend-Models\llm",
    "AI-Girlfriend-Models\vad\silero-vad\.github",
    "AI-Girlfriend-Models\vad\silero-vad\datasets",
    "AI-Girlfriend-Models\vad\silero-vad\examples",
    "AI-Girlfriend-Models\vad\silero-vad\files",
    "AI-Girlfriend-Models\vad\silero-vad\tests",
    "AI-Girlfriend-Models\vad\silero-vad\tuning",
    "AI-Girlfriend-Models\vad\silero-vad\__pycache__",
    "AI-Girlfriend-Models\vad\silero-vad\silero-vad.ipynb",
    "app\.venv\Lib\site-packages\faster_qwen3_tts*",
    "app\.venv\Lib\site-packages\qwen_tts*",
    "app\.venv\Lib\site-packages\gradio*",
    "app\.venv-omni-overlay\qwen_tts*",
    "app\.venv-omni-overlay\funasr*",
    "app\.venv-omni-overlay\modelscope*",
    "app\.venv-omni-overlay\omnivoice\cli",
    "app\.venv-omni-overlay\omnivoice\eval"
)

$targets = foreach ($pattern in $patterns) {
    Get-Item -Path (Join-Path $bundleRoot $pattern) -Force -ErrorAction SilentlyContinue
}
$targets = @($targets | Sort-Object FullName -Unique)

$totalBytes = 0L
foreach ($target in $targets) {
    $fullPath = [IO.Path]::GetFullPath($target.FullName)
    if (-not $fullPath.StartsWith($bundlePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝处理整合包之外的路径：$fullPath"
    }
    $bytes = if ($target.PSIsContainer) {
        (Get-ChildItem -LiteralPath $fullPath -Recurse -File -Force -ErrorAction SilentlyContinue |
            Measure-Object Length -Sum).Sum
    } else {
        $target.Length
    }
    $totalBytes += [long]$bytes
    Write-Host ("  {0}  ({1:N1} MB)" -f $fullPath.Substring($bundlePrefix.Length), ($bytes / 1MB))
}

Write-Host ("合计可清理：{0:N1} MB" -f ($totalBytes / 1MB)) -ForegroundColor Cyan
if (-not $Apply) {
    Write-Host "这是预览；确认后加 -Apply 执行。"
    exit 0
}

foreach ($target in $targets) {
    Remove-Item -LiteralPath $target.FullName -Recurse -Force
}
Write-Host "发行包冗余已清理。" -ForegroundColor Green
