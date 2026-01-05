# Modalflow

A serverless Airflow Executor for Modal.

## Usage

### Prerequisites
- pip
- A Modal account, workspace, and environment
- Modal CLI configured to your workspace

### Steps

1. `pip install modalflow` on your local machine, and add it to your Airflow cluster.
2. Run `modalflow deploy --env {environment name}` to deploy the resources into your Modal environment.
3. Add `modalflow.executor.ModalExecutor` to your Airflow config

## Networking Configuration

### Local Development

ModalExecutor automatically detects when you're running Airflow locally and creates a tunnel to your local Airflow instance (localhost:8080). No additional configuration needed.

The executor will:
- Detect if localhost:8080 is accessible
- Create a Modal tunnel to expose your local Airflow API
- Pass the tunnel URL to Modal Functions so they can "phone home"

### Production / VPC Deployments

If your Airflow deployment is in a VPC or behind a firewall, you need to configure the execution API URL so Modal Functions can reach it.

**Option 1: Environment Variable (Recommended)**

Set the execution API URL as an environment variable:

```bash
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL=https://your-airflow-api.example.com/execution/
```

**Option 2: Airflow Config**

Set in `airflow.cfg`:

```ini
[core]
execution_api_server_url = https://your-airflow-api.example.com/execution/
```

**VPC Setup Considerations**

If Airflow is deployed in a VPC, ensure:

- **Public Endpoint**: Airflow API must be accessible via a public endpoint. Options include:
  - Application Load Balancer (ALB) in public subnets
  - API Gateway in front of Airflow
  - Reverse tunnel (ngrok, Cloudflare Tunnel, AWS IoT Secure Tunneling)
  
- **Security**: Use authentication/authorization to protect the endpoint:
  - API keys or bearer tokens
  - Security groups restricting access
  - VPN or private networking (if Modal supports VPC peering)

- **Network Access**: Modal Functions run in Modal's cloud infrastructure and can reach public internet endpoints. Ensure:
  - No firewall rules blocking Modal's IP ranges
  - The endpoint is reachable from the internet (not just internal VPC)

**Example: Using Reverse Tunnel**

If you can't expose Airflow directly, use a reverse tunnel:

1. Set up ngrok or similar: `ngrok http 8080`
2. Configure the executor with the ngrok URL:
   ```bash
   export AIRFLOW__CORE__EXECUTION_API_SERVER_URL=https://abc123.ngrok.io/execution/
   ```

## Development

We use `uv` for development. To setup:

1. `cd modalflow`
2. `uv sync`
3. To run the CLI, use: `uv run -- modalflow [COMMAND]`
4. To run unit tests, use: `uv run pytest`