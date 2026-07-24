[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet('open5gs', 'free5gc', 'oai', 'all')]
    [string]$Project = 'all',

    [ValidateSet('main', 'multiSeq', 'both')]
    [string]$Component = 'both',

    [string]$WorkspaceRoot = 'D:\state-learning-lab'
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($WorkspaceRoot)
if (-not (Test-Path -LiteralPath (Join-Path $root 'workspace.yaml'))) {
    throw "Not a state-learning-lab workspace: $root"
}

Add-Type -AssemblyName System.IO.Compression

function Get-TreeHash {
    param(
        [System.IO.FileInfo[]]$Files,
        [string]$ComponentRoot
    )

    $records = foreach ($file in $Files) {
        $relativePath = [System.IO.Path]::GetRelativePath($ComponentRoot, $file.FullName).Replace('\', '/')
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$relativePath`0$hash"
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($records -join "`n"))
    return [System.Convert]::ToHexString(
        [System.Security.Cryptography.SHA256]::HashData($bytes)
    ).ToLowerInvariant()
}

function Write-DeterministicScriptArchive {
    param(
        [string]$ComponentRoot,
        [string]$ArchivePath
    )

    $scriptsRoot = Join-Path $ComponentRoot 'scripts'
    if (-not (Test-Path -LiteralPath $scriptsRoot -PathType Container)) {
        throw "Scripts directory is missing: $scriptsRoot"
    }

    $files = @(
        Get-ChildItem -LiteralPath $scriptsRoot -File -Recurse -Force |
            Sort-Object FullName
    )
    if ($files.Count -eq 0) {
        throw "Scripts directory is empty: $scriptsRoot"
    }

    $temporaryArchive = $ArchivePath + '.publishing'
    if (Test-Path -LiteralPath $temporaryArchive) {
        Remove-Item -LiteralPath $temporaryArchive -Force
    }

    $fileStream = [System.IO.File]::Open(
        $temporaryArchive,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    try {
        $archive = [System.IO.Compression.ZipArchive]::new(
            $fileStream,
            [System.IO.Compression.ZipArchiveMode]::Create,
            $true,
            [System.Text.Encoding]::UTF8
        )
        try {
            $directoryEntry = $archive.CreateEntry('scripts/')
            $directoryEntry.LastWriteTime = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)

            foreach ($file in $files) {
                $relativePath = [System.IO.Path]::GetRelativePath(
                    $ComponentRoot,
                    $file.FullName
                ).Replace('\', '/')
                $entry = $archive.CreateEntry(
                    $relativePath,
                    [System.IO.Compression.CompressionLevel]::Optimal
                )
                $entry.LastWriteTime = [DateTimeOffset]::new(
                    1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero
                )

                $inputStream = [System.IO.File]::OpenRead($file.FullName)
                $outputStream = $entry.Open()
                try {
                    $inputStream.CopyTo($outputStream)
                } finally {
                    $outputStream.Dispose()
                    $inputStream.Dispose()
                }
            }
        } finally {
            $archive.Dispose()
        }
    } finally {
        $fileStream.Dispose()
    }

    $expectedEntries = @('scripts/') + @(
        $files | ForEach-Object {
            [System.IO.Path]::GetRelativePath($ComponentRoot, $_.FullName).Replace('\', '/')
        }
    )
    $validationStream = [System.IO.File]::OpenRead($temporaryArchive)
    try {
        $validationArchive = [System.IO.Compression.ZipArchive]::new(
            $validationStream,
            [System.IO.Compression.ZipArchiveMode]::Read
        )
        try {
            $actualEntries = @($validationArchive.Entries | ForEach-Object FullName)
        } finally {
            $validationArchive.Dispose()
        }
    } finally {
        $validationStream.Dispose()
    }

    if ((Compare-Object -ReferenceObject $expectedEntries -DifferenceObject $actualEntries).Count -ne 0) {
        Remove-Item -LiteralPath $temporaryArchive -Force
        throw "Archive validation failed: $temporaryArchive"
    }

    Move-Item -LiteralPath $temporaryArchive -Destination $ArchivePath -Force
    return [pscustomobject]@{
        FileCount = $files.Count
        TreeHash = Get-TreeHash -Files $files -ComponentRoot $ComponentRoot
    }
}

$definitions = @{
    open5gs = @{ Repo = 'open5gs-state-learning'; Main = 'Corelearner_open5gs' }
    free5gc = @{ Repo = 'free5gc-state-learning'; Main = 'Corelearner_free5gc' }
    oai = @{ Repo = 'oai-state-learning'; Main = 'Corelearner_OAI' }
}
$selectedProjects = if ($Project -eq 'all') { @('open5gs', 'free5gc', 'oai') } else { @($Project) }
$selectedComponents = if ($Component -eq 'both') { @('main', 'multiSeq') } else { @($Component) }

foreach ($projectId in $selectedProjects) {
    $definition = $definitions[$projectId]
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $root ('projects\' + $definition.Repo)))

    foreach ($componentId in $selectedComponents) {
        $componentDirectory = if ($componentId -eq 'main') {
            $definition.Main
        } else {
            'Corelearner_seqTest_pack'
        }
        $componentRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $componentDirectory))
        if (-not $componentRoot.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Component escaped repository root: $componentRoot"
        }

        $archivePath = Join-Path $componentRoot 'scripts.zip'
        if ($PSCmdlet.ShouldProcess($archivePath, 'Package versioned scripts directory')) {
            $scriptsStatus = @(
                & git -C $repoRoot status --porcelain=v1 -- "$componentDirectory/scripts"
            )
            if ($LASTEXITCODE -ne 0) {
                throw "Could not inspect scripts status for $componentRoot"
            }

            $result = Write-DeterministicScriptArchive `
                -ComponentRoot $componentRoot `
                -ArchivePath $archivePath
            $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash

            [pscustomobject]@{
                project = $projectId
                component = $componentId
                scripts_changed = ($scriptsStatus.Count -gt 0)
                archive = $archivePath
                archive_sha256 = $archiveHash
                source_tree_sha256 = $result.TreeHash
                file_count = $result.FileCount
                chmod_required_after_linux_extract = $true
                chmod_command = 'chmod +x scripts/*.sh'
            } | ConvertTo-Json -Compress
        }
    }
}
