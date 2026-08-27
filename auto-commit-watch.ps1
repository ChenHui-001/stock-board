#Requires -Version 5.1
<#
.SYNOPSIS
  监听 backend/ 和 frontend/static/ 变更,自动跑 smoke_test,通过则 commit + push 到 main。

.PARAMETER PollSeconds
  轮询间隔秒数,默认 3 秒。

.PARAMETER DebounceSeconds
  改动稳定后等待秒数(避免边改边触发),默认 5 秒。

.EXAMPLE
  powershell -File auto-commit-watch.ps1
  powershell -File auto-commit-watch.ps1 -PollSeconds 3 -DebounceSeconds 5

.NOTES
  GitHub PAT 不得入库。本脚本按下列顺序查找:
    1) $env:STOCK_BOARD_GITHUB_TOKEN  环境变量
    2) <repo>/.credentials            gitignore 文件,内容: GITHUB_TOKEN=ghp_xxx
#>
[CmdletBinding()]
param(
    [int]$PollSeconds = 3,
    [int]$DebounceSeconds = 5
)

$ErrorActionPreference = "Continue"
$projectRoot = $PSScriptRoot
Set-Location $projectRoot

$watchPaths = @("backend", "frontend/static")
$exclExts = @(".pyc", ".tmp", ".swp", ".log", ".ps1")
$logFile = Join-Path $projectRoot "auto-commit.log"

function Get-GitHubToken {
    param([string]$RepoRoot)
    $envTok = $env:STOCK_BOARD_GITHUB_TOKEN
    if ($envTok) { return $envTok }
    $credFile = Join-Path $RepoRoot ".credentials"
    if (Test-Path $credFile) {
        $line = Get-Content $credFile -ErrorAction SilentlyContinue | Select-String -Pattern "^GITHUB_TOKEN="
        if ($line) {
            $tok = ($line -split "=", 2)[1].Trim()
            if ($tok) { return $tok }
        }
    }
    throw "未找到 GITHUB_TOKEN。请设置 `$env:STOCK_BOARD_GITHUB_TOKEN 或创建 .credentials 文件(已在 .gitignore 中)。"
}

$githubToken = Get-GitHubToken -RepoRoot $projectRoot
$remoteUrl = "https://x-access-token:$githubToken@github.com/ChenHui-001/stock-board.git"

function Write-Log {
    param([string]$Msg, [string]$Level = "INFO")
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts [$Level] $Msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line
}

function Invoke-SmokeTest {
    Write-Log "Running smoke_test..."
    $output = & python -m backend.smoke_test 2>&1
    if ($LASTEXITCODE -eq 0) {
        $tail = ($output | Select-Object -Last 2) -join " | "
        Write-Log "smoke_test OK: $tail"
        return $true
    } else {
        Write-Log "smoke_test FAILED (exit=$LASTEXITCODE)" "ERROR"
        $nl = [Environment]::NewLine
        Add-Content -Path $logFile -Value ($output -join $nl)
        return $false
    }
}

function Invoke-AutoCommit {
    $status = git status --porcelain backend frontend
    if (-not $status) {
        Write-Log "No staged changes, skip"
        return
    }
    $files = ($status | ForEach-Object { ($_ -split "\s+", 2)[1] }) -join ", "
    if ($files.Length -gt 80) { $files = $files.Substring(0, 77) + "..." }
    $msg = "auto-commit: smoke_test passed | changes: $files"

    git add backend frontend 2>&1 | Out-Null
    Write-Log "git commit..."
    git commit -m "$msg" 2>&1 | ForEach-Object { Write-Log "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "git commit failed, skip push" "ERROR"
        return
    }

    Write-Log "git push origin main..."
    $env:GIT_TERMINAL_PROMPT = "0"
    $pushOutput = git push $remoteUrl main 2>&1
    foreach ($line in $pushOutput) { Write-Log "  $line" }
    if ($LASTEXITCODE -eq 0) {
        Write-Log "push OK"
    } else {
        Write-Log "push failed (exit=$LASTEXITCODE)" "ERROR"
    }
}

Write-Log "========== auto-commit-watch start (polling mode) =========="
Write-Log "Project: $projectRoot"
Write-Log "Watch: $($watchPaths -join ', ')"
Write-Log "Poll: ${PollSeconds}s, Debounce: ${DebounceSeconds}s"

$lastFingerprint = $null
$lastChangeAt = Get-Date
$running = $true

while ($running) {
    Start-Sleep -Seconds $PollSeconds

    # 计算 git 状态指纹 (只看 backend/frontend 路径,排除临时文件)
    $fingerprint = git status --porcelain backend frontend 2>&1 | Where-Object {
        $_.Trim() -ne "" -and
        ($_ -notmatch "\.pyc$") -and
        ($_ -notmatch "__pycache__") -and
        ($_ -notmatch "\.tmp$")
    } | Out-String

    if ($fingerprint -ne $lastFingerprint) {
        if ($fingerprint.Trim() -ne "") {
            $lastChangeAt = Get-Date
            Write-Log "Detected changes, fingerprint updated"
        } else {
            Write-Log "Working tree clean"
        }
        $lastFingerprint = $fingerprint
    }

    $quietSec = ((Get-Date) - $lastChangeAt).TotalSeconds
    if ($fingerprint.Trim() -ne "" -and $quietSec -ge $DebounceSeconds) {
        Write-Log "Debounce passed (${quietSec}s), processing..."

        # 清空指纹,避免重复触发
        $savedFingerprint = $fingerprint
        $lastFingerprint = $null
        $lastChangeAt = Get-Date

        # 先确认 fingerprint 没变化
        $current = git status --porcelain backend frontend 2>&1 | Where-Object {
            $_.Trim() -ne "" -and
            ($_ -notmatch "\.pyc$") -and
            ($_ -notmatch "__pycache__") -and
            ($_ -notmatch "\.tmp$")
        } | Out-String
        if ($current.Trim() -eq "") {
            Write-Log "Fingerprint cleared by other process, skip"
            continue
        }

        if (Invoke-SmokeTest) {
            Invoke-AutoCommit
        } else {
            Write-Log "smoke_test failed, will NOT commit. Fix and re-edit any file to retry." "WARN"
            $lastFingerprint = $current  # 保持指纹,等修复后再次触发
        }
    }
}

Write-Log "========== auto-commit-watch exit =========="
