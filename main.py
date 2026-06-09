import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import requests
from cachetools import cached, TTLCache
from datetime import datetime, timedelta
import math

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

cache = TTLCache(maxsize=128, ttl=5)

def run_query(query: str):
    """Запрос PromQL возвращает число."""
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=5)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data["data"]["result"]:
            val = float(data["data"]["result"][0]["value"][1])
            return val if not math.isnan(val) else None
        return None
    except Exception:
        return None

# --- СИСТЕМНЫЕ МЕТРИКИ
@cached(cache)
def get_all_metrics():
    cpu = run_query('100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)')
    ram = run_query('100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))')
    disk = run_query('100 - ((node_filesystem_avail_bytes{mountpoint="/", fstype!="rootfs"} * 100) / node_filesystem_size_bytes{mountpoint="/", fstype!="rootfs"})')
    uptime_seconds = run_query('time() - node_boot_time_seconds')
    uptime_days = round(uptime_seconds / 86400, 1) if uptime_seconds is not None else None
    load1 = run_query('node_load1')
    ctx_switches = run_query('rate(node_context_switches_total[5m])')
    tcp_connections = run_query('node_netstat_Tcp_CurrEstab')
    fd_open = run_query('node_filefd_allocated')
    swap = run_query('(1 - (node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes)) * 100')

    return {
        "cpu_usage": cpu,
        "ram_usage": ram,
        "disk_usage": disk,
        "uptime_days": uptime_days,
        "load1": load1,
        "ctx_switches": ctx_switches,
        "tcp_connections": tcp_connections,
        "fd_open": fd_open,
        "swap_usage": swap
    }

# --- МЕТРИКИ MINECRAFT
@cached(cache)
def get_minecraft_metrics():
    players = run_query('sum(mc_players_online_total)')
    chunks = run_query('sum(mc_loaded_chunks_total)')
    entities = run_query('sum(mc_entities_total)')
    world_bytes = run_query('sum(mc_world_size)')
    tps = run_query('mc_tps')

    world_gb = round(world_bytes / (1024**3), 2) if world_bytes is not None else None

    return {
        "players_online": int(players) if players is not None else 0,
        "loaded_chunks": int(chunks) if chunks is not None else 0,
        "entities": int(entities) if entities is not None else 0,
        "world_size_gb": world_gb,
        "tps": round(tps, 1) if tps is not None else 0
    }

# --- ЭНДПОИНТЫ 
@app.get("/api/metrics")
def get_metrics():
    """cистемные метрики (CPU, RAM, диск и пр.)"""
    return get_all_metrics()

@app.get("/api/minecraft/metrics")
def get_minecraft():
    """игровые метрики (онлайн, чанки, энтити, размер мира, TPS)"""
    return get_minecraft_metrics()

@app.get("/api/metrics/range")
def get_metrics_range(metric: str = "cpu", hours: float = 1.0, step: int = 15):
    """
    системные метрики за интервал.
    metric: cpu, ram, disk, net_in, net_out, load1
    """
    end = datetime.now()
    start = end - timedelta(hours=hours)
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())

    queries = {
    "cpu": '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "ram": '100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))',
    "disk": '100 - ((node_filesystem_avail_bytes{mountpoint="/", fstype!="rootfs"} * 100) / node_filesystem_size_bytes{mountpoint="/", fstype!="rootfs"})',
    "swap": '(1 - (node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes)) * 100',
    "net_in": 'rate(node_network_receive_bytes_total{device="eth0"}[5m]) / 1024 / 1024',
    "net_out": 'rate(node_network_transmit_bytes_total{device="eth0"}[5m]) / 1024 / 1024',
    "load1": 'node_load1',
    }
    
    if metric not in queries:
        return {"error": f"Unknown metric: {metric}"}
    query = queries[metric]

    params = {"query": query, "start": start_ts, "end": end_ts, "step": f"{step}s"}
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query_range", params=params, timeout=10)
        if resp.status_code != 200:
            return {"error": "Prometheus range query failed"}
        data = resp.json()
        result = []
        for item in data["data"]["result"][0]["values"]:
            ts, val = item
            result.append({"time": int(float(ts)), "value": float(val) if val != "NaN" else None})
        return {"metric": metric, "data": result}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/minecraft/range")

def get_minecraft_range(metric: str = "tps", hours: int = 1, step: int = 15):
    """
    игровые метрики за интервал: tps, players, chunks, entities
    """
    end = datetime.now()
    start = end - timedelta(hours=hours)
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())

    queries = {
        "tps": "mc_tps",
        "players": "sum(mc_players_online_total)",
        "chunks": "sum(mc_loaded_chunks_total)",
        "entities": "sum(mc_entities_total)",
    }
    if metric not in queries:
        return {"error": f"Unknown minecraft metric: {metric}"}
    query = queries[metric]

    params = {"query": query, "start": start_ts, "end": end_ts, "step": f"{step}s"}
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query_range", params=params, timeout=10)
        if resp.status_code != 200:
            return {"error": "Prometheus range query failed"}
        data = resp.json()
        result = []
        for item in data["data"]["result"][0]["values"]:
            ts, val = item
            result.append({"time": int(float(ts)), "value": float(val) if val != "NaN" else None})
        return {"metric": metric, "data": result}
    except Exception as e:
        return {"error": str(e)}