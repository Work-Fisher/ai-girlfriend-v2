[CmdletBinding()]
param(
    [ValidateSet("xiaoman", "linlin")]
    [string]$Character = "xiaoman",
    [switch]$SkipHeyGem,
    [switch]$SkipLiveAct,
    [switch]$NoBrowser,
    [switch]$ResetServices,
    [switch]$AutoPort,
    [ValidateRange(1, 65535)]
    [int]$UiPort = 7860,
    [ValidateRange(1, 65535)]
    [int]$LlmPort = 8080,
    [ValidateRange(1, 65535)]
    [int]$HeyGemPort = 8383,
    [ValidateRange(1, 65535)]
    [int]$LiveActPort = 8390,
    [ValidateRange(1, 65535)]
    [int]$RealtimePort = 8766
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

function Write-StartupAuthorCredit {
    Write-Host ""
    Write-Host "  AI GIRLFRIEND" -ForegroundColor DarkYellow
    Write-Host ""
}

if ($env:AI_GIRLFRIEND_AUTHOR_SHOWN -ne "1") {
    Write-StartupAuthorCredit
    $env:AI_GIRLFRIEND_AUTHOR_SHOWN = "1"
}

$root = $PSScriptRoot
$modelsRoot = Join-Path (Split-Path -Parent $root) "AI-Girlfriend-Models"
$defaultPython = Join-Path $root ".venv\Scripts\python.exe"
$bundledPython = [string]$env:AI_GIRLFRIEND_PYTHON
$python = if (
    -not [string]::IsNullOrWhiteSpace($bundledPython) -and
    (Test-Path -LiteralPath $bundledPython -PathType Leaf)
) {
    $bundledPython
} else {
    $defaultPython
}
$server = Join-Path $root "llama.cpp\llama-server.exe"
$model = Join-Path $modelsRoot "llm\qwen3.5-9b-GGUF\Qwen_Qwen3.5-9B-Q5_K_M.gguf"
$vadModel = Join-Path $modelsRoot "vad\silero-vad"
$sttModel = Join-Path $modelsRoot "stt\whisper-large-v3-turbo"
$ttsModel = Join-Path $modelsRoot "tts\qwen3-tts-customvoice"
$llmProviderFile = Join-Path $root "heygem-data\llm-provider.json"
$avatarStateFile = Join-Path $root "heygem-data\state.json"
$liveactCloudConfigFile = Join-Path $root "config\liveact-cloud.json"
# 本地 Qwen 那一档已经去掉了：模型统一走外部厂商，经 DSH bridge 转发（记忆在它那儿）。
# 这个变量还被下面的必需文件检查和显存管理用到，所以固定成 external，
# llama.cpp 和那个 6.6GB 的 gguf 都不会再被要求存在。
$llmProviderMode = "external"
$avatarDriverMode = "video"
if (Test-Path -LiteralPath $avatarStateFile) {
    try {
        $savedAvatarState = Get-Content -LiteralPath $avatarStateFile -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if ([string]$savedAvatarState.avatar_driver -eq "image") {
            $avatarDriverMode = "image"
        }
    } catch {
        Write-Warning "上次的口型方案无法读取，本次按视频驱动启动。"
    }
}
$pipelineModule = "speech_to_speech.s2s_pipeline"
$heygemWarmup = Join-Path $root "scripts\warmup_heygem.py"
$pipelineConfig = if ($Character -eq "xiaoman") {
    Join-Path $root "config\pipeline-realtime.json"
} else {
    Join-Path $root "config\pipeline-realtime-linlin.json"
}
$logDir = Join-Path $root "logs"
$llmProcess = $null
$pipelineProcess = $null
$uiProcess = $null
$llmStartedHere = $false
$pipelineStartedHere = $false
$heygemEnabled = -not $SkipHeyGem -and $avatarDriverMode -eq "video"
$liveactCloudEnabled = $false
$liveactCloudUrl = "http://127.0.0.1:$LiveActPort"
if (Test-Path -LiteralPath $liveactCloudConfigFile) {
    try {
        $liveactCloudConfig = Get-Content -LiteralPath $liveactCloudConfigFile -Raw -Encoding UTF8 |
            ConvertFrom-Json
        $configuredUrl = [string]$liveactCloudConfig.url
        $parsedUrl = [uri]$configuredUrl
        if (
            -not $parsedUrl.IsAbsoluteUri -or
            $parsedUrl.Scheme -notin @("http", "https")
        ) {
            throw "云端图片驱动地址必须是 http 或 https URL。"
        }
        $liveactCloudEnabled = [bool]$liveactCloudConfig.enabled
        $liveactCloudUrl = $configuredUrl.TrimEnd("/")
    } catch {
        Write-Warning "云端图片驱动配置无效，本次不启用：$($_.Exception.Message)"
        $liveactCloudEnabled = $false
    }
}
$startLocalLiveAct = $false
$liveactEnabled = $liveactCloudEnabled -and $avatarDriverMode -eq "image"

function Test-LlmHealth {
    param([int]$Port)

    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        return $health.status -eq "ok"
    } catch {
        return $false
    }
}

