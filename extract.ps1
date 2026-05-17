$synapse_workspace = "sra1paswgdata"
$pipeline = "ndiaDaily"

# Run the Python script with redirected output
Start-Process -FilePath "python" -ArgumentList ".\ndia-downloader.py" -NoNewWindow -Wait

$arguments = @(
    "pipeline.py",
    "--synapse_workspace", $synapse_workspace,
    "--pipeline", $pipeline
)

Start-Process -FilePath "python" -ArgumentList $arguments -NoNewWindow -Wait

