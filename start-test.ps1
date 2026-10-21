param(
    [switch]$Discover,
    [switch]$SetupOnly,
    [switch]$Check,
    [long]$GrantChat = 0,
    [long]$GrantUser = 0,
    [long]$ResolveJob = 0
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location -LiteralPath $PSScriptRoot
$taskData = Join-Path $PSScriptRoot '.alpha'
New-Item -ItemType Directory -Path $taskData -Force | Out-Null
$taskSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $taskData /inheritance:r /grant:r ('*{0}:(OI)(CI)F' -f $taskSid) '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot restrict local secret directory permissions.' }
$taskPython = Join-Path $PSScriptRoot '.venv-alpha\Scripts\python.exe'
$taskVenvCreated = $false
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskVenvCreated = $true
    $taskBundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $taskBundledPython) {
        & $taskBundledPython -m venv .venv-alpha
    } else {
        & py -3.12 -m venv .venv-alpha
    }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required. See alpha/README.md.' }
}
& $taskPython -c "import sys; assert sys.version_info[:2] == (3, 12), 'Python 3.12 is required'"
if ($LASTEXITCODE -ne 0) { throw 'The alpha venv must use Python 3.12.' }
$taskLockHash = (Get-FileHash -LiteralPath alpha/requirements.lock -Algorithm SHA256).Hash
$taskStamp = Join-Path $taskData 'requirements.sha256'
if ($taskVenvCreated -or -not (Test-Path -LiteralPath $taskStamp) -or (Get-Content -LiteralPath $taskStamp -Raw).Trim() -ne $taskLockHash) {
    & $taskPython -m pip install -r alpha/requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; no bot was started.' }
    Set-Content -LiteralPath $taskStamp -Value $taskLockHash -Encoding ascii
}
$taskPreviousPythonPath = $env:PYTHONPATH
$taskPreviousToken = $env:TELEGRAM_BOT_TOKEN
$taskPreviousScopes = $env:ALPHA_CHAT_ADMINS
$taskPreviousData = $env:ALPHA_DATA_DIR
try {
    $env:PYTHONPATH = Join-Path $PSScriptRoot 'alpha\src'
    $env:ALPHA_DATA_DIR = $taskData
    if (-not $Discover) {
        & $taskPython alpha/setup_db.py --secrets-only
        if ($LASTEXITCODE -ne 0) { throw 'Local secret setup failed.' }
        & $taskPython alpha/docker_ready.py
        if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop (Linux containers), then retry. No data reset is needed.' }
        & docker compose -f alpha/compose.yaml up -d --wait --wait-timeout 60
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL did not become healthy; the bot remains stopped.' }
        & $taskPython alpha/setup_db.py
        if ($LASTEXITCODE -ne 0) { throw 'Database bootstrap failed; preserve .alpha and the Docker volume.' }
        if ($SetupOnly) { return }
        if ($Check) {
            & $taskPython alpha/check.py
            if ($LASTEXITCODE -ne 0) { throw 'Alpha checks failed.' }
            return
        }
        if (-not $env:ALPHA_CHAT_ADMINS) {
            $taskScopeFile = Join-Path $taskData 'chat-admins'
            if (-not (Test-Path -LiteralPath $taskScopeFile)) {
                $taskScope = Read-Host 'Test chat ID and product admin ID(s), e.g. -1001234567890:12345678 (use -Discover to get IDs)'
                Set-Content -LiteralPath $taskScopeFile -Value $taskScope.Trim() -Encoding ascii
            }
            $env:ALPHA_CHAT_ADMINS = (Get-Content -LiteralPath $taskScopeFile -Raw).Trim()
        }
    }
    if (-not $env:TELEGRAM_BOT_TOKEN) {
        $taskTokenSecure = Read-Host 'BotFather token (hidden; never saved)' -AsSecureString
        $env:TELEGRAM_BOT_TOKEN = [Net.NetworkCredential]::new('', $taskTokenSecure).Password
    }
    $taskRunArgs = @('-m', 'rolebot')
    if ($Discover) { $taskRunArgs += '--discover' }
    if ($GrantChat -ne 0 -or $GrantUser -ne 0) { $taskRunArgs += @('--grant', [string]$GrantChat, [string]$GrantUser) }
    if ($ResolveJob -gt 0) { $taskRunArgs += @('--resolve', [string]$ResolveJob) }
    & $taskPython @taskRunArgs
    if ($LASTEXITCODE -ne 0) { throw 'Alpha stopped. See the safe diagnostic above and alpha/README.md.' }
} finally {
    $env:PYTHONPATH = $taskPreviousPythonPath
    $env:TELEGRAM_BOT_TOKEN = $taskPreviousToken
    $env:ALPHA_CHAT_ADMINS = $taskPreviousScopes
    $env:ALPHA_DATA_DIR = $taskPreviousData
}
