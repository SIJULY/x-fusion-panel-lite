#!/usr/bin/env bash
set -u

# X-Fusion Lite Probe Agent one-click installer.
# Usage:
#   curl -fsSL https://panel.example.com/static/x-install.sh | bash -s -- "TOKEN" "https://panel.example.com/api/probe/register" "1800"

TOKEN="${1:-}"
REGISTER_URL="${2:-}"
PUSH_INTERVAL="${3:-1800}"

AGENT_NAME="x-fusion-agent-lite"
AGENT_SCRIPT="/root/x_fusion_agent_lite.py"
LEGACY_AGENT_NAME="x-fusion-agent"
LEGACY_AGENT_SCRIPT="/root/x_fusion_agent.py"

log() { printf '\033[32m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[WARN]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[ERROR]\033[0m %s\n' "$*" >&2; }

if [ -z "$TOKEN" ] || [ -z "$REGISTER_URL" ]; then
  err "参数缺失：TOKEN 和 REGISTER_URL 必填"
  err "示例：curl -fsSL https://panel.example.com/static/x-install.sh | bash -s -- TOKEN https://panel.example.com/api/probe/register 1800"
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo bash -s -- "$TOKEN" "$REGISTER_URL" "$PUSH_INTERVAL" < "$0"
  fi
  err "需要 root 权限运行"
  exit 1
fi

MANAGER_URL="${REGISTER_URL%/api/probe/register}"
if [ "$MANAGER_URL" = "$REGISTER_URL" ]; then
  MANAGER_URL="${REGISTER_URL%/}"
fi
PUSH_URL="${MANAGER_URL%/}/api/probe/push"

case "$PUSH_INTERVAL" in
  ''|*[!0-9]*) PUSH_INTERVAL="1800" ;;
esac
if [ "$PUSH_INTERVAL" -lt 60 ]; then PUSH_INTERVAL="60"; fi
if [ "$PUSH_INTERVAL" -gt 21600 ]; then PUSH_INTERVAL="21600"; fi

