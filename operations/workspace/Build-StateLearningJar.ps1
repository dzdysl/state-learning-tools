[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet('open5gs', 'free5gc', 'oai', 'all')]
    [string]$Project = 'all',

    [ValidateSet('main', 'multiSeq', 'both')]
    [string]$Component = 'both',

    [string]$WorkspaceRoot = 'D:\state-learning-lab',
    [string]$JdkHome = 'C:\Program Files\Java\jdk-17',
    [switch]$RunFullTests,
    [switch]$KeepTargets
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($WorkspaceRoot)
if (-not (Test-Path -LiteralPath (Join-Path $root 'workspace.yaml'))) {
    throw "Not a state-learning-lab workspace: $root"
}
if (-not (Test-Path -LiteralPath (Join-Path $JdkHome 'bin\java.exe'))) {
    throw "JDK not found: $JdkHome"
}

function Write-BuildProvenance {
    param(
        [string]$RepoRoot,
        [string]$ComponentRoot,
        [string]$ComponentId
    )

    $commit = (& git -C $RepoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not determine Git commit for $RepoRoot" }
    $branch = (& git -C $RepoRoot branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($branch)) { $branch = 'detached' }
    $describe = (& git -C $RepoRoot describe --tags --always --dirty).Trim()
    if ($LASTEXITCODE -ne 0) { $describe = $commit }
    $status = (& git -C $RepoRoot status --porcelain | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "Could not determine Git status for $RepoRoot" }
    $statusBytes = [System.Text.Encoding]::UTF8.GetBytes($status)
    $statusHash = [System.Security.Cryptography.SHA256]::HashData($statusBytes)
    $statusHashText = [System.Convert]::ToHexString($statusHash).ToLowerInvariant()
    $dirty = if ([string]::IsNullOrWhiteSpace($status)) { 'false' } else { 'true' }

    $metadataPath = Join-Path $ComponentRoot 'mylearner\src\main\resources\META-INF\state-learning-build.properties'
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($metadataPath)) | Out-Null
    $lines = [string[]]@(
        "repository=$([System.IO.Path]::GetFileName($RepoRoot))"
        "component=$ComponentId"
        "build.utc=$([DateTime]::UtcNow.ToString('o'))"
        "git.commit=$commit"
        "git.branch=$branch"
        "git.describe=$describe"
        "git.dirty=$dirty"
        "git.status_sha256=$statusHashText"
    )
    [System.IO.File]::WriteAllLines(
        $metadataPath,
        $lines,
        [System.Text.UTF8Encoding]::new($false))
}

function Write-RuntimeContract {
    param(
        [string]$ProjectId,
        [string]$ComponentRoot
    )

    $runtimeFiles = @(
        'scripts/start_core.sh',
        'scripts/kill_core.sh',
        'scripts/kill_gnb.sh',
        'scripts/kill_ue.sh'
    )
    if ($ProjectId -eq 'oai') {
        $runtimeFiles += @(
            'scripts/oai-compose.override.yaml',
            'scripts/init_db.py'
        )
    }

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('contract.version=state-learning-runtime/v2')
    $lines.Add("file.count=$($runtimeFiles.Count)")
    for ($index = 0; $index -lt $runtimeFiles.Count; $index++) {
        $relativePath = $runtimeFiles[$index]
        $nativeRelativePath = $relativePath.Replace('/', '\')
        $runtimePath = Join-Path $ComponentRoot $nativeRelativePath
        if (-not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) {
            throw "Required runtime file is missing: $runtimePath"
        }
        $hash = (Get-FileHash -LiteralPath $runtimePath -Algorithm SHA256).Hash.ToLowerInvariant()
        $lines.Add("file.$index.path=$relativePath")
        $lines.Add("file.$index.sha256=$hash")
    }

    $metadataPath = Join-Path $ComponentRoot 'mylearner\src\main\resources\META-INF\state-learning-runtime.properties'
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($metadataPath)) | Out-Null
    [System.IO.File]::WriteAllLines(
        $metadataPath,
        $lines,
        [System.Text.UTF8Encoding]::new($false))
}

function Write-EmbeddedFinalizer {
    param(
        [string]$WorkspaceRoot,
        [string]$ComponentRoot
    )

    $source = Join-Path $WorkspaceRoot 'projects\state-learning-tools\protocol_events\protocol_events.py'
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Protocol finalizer source is missing: $source"
    }
    $destination = Join-Path $ComponentRoot 'mylearner\src\main\resources\META-INF\state-learning-tools\protocol_events.py'
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($destination)) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
    return $destination
}

