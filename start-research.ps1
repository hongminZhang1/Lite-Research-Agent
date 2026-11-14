param([switch]$NoBrowser, [switch]$CloudOnly)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:NO_PROXY = 'localhost,127.0.0.1'
$runtime = Join-Path $PSScriptRoot '.runtime'
$model = Join-Path $runtime 'models\qwen2.5-3b-instruct-q4_k_m.gguf'
$modelServer = Join-Path $runtime 'llama\llama-server.exe'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

function Read-LocalJson($url) {
    try {
        $result = & curl.exe --noproxy '*' --silent --fail --max-time 2 $url
        if ($LASTEXITCODE -eq 0) { return ($result | ConvertFrom-Json) }
    } catch {}
}

function Wait-LocalService($url, $process, $description) {
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (Read-LocalJson $url) { return }
        if ($process.HasExited) { break }
        Start-Sleep -Seconds 1
    }
    throw "$description failed to start. Check the logs in $runtime."
}

$modelUrl = 'http://127.0.0.1:8081/v1/models'
if (-not $CloudOnly -and -not (Read-LocalJson $modelUrl)) {
    if (-not (Test-Path -LiteralPath $model) -or -not (Test-Path -LiteralPath $modelServer)) {
        throw 'The local model or llama.cpp is missing from .runtime.'
    }
    $modelProcess = Start-Process -FilePath $modelServer -ArgumentList @(
        '--model', ('"' + $model + '"'), '--alias', 'qwen2.5-3b-instruct',
        '--host', '127.0.0.1', '--port', '8081', '--ctx-size', '8192', '--parallel', '1', '--n-gpu-layers', '0', '--no-webui'
    ) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'model-out.log') -RedirectStandardError (Join-Path $runtime 'model-error.log')
    Wait-LocalService $modelUrl $modelProcess 'Local Qwen model'
}

$webUrl = 'http://127.0.0.1:8000'
if ($CloudOnly) { $env:RESEARCH_CLOUD_ONLY = '1' } else { Remove-Item Env:RESEARCH_CLOUD_ONLY -ErrorAction SilentlyContinue }
if (-not (Read-LocalJson "$webUrl/api/health")) {
    $webProcess = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'web.server:app', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'web-out.log') -RedirectStandardError (Join-Path $runtime 'web-error.log')
    Wait-LocalService "$webUrl/api/health" $webProcess 'Research web server'
}

Write-Output $webUrl
if (-not $NoBrowser) { Start-Process $webUrl }