function Test-TcpPort {
    param([int]$Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $connection.Wait(350)) {
            return $false
        }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-PortBindable {
    param([int]$Port)

    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        $Port
    )
    try {
        $listener.Start()
        return $true
    } catch [System.Net.Sockets.SocketException] {
        return $false
    } finally {
        $listener.Stop()
    }
}

function Find-FreePort {
    param(
        [int]$StartPort,
        [System.Collections.Generic.HashSet[int]]$Reserved
    )

    for ($candidate = $StartPort; $candidate -le 65535; $candidate++) {
        if (-not $Reserved.Contains($candidate) -and (Test-PortBindable $candidate)) {
            [void]$Reserved.Add($candidate)
            return $candidate
        }
    }
    throw "从端口 $StartPort 开始没有找到可用端口。"
}

function Stop-OwnedProcessTree {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F 2>$null | Out-Null
}

function Test-TextContains {
    param(
        [string]$Text,
        [string]$Value
    )

    return -not [string]::IsNullOrWhiteSpace($Text) -and
        $Text.IndexOf($Value, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Get-ExistingLaunchers {
    $scriptPath = Join-Path $root "start-ui.ps1"
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        if (
            $process.ProcessId -ne $PID -and
            $process.Name -in @("powershell.exe", "pwsh.exe") -and
            (Test-TextContains -Text ([string]$process.CommandLine) -Value $scriptPath)
        ) {
            [PSCustomObject]@{
                ProcessId = [int]$process.ProcessId
                ParentProcessId = [int]$process.ParentProcessId
                Name = [string]$process.Name
            }
        }
    }
}

function Get-ExistingServices {
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $executable = [string]$process.ExecutablePath
        $commandLine = [string]$process.CommandLine
        $role = $null

        if ($executable -ieq $server) {
            $role = "Qwen3"
        } elseif (
            $process.Name -ieq "python.exe" -and
            $executable -ieq $python -and
            (Test-TextContains -Text $commandLine -Value $pipelineModule)
        ) {
            $role = "Realtime"
        } elseif (
            $process.Name -ieq "python.exe" -and
            (Test-TextContains -Text $commandLine -Value "ui.server:app") -and
            (
                $executable -ieq $python -or
                (Test-TextContains -Text $commandLine -Value $root)
            )
        ) {
            $role = "UI"
        }

        if ($null -ne $role) {
            [PSCustomObject]@{
                ProcessId = [int]$process.ProcessId
                Role = $role
            }
        }
    }
}

function Stop-ProcessTreeById {
    param([int]$ProcessId)

    if ($ProcessId -eq $PID -or $null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        return
    }
    & "$env:SystemRoot\System32\taskkill.exe" /PID $ProcessId /T /F 2>$null | Out-Null
}

function Test-HeyGemContainerRunning {
    if ($null -eq (Get-Command "docker.exe" -ErrorAction SilentlyContinue)) {
        return $false
    }
    try {
        $runningContainers = @(
            docker container ls --format "{{.Names}}" 2>$null
        )
        return (
            $LASTEXITCODE -eq 0 -and
            $runningContainers -contains "cyber-girlfriend-heygem"
        )
    } catch {
        return $false
    }
}

