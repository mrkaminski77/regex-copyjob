azcopy login --identity
$destemail= 'david.leyden@serco-ap.com;craig.cavanagh@serco-ap.com'
$sendemail= 'svc.IBMMaximo@serco-ap.com'

function download  {
	$source='sragpstondia'
	
	$ndia = (azcopy list "https://$source.blob.core.windows.net/upload" | Select-String "Content Length").Count
	azcopy copy "https://$source.blob.core.windows.net/upload/*" "E:\azcopy"
	$copy=(Get-ChildItem "E:\azcopy" | Measure-Object).count

	if ($ndia -eq $copy) {
		write-host "Files copied to local disk successfully"
		azcopy rm  "https://$source.blob.core.windows.net/upload/*" --recursive=false
	}
	else {
		Write-host "Files copied to local disk not successful"
		Send-MailMessage -To $destemail -from $sendemail -subject "storage file are not deleted $(get-date -format dd-MM-yyyy)" -Priority high -BodyAsHtml -body "files from storage account are not deleted<br />" -Port 25 -SmtpServer smtpgw.ap.serco.com
	}
	
}

function rename {
	Get-ChildItem -Path "E:\azcopy" -Recurse -Include "*%3A*.jso*" | Rename-Item -NewName { $_.Name -replace "%3A","_"}
	
}
function upload {

	$destination='sra1pstagdatatemp'
	$sub=(-2)
	#$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/agentevents/$((Get-Date).ToUniversalTime().ToString('yyyy/MM/dd'))"
	$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/agentevents"
	azcopy copy "E:\azcopy\*" $destinationPath --include-pattern ncc-agent-event*

	#$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/contacts/$((Get-Date).ToUniversalTime().ToString('yyyy/MM/dd'))"
	$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/contacts"
	azcopy copy "E:\azcopy\*" $destinationPath --include-pattern ncc-contact-trace*

	#$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/eval/$((Get-Date).ToUniversalTime().ToString('yyyy/MM/dd'))"
	$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/eval"
	azcopy copy "E:\azcopy\*" $destinationPath --include-pattern *[0-9]*_DELETED.jso*

	#$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/eval/$((Get-Date).ToUniversalTime().ToString('yyyy/MM/dd'))"
	$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/eval"
	azcopy copy "E:\azcopy\*" $destinationPath --include-pattern *[0-9].jso*

	#$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/metrics/$((Get-Date).ToUniversalTime().ToString('yyyy/MM/dd'))"
	$destinationPath = "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/metrics"
	azcopy copy "E:\azcopy\*" $destinationPath --include-pattern *ocs-ncc-*.jso*
	
	$agent= (azcopy list "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/agentevents" | Select-String "Content Length").Count
	$contacts= (azcopy list "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/contacts" | Select-String "Content Length").Count
	$eval= (azcopy list "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/eval" | Select-String "Content Length").Count
	$metrics= (azcopy list "https://$destination.blob.core.windows.net/json/amazonConnect/ndia/metrics" | Select-String "Content Length").Count
	$copy=(Get-ChildItem "E:\azcopy" | Measure-Object).count
	$total = ($agent) + ($contacts) + ($eval) + ($metrics)

	if ($total -eq $copy) {
		write-host "Files are uploaded to destination storage account"
		
	}
	else {
		Write-host "Files are not uploaded to destination storage account"
		#Send-MailMessage -To $destemail -from $sendemail -subject "File Upload ISSUE $(get-date -format dd-MM-yyyy)" -Priority high -BodyAsHtml -body "File upload experienced an issue downloaded $copy files but uploaded files are $total<br />" -Port 25 -SmtpServer smtpgw.ap.serco.com
	}
	
	
}
function backup {

	7z a -t7z "E:\archives\ndia\Backup_$([datetime]::Now.ToString("yyyyMMdd")).7z" "E:\azcopy\*" | Set-Variable out
	$ok = $out -like '*Everything is Ok*'
	if ($ok) {
		write-host "Zip completed"
		Get-ChildItem -Path E:\azcopy\  | ForEach-Object { $_.Delete()}
		
	}
	else {
		write-host "Zip not completed successfully" 
		Send-MailMessage -To $destemail -from $sendemail -subject "Zip failed $(get-date -format dd-MM-yyyy)" -Priority high -BodyAsHtml -body "Zip Operation failed Files are not deleted from downloads folder<br />" -Port 25 -SmtpServer smtpgw.ap.serco.com
	}
	Get-ChildItem "E:\archives\ndia" -Recurse -File | Where-Object CreationTime -lt  (Get-Date).AddDays(-7)  | Remove-Item -Force	
	
}



download
rename
upload
backup



azcopy logout