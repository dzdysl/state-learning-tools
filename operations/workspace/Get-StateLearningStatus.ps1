[CmdletBinding()]
param(
    [string]$WorkspaceRoot = 'D:\state-learning-lab'
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($WorkspaceRoot)
if (-not (Test-Path -LiteralPath (Join-Path $root 'workspace.yaml'))) {
    throw "Not a state-learning-lab workspace: $root"
}

$repositories = @(
    @{ Id = 'open5gs'; RelativePath = 'projects\open5gs-state-learning' },
    @{ Id = 'free5gc'; RelativePath = 'projects\free5gc-state-learning' },
    @{ Id = 'oai'; RelativePath = 'projects\oai-state-learning' },
    @{ Id = 'tools'; RelativePath = 'projects\state-learning-tools' },
    @{ Id = 'experiments'; RelativePath = 'projects\state-learning-experiments' }
)

$result = foreach ($repository in $repositories) {
    $path = [System.IO.Path]::GetFullPath((Join-Path $root $repository.RelativePath))
    if (-not $path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Repository escaped workspace root: $path"
    }

    $branch = (& git -C $path branch --show-current 2>$null)
    $head = (& git -C $path rev-parse --short HEAD 2>$null)
    $changes = @(& git -C $path status --porcelain=v1 2>$null)
    $targets = @(Get-ChildItem -LiteralPath $path -Directory -Recurse -Filter target -ErrorAction SilentlyContinue)
    $archives = @(Get-ChildItem -LiteralPath $path -File -Recurse -Include *.jar,*.zip -ErrorAction SilentlyContinue)

    [pscustomobject]@{
        project = $repository.Id
        path = $path
        branch = ($branch -join '')
        head = ($head -join '')
        clean = ($changes.Count -eq 0)
        changes = $changes
        target_directory_count = $targets.Count
        jar_zip_count = $archives.Count
    }
}

$result | ConvertTo-Json -Depth 5
