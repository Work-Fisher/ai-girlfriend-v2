[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$root = $PSScriptRoot
$modelsRoot = Join-Path (Split-Path -Parent $root) "AI-Girlfriend-Models"
$server = Join-Path $root "llama.cpp\llama-server.exe"
$model = Join-Path $modelsRoot "llm\qwen3.5-9b-GGUF\Qwen_Qwen3.5-9B-Q5_K_M.gguf"

if (-not (Test-Path -LiteralPath $server)) {
    throw "llama-server.exe 不存在：$server"
}
if (-not (Test-Path -LiteralPath $model)) {
    throw "Qwen3 权重不存在：$model"
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 2
    if ($health.status -eq "ok") {
        Write-Host "LLM 已在 http://127.0.0.1:8080 运行。"
        return
    }
} catch {
    # 端口未监听时继续启动。
}

Push-Location $root
try {
    & $server `
        -m $model `
        --host 127.0.0.1 `
        --port 8080 `
        -ngl all `
        -c 8192 `
        -np 1 `
        -fa on `
        --temp 0.7 `
        --top-p 0.8 `
        --top-k 20 `
        --repeat-penalty 1.08 `
        --reasoning off `
        --reasoning-format deepseek
} finally {
    Pop-Location
}
