param(
    [Parameter(Mandatory = $true)]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$InputFile
)

if ([Environment]::Is64BitProcess) {
    $wow64PowerShell = Join-Path $env:WINDIR "SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
    if (Test-Path -LiteralPath $wow64PowerShell) {
        & $wow64PowerShell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Action $Action -InputFile $InputFile
        exit $LASTEXITCODE
    }
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function New-Response {
    param(
        [bool]$Ok,
        [object]$Data = $null,
        [string]$Error = $null
    )

    [pscustomobject]@{
        ok    = $Ok
        data  = $Data
        error = $Error
    }
}

function Write-Json {
    param([object]$Object)
    $Object | ConvertTo-Json -Depth 8 -Compress
}

function ConvertTo-HashtableCompat {
    param([Parameter(Mandatory = $true)]$InputObject)

    if ($null -eq $InputObject) {
        return $null
    }

    if ($InputObject -is [System.Collections.IDictionary]) {
        $result = @{}
        foreach ($key in $InputObject.Keys) {
            $result[$key] = ConvertTo-HashtableCompat -InputObject $InputObject[$key]
        }
        return $result
    }

    if ($InputObject -is [System.Collections.IEnumerable] -and -not ($InputObject -is [string])) {
        $list = New-Object System.Collections.ArrayList
        foreach ($item in $InputObject) {
            [void]$list.Add((ConvertTo-HashtableCompat -InputObject $item))
        }
        return ,$list.ToArray()
    }

    if ($InputObject -is [psobject]) {
        $properties = @($InputObject.PSObject.Properties)
        if ($properties.Count -gt 0) {
            $result = @{}
            foreach ($property in $properties) {
                $result[$property.Name] = ConvertTo-HashtableCompat -InputObject $property.Value
            }
            return $result
        }
    }

    return $InputObject
}

function Read-InputPayload {
    if (-not (Test-Path -LiteralPath $InputFile)) {
        return @{}
    }

    $bytes = [System.IO.File]::ReadAllBytes($InputFile)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $raw = [System.Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3)
    } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        $raw = [System.Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2)
    } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) {
        $raw = [System.Text.Encoding]::BigEndianUnicode.GetString($bytes, 2, $bytes.Length - 2)
    } else {
        $raw = [System.Text.Encoding]::UTF8.GetString($bytes)
    }
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return @{}
    }

    $parsed = $raw | ConvertFrom-Json
    return (ConvertTo-HashtableCompat -InputObject $parsed)
}

function Resolve-MSRoot {
    if ($env:MATERIALS_STUDIO_ROOT -and (Test-Path -LiteralPath $env:MATERIALS_STUDIO_ROOT)) {
        return $env:MATERIALS_STUDIO_ROOT
    }

    $roots = @(
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )

    $entries = @(Get-ItemProperty $roots -ErrorAction SilentlyContinue |
        Where-Object {
            $_.PSObject.Properties["DisplayName"] -and
            $_.DisplayName -match "BIOVIA Materials Studio 2023" -and
            $_.DisplayName -notmatch "Documentation|Server"
        })

    foreach ($entry in $entries) {
        if (-not $entry.InstallLocation) {
            continue
        }

        $install = $entry.InstallLocation.Trim()
        if (Test-Path -LiteralPath (Join-Path $install "Materials Studio 23.1")) {
            return (Join-Path $install "Materials Studio 23.1")
        }

        if (Test-Path -LiteralPath $install) {
            return $install
        }
    }

    $candidates = @(
        "D:\Program Files (x86)\BIOVIA\Materials Studio 23.1",
        "C:\Program Files (x86)\BIOVIA\Materials Studio 23.1"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    throw "Materials Studio 2023 installation was not found. Set MATERIALS_STUDIO_ROOT or install BIOVIA Materials Studio 2023."
}

function Get-MSPaths {
    $root = Resolve-MSRoot

    [pscustomobject]@{
        root                = $root
        run_mat_script      = Join-Path $root "etc\Scripting\bin\RunMatScript.bat"
        perl_bin            = Join-Path $root "bin\perl.exe"
        scripting_help_root = Join-Path $root "share\doc\content\scripting"
        scripting_pdf       = Join-Path $root "share\doc\content\pdfs\materialsscriptapi.pdf"
        examples_root       = Join-Path $root "share\Examples"
        prog_id             = "SBContainerCore.ContainerCore"
    }
}

function New-XmlDocument {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Document does not exist: $Path"
    }

    $xml = New-Object System.Xml.XmlDocument
    try {
        $xml.Load($Path)
    } catch {
        throw "Failed to parse XML document: $Path"
    }

    return $xml
}

