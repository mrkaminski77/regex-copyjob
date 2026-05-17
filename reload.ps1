azcopy login --identity
$sub=(-3) 
$source='sragpstondia'
$ndia=(azcopy list "https://$source.blob.core.windows.net/upload" | measure).count
azcopy copy "https://$source.blob.core.windows.net/upload/*" "E:\temp"

$cal = $ndia + $sub
Write-host $cal
if ($cal -eq $copy) {
	write-host "Files copied to local disk successfully"
	azcopy rm  "https://$source.blob.core.windows.net/upload/*" --recursive=false
}



azcopy login --identity
$destination='sra1pstagdatatemp'
$destinationPath = "https://$destination.blob.core.windows.net/temp/amazonConnect/ndia/agentevents"
azcopy copy "E:\script\archive\Backup_20240817\*" $destinationPath --include-pattern ncc-agent-event*
$destinationPath = "https://$destination.blob.core.windows.net/temp/amazonConnect/ndia/contacts"
azcopy copy "E:\script\archive\Backup_20240817\*" $destinationPath --include-pattern ncc-contact-trace*
$destinationPath = "https://$destination.blob.core.windows.net/temp/amazonConnect/ndia/eval"
azcopy copy "E:\script\archive\Backup_20240817\*" $destinationPath --include-pattern *[0-9]*_DELETED.jso*
$destinationPath = "https://$destination.blob.core.windows.net/temp/amazonConnect/ndia/eval"
azcopy copy "E:\script\archive\Backup_20240817\*" $destinationPath --include-pattern *[0-9].jso*
$destinationPath = "https://$destination.blob.core.windows.net/temp/amazonConnect/ndia/metrics"
azcopy copy "E:\script\archive\Backup_20240817\*" $destinationPath --include-pattern *ocs-ncc-*.jso*