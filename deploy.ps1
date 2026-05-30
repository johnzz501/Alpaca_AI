param(
    [string]$Remote = "origin"
)

$ErrorActionPreference = "Stop"
$CommitPattern = '^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-zA-Z0-9._-]+\))?!?: .{1,100}$'

function Fail($Message) {
    Write-Error $Message
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git is not installed."
}

git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Not inside a git repository. Run: git init"
}

git remote get-url $Remote *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Remote '$Remote' is not configured. Run: git remote add origin <git@github.com:USER/REPO.git>"
}

$Status = git status --short
if (-not $Status) {
    Write-Host "No changes to commit."
    exit 0
}

Write-Host "Detected changes:"
$Status | ForEach-Object { Write-Host $_ }

$CurrentBranch = git branch --show-current
if (-not $CurrentBranch) {
    Fail "Detached HEAD. Checkout a branch before deploying."
}

Write-Host ""
Write-Host "Conventional Commit examples:"
Write-Host "  feat(scanner): add intraday filter"
Write-Host "  fix(trading): handle missing order id"
Write-Host "  docs: update setup guide"
Write-Host ""

$CommitMsg = Read-Host "Commit message"
if ($CommitMsg -notmatch $CommitPattern) {
    Fail "Invalid commit message. Use: type(optional-scope): summary"
}

git add -A
git diff --cached --quiet --exit-code
if ($LASTEXITCODE -eq 0) {
    Write-Host "No staged changes after git add."
    exit 0
}

git commit -m $CommitMsg
if ($LASTEXITCODE -ne 0) {
    Fail "git commit failed."
}

$CreatedCommit = git rev-parse HEAD

try {
    git push -u $Remote $CurrentBranch
    if ($LASTEXITCODE -ne 0) {
        throw "git push failed."
    }
    Write-Host "Pushed $CreatedCommit to $Remote/$CurrentBranch"
}
catch {
    Write-Host "Push failed. Rolling back local commit $CreatedCommit ..."
    git reset --soft HEAD~1
    Write-Host "Rollback complete. Your changes are still staged locally."
    exit 1
}