function Get-ElementFormula {
    param([System.Xml.XmlDocument]$Xml)

    $elementCounts = @{}
    $atomNodes = @($Xml.SelectNodes("//*[local-name()='Atom3d']"))
    foreach ($atom in $atomNodes) {
        $component = $atom.GetAttribute("Components")
        if ([string]::IsNullOrWhiteSpace($component)) {
            continue
        }

        if (-not $elementCounts.ContainsKey($component)) {
            $elementCounts[$component] = 0
        }
        $elementCounts[$component] += 1
    }

    if ($elementCounts.Count -eq 0) {
        return $null
    }

    $orderedKeys = $elementCounts.Keys | Sort-Object
    $parts = foreach ($key in $orderedKeys) {
        $count = [int]$elementCounts[$key]
        if ($count -eq 1) {
            $key
        } else {
            "$key$count"
        }
    }

    return ($parts -join " ")
}

function Get-DocumentSummary {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $extension = [System.IO.Path]::GetExtension($resolvedPath)
    if ($null -eq $extension) {
        $extension = ""
    }
    $extension = $extension.ToLowerInvariant()

    if ($extension -eq ".stp") {
        $xml = New-XmlDocument -Path $resolvedPath
        $urls = @($xml.SelectNodes("/Project/DocumentManager/Document/URL")) | ForEach-Object { $_.InnerText }

        return [pscustomobject]@{
            path      = $resolvedPath
            extension = $extension
            type      = "materials-studio-project"
            summary   = [pscustomobject]@{
                version        = $xml.SelectSingleNode("/Project/Version").InnerText
                document_count = $urls.Count
                documents      = $urls
            }
            notes     = @(
                "Parsed directly from the .stp XML project file.",
                "Document paths are project-relative URLs."
            )
        }
    }

    if ($extension -notin @(".xsd", ".xtd")) {
        return [pscustomobject]@{
            path      = $resolvedPath
            extension = $extension
            type      = "generic-file"
            summary   = [pscustomobject]@{
                size_bytes = (Get-Item -LiteralPath $resolvedPath).Length
            }
            notes     = @(
                "This file type is not yet deeply parsed.",
                "Supported structured parsing currently covers .xsd, .xtd, and .stp."
            )
        }
    }

    $xml = New-XmlDocument -Path $resolvedPath
    $xsdNode = $xml.DocumentElement
    $rootNode = $xml.SelectSingleNode("/XSD/*[1]")
    $trajectoryNode = $xml.SelectSingleNode("//*[local-name()='Trajectory']")
    $spaceGroupNode = $xml.SelectSingleNode("//*[local-name()='SpaceGroup']")
    $symmetryNode = $xml.SelectSingleNode("//*[local-name()='SymmetrySystem']")
    $atomNodes = @($xml.SelectNodes("//*[local-name()='Atom3d']"))
    $bondNodes = @($xml.SelectNodes("//*[local-name()='Bond']"))
    $moleculeNodes = @($xml.SelectNodes("//*[local-name()='Molecule']"))

    $trajectorySummary = $null
    if ($trajectoryNode) {
        $frame = $trajectoryNode.GetAttribute("Frame")
        $end = $trajectoryNode.GetAttribute("End")
        $trajectorySummary = [pscustomobject]@{
            current_frame = if ($frame) { [int]$frame } else { $null }
            end_frame     = if ($end) { [int]$end } else { $null }
            type          = $trajectoryNode.GetAttribute("Type")
            frame_class   = $trajectoryNode.GetAttribute("FrameClassType")
        }
    }

    $spaceGroupSummary = $null
    if ($spaceGroupNode) {
        $spaceGroupSummary = [pscustomobject]@{
            name       = $spaceGroupNode.GetAttribute("Name")
            group_name = $spaceGroupNode.GetAttribute("GroupName")
            system     = $spaceGroupNode.GetAttribute("System")
            a_vector   = $spaceGroupNode.GetAttribute("AVector")
            b_vector   = $spaceGroupNode.GetAttribute("BVector")
            c_vector   = $spaceGroupNode.GetAttribute("CVector")
        }
    }

    [pscustomobject]@{
        path      = $resolvedPath
        extension = $extension
        type      = if ($extension -eq ".xtd") { "trajectory-document" } else { "structure-document" }
        summary   = [pscustomobject]@{
            xsd_version       = $xsdNode.GetAttribute("Version")
            written_by        = $xsdNode.GetAttribute("WrittenBy")
            root_tag          = if ($rootNode) { $rootNode.Name } else { $null }
            name              = if ($rootNode) { $rootNode.GetAttribute("Name") } else { $null }
            atom_count        = $atomNodes.Count
            bond_count        = $bondNodes.Count
            molecule_count    = $moleculeNodes.Count
            periodic          = [bool]$symmetryNode
            chemical_formula  = Get-ElementFormula -Xml $xml
            trajectory        = $trajectorySummary
            space_group       = $spaceGroupSummary
        }
        notes     = @(
            "Parsed directly from the Materials Studio XML file without COM automation.",
            "Counts are based on XML nodes and may differ from symmetry-expanded visual counts."
        )
    }
}