function Test-LiveActContainerRunning {
    if ($null -eq (Get-Command "docker.exe" -ErrorAction SilentlyContinue)) {
        return $false
    }
    try {
        $runningContainers = @(
            docker container ls --format "{{.Names}}" 2>$null
        )
        return (
            $LASTEXITCODE -eq 0 -and
            $runningContainers -contains "cyber-girlfriend-liveact"
        )
    } catch {
        return $false
    }
}

function Stop-ExistingServices {
    $found = $false
    $oneClickPath = Join-Path $root "一键启动.cmd"
    $launchers = @(Get-ExistingLaunchers)

    if ($launchers.Count -gt 0) {
        $found = $true
        Write-Host "检测到上一次启动的进程，正在关闭……"
        foreach ($launcher in $launchers) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($launcher.ParentProcessId)" -ErrorAction SilentlyContinue
            if (
                $null -ne $parent -and
                $parent.Name -ieq "cmd.exe" -and
                (
                    (Test-TextContains -Text ([string]$parent.CommandLine) -Value $oneClickPath) -or
                    (Test-TextContains -Text ([string]$parent.CommandLine) -Value ([IO.Path]::GetFileName($oneClickPath)))
                )
            ) {
                Stop-ProcessTreeById -ProcessId ([int]$parent.ProcessId)
            } else {
                Stop-ProcessTreeById -ProcessId $launcher.ProcessId
            }
        }
        Start-Sleep -Milliseconds 500
    }

    $services = @(Get-ExistingServices)
    if ($services.Count -gt 0) {
        $found = $true
        $serviceList = ($services | ForEach-Object { "$($_.Role)#$($_.ProcessId)" }) -join "、"
        Write-Host "正在终止旧服务：$serviceList"
        foreach ($service in $services) {
            Stop-ProcessTreeById -ProcessId $service.ProcessId
        }
    }

    if (Test-HeyGemContainerRunning) {
        $found = $true
        Write-Host "正在停止旧 HeyGem GPU 容器……"
        docker stop --time 10 "cyber-girlfriend-heygem" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "无法停止旧 HeyGem 容器。"
        }
    }
    if (Test-LiveActContainerRunning) {
        $found = $true
        Write-Host "正在停止旧图片驱动 GPU 容器……"
        docker stop --time 10 "cyber-girlfriend-liveact" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "无法停止旧图片驱动容器。"
        }
    }
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $remainingLaunchers = @(Get-ExistingLaunchers)
        $remainingServices = @(Get-ExistingServices)
        $heygemRunning = Test-HeyGemContainerRunning
        $liveactRunning = Test-LiveActContainerRunning
        if (
            $remainingLaunchers.Count -eq 0 -and
            $remainingServices.Count -eq 0 -and
            -not $heygemRunning -and
            -not $liveactRunning
        ) {
            if ($found) {
                Write-Host "旧服务已全部关闭。"
            }
            return
        }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)

    throw "旧服务未能在十五秒内完全退出，请稍后重试。"
}

$requiredFiles = @(
    $python,
    $pipelineConfig,
    $heygemWarmup,
    $vadModel,
    $sttModel,
    (Join-Path $root "ui\server.py")
)
# Qwen3-TTS 的模型目录只有真用它时才需要存在。我们换成了 OmniVoice（跑在旁挂
# 进程里，模型在 AI-Girlfriend-Models	ts\omnivoice），那 4.2GB 的 qwen3-tts
# 就删掉了；这里跟着按实际引擎判断，否则启动会卡在"缺少界面运行文件"。
# 判断依据取自 pipeline 配置里的 tts 字段，和管线实际分发用的是同一个值。
$ttsEngine = "qwen3"
try {
    $ttsEngine = [string](([System.IO.File]::ReadAllText($pipelineConfig, [System.Text.Encoding]::UTF8) | ConvertFrom-Json).tts)
} catch {
    Write-Host "  读不出 pipeline 配置里的 tts，按 qwen3 处理" -ForegroundColor Yellow
}
if ($ttsEngine -eq "qwen3") {
    $requiredFiles += $ttsModel
}
if ($llmProviderMode -eq "local") {
    $requiredFiles += @($server, $model)
}
if ($startLocalLiveAct) {
    $requiredFiles += (Join-Path $root "start-liveact.ps1")
}
foreach ($required in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "缺少界面运行文件：$required"
    }
}
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

