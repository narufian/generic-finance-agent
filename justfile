set shell := ["bash", "-uc"]

host := "127.0.0.1"
port := "3000"
pid_file := ".dev-server.pid"
log_file := ".dev-server.log"

# Show available commands.
default:
    @just --list

# Run the frontend and API together in the foreground with auto-reload.
dev:
    uv run uvicorn finance_agent.api:app --app-dir backend --host {{host}} --port {{port}} --reload

# The frontend is served by the API process; this is an explicit convenience alias.
frontend:
    @just dev

# Start the frontend and API together in the background.
start:
    @if curl --fail --silent --output /dev/null "http://{{host}}:{{port}}/"; then \
        echo "Generic Finance Agent is already running at http://{{host}}:{{port}}"; \
        exit 0; \
    fi; \
    if lsof -tiTCP:{{port}} -sTCP:LISTEN >/dev/null 2>&1; then \
        echo "Port {{port}} is occupied but not responding; run 'just restart'"; \
        exit 1; \
    fi; \
    nohup uv run uvicorn finance_agent.api:app --app-dir backend --host {{host}} --port {{port}} > "{{log_file}}" 2>&1 & \
    pid=$!; \
    echo "$pid" > "{{pid_file}}"; \
    for _ in $(seq 1 50); do \
        if curl --fail --silent --output /dev/null "http://{{host}}:{{port}}/"; then \
            echo "Generic Finance Agent started at http://{{host}}:{{port}} (PID $pid)"; \
            exit 0; \
        fi; \
        sleep 0.1; \
    done; \
    echo "Server failed to start; see {{log_file}}"; \
    exit 1

# Stop the frontend and API process listening on the development port.
stop:
    @pids=$(lsof -tiTCP:{{port}} -sTCP:LISTEN 2>/dev/null || true); \
    if [ -z "$pids" ]; then \
        rm -f "{{pid_file}}"; \
        echo "Generic Finance Agent is not running"; \
        exit 0; \
    fi; \
    if ! kill $pids; then \
        echo "Could not stop the process on port {{port}}"; \
        exit 1; \
    fi; \
    rm -f "{{pid_file}}"; \
    echo "Generic Finance Agent stopped"

# Stop and then start the frontend and API.
restart: stop start

# Check whether the development server is responding.
status:
    @if curl --fail --silent --output /dev/null "http://{{host}}:{{port}}/"; then \
        echo "Generic Finance Agent is running at http://{{host}}:{{port}}"; \
    else \
        echo "Generic Finance Agent is not running"; \
        exit 1; \
    fi

# Follow output from a background server started with `just start`.
logs:
    @touch "{{log_file}}"
    tail -f "{{log_file}}"

# Run the test suite.
test:
    uv run pytest -q