function Search-Help {
    param(
        [Parameter(Mandatory = $true)][string]$Query,
        [int]$MaxResults = 10
    )

    $paths = Get-MSPaths
    $helpRoot = $paths.scripting_help_root
    if (-not (Test-Path -LiteralPath $helpRoot)) {
        throw "Scripting help directory not found: $helpRoot"
    }

    $tokens = @($Query -split "\s+" | Where-Object { $_ })
    $regexOptions = [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    $previewOptions = (
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
        [System.Text.RegularExpressions.RegexOptions]::Singleline
    )
    $files = Get-ChildItem -LiteralPath $helpRoot -Recurse -File |
        Where-Object { $_.Extension -in @(".htm", ".html") }
    $matches = New-Object System.Collections.Generic.List[object]

    foreach ($file in $files) {
        try {
            $content = Get-Content -LiteralPath $file.FullName -Raw -Encoding utf8
        } catch {
            continue
        }

        $score = 0
        foreach ($token in $tokens) {
            $escaped = [regex]::Escape($token)
            $count = ([regex]::Matches($content, $escaped, $regexOptions)).Count
            $score += $count
        }

        if ($score -le 0) {
            continue
        }

        $title = [regex]::Match($content, "<title>(.*?)</title>", $regexOptions).Groups[1].Value
        if ([string]::IsNullOrWhiteSpace($title)) {
            $title = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
        }

        $preview = ""
        if ($tokens.Count -gt 0) {
            $previewPattern = ".{0,80}$([regex]::Escape($tokens[0])).{0,160}"
            $previewMatch = [regex]::Match($content, $previewPattern, $previewOptions)
            if ($previewMatch.Success) {
                $preview = ($previewMatch.Value -replace "<[^>]+>", " " -replace "\s+", " ").Trim()
            }
        }

        $matches.Add([pscustomobject]@{
            title   = $title
            path    = $file.FullName
            score   = $score
            preview = $preview
        })
    }

    return $matches |
        Sort-Object -Property @{ Expression = "score"; Descending = $true }, @{ Expression = "title"; Descending = $false } |
        Select-Object -First $MaxResults
}

function List-Examples {
    param(
        [string]$Pattern = "*.xsd",
        [int]$MaxResults = 50
    )

    $paths = Get-MSPaths
    $root = $paths.examples_root
    if (-not (Test-Path -LiteralPath $root)) {
        throw "Examples directory not found: $root"
    }

    Get-ChildItem -LiteralPath $root -Recurse -File -Filter $Pattern |
        Select-Object -First $MaxResults -Property FullName, Name, DirectoryName, Length
}

function Scan-Workspace {
    param(
        [Parameter(Mandatory = $true)][string]$RootDir,
        [string[]]$Patterns,
        [int]$MaxResults = 200
    )

    if (-not (Test-Path -LiteralPath $RootDir)) {
        throw "Workspace directory does not exist: $RootDir"
    }

    $results = New-Object System.Collections.Generic.List[object]
    foreach ($pattern in $Patterns) {
        $items = Get-ChildItem -LiteralPath $RootDir -Recurse -File -Filter $pattern -ErrorAction SilentlyContinue
        foreach ($item in $items) {
            $results.Add([pscustomobject]@{
                path     = $item.FullName
                name     = $item.Name
                pattern  = $pattern
                size     = $item.Length
                modified = $item.LastWriteTime
            })
        }
    }

    $results |
        Sort-Object -Property path -Unique |
        Select-Object -First $MaxResults
}

function Convert-HtmlToText {
    param([string]$Html)

    if ([string]::IsNullOrWhiteSpace($Html)) {
        return ""
    }

    $text = $Html -replace "(?is)<script.*?</script>", " "
    $text = $text -replace "(?is)<style.*?</style>", " "
    $text = $text -replace "(?i)<br\s*/?>", "`n"
    $text = $text -replace "(?i)</p>", "`n"
    $text = $text -replace "(?i)</tr>", "`n"
    $text = $text -replace "<[^>]+>", " "
    $text = [System.Net.WebUtility]::HtmlDecode($text)
    $text = $text -replace "[ \t]+", " "
    $text = $text -replace "(\r?\n\s*){3,}", "`n`n"
    return $text.Trim()
}

function Extract-CodeExamples {
    param([string]$Html)

    $options = (
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
        [System.Text.RegularExpressions.RegexOptions]::Singleline
    )
    $matches = [regex]::Matches($Html, "<pre class=""example""[^>]*>(.*?)</pre>", $options)
    $examples = New-Object System.Collections.Generic.List[string]

    foreach ($match in $matches) {
        $code = $match.Groups[1].Value
        $code = $code -replace "<!--", ""
        $code = $code -replace "-->", ""
        $code = $code -replace "(?i)<br\s*/?>", "`n"
        $code = $code -replace "<[^>]+>", ""
        $code = [System.Net.WebUtility]::HtmlDecode($code)
        $code = $code.Trim()
        if (-not [string]::IsNullOrWhiteSpace($code)) {
            $examples.Add($code)
        }
    }

    return @($examples)
}

function Resolve-HelpPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (Test-Path -LiteralPath $Path) {
        return (Resolve-Path -LiteralPath $Path).Path
    }

    $helpRoot = (Get-MSPaths).scripting_help_root
    $combined = Join-Path $helpRoot $Path
    if (Test-Path -LiteralPath $combined) {
        return (Resolve-Path -LiteralPath $combined).Path
    }

    throw "Help page not found: $Path"
}

