[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateSet('open5gs', 'free5gc', 'oai', 'cross-platform')]
    [string]$Platform,

    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')]
    [string]$FailureId,

    [Parameter(Mandatory)]
    [string[]]$InputPath,

    [string]$ExperimentRepo = 'D:\state-learning-lab\projects\state-learning-experiments',
    [string]$RunDataRoot = 'D:\state-learning-lab\run-data',
    [string]$RunId,
    [ValidateRange(1, 1024)]
    [int]$DirectGitLimitMiB = 10,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath (Join-Path $ExperimentRepo '.git'))) {
    throw "Experiment repository not found: $ExperimentRepo"
}

$recordRoot = [System.IO.Path]::GetFullPath((Join-Path $ExperimentRepo ("failures\$Platform\$FailureId")))
$experimentRoot = [System.IO.Path]::GetFullPath($ExperimentRepo)
if (-not $recordRoot.StartsWith($experimentRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Failure record escaped experiment repository: $recordRoot"
}

$files = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
foreach ($candidate in $InputPath) {
    $resolved = Resolve-Path -Path $candidate -ErrorAction Stop
    foreach ($path in $resolved) {
        $item = Get-Item -LiteralPath $path.Path -Force
        if ($item.PSIsContainer) {
            Get-ChildItem -LiteralPath $item.FullName -File -Recurse -Force | ForEach-Object { $files.Add($_) }
        } else {
            $files.Add($item)
        }
    }
}

$uniqueFiles = @($files | Sort-Object FullName -Unique)
if ($uniqueFiles.Count -eq 0) {
    throw 'No files selected for archive.'
}
if (($uniqueFiles | Group-Object Name | Where-Object Count -gt 1).Count -gt 0) {
    throw 'Selected files contain duplicate names. Freeze them into a dedicated run directory first.'
}

$totalBytes = [long]0
$manifestFiles = foreach ($file in $uniqueFiles) {
    $totalBytes += $file.Length
    [pscustomobject]@{
        source_path = $file.FullName
        bytes = $file.Length
        modified_utc = $file.LastWriteTimeUtc.ToString('o')
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
}

$limitBytes = [long]$DirectGitLimitMiB * 1MB
$strategy = if ($totalBytes -le $limitBytes) { 'git_raw' } else { 'external_snapshot' }
$effectiveRunId = if ([string]::IsNullOrWhiteSpace($RunId)) { $FailureId } else { $RunId }
$destination = if ($strategy -eq 'git_raw') {
    Join-Path $recordRoot 'raw'
} else {
    Join-Path $RunDataRoot ("$Platform\$effectiveRunId")
}

$summary = [ordered]@{
    failure_id = $FailureId
    platform = $Platform
    strategy = $strategy
    threshold_bytes = $limitBytes
    selected_bytes = $totalBytes
    file_count = $manifestFiles.Count
    destination = $destination
    files = $manifestFiles
}

if (-not $Apply) {
    $summary | ConvertTo-Json -Depth 5
    return
}

if ($PSCmdlet.ShouldProcess($destination, "Archive $($manifestFiles.Count) files using $strategy")) {
    [System.IO.Directory]::CreateDirectory($destination) | Out-Null
    foreach ($file in $uniqueFiles) {
        $target = Join-Path $destination $file.Name
        if (Test-Path -LiteralPath $target) {
            $existingHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
            $sourceHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            if ($existingHash -ne $sourceHash) {
                throw "Refusing to overwrite different archived file: $target"
            }
            continue
        }
        Copy-Item -LiteralPath $file.FullName -Destination $target
    }

    [System.IO.Directory]::CreateDirectory($recordRoot) | Out-Null
    $summaryPath = Join-Path $recordRoot 'archive-summary.json'
    [System.IO.File]::WriteAllText(
        $summaryPath,
        (($summary | ConvertTo-Json -Depth 5) + [Environment]::NewLine),
        [System.Text.UTF8Encoding]::new($false))
    $summary | ConvertTo-Json -Depth 5
}
