# Remote deployment examples

These files are deployment starting points, not automatic installers. Replace hostnames, paths, and
service identities before use.

The recommended topology is:

```text
Internet -> TLS reverse proxy / WAF / tunnel -> 127.0.0.1:8765 -> EvoPi Remote Gateway
```

Initialize the local Host and start the loopback Gateway:

```bash
evopi remote init default --workspace /absolute/workspace
evopi remote serve default --proxy --bind 127.0.0.1 --port 8765 \
  --allowed-host agent.example.com --trusted-proxy 127.0.0.0/8
```

When a reverse proxy runs on another address, replace the trusted proxy CIDR precisely. Never trust
all forwarded headers. Caddy, Nginx, Cloudflare Tunnel, systemd, and Task Scheduler do not change
EvoPi scopes or Policy decisions; they only operate the transport and process.