function Read-HelpPage {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolvedPath = Resolve-HelpPath -Path $Path
    $content = Get-Content -LiteralPath $resolvedPath -Raw -Encoding utf8
    $title = [regex]::Match($content, "<title>(.*?)</title>", [System.Text.RegularExpressions.RegexOptions]::IgnoreCase).Groups[1].Value
    $examples = Extract-CodeExamples -Html $content
    $text = Convert-HtmlToText -Html $content

    [pscustomobject]@{
        path          = $resolvedPath
        title         = $title
        text_excerpt  = if ($text.Length -gt 4000) { $text.Substring(0, 4000) } else { $text }
        code_examples = $examples
    }
}

function Find-CodeExamples {
    param(
        [Parameter(Mandatory = $true)][string]$Query,
        [int]$MaxResults = 8
    )

    $helpRoot = (Get-MSPaths).scripting_help_root
    $tokens = @($Query -split "\s+" | Where-Object { $_ })
    $files = Get-ChildItem -LiteralPath $helpRoot -Recurse -File |
        Where-Object { $_.Extension -in @(".htm", ".html") }
    $results = New-Object System.Collections.Generic.List[object]

    foreach ($file in $files) {
        try {
            $content = Get-Content -LiteralPath $file.FullName -Raw -Encoding utf8
        } catch {
            continue
        }

        $title = [regex]::Match($content, "<title>(.*?)</title>", [System.Text.RegularExpressions.RegexOptions]::IgnoreCase).Groups[1].Value
        $examples = Extract-CodeExamples -Html $content
        foreach ($code in $examples) {
            $score = 0
            foreach ($token in $tokens) {
                $escaped = [regex]::Escape($token)
                $score += ([regex]::Matches($code, $escaped, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)).Count
                $score += ([regex]::Matches($title, $escaped, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)).Count
            }

            if ($score -le 0) {
                continue
            }

            $results.Add([pscustomobject]@{
                title = $title
                path  = $file.FullName
                score = $score
                code  = $code
            })
        }
    }

    return $results |
        Sort-Object -Property @{ Expression = "score"; Descending = $true }, @{ Expression = "title"; Descending = $false } |
        Select-Object -First $MaxResults
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Assert-AsciiAbsolutePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not [System.IO.Path]::IsPathRooted($Path)) {
        throw "$Label must be an absolute path: $Path"
    }
    if ($Path -match '[^\x00-\x7F]') {
        throw "$Label must contain ASCII characters only because Materials Studio 23.1 can crash when MatServer starts from a Unicode path: $Path"
    }
}

