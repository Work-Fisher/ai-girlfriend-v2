#requires -Version 5.1
<#
.SYNOPSIS
  启动我们这套改装后的服务：OmniVoice 声音克隆 + DSH 记忆大脑 + WSL 数字人引擎，
  然后启动浏览器 UI。

.DESCRIPTION
  当前整合包的四个组成部分：

    语音识别   Whisper
    语音合成   OmniVoice（参考音频克隆）
    语言模型   外部 API，经 DSH 加入长期记忆
    数字人     WSL2 中的 Duix 发行版

  UI、前端和语音管线通过配置及端口对接：
    - llm-provider.json 指向 dsh-bridge
    - pipeline 配置里 tts 改成 omnivoice，走我们新加的 handler
    - 数字人本来就是 HTTP 调 127.0.0.1:8383，WSL 里那个引擎接口完全一致

  端口分配：
    7860  他的 UI（浏览器打开这个）
    8766  他的实时语音管线（WebSocket）
    8790  DSH bridge（OpenAI 兼容，我们加的）
    8791  OmniVoice 旁挂服务（我们加的）
    8383  数字人引擎（WSL 里）
#>
[CmdletBinding()]
param(
    [string]$Voice = "苏晚-单句",
    [double]$Speed = 0.85,
    [string]$WslDistro = "DuixDistro",
    [switch]$SkipDigitalHuman,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

$appRoot = $PSScriptRoot
$bundleRoot = Split-Path $appRoot -Parent
$python = Join-Path $bundleRoot "runtime\python311\python.exe"
$sitePackages = Join-Path $appRoot ".venv\Lib\site-packages"
$s2sSrc = Join-Path $appRoot "speech-to-speech\src"
$logDir = Join-Path $appRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# OmniVoice 要 transformers 5.x，而 Whisper 所在的主环境是 4.x。以前的办法是
# 另装一整套解释器——代价是第二份 4.2G 的 torch。
#
# 现在换成 overlay：只把真正冲突的那些**纯 Python** 包（transformers 5.x、
# huggingface_hub、omnivoice 本体等纯 Python 包）放一个目录，启动时塞进
# PYTHONPATH 最前面，torch / numpy / scipy 这些大件复用主 venv。省 5.4G，
# 而且两边 torch 本来就是同一个版本，没有折损。
#
# overlay 里不能放带 C 扩展的包：主 venv 是 Python 3.11，那些 .pyd 按解释器
# 版本编译，跨版本直接崩（之前从 3.14 那套复制过来就是这么炸的）。
$omniOverlay = Join-Path $appRoot ".venv-omni-overlay"

function Wait-Endpoint {
    param([string]$Url, [int]$TimeoutSeconds = 120, [string]$Label)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing | Out-Null
            Write-Host ("  {0} 就绪" -f $Label) -ForegroundColor Green
            return $true
        } catch {
            Start-Sleep -Milliseconds 800
        }
    }
    Write-Host ("  {0} 等待超时：{1}" -f $Label, $Url) -ForegroundColor Yellow
    return $false
}

function Test-Port {
    param([int]$Port)
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $Port); $c.Close(); return $true
    } catch { return $false }
}

Write-Host ""
Write-Host "=== 1/4  OmniVoice 声音克隆 (:8791) ===" -ForegroundColor Cyan
if (Test-Port 8791) {
    Write-Host "  已在运行，跳过"
} elseif (-not (Test-Path -LiteralPath $omniOverlay)) {
    Write-Host ("  找不到 OmniVoice 依赖层：{0}" -f $omniOverlay) -ForegroundColor Red
    Write-Host "  这个目录是整合包的一部分，缺了说明包不完整。" -ForegroundColor Red
} else {
    $env:PYTHONPATH = "$omniOverlay;$sitePackages"
    $modelDir  = Join-Path $bundleRoot "AI-Girlfriend-Models\tts\omnivoice"
    $voicesDir = Join-Path $appRoot "voices"
    Start-Process -FilePath $python `
        -ArgumentList @(
            (Join-Path $appRoot "omnivoice\server.py"),
            "--port", "8791",
            "--model-dir", $modelDir,
            "--voices-dir", $voicesDir,
            "--voice", $Voice,
            "--speed", [string]$Speed
        ) `
        -WorkingDirectory (Join-Path $appRoot "omnivoice") `
        -RedirectStandardOutput (Join-Path $logDir "omnivoice.stdout.log") `
        -RedirectStandardError  (Join-Path $logDir "omnivoice.stderr.log") `
        -WindowStyle Hidden | Out-Null
    Wait-Endpoint -Url "http://127.0.0.1:8791/health" -Label "OmniVoice" | Out-Null
}

Write-Host ""
Write-Host "=== 2/4  DSH 记忆大脑 (:8790) ===" -ForegroundColor Cyan
$dshConfig = Join-Path $appRoot "dsh-bridge\config.json"
if (-not (Test-Path -LiteralPath $dshConfig)) {
    Write-Host "  还没有 dsh-bridge\config.json —— 复制 config.example.json 并填上 api_key。" -ForegroundColor Yellow
}
if (Test-Port 8790) {
    Write-Host "  已在运行，跳过"
} else {
    $env:PYTHONPATH = "$sitePackages;$s2sSrc"
    Start-Process -FilePath $python `
        -ArgumentList @((Join-Path $appRoot "dsh-bridge\server.py"), "--port", "8790") `
        -WorkingDirectory (Join-Path $appRoot "dsh-bridge") `
        -RedirectStandardOutput (Join-Path $logDir "dsh-bridge.stdout.log") `
        -RedirectStandardError  (Join-Path $logDir "dsh-bridge.stderr.log") `
        -WindowStyle Hidden | Out-Null
    Wait-Endpoint -Url "http://127.0.0.1:8790/health" -Label "DSH bridge" | Out-Null
}

