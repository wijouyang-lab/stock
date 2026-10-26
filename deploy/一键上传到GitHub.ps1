# =============================================================================
#  一键把 cn-stock-screener 传到你的 GitHub（Windows PowerShell）
#
#  它会做：
#    1. 验证 Token、查出你的用户名
#    2. 建仓库（已存在就复用）
#    3. 把 50 个代码文件逐个传上去（走 API，不需要装 git）
#    4. 打开 Pages 托管（分支 main + /docs 目录）
#    5. 在浏览器里打开"添加 Secret"页面，你粘贴一次 Token
#    6. 触发第一次运行
#
#  你需要准备：
#    - 一个 GitHub 经典 Token（classic），勾选 repo + workflow
#      创建地址：https://github.com/settings/tokens/new
#    - 解压好的 cn-stock-screener 文件夹（就在本脚本旁边）
#
#  不需要：git、Python、命令行知识。Token 只在本机内存里用，不会发给我。
# =============================================================================
[CmdletBinding()]
param(
    [string]$RepoName = "stock",
    [string]$SourceDir = "",
    [string]$Token = "",
    [switch]$SkipRun
)

$ErrorActionPreference = "Stop"
$ApiBase = "https://api.github.com"
$Utf8 = [System.Text.Encoding]::UTF8

function Write-Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "  √ $text" -ForegroundColor Green }
function Write-Warn2($text){ Write-Host "  ! $text" -ForegroundColor Yellow }
function Write-Err($text)  { Write-Host "  × $text" -ForegroundColor Red }

function Invoke-GitHub {
    param(
        [string]$Method,
        [string]$Path,
        $Body = $null
    )
    $headers = @{
        Authorization          = "Bearer $Token"
        Accept                 = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
        "User-Agent"           = "cn-stock-screener-setup"
    }
    $params = @{
        Uri         = "$ApiBase$Path"
        Method      = $Method
        Headers     = $headers
        ErrorAction = "Stop"
    }
    if ($null -ne $Body) {
        $params["Body"]        = ($Body | ConvertTo-Json -Depth 10 -Compress)
        $params["ContentType"] = "application/json; charset=utf-8"
    }
    try {
        return Invoke-RestMethod @params
    } catch [Microsoft.PowerShell.Commands.HttpResponseException] {
        $resp = $_.Exception.Response
        $code = [int]$resp.StatusCode
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $detail = $reader.ReadToEnd()
        throw "HTTP $code  $($_.Exception.Message)`n$detail"
    }
}

Write-Host "============================================================" -ForegroundColor White
Write-Host "  cn-stock-screener  →  GitHub 一键上传" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor White

# ---------------------------------------------------------------- 0. 定位源码
if (-not $SourceDir) {
    $candidates = @(
        (Join-Path $PSScriptRoot "..\cn-stock-screener"),
        (Join-Path $PSScriptRoot "cn-stock-screener"),
        $PSScriptRoot
    )
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c "main.py")) { $SourceDir = $c; break }
    }
}
if (-not $SourceDir -or -not (Test-Path (Join-Path $SourceDir "main.py"))) {
    Write-Err "找不到源码目录（需要有 main.py）"
    Write-Host "  请把本脚本放在 cn-stock-screener 文件夹的 deploy 目录里再运行。" -ForegroundColor Yellow
    Read-Host "按回车退出"
    exit 1
}
$SourceDir = (Resolve-Path $SourceDir).Path
Write-Ok "源码目录：$SourceDir"