function Get-MaterialsScriptScratchRoot {
    $root = $env:MATERIALS_STUDIO_MCP_ASCII_SCRATCH_ROOT
    if ([string]::IsNullOrWhiteSpace($root)) {
        $root = "E:\ms_mcp\ms_mcp_runtime\scratch\materials_studio_mcp"
    }
    Assert-AsciiAbsolutePath -Path $root -Label "MaterialsScript scratch root"
    Ensure-Directory -Path $root
    return $root
}

function Assert-NoUnicodeAbsolutePathLiteral {
    param([Parameter(Mandatory = $true)][string]$Text)

    $pattern = '(["''])(?<path>(?:[A-Za-z]:[\\/]|\\\\).*?)\1'
    foreach ($match in [System.Text.RegularExpressions.Regex]::Matches($Text, $pattern, [System.Text.RegularExpressions.RegexOptions]::Singleline)) {
        $literalPath = $match.Groups["path"].Value
        if ($literalPath -match '[^\x00-\x7F]') {
            throw "MaterialsScript contains a non-ASCII absolute path literal. Stage the input and use a template placeholder instead: $literalPath"
        }
    }
}

function Convert-ToAsciiSafeName {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$Fallback = "item"
    )

    $safe = ($Name -replace "[^A-Za-z0-9._-]", "_").Trim("_")
    if ([string]::IsNullOrWhiteSpace($safe)) {
        $safe = $Fallback
    }
    return $safe
}