Write-Host ""
Write-Host "=== 3/4  数字人引擎 (:8383, WSL) ===" -ForegroundColor Cyan
if ($SkipDigitalHuman) {
    Write-Host "  按要求跳过"
} elseif (Test-Port 8383) {
    Write-Host "  已在运行，跳过"
} else {
    # unshare -m：在独立挂载命名空间里把整合包的 heygem-data 绑到 /code/data。
    # 原版靠 Docker 挂载做这件事，我们没有 Docker，用 bind mount 复刻同样的路径映射，
    # 他 server.py 里的 container_path() 生成的 /code/data/xxx 才能对得上。
    # 必须套一层 cmd /c：直接 Start-Process wsl.exe 并重定向输出时，那个进程会
    # 立刻退出，发行版跟着从 Running 掉回 Stopped，引擎根本没机会起来。
    # 套一层之后 cmd 作为父进程留着，wsl 会话才活得下去。
    $engineCmd = ('wsl.exe -d {0} -e bash -lc "unshare -m /code/start_for_v2.sh"' -f $WslDistro)
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList @("/c", $engineCmd) `
        -RedirectStandardOutput (Join-Path $logDir "duix.stdout.log") `
        -RedirectStandardError  (Join-Path $logDir "duix.stderr.log") `
        -WindowStyle Hidden | Out-Null
    $digitalHumanReady = Wait-Endpoint `
        -Url "http://127.0.0.1:8383/easy/query?code=chk" `
        -TimeoutSeconds 180 `
        -Label "数字人引擎"
    if (-not $digitalHumanReady) {
        $SkipDigitalHuman = $true
        Write-Host "  本次继续使用纯语音，不再尝试旧 Docker 数字人。" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "=== 4/4  浏览器 UI (:7860) ===" -ForegroundColor Cyan
# 让 s2s 管线知道去哪儿找 OmniVoice。handler 读的就是这几个环境变量。
$env:OMNIVOICE_URL = "http://127.0.0.1:8791"
# 故意留空：handler 一旦拿到 voice 就会每次请求都带上，把旁挂服务的当前音色
# 顶掉，于是界面里换音色不起作用（除非重启整条管线）。留空 = 由旁挂服务说了算，
# 换音色就是一次 HTTP 调用，下一句话就是新声音。
$env:OMNIVOICE_VOICE = ""
$env:OMNIVOICE_SPEED = [string]$Speed
$env:AI_GIRLFRIEND_PYTHON = $python
# 这两个本来是外层 launch.ps1 设的，我们直接调 start-ui.ps1 就得自己补上，
# 否则 UI 进程起不来（No module named uvicorn）。
$env:PYTHONPATH = "$sitePackages;$s2sSrc"

# 必须用哈希表 splat：数组 splat 是按位置传参，"-NoBrowser" 会被当成
# start-ui.ps1 的第一个位置参数（Character）塞进去，然后 ValidateSet 报错。
$uiArgs = @{}
if ($NoBrowser) { $uiArgs["NoBrowser"] = $true }
if ($SkipDigitalHuman) { $uiArgs["SkipHeyGem"] = $true }
$uiArgs["SkipLiveAct"] = $true      # 图片驱动我们用不到，省一份显存
& (Join-Path $appRoot "start-ui.ps1") @uiArgs
