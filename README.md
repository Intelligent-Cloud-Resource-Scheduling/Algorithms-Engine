# Algorithms Engine – Cloud Resource Scheduler

FastAPI service that schedules cloud processes to VMs using a **Genetic Algorithm (GA)** and an **LPT (Longest Processing Time) greedy baseline**, then compares both results and POSTs them back to your backend.

---

## API Endpoints

### `POST /start-new`
Accepts a scheduling request, immediately returns an acknowledgement, and runs GA + LPT in the background.

**Request body:**
```json
{
  "session_uuid": "abc-123",
  "processes": [
    { "uuid": "p1", "duration": 120, "cores": 2, "memory": 4 },
    { "uuid": "p2", "duration": 60,  "cores": 1, "memory": 2 }
  ],
  "vms": [
    { "uuid": "vm1", "cores": 4, "memory": 8, "cost": 5 },
    { "uuid": "vm2", "cores": 2, "memory": 4, "cost": 2 }
  ]
}
```

**Immediate response:**
```json
{
  "session_uuid": "abc-123",
  "status": "queued",
  "message": "Scheduling algorithms (GA + LPT) have been started. Results will be POSTed to CALLBACK_SERVER_URL when ready.",
  "num_processes": 2,
  "num_vms": 2
}
```

---

### `GET /results/{session_uuid}`
Poll the status / retrieve completed results for a session.

**While running:**
```json
{ "session_uuid": "abc-123", "status": "running", "message": "Algorithms are still running." }
```

**When completed** — same rich payload that is also POSTed to `CALLBACK_SERVER_URL`.

---

### `GET /health`
Returns `{"status": "ok"}`.

---

## Callback Payload (POSTed to `CALLBACK_SERVER_URL`)

```json
{
  "session_uuid": "abc-123",
  "status": "completed",
  "ga_result": {
    "algorithm": "Genetic Algorithm (GA)",
    "vms": [
      {
        "vm_uuid": "vm1",
        "cores": 4,
        "memory": 8,
        "cost_per_second": 5,
        "assigned_processes": ["p1"],
        "process_details": [
          {
            "process_uuid": "p1",
            "duration_seconds": 120,
            "cores_required": 2,
            "memory_required": 4,
            "start_time_seconds": 0,
            "finish_time_seconds": 120
          }
        ],
        "runtime_seconds": 120.0,
        "total_cost": 600.0
      }
    ],
    "overall_cost": 720.0,
    "overall_runtime_seconds": 120.0,
    "average_waiting_time_seconds": 0.0,
    "feasible": true
  },
  "lpt_result": { "...same shape as ga_result..." },
  "comparison": {
    "overall_winner": "GA",
    "cost_comparison":    { "winner": "GA",  "ga": 720.0, "lpt": 780.0, "difference": -60.0 },
    "runtime_comparison": { "winner": "tie", "ga": 120.0, "lpt": 120.0, "difference":   0.0 },
    "average_waiting_time_comparison": { "winner": "LPT", "ga": 30.0, "lpt": 0.0, "difference": 30.0 }
  }
}
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `CALLBACK_SERVER_URL` | **Yes** | Full URL the engine POSTs results to (e.g. `http://backend:3000/api/scheduling/results`) |
| `HOST` | No | Bind address (default `0.0.0.0`) |
| `PORT` | No | Listen port (default `8000`) |

---

## Local Development

```bash
pip install -r requirements.txt
export CALLBACK_SERVER_URL=http://localhost:3000/api/scheduling/results
uvicorn server:app --reload --port 8000
```

---

## Docker (EC2 Deployment)

### Build & run locally
```bash
docker build -t algorithms-engine .

docker run -p 8000:8000 \
  -e CALLBACK_SERVER_URL=http://your-backend.example.com/api/scheduling/results \
  algorithms-engine
```

### EC2 Deployment Steps

1. **SSH into your EC2 instance** (Amazon Linux 2 / Ubuntu):

```bash
# Install Docker (Amazon Linux 2)
sudo yum update -y
sudo yum install -y docker
sudo service docker start
sudo usermod -aG docker ec2-user
```

2. **Copy files** (or clone your repo):

```bash
# From your local machine
scp -r -i your-key.pem ./Algorithms-Engine ec2-user@<EC2-PUBLIC-IP>:~/algorithms-engine
```

3. **Build and run on EC2**:

```bash
cd ~/algorithms-engine
docker build -t algorithms-engine .

docker run -d \
  --name algorithms-engine \
  --restart unless-stopped \
  -p 8000:8000 \
  -e CALLBACK_SERVER_URL=http://<BACKEND-IP-OR-DNS>/api/scheduling/results \
  algorithms-engine
```

4. **Open port 8000** in the EC2 Security Group (inbound TCP 8000 from your backend's IP or 0.0.0.0/0 for testing).

5. **Check logs**:
```bash
docker logs -f algorithms-engine
```

6. **Interactive API docs**: `http://<EC2-PUBLIC-IP>:8000/docs`