function Convert-ToPerlPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return ($Path -replace "\\", "/")
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Expand-Template {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][hashtable]$Variables
    )

    $expanded = $Text
    foreach ($key in $Variables.Keys) {
        $placeholder = "{{{0}}}" -f $key
        $expanded = $expanded.Replace($placeholder, [string]$Variables[$key])
    }
    return $expanded
}

function Invoke-MaterialsScriptJob {
    param([hashtable]$Payload)

    $paths = Get-MSPaths
    Assert-AsciiAbsolutePath -Path $paths.root -Label "Materials Studio root"
    Assert-AsciiAbsolutePath -Path $paths.run_mat_script -Label "RunMatScript path"
    $jobName = if ($Payload.ContainsKey("job_name")) { [string]$Payload.job_name } else { "ms_job" }
    $jobName = Convert-ToAsciiSafeName -Name $jobName -Fallback "ms_job"
    $runMode = if ($Payload.ContainsKey("run_mode")) { [string]$Payload.run_mode } else { "flat" }
    $keepJobDir = if ($Payload.ContainsKey("keep_job_dir")) { [bool]$Payload.keep_job_dir } else { $true }
    $scriptTemplate = [string]$Payload.script_template

    if ([string]::IsNullOrWhiteSpace($scriptTemplate)) {
        throw "script_template is required for run-script."
    }

    $jobId = "{0:yyyyMMdd_HHmmss}_{1}" -f (Get-Date), (Get-Random -Minimum 10000 -Maximum 99999)
    $root = Get-MaterialsScriptScratchRoot
    $inputDir = Join-Path (Join-Path $root "inputs") $jobId
    $jobDir = Join-Path (Join-Path $root "jobs") $jobId
    $outputDir = Join-Path $jobDir "outputs"
    Assert-AsciiAbsolutePath -Path $inputDir -Label "MaterialsScript input directory"
    Assert-AsciiAbsolutePath -Path $jobDir -Label "MaterialsScript job directory"
    Assert-AsciiAbsolutePath -Path $outputDir -Label "MaterialsScript output directory"
    Ensure-Directory -Path $inputDir
    Ensure-Directory -Path $jobDir
    Ensure-Directory -Path $outputDir

    $templateVars = @{
        "ms_root"    = Convert-ToPerlPath $paths.root
        "job_dir"    = Convert-ToPerlPath $jobDir
        "input_dir"  = Convert-ToPerlPath $inputDir
        "output_dir" = Convert-ToPerlPath $outputDir
    }

    $stagedInputs = @{}
    if ($Payload.ContainsKey("input_files")) {
        $inputFiles = $Payload.input_files
        foreach ($alias in $inputFiles.Keys) {
            $sourcePath = [string]$inputFiles[$alias]
            if (-not (Test-Path -LiteralPath $sourcePath)) {
                throw "Input file does not exist: $sourcePath"
            }

            $safeAlias = Convert-ToAsciiSafeName -Name ([string]$alias) -Fallback "input"
            $extension = [System.IO.Path]::GetExtension($sourcePath)
            if ($extension -match '[^\x00-\x7F]') {
                throw "Input file extension must contain ASCII characters only for MaterialsScript staging: $extension"
            }
            $stagedPath = Join-Path $inputDir ($safeAlias + $extension)
            Assert-AsciiAbsolutePath -Path $stagedPath -Label "Staged input path"
            Copy-Item -LiteralPath $sourcePath -Destination $stagedPath -Force
            $templateVars["input.$alias"] = Convert-ToPerlPath $stagedPath
            $stagedInputs[$alias] = [pscustomobject]@{
                source_path = $sourcePath
                staged_path = $stagedPath
            }
        }
    }

    $requestedOutputs = @{}
    if ($Payload.ContainsKey("output_files")) {
        $outputFiles = $Payload.output_files
        foreach ($alias in $outputFiles.Keys) {
            $entry = $outputFiles[$alias]
            $relativePath = $null
            $destinationPath = $null

            if ($entry -is [string]) {
                $relativePath = [string]$entry
            } else {
                if ($entry.ContainsKey("relative_path")) {
                    $relativePath = [string]$entry.relative_path
                }
                if ($entry.ContainsKey("destination_path")) {
                    $destinationPath = [string]$entry.destination_path
                }
            }

            if ([string]::IsNullOrWhiteSpace($relativePath)) {
                throw "Each output_files entry must define relative_path."
            }

            if ([System.IO.Path]::IsPathRooted($relativePath)) {
                throw "Each output_files relative_path must be relative to the job directory."
            }
            if ($relativePath -match '[^\x00-\x7F]') {
                throw "Each output_files relative_path must contain ASCII characters only."
            }
            $fullOutputPath = [System.IO.Path]::GetFullPath((Join-Path $outputDir $relativePath))
            $jobRootPrefix = [System.IO.Path]::GetFullPath($jobDir).TrimEnd('\') + '\'
            if (-not $fullOutputPath.StartsWith($jobRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Each output_files relative_path must stay inside the MaterialsScript job directory."
            }
            Assert-AsciiAbsolutePath -Path $fullOutputPath -Label "Staged output path"
            $parent = Split-Path -Parent $fullOutputPath
            if ($parent) {
                Ensure-Directory -Path $parent
            }

            $templateVars["output.$alias"] = Convert-ToPerlPath $fullOutputPath
            $requestedOutputs[$alias] = [pscustomobject]@{
                relative_path     = $relativePath
                full_output_path  = $fullOutputPath
                destination_path  = $destinationPath
            }
        }
    }

    $renderedScript = Expand-Template -Text $scriptTemplate -Variables $templateVars
    Assert-NoUnicodeAbsolutePathLiteral -Text $renderedScript
    $scriptPath = Join-Path $jobDir ($jobName + ".pl")
    Write-Utf8NoBom -Path $scriptPath -Content $renderedScript

    $arguments = @()
    if ($runMode -eq "project") {
        $arguments += "-project"
    } else {
        $arguments += "-flat"
    }
    $arguments += $jobName

    $exitCode = 0
    Push-Location $jobDir
    try {
        & $paths.run_mat_script @arguments
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    $stdoutPath = $scriptPath + ".out"
    $logPath = Join-Path $jobDir ($jobName + "MatStudioLog.htm")
    $stdoutText = if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw -Encoding utf8 } else { "" }
    $logHtml = if (Test-Path -LiteralPath $logPath) { Get-Content -LiteralPath $logPath -Raw -Encoding utf8 } else { "" }
    $logText = Convert-HtmlToText -Html $logHtml

    $copiedOutputs = @{}
    foreach ($alias in $requestedOutputs.Keys) {
        $entry = $requestedOutputs[$alias]
        $exists = Test-Path -LiteralPath $entry.full_output_path
        $copiedTo = $null
        if ($exists -and $entry.destination_path) {
            $destinationParent = Split-Path -Parent $entry.destination_path
            if ($destinationParent) {
                Ensure-Directory -Path $destinationParent
            }
            Copy-Item -LiteralPath $entry.full_output_path -Destination $entry.destination_path -Force
            $copiedTo = $entry.destination_path
        }

        $copiedOutputs[$alias] = [pscustomobject]@{
            exists            = $exists
            full_output_path  = $entry.full_output_path
            destination_path  = $entry.destination_path
            copied_to         = $copiedTo
        }
    }

    $success = $false
    if ($logText -match "Exiting MatServer: status OK\." -and $logText -match "Completion status: \(OK\)") {
        $success = $true
    }
    if (-not [string]::IsNullOrWhiteSpace($stdoutText)) {
        $success = $false
    }

    $data = [pscustomobject]@{
        success             = $success
        job_id              = $jobId
        job_name            = $jobName
        run_mode            = $runMode
        job_dir             = if ($keepJobDir) { $jobDir } else { $null }
        input_dir           = if ($keepJobDir) { $inputDir } else { $null }
        output_dir          = if ($keepJobDir) { $outputDir } else { $null }
        rendered_script_path = $scriptPath
        staged_inputs       = $stagedInputs
        outputs             = $copiedOutputs
        run_mat_script_exit_code = $exitCode
        stdout_path         = if (Test-Path -LiteralPath $stdoutPath) { $stdoutPath } else { $null }
        stdout_text         = $stdoutText
        matstudio_log_path  = if (Test-Path -LiteralPath $logPath) { $logPath } else { $null }
        matstudio_log_excerpt = if ($logText.Length -gt 2000) { $logText.Substring(0, 2000) } else { $logText }
    }

    if (-not $keepJobDir -and $success) {
        Remove-Item -Recurse -Force $inputDir,$jobDir -ErrorAction SilentlyContinue
        $data.job_dir = $null
        $data.input_dir = $null
        $data.output_dir = $null
    }

    return $data
}

try {
    $payload = Read-InputPayload

    switch ($Action) {
        "detect" {
            $paths = Get-MSPaths
            Write-Output (Write-Json (New-Response -Ok $true -Data $paths))
        }
        "search-help" {
            $query = [string]$payload.query
            $maxResults = if ($payload.ContainsKey("max_results")) { [int]$payload.max_results } else { 10 }
            $data = [pscustomobject]@{
                query   = $query
                results = @(Search-Help -Query $query -MaxResults $maxResults)
            }
            Write-Output (Write-Json (New-Response -Ok $true -Data $data))
        }
        "list-examples" {
            $pattern = if ($payload.ContainsKey("pattern")) { [string]$payload.pattern } else { "*.xsd" }
            $maxResults = if ($payload.ContainsKey("max_results")) { [int]$payload.max_results } else { 50 }
            $data = [pscustomobject]@{
                pattern = $pattern
                results = @(List-Examples -Pattern $pattern -MaxResults $maxResults)
            }
            Write-Output (Write-Json (New-Response -Ok $true -Data $data))
        }
        "scan-workspace" {
            $patterns = if ($payload.ContainsKey("patterns")) { [string[]]$payload.patterns } else { @("*.xsd", "*.xtd", "*.stp") }
            $maxResults = if ($payload.ContainsKey("max_results")) { [int]$payload.max_results } else { 200 }
            $data = [pscustomobject]@{
                root_dir = [string]$payload.root_dir
                results  = @(Scan-Workspace -RootDir ([string]$payload.root_dir) -Patterns $patterns -MaxResults $maxResults)
            }
            Write-Output (Write-Json (New-Response -Ok $true -Data $data))
        }
        "inspect-document" {
            $data = Get-DocumentSummary -Path ([string]$payload.path)
            Write-Output (Write-Json (New-Response -Ok $true -Data $data))
        }
        "read-help-page" {
            $data = Read-HelpPage -Path ([string]$payload.path)
            Write-Output (Write-Json (New-Response -Ok $true -Data $data))
        }
        "find-code-examples" {
            $query = [string]$payload.query
            $maxResults = if ($payload.ContainsKey("max_results")) { [int]$payload.max_results } else { 8 }
            $data = [pscustomobject]@{
                query   = $query
                results = @(Find-CodeExamples -Query $query -MaxResults $maxResults)
            }
            Write-Output (Write-Json (New-Response -Ok $true -Data $data))
        }
        "run-script" {
            $data = Invoke-MaterialsScriptJob -Payload $payload
            Write-Output (Write-Json (New-Response -Ok $true -Data $data))
        }
        default {
            throw "Unsupported action: $Action"
        }
    }
} catch {
    $message = $_.Exception.Message
    Write-Output (Write-Json (New-Response -Ok $false -Error $message))
    exit 1
}
