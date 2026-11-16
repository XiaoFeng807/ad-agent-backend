param(
    [Parameter(Mandatory=$true)][string]$Evidence,
    [string]$GuestAssets = 'artifacts/storepilot-g0-g1/host/vm',
    [string]$Python = '.\.venv-acceptance\Scripts\python.exe',
    [ValidateSet('full', 'lifecycle', 'rate', 'approval', 'mix-a', 'mix-b', 'g3', 'g4')][string]$Selection = 'full',
    [ValidateSet('reconnect', 'lost-start', 'kill-before', 'kill-after', 'version-change')][string]$G3Case,
    [ValidateSet('race', 'kill-before', 'kill-after', 'final-commit')][string]$G4Case
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$vm = (Resolve-Path $GuestAssets).Path
$pythonExe = (Resolve-Path $Python).Path
$out = (New-Item -ItemType Directory -Path $Evidence).FullName
$frozen = Join-Path $out 'frozen'
$guest = Join-Path $out 'guest.cpio.gz'

function Invoke-Fixture {
    & $pythonExe @args
    if ($LASTEXITCODE -ne 0) { throw "Fixture exited with $LASTEXITCODE. See $out" }
}

Invoke-Fixture deploy/storepilot-gateway/g2_guest.py "$vm/guest-g1.cpio.gz" $guest
Invoke-Fixture -m scripts.storepilot_g2 init $frozen
$envoyLog = Join-Path $out 'envoy.log'
$vmProcess = Start-Process "$vm/msys64/ucrt64/bin/qemu-system-x86_64.exe" -WindowStyle Hidden -PassThru `
    -RedirectStandardError (Join-Path $out 'qemu.stderr.log') -ArgumentList @(
    '-accel','tcg','-M','q35','-cpu','max','-m','1536','-smp','2','-display','none','-monitor','none',
    '-serial',"`"file:$envoyLog`"",'-kernel',"`"$vm/vmlinuz-virt`"",'-initrd',"`"$guest`"",
    '-append','"console=ttyS0 quiet loglevel=3 rdinit=/storepilot-g2-init"',
    '-nic','user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:18081-:8080,hostfwd=tcp:127.0.0.1:18082-:8081',
    '-no-reboot'
)
try {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 250
        if ($vmProcess.HasExited -or [DateTimeOffset]::UtcNow -gt $deadline) { throw "Envoy startup failed. See $envoyLog" }
        $console = if (Test-Path $envoyLog) { Get-Content $envoyLog -Raw } else { '' }
    } until ($console -match "configuration '/g2-1.json' OK" -and $console -match "configuration '/g2-2.json' OK" -and $console -match 'G2 Envoy processes launched')
    if ($Selection -eq 'g4') {
        if ($G4Case) {
            Invoke-Fixture -m scripts.storepilot_g4 run (Join-Path $out 'g4') --case $G4Case
        } else {
            Invoke-Fixture -m scripts.storepilot_g4 run (Join-Path $out 'g4')
            Invoke-Fixture -m scripts.storepilot_g3 run (Join-Path $out 'g3')
            Invoke-Fixture -m scripts.storepilot_g3 audit $out
            Invoke-Fixture -m scripts.storepilot_g2 run $frozen --case lifecycle --label lifecycle
        }
    } elseif ($Selection -eq 'g3') {
        if ($G3Case) {
            Invoke-Fixture -m scripts.storepilot_g3 run (Join-Path $out 'g3') --case $G3Case
        } else {
            Invoke-Fixture -m scripts.storepilot_g3 run (Join-Path $out 'g3')
            foreach ($case in @('lifecycle', 'approval')) {
                Invoke-Fixture -m scripts.storepilot_g2 run $frozen --case $case --label $case
            }
        }
        Invoke-Fixture -m scripts.storepilot_g3 audit $out
    } elseif ($Selection -in @('lifecycle', 'rate', 'approval')) {
        Invoke-Fixture -m scripts.storepilot_g2 run $frozen --case $Selection --label $Selection
    } elseif ($Selection -in @('mix-a', 'mix-b')) {
        Invoke-Fixture -m scripts.storepilot_g2 run $frozen --case mix --mode $Selection[-1].ToString().ToUpper() --label $Selection
    } else {
        foreach ($case in @('rate', 'lifecycle', 'approval')) {
            Invoke-Fixture -m scripts.storepilot_g2 run $frozen --case $case --label $case
        }
        $manifest = Get-Content (Join-Path $frozen 'manifest.json') -Raw | ConvertFrom-Json
        foreach ($repeat in 1..$manifest.load.repeats) {
            foreach ($mode in @('A', 'B')) {
                Invoke-Fixture -m scripts.storepilot_g2 run $frozen --case mix --mode $mode --label "mix-$repeat-$mode"
            }
        }
        $regression = Join-Path $out 'g1-regression'
        Invoke-Fixture -m scripts.storepilot_gateway init $regression
        $env:STOREPILOT_GROUP_INFLIGHT = '2:3'
        $env:STOREPILOT_STARTS_PER_SECOND = '100'
        foreach ($case in @('normal', 'client-abort', 'upstream-abort', 'lost-done', 'rollback')) {
            Invoke-Fixture -m scripts.storepilot_gateway run $regression --label dual --case $case --bind '127.0.0.1:5010' --origin 'http://127.0.0.1:18082'
        }
        $last = Get-Content (Join-Path $regression 'dual/rollback/browser.json') -Raw | ConvertFrom-Json
        $lastIds = @($last.stream.request_id, $last.post.request_id, $last.independentGet.request_id)
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(20)
        do {
            if ($vmProcess.HasExited -or [DateTimeOffset]::UtcNow -gt $deadline) { throw "Envoy log did not flush. See $envoyLog" }
            $console = Get-Content $envoyLog -Raw
            $missing = @($lastIds | Where-Object { -not $console.Contains($_) })
            if ($missing.Count) { Start-Sleep -Milliseconds 250 }
        } while ($missing.Count)
        Invoke-Fixture -m scripts.audit_storepilot_g2 $out
    }
} finally {
    Stop-Process -InputObject $vmProcess -ErrorAction SilentlyContinue
}
