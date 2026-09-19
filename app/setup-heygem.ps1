[CmdletBinding()]
param(
    [string]$SourceRoot = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) {
    throw "没有找到 Docker。请先启动 Docker Desktop。"
}

docker version --format "{{.Server.Version}}" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop 尚未运行。"
}

$installedImages = @(docker image ls --format "{{.Repository}}:{{.Tag}}")
if ($installedImages -contains "heygem:latest") {
    Write-Host "HeyGem Docker 镜像已经安装。"
    exit 0
}

if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $bundleRoot = Split-Path $PSScriptRoot -Parent
    $bundledArchive = Join-Path $bundleRoot "resources\heygem\heygem.tar"
    if (Test-Path -LiteralPath $bundledArchive) {
        $imageArchive = $bundledArchive
    } elseif (-not [string]::IsNullOrWhiteSpace($env:HEYGEM_SOURCE_ROOT)) {
        $imageArchive = Join-Path $env:HEYGEM_SOURCE_ROOT "heygem.tar"
    } else {
        throw "找不到 HeyGem 镜像。请使用 -SourceRoot 或 HEYGEM_SOURCE_ROOT 指定资源目录。"
    }
} else {
    $imageArchive = Join-Path $SourceRoot "heygem.tar"
}

if (-not (Test-Path -LiteralPath $imageArchive)) {
    throw "找不到 HeyGem 镜像包：$imageArchive"
}

Write-Host "正在导入 HeyGem 镜像（约 13GB，首次需要几分钟）……"
docker load --input $imageArchive
if ($LASTEXITCODE -ne 0) {
    throw "HeyGem 镜像导入失败。"
}

$installedImages = @(docker image ls --format "{{.Repository}}:{{.Tag}}")
if ($installedImages -notcontains "heygem:latest") {
    throw "镜像已导入，但没有找到 heygem:latest。"
}

Write-Host "HeyGem 镜像导入完成。"