if ($ResetServices) {
    Stop-ExistingServices
}

if ($AutoPort) {
    $reservedPorts = [System.Collections.Generic.HashSet[int]]::new()
    $UiPort = Find-FreePort -StartPort $UiPort -Reserved $reservedPorts
    $LlmPort = Find-FreePort -StartPort $LlmPort -Reserved $reservedPorts
    $HeyGemPort = Find-FreePort -StartPort $HeyGemPort -Reserved $reservedPorts
    $LiveActPort = Find-FreePort -StartPort $LiveActPort -Reserved $reservedPorts
    $RealtimePort = Find-FreePort -StartPort $RealtimePort -Reserved $reservedPorts
} else {
    $distinctPorts = @($UiPort, $LlmPort, $HeyGemPort, $LiveActPort, $RealtimePort) | Sort-Object -Unique
    if ($distinctPorts.Count -ne 5) {
        throw "UI、LLM、HeyGem 和 Realtime 端口必须互不相同。"
    }
}

if (Test-TcpPort $UiPort) {
    throw "端口 $UiPort 已被占用。可以添加 -AutoPort 自动顺延。"
}

$runtimeConfigDir = Join-Path $root "heygem-data\runtime"
if (-not (Test-Path -LiteralPath $runtimeConfigDir)) {
    New-Item -ItemType Directory -Path $runtimeConfigDir -Force | Out-Null
}
$runtimePipelineConfig = Join-Path $runtimeConfigDir "pipeline-$Character.json"
$charactersConfig = Join-Path $root "config\characters.json"
$characters = [System.IO.File]::ReadAllText(
    $charactersConfig,
    [System.Text.Encoding]::UTF8
) | ConvertFrom-Json
$characterSettings = $characters.$Character
if ($null -eq $characterSettings -or [string]::IsNullOrWhiteSpace([string]$characterSettings.system_prompt)) {
    throw "角色 $Character 没有可用的 System Prompt。请在 config\characters.json 中配置。"
}
$pipelineSource = [System.IO.File]::ReadAllText(
    $pipelineConfig,
    [System.Text.Encoding]::UTF8
)
$pipelineSettings = $pipelineSource | ConvertFrom-Json
$pipelineSettings | Add-Member -NotePropertyName "init_chat_prompt" `
    -NotePropertyValue ([string]$characterSettings.system_prompt) -Force
$pipelineSettings.vad_model_repo = $vadModel
$pipelineSettings.stt_model_name = $sttModel
$pipelineSettings.qwen3_tts_model_name = $ttsModel
$pipelineSettings.ws_port = $RealtimePort
$pipelineSettings.responses_api_api_key = "local-gateway"
$pipelineSettings.responses_api_base_url = "http://127.0.0.1:$UiPort/api/llm/v1"
$pipelineJson = $pipelineSettings | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText(
    $runtimePipelineConfig,
    $pipelineJson,
    [System.Text.UTF8Encoding]::new($false)
)

$env:CYBER_CHARACTER = $Character
$env:CYBER_HEYGEM_ENABLED = if ($heygemEnabled) { "1" } else { "0" }
$env:CYBER_LLM_HEALTH_URL = "http://127.0.0.1:$LlmPort/health"
$env:CYBER_LLM_API_BASE_URL = "http://127.0.0.1:$LlmPort/v1"
$env:CYBER_LLM_SERVER = $server
$env:CYBER_LLM_MODEL = $model
$env:CYBER_LLM_PORT = [string]$LlmPort
$env:CYBER_LLM_PID = ""
$env:CYBER_REALTIME_URL = "ws://127.0.0.1:$RealtimePort/v1/realtime"
$env:CYBER_REALTIME_PORT = [string]$RealtimePort
$env:CYBER_HEYGEM_URL = "http://127.0.0.1:$HeyGemPort"
$env:CYBER_HEYGEM_PORT = [string]$HeyGemPort
$env:CYBER_LIVEACT_ENABLED = if ($liveactEnabled) { "1" } else { "0" }
$env:CYBER_LIVEACT_URL = $liveactCloudUrl
$env:CYBER_LIVEACT_PORT = [string]$LiveActPort

# 数字人引擎如果已经在监听（我们把它跑在 WSL 里，接口和 Docker 版一致），
# 就不要再走 Docker 那条路：setup-heygem 会因为找不到 Docker 抛错，
# 然后把 heygemEnabled 关掉，明明能用的口型功能就没了。
$heygemExternal = $false
if ($heygemEnabled) {
    try {
        $probe = New-Object System.Net.Sockets.TcpClient
        $probe.Connect("127.0.0.1", $HeyGemPort)
        $probe.Close()
        $heygemExternal = $true
        Write-Host ("检测到 {0} 端口已有数字人引擎，跳过 Docker 启动流程。" -f $HeyGemPort) -ForegroundColor Green
    } catch {
        $heygemExternal = $false
    }
}

Push-Location $root
try {
    if ($heygemEnabled -and -not $heygemExternal) {
        try {
            & (Join-Path $root "setup-heygem.ps1")
            if (Test-HeyGemContainerRunning) {
                Write-Host "正在重启专用 HeyGem 容器……"
                docker stop --time 10 "cyber-girlfriend-heygem" | Out-Null
            }
        } catch {
            $heygemEnabled = $false
            $env:CYBER_HEYGEM_ENABLED = "0"
            Write-Warning "HeyGem 准备失败，本次只能使用声音模式：$($_.Exception.Message)"
        }
    }

    if ($llmProviderMode -eq "local") {
        if ($heygemEnabled -and (Test-LlmHealth -Port $LlmPort)) {
            throw "检测到端口 $LlmPort 上已有 Qwen3。口型模式需要由 start-ui.ps1 统一管理模型显存，请先关闭已有 LLM。"
        }

        if (-not (Test-LlmHealth -Port $LlmPort)) {
            $llmGpuLayers = "all"
            if ($startLocalLiveAct) {
                $llmGpuLayers = "0"
            }
            if ($heygemEnabled) {
                Write-Host "正在以 GPU 模式启动 Qwen3，视频驱动与 Qwen3 共用 GPU……"
            } else {
                Write-Host "正在以 GPU 模式启动 Qwen3……"
            }
            $llmProcess = Start-Process `
                -FilePath $server `
                -ArgumentList @(
                    "-m", $model,
                    "--host", "127.0.0.1",
                    "--port", [string]$LlmPort,
                    "-ngl", $llmGpuLayers,
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
                -RedirectStandardOutput (Join-Path $logDir "ui-llama.stdout.log") `
                -RedirectStandardError (Join-Path $logDir "ui-llama.stderr.log") `
                -WindowStyle Hidden `
                -PassThru
            $llmStartedHere = $true
            $env:CYBER_LLM_PID = [string]$llmProcess.Id

            $deadline = (Get-Date).AddSeconds(60)
            while (-not (Test-LlmHealth -Port $LlmPort)) {
                if ($llmProcess.HasExited) {
                    throw "Qwen3 启动失败，请查看 logs\ui-llama.stderr.log。"
                }
                if ((Get-Date) -ge $deadline) {
                    throw "等待 Qwen3 启动超时。"
                }
                Start-Sleep -Milliseconds 500
            }
        }
    } else {
        $staleLocalLlm = @(
            Get-ExistingServices |
                Where-Object { $_.Role -eq "Qwen3" }
        )
        foreach ($service in $staleLocalLlm) {
            Stop-ProcessTreeById -ProcessId $service.ProcessId
        }
        Write-Host "上次选择三方 API，本次不加载本地 Qwen。"
    }

    $uiProcess = Start-Process `
        -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "ui.server:app", "--host", "127.0.0.1", "--port", [string]$UiPort) `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $logDir "ui-server.stdout.log") `
        -RedirectStandardError (Join-Path $logDir "ui-server.stderr.log") `
        -WindowStyle Hidden `
        -PassThru

    $deadline = (Get-Date).AddSeconds(300)
    while (-not (Test-TcpPort $UiPort)) {
        if ($uiProcess.HasExited) {
            throw "UI 服务启动失败，请查看 logs\ui-server.stderr.log。"
        }
        if ((Get-Date) -ge $deadline) {
            throw "等待 UI 服务启动超时（300 秒），请查看 logs\ui-server.stderr.log。"
        }
        Start-Sleep -Milliseconds 300
    }

    if (-not (Test-TcpPort $RealtimePort)) {
        Write-Host "正在加载 Whisper 与 Qwen3-TTS……"
        $pipelineProcess = Start-Process `
            -FilePath $python `
            -ArgumentList @("-m", $pipelineModule, $runtimePipelineConfig) `
            -WorkingDirectory $root `
            -RedirectStandardOutput (Join-Path $logDir "ui-pipeline.stdout.log") `
            -RedirectStandardError (Join-Path $logDir "ui-pipeline.stderr.log") `
            -WindowStyle Hidden `
            -PassThru
        $pipelineStartedHere = $true

        $deadline = (Get-Date).AddSeconds(300)
        while (-not (Test-TcpPort $RealtimePort)) {
            if ($pipelineProcess.HasExited) {
                throw "声音管线启动失败，请查看 logs\ui-pipeline.stderr.log。"
            }
            if ((Get-Date) -ge $deadline) {
                throw "等待声音管线启动超时。"
            }
            Start-Sleep -Milliseconds 500
        }
    }

    if ($heygemEnabled -and -not $heygemExternal) {
        Write-Host "正在启动常驻 HeyGem GPU 服务……"
        & (Join-Path $root "start-heygem.ps1") -Port $HeyGemPort
        Write-Host "正在预热 HeyGem，完成后第一轮对话无需冷启动……"
        & $python $heygemWarmup
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "HeyGem 预热失败，服务仍会继续运行，但第一轮口型可能较慢。"
        }
    }
    if ($startLocalLiveAct) {
        try {
            Write-Host "正在启动常驻图片驱动服务……"
            & (Join-Path $root "start-liveact.ps1") -Port $LiveActPort
        } catch {
            Write-Warning "图片驱动准备失败：$($_.Exception.Message)"
        }
    }

    Write-Host ""
    Write-Host "口型方案：$(if ($avatarDriverMode -eq 'image') { '图片驱动' } else { '视频驱动' })"
    Write-Host "端口：UI=$UiPort  LLM=$LlmPort  视频驱动=$HeyGemPort  Realtime=$RealtimePort"
    if ($liveactEnabled) {
        Write-Host "图片驱动：云端服务 $liveactCloudUrl"
    }
    Write-Host "界面已启动：http://127.0.0.1:$UiPort"
    Write-Host "关闭此 PowerShell 窗口或按 Ctrl+C 可停止本次启动的服务。"
    if (-not $NoBrowser) {
        $browserUrl = "http://127.0.0.1:$UiPort/?startup=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())"
        Start-Process $browserUrl
    }
    Wait-Process -Id $uiProcess.Id
} finally {
    Stop-OwnedProcessTree $uiProcess
    if ($pipelineStartedHere) {
        Stop-OwnedProcessTree $pipelineProcess
    }
    if ($llmStartedHere -and $null -ne $llmProcess -and -not $llmProcess.HasExited) {
        Stop-Process -Id $llmProcess.Id
    }
    if ($heygemEnabled) {
        try {
            if (Test-HeyGemContainerRunning) {
                docker stop --time 10 "cyber-girlfriend-heygem" | Out-Null
            }
        } catch {
            Write-Warning "HeyGem 容器清理失败：$($_.Exception.Message)"
        }
    }
    if ($startLocalLiveAct) {
        try {
            if (Test-LiveActContainerRunning) {
                docker stop --time 10 "cyber-girlfriend-liveact" | Out-Null
            }
        } catch {
            Write-Warning "图片驱动容器清理失败：$($_.Exception.Message)"
        }
    }
    Pop-Location
}
