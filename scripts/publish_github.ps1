param(
    [string]$RepositoryUrl = "https://github.com/williamf48/orkestryn.git",
    [switch]$Push
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $repoRoot
if ($RepositoryUrl -notmatch '^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$') {
    throw "Use a normal GitHub HTTPS URL without credentials."
}
function Invoke-Git {
    & git @args
    if ($LASTEXITCODE -ne 0) { throw "Git failed; review the message above." }
}
if (-not (Test-Path .git)) { Invoke-Git init -b main }
$pythonCommand = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
if ($pythonCommand -eq "py") { & py -3 scripts/check_repository.py } else { & python scripts/check_repository.py }
if ($LASTEXITCODE -ne 0) { throw "Source check failed." }
Invoke-Git add --all
if ($pythonCommand -eq "py") { & py -3 scripts/check_repository.py --staged } else { & python scripts/check_repository.py --staged }
if ($LASTEXITCODE -ne 0) { throw "Staged source check failed." }
Invoke-Git diff --cached --stat
if (-not $Push) {
    Write-Host "Source is staged and checked. Review it in GitHub Desktop. No commit or push was made."
    Write-Host "Create the public remote repository first. Re-run with -Push when ready."
    exit 0
}
    $remotes = & git remote
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect repository remotes." }
    if ($remotes -contains "origin") {
        $existing = & git remote get-url origin
        if ($LASTEXITCODE -ne 0) { throw "Could not read origin." }
    if ($existing -ne $RepositoryUrl) { throw "Existing origin differs. Review it; this helper will not replace it." }
} else { Invoke-Git remote add origin $RepositoryUrl }
& git diff --cached --quiet
if ($LASTEXITCODE -eq 1) { Invoke-Git commit -m "Prepare Orkestryn main app and judge demo" }
elseif ($LASTEXITCODE -ne 0) { throw "Could not inspect staged changes." }
$branch = & git branch --show-current
if ($LASTEXITCODE -ne 0 -or -not $branch) { throw "Choose an ordinary local branch before pushing." }
Invoke-Git push -u origin $branch