# ---------------------------------------------------------------- 1. Token
if (-not $Token) {
    Write-Host ""
    Write-Host "请粘贴你的 GitHub Token（输入时不显示，粘贴后按回车）：" -ForegroundColor Yellow
    Write-Host "  没有的话先去 https://github.com/settings/tokens/new 建一个" -ForegroundColor DarkGray
    Write-Host "  勾选：repo + workflow，有效期随意" -ForegroundColor DarkGray
    $secure = Read-Host -AsSecureString "Token"
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
if (-not $Token -or $Token.Length -lt 20) {
    Write-Err "Token 看起来不对（长度不足）"
    Read-Host "按回车退出"; exit 1
}

Write-Step 1 "验证 Token"
try {
    $me = Invoke-GitHub GET "/user"
} catch {
    Write-Err "Token 无效：$_"
    Read-Host "按回车退出"; exit 1
}
$owner = $me.login
Write-Ok "已登录：$owner"

# ---------------------------------------------------------------- 2. 建仓库
Write-Step 2 "创建（或复用）仓库 $RepoName"
$repoPath = "/repos/$owner/$RepoName"
$created = $false
try {
    $repo = Invoke-GitHub GET $repoPath
    Write-Ok "仓库已存在，直接复用：$($repo.full_name)"
} catch {
    if ($_ -match "HTTP 404") {
        $repo = Invoke-GitHub POST "/user/repos" @{
            name        = $RepoName
            private     = $false
            auto_init   = $false
            description = "A-share technical screener (auto-uploaded)"
        }
        $created = $true
        Write-Ok "已创建：$($repo.full_name)（公开仓库，Pages 免费）"
    } else {
        Write-Err "创建失败：$_"; Read-Host "按回车退出"; exit 1
    }
}

# ---------------------------------------------------------------- 3. 上传文件
Write-Step 3 "上传代码文件"
$skipDirs = @("__pycache__", "data\cache", "output", "pack_exe", ".git")
$files = Get-ChildItem -Path $SourceDir -Recurse -File | Where-Object {
    $rel = $_.FullName.Substring($SourceDir.Length).TrimStart('\')
    $bad = $false
    foreach ($d in $skipDirs) { if ($rel -like "$d*") { $bad = $true } }
    if ($_.Name -like "*.pyc" -or $_.Name -like "*.zip" -or $_.Name -like "*.exe") { $bad = $true }
    -not $bad
}
Write-Ok "待上传 $($files.Count) 个文件"

# 先放一个 docs/.nojekyll，保证 /docs 目录存在（Pages 要用）
$nojekyll = @{
    message = "init docs"
    content = [Convert]::ToBase64String($Utf8.GetBytes(""))
}
$i = 0
$fail = 0
foreach ($f in $files) {
    $i++
    $rel = $f.FullName.Substring($SourceDir.Length).TrimStart('\') -replace '\\', '/'
    $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($f.FullName))
    $body = @{ message = "upload $rel"; content = $b64 }
    try {
        Invoke-GitHub PUT "$repoPath/contents/$rel" $body | Out-Null
        if ($i % 10 -eq 0 -or $i -eq $files.Count) { Write-Host "    $i / $($files.Count)" -ForegroundColor DarkGray }
    } catch {
        # 文件已存在时会 422，需要先取 sha 再更新
        if ($_ -match "HTTP 422|HTTP 409") {
            try {
                $existing = Invoke-GitHub GET "$repoPath/contents/$rel"
                $body["sha"] = $existing.sha
                Invoke-GitHub PUT "$repoPath/contents/$rel" $body | Out-Null
            } catch {
                $fail++; Write-Warn2 "跳过 $rel"
            }
        } else {
            $fail++; $msg = ($_.Exception.Message -split "`n")[0]; Write-Warn2 "失败 $rel ：$msg"
        }
    }
}
try {
    $njRel = "docs/.nojekyll"
    Invoke-GitHub PUT "$repoPath/contents/$njRel" @{
        message = "keep docs dir"; content = [Convert]::ToBase64String($Utf8.GetBytes(""))
    } | Out-Null
} catch { }
Write-Ok "上传完成（失败 $fail 个）"

# ---------------------------------------------------------------- 4. Pages
Write-Step 4 "开启 Pages 托管"
try {
    Invoke-GitHub POST "$repoPath/pages" @{ source = @{ branch = "main"; path = "/docs" } } | Out-Null
    Write-Ok "Pages 已开启"
} catch {
    if ($_ -match "HTTP 409") { Write-Ok "Pages 之前就开着了" }
    else { $msg = ($_.Exception.Message -split "`n")[0]; Write-Warn2 "Pages 开启失败：$msg" }
}

# ---------------------------------------------------------------- 5. Secret
Write-Step 5 "添加 TUSHARE_TOKEN"
$secretUrl = "https://github.com/$owner/$RepoName/settings/secrets/actions/new"
Write-Host "  浏览器将打开这个页面，请填：" -ForegroundColor Yellow
Write-Host "     Name   : TUSHARE_TOKEN" -ForegroundColor White
Write-Host "     Secret : 你的 Tushare Token（40 位那串）" -ForegroundColor White
Write-Host "  填完点 Add secret，然后**回到这个窗口按回车**。" -ForegroundColor Yellow
Start-Process $secretUrl
Read-Host "填好 Secret 后按回车继续"

# ---------------------------------------------------------------- 6. 触发
if (-not $SkipRun) {
    Write-Step 6 "触发第一次运行"
    try {
        Invoke-GitHub POST "$repoPath/actions/workflows/stock.yml/dispatches" @{ ref = "main" } | Out-Null
        Write-Ok "已触发（第一次要拉 750 天历史，约 5~10 分钟）"
    } catch {
        $msg = ($_.Exception.Message -split "`n")[0]; Write-Warn2 "触发失败：$msg"
    }
}

# ---------------------------------------------------------------- 完成
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " 完成" -ForegroundColor Green
Write-Host "   仓库   : https://github.com/$owner/$RepoName"
Write-Host "   Actions: https://github.com/$owner/$RepoName/actions"
Write-Host "   网页   : https://$owner.github.io/$RepoName/"
Write-Host ""
Write-Host " 下一步：" -ForegroundColor Yellow
Write-Host "   1. 等 Actions 出现绿勾（首次 5~10 分钟）"
Write-Host "   2. 绿勾后等 1~2 分钟，用手机打开上面那个网址"
Write-Host "   3. 想让它准点跑，看 deploy\新手步骤.md 第 7 步"
Write-Host "============================================================" -ForegroundColor Green
Read-Host "按回车退出"