$definitions = @{
    open5gs = @{ Repo = 'open5gs-state-learning'; Main = 'Corelearner_open5gs' }
    free5gc = @{ Repo = 'free5gc-state-learning'; Main = 'Corelearner_free5gc' }
    oai = @{ Repo = 'oai-state-learning'; Main = 'Corelearner_OAI' }
}
$selectedProjects = if ($Project -eq 'all') { @('open5gs', 'free5gc', 'oai') } else { @($Project) }
$selectedComponents = if ($Component -eq 'both') { @('main', 'multiSeq') } else { @($Component) }

$previousJavaHome = $env:JAVA_HOME
$previousPath = $env:Path
$env:JAVA_HOME = $JdkHome
$env:Path = (Join-Path $JdkHome 'bin') + ';' + $env:Path

try {
    foreach ($projectId in $selectedProjects) {
        $definition = $definitions[$projectId]
        $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $root ('projects\' + $definition.Repo)))

        foreach ($componentId in $selectedComponents) {
            if ($componentId -eq 'main') {
                $componentDirectory = $definition.Main
                $publishedName = 'Corelearner.jar'
                $builtJarName = 'mylearner-1.0-SNAPSHOT.jar'
            } else {
                $componentDirectory = 'Corelearner_seqTest_pack'
                $publishedName = 'Corelearner_SeqTest.jar'
                $builtJarName = 'Corelearner.jar'
            }

            $componentRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $componentDirectory))
            if (-not $componentRoot.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Component escaped repository root: $componentRoot"
            }
            $sourceJar = Join-Path $componentRoot ('mylearner\target\' + $builtJarName)
            $publishedJar = Join-Path $componentRoot $publishedName
            $temporaryJar = $publishedJar + '.publishing'

            if ($PSCmdlet.ShouldProcess($componentRoot, "Build and publish $publishedName")) {
                Write-BuildProvenance -RepoRoot $repoRoot -ComponentRoot $componentRoot -ComponentId $componentId
                Write-RuntimeContract -ProjectId $projectId -ComponentRoot $componentRoot
                $embeddedFinalizer = Write-EmbeddedFinalizer -WorkspaceRoot $root -ComponentRoot $componentRoot
                try {
                    Push-Location $componentRoot
                    try {
                        $mavenArguments = @('-q')
                        if (-not $RunFullTests) { $mavenArguments += '-DskipTests' }
                        $mavenArguments += @('-pl', 'mylearner', '-am')
                        $mavenArguments += 'package'
                        & mvn @mavenArguments
                        if ($LASTEXITCODE -ne 0) { throw "Maven failed for $projectId/$componentId" }
                    } finally {
                        Pop-Location
                    }
                } finally {
                    Remove-Item -LiteralPath $embeddedFinalizer -Force -ErrorAction SilentlyContinue
                }

                if (-not (Test-Path -LiteralPath $sourceJar -PathType Leaf)) {
                    throw "Expected fat JAR was not produced: $sourceJar"
                }
                & jar tf $sourceJar | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "JAR validation failed: $sourceJar" }

                Copy-Item -LiteralPath $sourceJar -Destination $temporaryJar -Force
                Move-Item -LiteralPath $temporaryJar -Destination $publishedJar -Force
                $hash = (Get-FileHash -LiteralPath $publishedJar -Algorithm SHA256).Hash

                [pscustomobject]@{
                    project = $projectId
                    component = $componentId
                    artifact = $publishedJar
                    sha256 = $hash
                } | ConvertTo-Json -Compress

                if (-not $KeepTargets) {
                    $targetDirectories = @(Get-ChildItem -LiteralPath $componentRoot -Directory -Recurse -Filter target -ErrorAction SilentlyContinue)
                    foreach ($targetDirectory in $targetDirectories) {
                        $resolvedTarget = [System.IO.Path]::GetFullPath($targetDirectory.FullName)
                        if (-not $resolvedTarget.StartsWith($componentRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                            throw "Refusing to remove target outside component root: $resolvedTarget"
                        }
                        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
                    }
                }
            }
        }
    }
} finally {
    $env:JAVA_HOME = $previousJavaHome
    $env:Path = $previousPath
}
