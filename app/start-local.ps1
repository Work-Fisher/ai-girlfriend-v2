[CmdletBinding()]
param(
    [ValidateSet("xiaoman", "linlin")]
    [string]$Character = "xiaoman"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$root = $PSScriptRoot
$modelsRoot = Join-Path (Split-Path -Parent $root) "AI-Girlfriend-Models"
$server = Join-Path $root "llama.cpp\llama-server.exe"
$model = Join-Path $modelsRoot "llm\qwen3.5-9b-GGUF\Qwen_Qwen3.5-9B-Q5_K_M.gguf"
$pipelineModule = "speech_to_speech.s2s_pipeline"
$python = Join-Path $root ".venv\Scripts\python.exe"
$audioCheck = Join-Path $root "scripts\check_audio.py"
$textClient = Join-Path $root "scripts\text_chat.py"
$config = Join-Path $root "config\pipeline-$Character.json"
$realtimeConfig = if ($Character -eq "xiaoman") {
    Join-Path $root "config\pipeline-realtime.json"
} else {
    Join-Path $root "config\pipeline-realtime-linlin.json"
}
$logDir = Join-Path $root "logs"
$llmProcess = $null
$pipelineProcess = $null
$startedHere = $false

function Test-LlmHealth {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 2
        return $health.status -eq "ok"
    } catch {
        return $false
    }
}

function Test-RealtimePort {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync("127.0.0.1", 8766)
        if (-not $connection.Wait(300)) {
            return $false
        }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Stop-OwnedProcessTree {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }
    & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F 2>$null | Out-Null
}

foreach ($required in @($server, $model, $python, $audioCheck, $textClient, $config, $realtimeConfig)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "缺少部署文件：$required"
    }
}
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

& $python $audioCheck
$microphoneAvailable = $LASTEXITCODE -eq 0
if (-not $microphoneAvailable) {
    Write-Host "未检测到可用麦克风，将自动切换为键盘输入 + 语音回答。"
}

Push-Location $root
try {
    if (-not (Test-LlmHealth)) {
        $llmProcess = Start-Process `
            -FilePath $server `
            -ArgumentList @(
                "-m", $model,
                "--host", "127.0.0.1",
                "--port", "8080",
                "-ngl", "all",
                "-c", "8192",
                "-np", "1",
                "-fa", "on",
                "--temp", "0.7",
                "--top-p", "0.8",
                "--top-k", "20",
                "--repeat-penalty", "1.08",
                "--reasoning", "off",
                "--reasoning-format", "deepseek"
            ) `
            -WorkingDirectory $root `
            -RedirectStandardOutput (Join-Path $logDir "llama-server.stdout.log") `
            -RedirectStandardError (Join-Path $logDir "llama-server.stderr.log") `
            -WindowStyle Hidden `
            -PassThru
        $startedHere = $true

        $deadline = (Get-Date).AddSeconds(60)
        while (-not (Test-LlmHealth)) {
            if ($llmProcess.HasExited) {
                throw "LLM 启动失败，请查看 logs\llama-server.stderr.log。"
            }
            if ((Get-Date) -ge $deadline) {
                throw "等待 LLM 启动超时，请查看 logs\llama-server.stderr.log。"
            }
            Start-Sleep -Milliseconds 500
        }
    }

    Write-Host "角色：$Character"
    if ($microphoneAvailable) {
        Write-Host "麦克风模式。按 Ctrl+C 结束语音对话。首次加载语音模型需要几十秒。"
        & $python -m $pipelineModule $config
    } else {
        if (Test-RealtimePort) {
            throw "端口 8766 已被占用，请先关闭现有 Realtime 服务。"
        }

        Write-Host "正在加载键盘对话服务，首次启动需要几十秒……"
        $pipelineProcess = Start-Process `
            -FilePath $python `
            -ArgumentList @("-m", $pipelineModule, $realtimeConfig) `
            -WorkingDirectory $root `
            -RedirectStandardOutput (Join-Path $logDir "keyboard-pipeline.stdout.log") `
            -RedirectStandardError (Join-Path $logDir "keyboard-pipeline.stderr.log") `
            -WindowStyle Hidden `
            -PassThru

        $deadline = (Get-Date).AddSeconds(120)
        while (-not (Test-RealtimePort)) {
            if ($pipelineProcess.HasExited) {
                throw "键盘对话服务启动失败，请查看 logs\keyboard-pipeline.stderr.log。"
            }
            if ((Get-Date) -ge $deadline) {
                throw "等待键盘对话服务启动超时，请查看 logs\keyboard-pipeline.stderr.log。"
            }
            Start-Sleep -Milliseconds 500
        }

        & $python $textClient
    }
    exit $LASTEXITCODE
} finally {
    Stop-OwnedProcessTree $pipelineProcess
    if ($startedHere -and $null -ne $llmProcess -and -not $llmProcess.HasExited) {
        Stop-Process -Id $llmProcess.Id
    }
    Pop-Location
}
