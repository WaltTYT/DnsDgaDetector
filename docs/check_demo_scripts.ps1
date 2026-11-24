param(
    [switch]$Run,
    [int]$TimeoutSec = 300
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = 'D:\study\miniconda\envs\dns-dga\python.exe'
$fail = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) {
        Write-Host "[PASS] $name"
    } else {
        Write-Host "[FAIL] $name $detail"
        $script:fail++
    }
}

Write-Host "=== Static checks ==="

$batMain = Join-Path $PSScriptRoot 'run_demo.bat'
$batRaw = Join-Path $PSScriptRoot 'run_demo_raw.bat'
$show = Join-Path $PSScriptRoot 'show_results.py'
$runner = Join-Path $root 'run_all.py'

Check 'run_demo.bat exists' (Test-Path $batMain)
Check 'run_demo_raw.bat exists' (Test-Path $batRaw)
Check 'show_results.py exists' (Test-Path $show)
Check 'run_all.py exists' (Test-Path $runner)
Check 'python exists' (Test-Path $python) $python

foreach ($rel in 'config.json', 'data\dns_log.csv', 'results\features.csv', 'results\baseline.json', 'results\llm_cache.jsonl', 'results\run_manifest.json', 'prompts\dga_judge_prompt.md') {
    Check "input exists: $rel" (Test-Path (Join-Path $root $rel))
}

$helpText = & $python $runner --help 2>&1 | Out-String
Check 'CLI supports --from features' ($helpText -match '--from' -and $helpText -match 'features')
Check 'CLI supports --offline' ($helpText -match '--offline')

$mainText = Get-Content $batMain -Raw
$rawText = Get-Content $batRaw -Raw
Check 'run_demo.bat uses offline pipeline' ($mainText -match 'run_all\.py --from features --offline')
Check 'run_demo_raw.bat uses offline pipeline' ($rawText -match 'run_all\.py --from features --offline')
Check 'bat files do not use --online' (-not ($mainText -match '--online') -and -not ($rawText -match '--online'))
Check 'run_demo.bat sets UTF-8' ($mainText -match 'chcp 65001')
Check 'run_demo.bat calls show_results.py' ($mainText -match 'show_results\.py')

& $python $show 2>&1 | Out-Null
Check 'show_results.py runs' ($LASTEXITCODE -eq 0)

if (-not $Run) {
    Write-Host ""
    Write-Host "Static checks done. Use -Run to execute both bat files."
    if ($fail -gt 0) { exit 1 } else { exit 0 }
}

Write-Host ""
Write-Host "=== Dynamic checks ==="

$sw = [Diagnostics.Stopwatch]::StartNew()
& cmd.exe /c 'docs\run_demo.bat --no-pause'
$codeMain = $LASTEXITCODE
$sw.Stop()
Check 'run_demo.bat exit code 0' ($codeMain -eq 0) "code=$codeMain"
Check 'run_demo.bat finished within timeout' ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) "seconds=$([math]::Round($sw.Elapsed.TotalSeconds,1))"
$logPath = Join-Path $root 'results\run_demo.log'
Check 'run_demo.log produced' ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 0))

$sw2 = [Diagnostics.Stopwatch]::StartNew()
& cmd.exe /c 'echo.| docs\run_demo_raw.bat'
$codeRaw = $LASTEXITCODE
$sw2.Stop()
Check 'run_demo_raw.bat exit code 0' ($codeRaw -eq 0) "code=$codeRaw"
Check 'run_demo_raw.bat finished within timeout' ($sw2.Elapsed.TotalSeconds -lt $TimeoutSec) "seconds=$([math]::Round($sw2.Elapsed.TotalSeconds,1))"

$m = Get-Content (Join-Path $root 'results\metrics.json') -Raw | ConvertFrom-Json
Check 'rule test precision 47.50%' ([math]::Abs($m.rule.test.precision - 0.4750) -lt 0.0001) "got=$($m.rule.test.precision)"
Check 'rule test recall 13.87%' ([math]::Abs($m.rule.test.recall - 0.1387) -lt 0.0001) "got=$($m.rule.test.recall)"
Check 'rule test FP=21' ($m.rule.test.fp -eq 21) "got=$($m.rule.test.fp)"
Check 'hybrid test precision 54.29%' ([math]::Abs($m.hybrid.test.precision - 0.5429) -lt 0.0001) "got=$($m.hybrid.test.precision)"
Check 'hybrid test recall 13.87%' ([math]::Abs($m.hybrid.test.recall - 0.1387) -lt 0.0001) "got=$($m.hybrid.test.recall)"
Check 'hybrid test FP=16' ($m.hybrid.test.fp -eq 16) "got=$($m.hybrid.test.fp)"
Check 'subset assertions passed' ($m.subset_assertions.val -eq 'passed' -and $m.subset_assertions.test -eq 'passed')

$manifest = Get-Content (Join-Path $root 'results\run_manifest.json') -Raw | ConvertFrom-Json
Check 'manifest mode offline' ($manifest.llm.mode -eq 'offline') "got=$($manifest.llm.mode)"
Check 'manifest cache hits 87/87' ($manifest.llm.cache_hits -eq 87 -and $manifest.llm.requested_unique -eq 87) "got=$($manifest.llm.cache_hits)/$($manifest.llm.requested_unique)"

Write-Host ""
if ($fail -eq 0) {
    Write-Host "ALL CHECKS PASSED"
    exit 0
} else {
    Write-Host "$fail CHECK(S) FAILED"
    exit 1
}
