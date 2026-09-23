[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

function Log($msg) {
	Write-Host "[STEP7] $msg"
}

function Fail($msg) {
	Write-Host "ERROR: $msg"
	exit 1
}

function Test-HttpUrl([string]$value) {
	if ([string]::IsNullOrWhiteSpace($value)) { return $false }
	$uri = $null
	if (![System.Uri]::TryCreate($value, [System.UriKind]::Absolute, [ref]$uri)) { return $false }
	return ($uri.Scheme -eq "http" -or $uri.Scheme -eq "https")
}

function Test-EmailAddress([string]$value) {
	if ([string]::IsNullOrWhiteSpace($value)) { return $false }
	$trimmed = $value.Trim()
	try {
		$mailAddress = New-Object -TypeName System.Net.Mail.MailAddress -ArgumentList $trimmed
		return $mailAddress.Address -eq $trimmed
	}
	catch {
		return $false
	}
}

function Get-ValidatedRecipients([string]$rawValue) {
	if ([string]::IsNullOrWhiteSpace($rawValue)) { return @() }

	$seen = @{}
	$recipients = New-Object System.Collections.Generic.List[string]
	foreach ($candidate in ($rawValue -split ",")) {
		$address = $candidate.Trim()
		if ([string]::IsNullOrWhiteSpace($address)) { continue }
		if (!(Test-EmailAddress -value $address)) {
			throw "OSS_MAIL_TO contains invalid email address: $address"
		}

		$key = $address.ToLowerInvariant()
		if (!$seen.ContainsKey($key)) {
			$seen[$key] = $true
			$recipients.Add($address)
		}
	}

	return $recipients.ToArray()
}

function Resolve-InputFile([string]$name) {
	$workspaceRoot = Split-Path $PSScriptRoot -Parent
	$runtimeRoot = Join-Path $workspaceRoot "jenkins_runtime"
	$candidates = @(
		(Join-Path $PWD $name),
		(Join-Path $runtimeRoot $name),
		(Join-Path (Join-Path $runtimeRoot "manifests") $name),
		(Join-Path $workspaceRoot $name)
	)
	foreach ($p in $candidates) {
		if (Test-Path $p) {
			return (Resolve-Path $p).Path
		}
	}
	return $null
}

function Get-YamlSimpleValue([string]$rawYaml, [string]$keyName) {
	$pattern = "(?m)^\s*$([Regex]::Escape($keyName))\s*:\s*(.+?)\s*$"
	$m = [Regex]::Match($rawYaml, $pattern)
	if ($m.Success) {
		return $m.Groups[1].Value.Trim().Trim('"').Trim("'")
	}
	return ""
}

function Get-YamlAllValues([string]$rawYaml, [string]$keyName) {
	$pattern = "(?m)^\s*$([Regex]::Escape($keyName))\s*:\s*(.+?)\s*$"
	$allMatches = [Regex]::Matches($rawYaml, $pattern)
	$vals = @()
	foreach ($m in $allMatches) {
		$vals += $m.Groups[1].Value.Trim().Trim('"').Trim("'")
	}
	return $vals
}

function Ensure-JiraPrefixedVersion([string]$ticket, [string]$versionName) {
	if ([string]::IsNullOrWhiteSpace($versionName)) { return $versionName }
	if ($versionName -match '^\[OSS-\d+\]') { return $versionName }
	return "[$ticket]$versionName"
}

function Get-BlackDuckVersionComponentsUrl([string]$bdUrl, [string]$bdToken, [string]$projectName, [string]$versionName) {
    if ([string]::IsNullOrWhiteSpace($bdUrl) -or [string]::IsNullOrWhiteSpace($bdToken)) {
        return $null
    }

    $base = $bdUrl.TrimEnd("/")
    $headers = @{
        Authorization = "token $bdToken"
        Accept = "application/json"
    }

    # PS 5.1 호환 SSL 인증서 검증 우회
    try {
        Add-Type -TypeDefinition @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAll : ICertificatePolicy {
    public bool CheckValidationResult(ServicePoint sp, X509Certificate cert, WebRequest req, int problem) { return true; }
}
"@ -ErrorAction SilentlyContinue
        [System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAll
    } catch {}
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

    try {
        $projectQuery = [Uri]::EscapeDataString("name:$projectName")
        $projectUri = "$base/api/projects?q=$projectQuery&limit=200"
        $projectResp = Invoke-RestMethod -Method Get -Uri $projectUri -Headers $headers
        
        $project = $projectResp.items | Where-Object { $_.name -eq $projectName } | Select-Object -First 1
        if ($null -eq $project) {
            return $null
        }

        $versionsUri = "$($project._meta.href)?limit=300"
        if ($versionsUri -notmatch "/versions") {
            $versionsUri = "$($project._meta.href)/versions?limit=300"
        }

        $versionResp = Invoke-RestMethod -Method Get -Uri $versionsUri -Headers $headers
        
        $version = $versionResp.items | Where-Object { $_.versionName -eq $versionName -or $_.name -eq $versionName } | Select-Object -First 1
        if ($null -eq $version) {
            return $null
        }

        $href = $version._meta.href
        $m = [Regex]::Match($href, "/api/projects/([^/]+)/versions/([^/?]+)")
        if ($m.Success) {
			return "$base/api/projects/$($m.Groups[1].Value)/versions/$($m.Groups[2].Value)/components"
        }
        return "$base/ui/projects"
    }
    catch {
        Log "WARN: Black Duck version lookup failed for '$projectName / $versionName' - $($_.Exception.Message)"
        return $null
    }
}

function Get-BlackDuckEntriesFromSummary($summary, [array]$fallbackProjects, [array]$fallbackVersions) {
	$entries = @()
	$units = @($summary.units)
	for ($i = 0; $i -lt $units.Count; $i++) {
		$unit = $units[$i]
		if ($null -eq $unit) { continue }
		if (([string]$unit.status).Trim().ToUpper() -ne "SUCCESS") { continue }

		$project = [string]$unit.blackduck_project
		if ([string]::IsNullOrWhiteSpace($project) -and $i -lt $fallbackProjects.Count) {
			$project = [string]$fallbackProjects[$i]
		}

		$version = [string]$unit.blackduck_version
		if ([string]::IsNullOrWhiteSpace($version) -and $i -lt $fallbackVersions.Count) {
			$version = [string]$fallbackVersions[$i]
		}

		$bomUrl = [string]$unit.blackduck_bom_url
		$componentsUrl = $bomUrl
		if (!(Test-HttpUrl -value $componentsUrl)) {
			$componentsUrl = [string]$unit.blackduck_ui_url
		}

		$entries += [PSCustomObject]@{
			Unit = [string]$unit.unit
			Project = $project
			Version = $version
			Url = $componentsUrl
		}
	}

	return $entries
}

function Build-BlackDuckButtonsHtml([array]$entries) {
	$effective = @($entries)
	if ($effective.Count -eq 0) {
		return ""
	}

	$buttonBlocks = @()
	foreach ($entry in $effective) {
		$label = HtmlEncode([string]$entry.Label)
		$url = HtmlEncode([string]$entry.Url)
		$buttonBlocks += @"
<td align="center" style="padding: 0 10px;">
    <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="220" style="table-layout: fixed; width: 220px;">
        <tr>
            <td align="center" style="background-color: #FF8300; border-radius: 4px; padding: 12px 10px;">
                <a href="$url" target="_blank"
                    style="font-family: Arial, sans-serif; font-size: 14px; color: #ffffff; text-decoration: none; display: block; width: 100%; border-radius: 4px; font-weight: bold; text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                    $label
                </a>
            </td>
        </tr>
    </table>
</td>
"@
	}

	return ($buttonBlocks -join "`r`n")
}

function HtmlEncode([string]$s) {
	if ($null -eq $s) { return "" }
	return [System.Net.WebUtility]::HtmlEncode($s)
}

function Replace-Placeholder([string]$html, [string]$placeholder, [string]$value) {
	if ($null -eq $html) { return "" }
	if ([string]::IsNullOrWhiteSpace($placeholder)) { return $html }
	if ($null -eq $value) { $value = "" }
	return $html.Replace($placeholder, [string]$value)
}

function Replace-InfoRowValue([string]$html, [string]$label, [string]$newValue) {
	$escapedLabel = [Regex]::Escape($label)
	$pattern = "(<td[^>]*>\s*$escapedLabel\s*</td>\s*<td[^>]*>)(.*?)(</td>)"
	$encoded = [System.Net.WebUtility]::HtmlEncode($newValue)
	return [Regex]::Replace(
		$html,
		$pattern,
		{ param($m) $m.Groups[1].Value + $encoded + $m.Groups[3].Value },
		[System.Text.RegularExpressions.RegexOptions]::Singleline
	)
}

function Replace-LinkHref([string]$html, [string]$buttonText, [string]$newHref) {
	$escapedText = [Regex]::Escape($buttonText)
	$pattern = '(<a\s+href=")([^"]*)("[^>]*>\s*' + $escapedText + '\s*</a>)'
	return [Regex]::Replace(
		$html,
		$pattern,
		{ param($m) $m.Groups[1].Value + $newHref + $m.Groups[3].Value },
		[System.Text.RegularExpressions.RegexOptions]::Singleline
	)
}

function Get-PipelineStatus($summary) {
	$override = [Environment]::GetEnvironmentVariable("PIPELINE_STATUS")
	if ([string]::IsNullOrWhiteSpace($override)) {
		$summaryStatus = ([string]$summary.status).Trim().ToUpper()
		if ($summaryStatus -eq "SUCCESS") { return "SUCCESS" }
		if ($summaryStatus -eq "FAILED" -or $summaryStatus -eq "FAILURE" -or $summaryStatus -eq "FAIL") { return "FAIL" }
		return "UNKNOWN"
	}

	$normalized = $override.Trim().ToUpper()
	if ($normalized -eq "FAILURE") { return "FAIL" }
	if ($normalized -eq "FAILED") { return "FAIL" }
	return $normalized
}

function Get-BlackDuckStatus($summary) {
	$units = @($summary.units)
	if ($units.Count -eq 0) {
		if ((($summary.status + "").ToUpper()) -eq "SUCCESS") {
			return "SUCCESS"
		}
		return "FAILED"
	}

	$successCount = @($units | Where-Object { (($_.status + "").ToUpper()) -eq "SUCCESS" }).Count
	$failedCount = $units.Count - $successCount

	if ($failedCount -eq 0) { return "SUCCESS" }
	if ($successCount -eq 0) { return "FAILED" }
	return "UNSTABLE"
}

function Get-StatusDetailLines($summary) {
	$lines = @()
	foreach ($unit in @($summary.units)) {
		$unitName = [string]$unit.unit
		$unitStatus = [string]$unit.status
		$stage = [string]$unit.stage
		if (($unitStatus + "").ToUpper() -eq "SUCCESS") {
			$lines += ("{0}: SUCCESS" -f $unitName)
		}
		elseif ([string]::IsNullOrWhiteSpace($stage)) {
			$lines += ("{0}: FAIL" -f $unitName)
		}
		else {
			$lines += ("{0}: FAIL({1})" -f $unitName, $stage)
		}
	}
	return $lines
}

function Build-MultilineHtml([array]$lines) {
	if ($null -eq $lines -or $lines.Count -eq 0) {
		return "N/A"
	}

	$encoded = @()
	foreach ($line in $lines) {
		$encoded += (HtmlEncode([string]$line))
	}
	return ($encoded -join "<br/>")
}

function Get-FailureLinesFromLog([string]$logPath) {
	if ([string]::IsNullOrWhiteSpace($logPath) -or !(Test-Path $logPath)) {
		return @()
	}

	$patterns = @(
		'Licensing issue',
		'FAILURE_BLACKDUCK_FEATURE_ERROR',
		'checkout command failed',
		'No valid scan paths found',
		'Failed to copy',
		'Missing environment variables',
		'Overall Status: FAILURE',
		'response was 402',
		'problem trying to POST',
		'DEBUG .* failed',
		'ERROR:'
	)

	$lines = Get-Content $logPath
	$failureMatches = New-Object System.Collections.Generic.List[string]
	foreach ($line in $lines) {
		if ([string]::IsNullOrWhiteSpace($line)) { continue }
		if ($line -match 'Automatically trusting server certificates') { continue }
		foreach ($pattern in $patterns) {
			if ($line -match $pattern) {
				if (-not $failureMatches.Contains($line.Trim())) {
					$failureMatches.Add($line.Trim())
				}
				break
			}
		}
	}

	if ($failureMatches.Count -gt 40) {
		return @($failureMatches.GetRange(0, 40))
	}
	return @($failureMatches)
}

function Get-ReasonText($summary, [array]$failureLines) {
	$failedUnits = @($summary.units | Where-Object { (($_.status + "").ToUpper()) -ne "SUCCESS" })
	if ($failedUnits.Count -eq 0) {
		return "No issues detected."
	}

	$primary = $failedUnits[0]
	$stageSuffix = ""
	if (-not [string]::IsNullOrWhiteSpace([string]$primary.stage)) {
		$stageSuffix = " at stage $($primary.stage)"
	}

	if ($failureLines.Count -gt 0) {
		return "$($primary.unit) failed$stageSuffix. $($failureLines[0])"
	}

	return "$($primary.unit) failed$stageSuffix."
}

function Write-FailureAttachment([string]$outputPath, [string]$pipelineStatus, [string]$blackDuckStatus, [array]$detailLines, [string]$reasonText, [array]$failureLines) {
	$content = @()
	$content += "Status(Pipeline): $pipelineStatus"
	$content += "Status(BlackDuck): $blackDuckStatus"
	$content += "Status Detail:"
	foreach ($line in $detailLines) {
		$content += (" - " + $line)
	}
	$content += (" - Reason: " + $reasonText)
	$content += ""
	$content += "Relevant Log:"
	if ($failureLines.Count -eq 0) {
		$content += " - No filtered failure lines found."
	}
	else {
		foreach ($line in $failureLines) {
			$content += (" - " + $line)
		}
	}

	Set-Content -Path $outputPath -Value $content -Encoding UTF8
}

$jiraPath = Resolve-InputFile "jira_key.txt"
$manifestPathFile = Resolve-InputFile "manifest_path.txt"
$summaryPath = Resolve-InputFile "last_run_summary.json"
$templatePath = Join-Path $PSScriptRoot "SendMail\main.html"

if ([string]::IsNullOrWhiteSpace($jiraPath)) { Fail "jira_key.txt not found" }
if ([string]::IsNullOrWhiteSpace($manifestPathFile)) { Fail "manifest_path.txt not found" }
if ([string]::IsNullOrWhiteSpace($summaryPath)) { Fail "last_run_summary.json not found" }
if ([string]::IsNullOrWhiteSpace($templatePath)) { Fail "mail template not found" }

$jiraTicket = (Get-Content $jiraPath -Raw).Trim().ToUpper()
if ([string]::IsNullOrWhiteSpace($jiraTicket)) { Fail "jira ticket is empty" }

$manifestRealPath = (Get-Content $manifestPathFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($manifestRealPath)) { Fail "manifest_path.txt is empty" }
if (!(Test-Path $manifestRealPath)) { Fail "manifest yaml not found: $manifestRealPath" }

$manifestRaw = Get-Content $manifestRealPath -Raw
$oem = Get-YamlSimpleValue -rawYaml $manifestRaw -keyName "oem"
if ([string]::IsNullOrWhiteSpace($oem)) { $oem = "N/A" }

$bdProjects = Get-YamlAllValues -rawYaml $manifestRaw -keyName "blackduck_project"
$bdVersionsRaw = Get-YamlAllValues -rawYaml $manifestRaw -keyName "blackduck_version"
$bdVersions = @()
foreach ($v in $bdVersionsRaw) {
	$bdVersions += (Ensure-JiraPrefixedVersion -ticket $jiraTicket -versionName $v)
}

$summary = Get-Content $summaryPath -Raw | ConvertFrom-Json
$pipelineStatus = Get-PipelineStatus -summary $summary
$blackDuckStatus = Get-BlackDuckStatus -summary $summary
$statusDetailLines = Get-StatusDetailLines -summary $summary

$jiraIssueUrl = "https://jira.example.com/browse/$jiraTicket"

$bdUrl = [Environment]::GetEnvironmentVariable("BLACKDUCK_URL")
if ([string]::IsNullOrWhiteSpace($bdUrl)) { $bdUrl = "https://192.0.2.10" }
$bdToken = [Environment]::GetEnvironmentVariable("BLACKDUCK_TOKEN")

$ossBuildLogPath = Resolve-InputFile "oss_build.log"
$failureLines = Get-FailureLinesFromLog -logPath $ossBuildLogPath
$reasonText = Get-ReasonText -summary $summary -failureLines $failureLines

# Unit results are authoritative. The API is only a compatibility fallback for older summaries.
$summaryEntries = Get-BlackDuckEntriesFromSummary -summary $summary -fallbackProjects $bdProjects -fallbackVersions $bdVersions
$blackduckEntries = @()

foreach ($entry in $summaryEntries) {
	$p = [string]$entry.Project
	$v = [string]$entry.Version
	$label = if ([string]::IsNullOrWhiteSpace($v)) { [string]$entry.Unit } else { $v }
	$resolved = [string]$entry.Url
	if (!(Test-HttpUrl -value $resolved) -and ![string]::IsNullOrWhiteSpace($p) -and ![string]::IsNullOrWhiteSpace($v)) {
		$resolved = Get-BlackDuckVersionComponentsUrl -bdUrl $bdUrl -bdToken $bdToken -projectName $p -versionName $v
		if (Test-HttpUrl -value $resolved) {
			Log "Black Duck link resolved from API fallback: $p / $v -> $resolved"
		}
	}

	if (Test-HttpUrl -value $resolved) {
		$blackduckEntries += [PSCustomObject]@{ Label = $label; Url = $resolved }
		Log "Black Duck link resolved from unit summary: $([string]$entry.Unit) / $label -> $resolved"
	}
	else {
		Log "WARN: no Black Duck link available for successful unit '$([string]$entry.Unit) / $label'; omitting button"
	}
}
if ($blackduckEntries.Count -eq 0) {
	Log "INFO: no successful Black Duck unit links; omitting Black Duck buttons"
}

$blackduckButtonsHtml = Build-BlackDuckButtonsHtml -entries $blackduckEntries
$statusDetailHtml = Build-MultilineHtml -lines $statusDetailLines

$html = Get-Content $templatePath -Raw
$html = Replace-Placeholder -html $html -placeholder "{{PIPELINE_STATUS}}" -value (HtmlEncode $pipelineStatus)
$html = Replace-Placeholder -html $html -placeholder "{{BLACKDUCK_STATUS}}" -value (HtmlEncode $blackDuckStatus)
$html = Replace-Placeholder -html $html -placeholder "{{PROJECT_NAME}}" -value (HtmlEncode $oem)
$html = Replace-Placeholder -html $html -placeholder "{{JIRA_TICKET}}" -value (HtmlEncode $jiraTicket)
$html = Replace-Placeholder -html $html -placeholder "{{STATUS_DETAIL_HTML}}" -value $statusDetailHtml
$html = Replace-Placeholder -html $html -placeholder "{{STATUS_REASON}}" -value (HtmlEncode $reasonText)
$html = Replace-Placeholder -html $html -placeholder "{{JIRA_ISSUE_URL}}" -value (HtmlEncode $jiraIssueUrl)
if ($html.Contains("{{BLACKDUCK_BUTTONS}}")) {
	$html = $html.Replace("{{BLACKDUCK_BUTTONS}}", $blackduckButtonsHtml)
}
else {
	Log "WARN: template placeholder {{BLACKDUCK_BUTTONS}} not found. Keeping template as-is."
}

$templateDir = Split-Path -Path $templatePath -Parent
if ([string]::IsNullOrWhiteSpace($templateDir) -or !(Test-Path $templateDir)) {
	Fail "template directory not found: $templateDir"
}
$renderedPath = Join-Path $templateDir "mail.rendered.html"
$html | Set-Content -Path $renderedPath -Encoding UTF8
Log "rendered html saved: $renderedPath"

$failureAttachmentPath = $null
if (($blackDuckStatus -ne "SUCCESS") -or ($failureLines.Count -gt 0)) {
	$failureAttachmentPath = Join-Path $templateDir "log.txt"
	Write-FailureAttachment -outputPath $failureAttachmentPath -pipelineStatus $pipelineStatus -blackDuckStatus $blackDuckStatus -detailLines $statusDetailLines -reasonText $reasonText -failureLines $failureLines
	Log "failure summary attachment saved: $failureAttachmentPath"
}

$subject = "[OSS][Pipeline-$pipelineStatus][BlackDuck-$blackDuckStatus] $oem $jiraTicket"

# SMTP defaults for the Jenkins test folder mail route.
$smtpHost = "smtp.example.com"
$smtpPort = 25
$smtpUseSsl = $false

$smtpUser = [Environment]::GetEnvironmentVariable("SMTP_CREDENTIALS_USR")
$smtpPassword = [Environment]::GetEnvironmentVariable("SMTP_CREDENTIALS_PSW")

if ([string]::IsNullOrWhiteSpace($smtpUser)) { Fail "SMTP_CREDENTIALS username is empty" }
if ([string]::IsNullOrWhiteSpace($smtpPassword)) { Fail "SMTP_CREDENTIALS password is empty" }
if (!(Test-EmailAddress -value $smtpUser)) { Fail "SMTP_CREDENTIALS username must be an email address" }

$from = $smtpUser.Trim()
$rawRecipients = [Environment]::GetEnvironmentVariable("OSS_MAIL_TO")
if ([string]::IsNullOrWhiteSpace($rawRecipients)) {
	Fail "OSS_MAIL_TO is empty; configure it in the test folder Folder Properties"
}
try {
	$recipients = @(Get-ValidatedRecipients -rawValue $rawRecipients)
}
catch {
	Fail $_.Exception.Message
}
if ($recipients.Count -eq 0) { Fail "OSS_MAIL_TO contains no recipients" }

Log "SMTP target: ${smtpHost}:${smtpPort} (SSL=$smtpUseSsl)"
Log "SMTP credential detected; using credential username as sender"

try {
	$mail = New-Object System.Net.Mail.MailMessage
	$mail.From = $from
	foreach ($recipient in $recipients) {
		$mail.To.Add($recipient)
	}
	$mail.Subject = $subject
	$mail.Body = $html
	$mail.IsBodyHtml = $true

	if (![string]::IsNullOrWhiteSpace($failureAttachmentPath) -and (Test-Path $failureAttachmentPath)) {
		$mail.Attachments.Add((New-Object System.Net.Mail.Attachment($failureAttachmentPath)))
		Log "attached failure summary log.txt"
	}

	$smtp = New-Object System.Net.Mail.SmtpClient($smtpHost, $smtpPort)
	$smtp.EnableSsl = $smtpUseSsl
	$smtp.Credentials = New-Object System.Net.NetworkCredential($smtpUser, $smtpPassword)

	$smtp.Send($mail)
	Log "SUCCESS: mail sent"
	Log ("TO=" + ($recipients -join ",") + " / SUBJECT=$subject")
	exit 0
}
catch {
	Fail "failed to send email: $($_.Exception.Message)"
}
