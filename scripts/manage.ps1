param(
    [ValidateSet('start', 'stop', 'install', 'test', 'check', 'prepare', 'voice', 'virtual-mic', 'legacy')]
    [string]$Action = 'start',
    [switch]$NoBrowser,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ToolArgs
)

$ErrorActionPreference = 'Stop'
$taskProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $taskProjectRoot

function Ensure-ProjectJunction {
    param([string]$Link, [string]$Target)
    $taskLinkPath = [System.IO.Path]::GetFullPath((Join-Path $taskProjectRoot $Link))
    $taskTargetPath = [System.IO.Path]::GetFullPath((Join-Path $taskProjectRoot $Target))
    $taskPrefix = $taskProjectRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $taskLinkPath.StartsWith($taskPrefix) -or -not $taskTargetPath.StartsWith($taskPrefix)) {
        throw 'Directory mapping must remain inside this project.'
    }
    if (-not (Test-Path -LiteralPath $taskTargetPath)) {
        New-Item -ItemType Directory -Path $taskTargetPath -Force | Out-Null
    }
    $taskExisting = Get-Item -LiteralPath $taskLinkPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $taskExisting) {
        if ($taskExisting.LinkType -ne 'Junction') {
            throw "Expected an automatically generated junction: $Link"
        }
        $taskExistingTarget = [System.IO.Path]::GetFullPath([string]$taskExisting.Target[0])
        if ($taskExistingTarget.TrimEnd('\') -ne $taskTargetPath.TrimEnd('\')) {
            throw "Existing junction has an unexpected target: $Link"
        }
        return
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $taskLinkPath) -Force | Out-Null
    New-Item -ItemType Junction -Path $taskLinkPath -Target $taskTargetPath | Out-Null
}

$taskMappings = @(
    @('backend\formal_app\web', 'frontend\console'),
    @('backend\formal_app\config', 'config'),
    @('backend\formal_app\runtime', 'data\runtime'),
    @('backend\virtual_mic_test\web', 'frontend\virtual-mic'),
    @('backend\virtual_mic_test\config', 'config\virtual-mic'),
    @('backend\virtual_mic_test\outputs', 'data\virtual-mic'),
    @('backend\voice_clone\config', 'config\voice-clone'),
    @('backend\voice_clone\recordings', 'data\voice-clone\recordings'),
    @('backend\voice_clone\outputs', 'data\voice-clone\outputs'),
    @('backend\src\ai_interpreter\web\static', 'frontend\legacy'),
    @('backend\config', 'config\legacy')
)
foreach ($taskMapping in $taskMappings) {
    Ensure-ProjectJunction -Link $taskMapping[0] -Target $taskMapping[1]
}

$taskConfigTemplates = @(
    @('config\settings.example.yaml', 'config\settings.yaml'),
    @('config\settings.local.example.yaml', 'config\settings.local.yaml'),
    @('config\legacy\settings.example.yaml', 'config\legacy\settings.yaml'),
    @('config\legacy\settings.local.example.yaml', 'config\legacy\settings.local.yaml'),
    @('config\legacy\terminology\zh_to_en.example.json', 'config\legacy\terminology\zh_to_en.json'),
    @('config\legacy\terminology\en_to_zh.example.json', 'config\legacy\terminology\en_to_zh.json'),
    @('config\virtual-mic\settings.example.yaml', 'config\virtual-mic\settings.yaml'),
    @('config\voice-clone\voice_clone.example.yaml', 'config\voice-clone\voice_clone.local.yaml')
)
foreach ($taskTemplate in $taskConfigTemplates) {
    if (-not (Test-Path -LiteralPath $taskTemplate[1]) -and (Test-Path -LiteralPath $taskTemplate[0])) {
        Copy-Item -LiteralPath $taskTemplate[0] -Destination $taskTemplate[1]
    }
}

$taskSearchPaths = @(
    (Join-Path $taskProjectRoot 'backend\src'),
    (Join-Path $taskProjectRoot 'backend'),
    (Join-Path $taskProjectRoot 'backend\voice_clone\src')
)
if ($env:PYTHONPATH) { $taskSearchPaths += $env:PYTHONPATH }
$env:PYTHONPATH = $taskSearchPaths -join [System.IO.Path]::PathSeparator
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

$taskVenvPython = Join-Path $taskProjectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $taskVenvPython) {
    $taskPython = $taskVenvPython
} else {
    $taskPython = (Get-Command python -ErrorAction Stop).Source
}

switch ($Action) {
    'prepare' {
        Write-Output 'Project directories are ready.'
        exit 0
    }
    'install' {
        if (-not (Test-Path -LiteralPath $taskVenvPython)) {
            & $taskPython -m venv (Join-Path $taskProjectRoot '.venv')
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        & $taskVenvPython -m pip install -e '.[dev,voice]'
    }
    'start' {
        $taskArguments = @('-B', '-m', 'formal_app.app', 'start')
        if ($NoBrowser) { $taskArguments += '--no-browser' }
        & $taskPython @taskArguments
    }
    'stop' {
        & $taskPython -B -c "from formal_app.app import DEFAULT_CONFIG,DEFAULT_SECRET,_healthy,stop; from formal_app.config import ConfigStore; w=ConfigStore(DEFAULT_CONFIG,DEFAULT_SECRET).raw()['web']; h='127.0.0.1' if w['host']=='localhost' else w['host']; raise SystemExit(stop() if _healthy(h,int(w['port'])) else 0)"
    }
    'check' {
        & $taskPython -B -c "from formal_app.app import DEFAULT_CONFIG,DEFAULT_SECRET; from formal_app.config import ConfigStore; from formal_app.server import STATIC; s=ConfigStore(DEFAULT_CONFIG,DEFAULT_SECRET); s.validate(for_start=False); p=s.public(); print({'configuration_valid':True,'frontend_available':(STATIC/'index.html').is_file(),'api_key_configured':p['aliyun']['api_key_configured']})"
    }
    'test' {
        & $taskPython -B -m pytest -p no:cacheprovider @ToolArgs
    }
    'voice' {
        & $taskPython -B -m bailian_voice_clone.cli --config 'config\voice-clone\voice_clone.local.yaml' @ToolArgs
    }
    'virtual-mic' {
        $taskArguments = @('-B', '-m', 'virtual_mic_test.app', '--config', 'config\virtual-mic\settings.local.yaml')
        if (-not (Test-Path -LiteralPath 'config\virtual-mic\settings.local.yaml')) {
            $taskArguments[-1] = 'config\virtual-mic\settings.yaml'
        }
        if ($NoBrowser) { $taskArguments += '--no-browser' }
        & $taskPython @taskArguments @ToolArgs
    }
    'legacy' {
        $taskArguments = @('-B', '-m', 'ai_interpreter.app', '--config', 'config\legacy\settings.local.yaml')
        if ($NoBrowser) { $taskArguments += '--no-browser' }
        & $taskPython @taskArguments @ToolArgs
    }
}
exit $LASTEXITCODE
