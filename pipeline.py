import requests
import argparse
import logging
import sys

# Get the access token
# Managed Identity
# https://docs.microsoft.com/en-us/azure/active-directory/managed-identities-azure-resources/overview
# managed service identity requiries the following permissions:
# Synapse Contributor
# Synapse Compute Operator
# Synapse Credential User



if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )    

    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline_name", type=str, help="Name of the pipeline to start")
    parser.add_argument("--synapse_workspace", type=str, help="Name of the Synapse workspace")
    args = parser.parse_args()

    pipeline_name = args.pipeline_name
    synapse_workspace = args.synapse_workspace



    msi_endpoint = "http://169.254.169.254/metadata/identity/oauth2/token"
    msi_headers = {"Metadata": "true"}
    msi_params = {
        "resource": "https://dev.azuresynapse.net",
        "api-version": "2019-08-01"
    }

    response = requests.get(msi_endpoint, headers=msi_headers, params=msi_params)

    if response.status_code == 200:
        print("Token obtained successfully")
        token = response.json()
    else:
        print(f"Failed to obtain token: {response.status_code}")
        print(response.text)

    access_token = response.json()["access_token"]
    # Start the pipeline
    synapse_url = f"https://{synapse_workspace}.dev.azuresynapse.net"
    start_pipeline_url = f"{synapse_url}/pipelines/{pipeline_name}/createRun?api-version=2020-12-01"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    response = requests.post(start_pipeline_url, headers=headers)

    if response.status_code == 202:
        logging.info("Pipeline started successfully.")
    else:
        logging.error(f"Failed to start pipeline. Status code: {response.status_code}") 
        logging.error(response.text)