install_deps() {
  log "安装基础依赖..."
  if [ -f /etc/debian_version ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y >/dev/null 2>&1 || true
    apt-get install -y python3 curl iputils-ping util-linux sqlite3 ca-certificates >/dev/null 2>&1 || true
  elif [ -f /etc/redhat-release ]; then
    yum install -y python3 curl iputils util-linux sqlite sqlite3 ca-certificates >/dev/null 2>&1 || true
  elif [ -f /etc/alpine-release ]; then
    apk add --no-cache python3 curl iputils util-linux sqlite sqlite3 ca-certificates >/dev/null 2>&1 || true
  fi
}

detect_ip() {
  local ip=""
  for endpoint in \
    "https://api.ipify.org" \
    "https://ifconfig.me/ip" \
    "http://checkip.amazonaws.com"; do
    ip="$(curl -fsSL --max-time 5 "$endpoint" 2>/dev/null | tr -d '[:space:]' || true)"
    if printf '%s' "$ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
      printf '%s' "$ip"
      return 0
    fi
  done
  hostname -I 2>/dev/null | awk '{print $1}'
}

PUBLIC_IP="$(detect_ip || true)"
if [ -n "$PUBLIC_IP" ]; then
  SERVER_URL="http://${PUBLIC_IP}:54322"
else
  SERVER_URL=""
  warn "公网 IP 探测失败，Agent 首次上报时会继续自动探测。"
fi

register_probe() {
  log "向主控注册探针：$REGISTER_URL"
  curl -fsSL --max-time 10 \
    -H 'Content-Type: application/json' \
    -d "{\"token\":\"$TOKEN\"}" \
    "$REGISTER_URL" >/tmp/x_fusion_lite_register.log 2>&1 || {
      warn "注册接口请求失败，Agent 仍会继续安装并主动上报。详情：$(cat /tmp/x_fusion_lite_register.log 2>/dev/null)"
      return 0
    }
}

write_agent() {
  log "写入 Agent：$AGENT_SCRIPT"
  cat > "$AGENT_SCRIPT" <<'PYTHON_EOF'
import json
import os
import platform
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

MANAGER_URL = os.environ.get("XFUSION_PUSH_URL", "")
TOKEN = os.environ.get("XFUSION_TOKEN", "")
SERVER_URL = os.environ.get("XFUSION_SERVER_URL", "")

# 推送间隔（秒）。面板会在每次推送响应中返回最新间隔；这里是兜底默认值。
PUSH_INTERVAL = int(os.environ.get("XFUSION_PUSH_INTERVAL", "1800") or "1800")
PUSH_INTERVAL_MIN = 60
PUSH_INTERVAL_MAX = 21600

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def get_cpu_model():
    model = "Unknown"
    try:
        try:
            out = subprocess.check_output("lscpu", shell=True, stderr=subprocess.DEVNULL).decode(errors="ignore")
            for line in out.split("\n"):
                if "Model name:" in line:
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
        with open("/proc/cpuinfo", "r", errors="ignore") as f:
            for line in f:
                if "model name" in line or "Hardware" in line:
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return model

def get_os_distro():
    try:
        if os.path.exists("/etc/os-release"):
            with open("/etc/os-release", errors="ignore") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    try:
        return platform.platform()
    except Exception:
        return "Linux (Unknown)"

STATIC_CACHE = {
    "cpu_model": get_cpu_model(),
    "arch": platform.machine(),
    "os": get_os_distro(),
    "virt": "Unknown",
}
try:
    v = subprocess.check_output("systemd-detect-virt", shell=True, stderr=subprocess.DEVNULL).decode().strip()
    if v and v != "none":
        STATIC_CACHE["virt"] = v
except Exception:
    pass

def get_network_bytes():
    r, t = 0, 0
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                cols = line.split(":")
                if len(cols) < 2 or cols[0].strip() == "lo":
                    continue
                parts = cols[1].split()
                if len(parts) >= 9:
                    r += int(parts[0])
                    t += int(parts[8])
    except Exception:
        pass
    return r, t

def get_xui_rows():
    for db_path in ["/etc/x-ui/x-ui.db", "/usr/local/x-ui/bin/x-ui.db", "/usr/local/x-ui/x-ui.db"]:
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT id, up, down, total, remark, enable, protocol, port, settings, stream_settings, expiry_time, listen FROM inbounds")
            rows = cursor.fetchall()
            conn.close()
            return [{
                "id": row[0], "up": row[1], "down": row[2], "total": row[3],
                "remark": row[4], "enable": row[5] == 1, "protocol": row[6],
                "port": row[7], "settings": row[8], "streamSettings": row[9],
                "expiryTime": row[10], "listen": row[11],
            } for row in rows]
        except Exception:
            pass
    return None

def get_info():
    global SERVER_URL
    data = {"token": TOKEN, "static": STATIC_CACHE}
    if not SERVER_URL:
        try:
            with urllib.request.urlopen("http://checkip.amazonaws.com", timeout=5, context=ssl_ctx) as r:
                my_ip = r.read().decode().strip()
                SERVER_URL = "http://" + my_ip + ":54322"
        except Exception:
            pass
    data["server_url"] = SERVER_URL
    try:
        net_in_1, net_out_1 = get_network_bytes()
        with open("/proc/stat") as f:
            fs = [float(x) for x in f.readline().split()[1:5]]
            tot1, idle1 = sum(fs), fs[3]
        time.sleep(1)
        net_in_2, net_out_2 = get_network_bytes()
        with open("/proc/stat") as f:
            fs = [float(x) for x in f.readline().split()[1:5]]
            tot2, idle2 = sum(fs), fs[3]
        data["cpu_usage"] = round((1 - (idle2 - idle1) / max(tot2 - tot1, 1)) * 100, 1)
        data["cpu_cores"] = os.cpu_count() or 1
        data["net_speed_in"] = net_in_2 - net_in_1
        data["net_speed_out"] = net_out_2 - net_out_1
        data["net_total_in"] = net_in_2
        data["net_total_out"] = net_out_2
        with open("/proc/loadavg") as f:
            data["load_1"] = float(f.read().split()[0])
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2:
                    mem[p[0].rstrip(":")] = int(p[1])
        total = mem.get("MemTotal", 1)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        data["mem_total"] = round(total / 1024 / 1024, 2)
        data["mem_usage"] = round(((total - avail) / total) * 100, 1)
        data["swap_total"] = round(mem.get("SwapTotal", 0) / 1024 / 1024, 2)
        data["swap_free"] = round(mem.get("SwapFree", 0) / 1024 / 1024, 2)
        st = os.statvfs("/")
        disk_total = st.f_blocks * st.f_frsize
        disk_free = st.f_bavail * st.f_frsize
        data["disk_total"] = round(disk_total / 1024 / 1024 / 1024, 2)
        data["disk_usage"] = round(((disk_total - disk_free) / disk_total) * 100, 1)
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        d = int(up // 86400)
        h = int((up % 86400) // 3600)
        m = int((up % 3600) // 60)
        data["uptime"] = "%d天 %d时 %d分" % (d, h, m)
        xui = get_xui_rows()
        if xui is not None:
            data["xui_data"] = xui
    except Exception:
        pass
    return data

def parse_interval(body, fallback):
    try:
        parts = body.split()
        if len(parts) >= 2:
            n = int(parts[1])
            if n > 0:
                return max(PUSH_INTERVAL_MIN, min(PUSH_INTERVAL_MAX, n))
    except Exception:
        pass
    return fallback

def push():
    interval = PUSH_INTERVAL
    while True:
        try:
            body = json.dumps(get_info()).encode("utf-8")
            req = urllib.request.Request(MANAGER_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as r:
                interval = parse_interval(r.read(64).decode("utf-8", "ignore"), interval)
        except Exception:
            pass
        time.sleep(interval)

if __name__ == "__main__":
    push()
PYTHON_EOF
  chmod 700 "$AGENT_SCRIPT"
}

write_service() {
  log "安装 systemd 服务：$AGENT_NAME"
  cat > "/etc/systemd/system/${AGENT_NAME}.service" <<SERVICE_EOF
[Unit]
Description=X-Fusion Probe Agent (Lite)
After=network.target

[Service]
Type=simple
User=root
Environment=XFUSION_PUSH_URL=$PUSH_URL
Environment=XFUSION_TOKEN=$TOKEN
Environment=XFUSION_SERVER_URL=$SERVER_URL
Environment=XFUSION_PUSH_INTERVAL=$PUSH_INTERVAL
ExecStart=/usr/bin/python3 $AGENT_SCRIPT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE_EOF
}

cleanup_legacy_agent() {
  if systemctl is-active --quiet "$AGENT_NAME" && [ -f "$LEGACY_AGENT_SCRIPT" ] && grep -qF "$PUSH_URL" "$LEGACY_AGENT_SCRIPT"; then
    systemctl stop "$LEGACY_AGENT_NAME" >/dev/null 2>&1 || true
    systemctl disable "$LEGACY_AGENT_NAME" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${LEGACY_AGENT_NAME}.service"
    rm -f "$LEGACY_AGENT_SCRIPT"
    systemctl daemon-reload >/dev/null 2>&1 || true
    echo "XFUSION_LEGACY_AGENT_REMOVED"
  fi
}

install_deps
register_probe
write_agent
write_service

systemctl daemon-reload
systemctl enable "$AGENT_NAME" >/dev/null 2>&1 || true
systemctl restart "$AGENT_NAME"
sleep 1
cleanup_legacy_agent

if systemctl is-active --quiet "$AGENT_NAME"; then
  log "X-Fusion Lite 探针安装完成，正在向 $PUSH_URL 上报"
else
  err "Agent 服务启动失败，请执行：journalctl -u $AGENT_NAME -n 80 --no-pager"
  exit 1
fi