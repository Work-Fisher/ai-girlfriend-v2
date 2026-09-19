[CmdletBinding()]
param(
    [int]$WaitSeconds = 180,
    [ValidateRange(1, 65535)]
    [int]$Port = 8383
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = $PSScriptRoot
$dataRoot = Join-Path $root "heygem-data"
$outputRoot = Join-Path $root "output"
$containerName = "cyber-girlfriend-heygem"

if (-not (Test-Path -LiteralPath $dataRoot)) {
    New-Item -ItemType Directory -Path $dataRoot | Out-Null
}
foreach ($directory in @("input", "temp", "result", "log")) {
    $path = Join-Path $dataRoot $directory
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
    }
}
foreach ($directory in @(
    $outputRoot,
    (Join-Path $outputRoot "audio"),
    (Join-Path $outputRoot "video")
)) {
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory | Out-Null
    }
}

$installedImages = @(docker image ls --format "{{.Repository}}:{{.Tag}}")
if ($installedImages -notcontains "heygem:latest") {
    & (Join-Path $root "setup-heygem.ps1")
}

$allContainers = @(docker container ls --all --format "{{.Names}}")
$existing = if ($allContainers -contains $containerName) {
    $containerName
} else {
    $null
}
if ($existing -eq $containerName) {
    $running = docker inspect --format "{{.State.Running}}" $containerName
    $portBindingsJson = docker inspect --format "{{json .HostConfig.PortBindings}}" $containerName
    $portBindings = $portBindingsJson | ConvertFrom-Json
    $portBinding = $portBindings.PSObject.Properties["8383/tcp"].Value
    $publishedPort = if ($null -ne $portBinding) { [int]$portBinding[0].HostPort } else { 0 }
    $mounts = docker inspect --format "{{json .Mounts}}" $containerName | ConvertFrom-Json
    $expectedDataRoot = (Resolve-Path -LiteralPath $dataRoot).Path.TrimEnd("\")
    $expectedOutputRoot = (Resolve-Path -LiteralPath $outputRoot).Path.TrimEnd("\")
    $hasCurrentDataMount = $null -ne ($mounts | Where-Object {
        $_.Destination -eq "/code/data" -and
        ([string]$_.Source).TrimEnd("\").Equals(
            $expectedDataRoot,
            [StringComparison]::OrdinalIgnoreCase
        )
    })
    $hasCurrentOutputMount = $null -ne ($mounts | Where-Object {
        $_.Destination -eq "/code/output" -and
        ([string]$_.Source).TrimEnd("\").Equals(
            $expectedOutputRoot,
            [StringComparison]::OrdinalIgnoreCase
        )
    })
    if (
        $publishedPort -ne $Port -or
        -not $hasCurrentDataMount -or
        -not $hasCurrentOutputMount
    ) {
        Write-Host "HeyGem 端口或输出目录挂载已变化，正在重建专用容器……"
        if ($running -eq "true") {
            docker stop --time 10 $containerName | Out-Null
        }
        docker rm $containerName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "无法重建 HeyGem 专用容器。"
        }
        $existing = $null
    }
}

if ($existing -eq $containerName) {
    $running = docker inspect --format "{{.State.Running}}" $containerName
    if ($running -ne "true") {
        docker start $containerName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "HeyGem 容器启动失败。"
        }
    }
} else {
    $dataMount = "$((Resolve-Path -LiteralPath $dataRoot).Path):/code/data"
    $outputMount = "$((Resolve-Path -LiteralPath $outputRoot).Path):/code/output"
    docker run `
        --detach `
        --name $containerName `
        --gpus all `
        --publish "127.0.0.1:$($Port):8383" `
        --volume $dataMount `
        --volume $outputMount `
        "heygem:latest" `
        bash -c "python /code/app_run.py" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "HeyGem 容器创建失败。"
    }
}

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    try {
        Invoke-WebRequest `
            -Uri "http://127.0.0.1:$Port/easy/query?code=_health" `
            -UseBasicParsing `
            -TimeoutSec 2 | Out-Null
        Write-Host "HeyGem 口型服务已就绪：http://127.0.0.1:$Port"
        exit 0
    } catch {
        $running = docker inspect --format "{{.State.Running}}" $containerName 2>$null
        if ($running -ne "true") {
            throw "HeyGem 容器已退出。请执行 docker logs $containerName 查看原因。"
        }
        Start-Sleep -Seconds 2
    }
}

throw "等待 HeyGem 服务启动超时。请执行 docker logs $containerName 查看日志。"
