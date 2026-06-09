import os
import asyncio
import math
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # добавьте свой домен при необходимости
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# Глобальный кэш для метрик
metrics_cache = {
    "system": {},
    "minecraft": {},
    "last_update": None
}

# ---------------- Асинхронный запрос к Prometheus ----------------
async def run_query(query: str, client: httpx.AsyncClient) -> float | None:
    """Асинхронный запрос PromQL возвращает число."""
    try:
        resp = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5.0
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data["data"]["result"]:
            val = float(data["data"]["result"][0]["value"][1])
            return val if not math.isnan(val) else None
        return None
    except Exception:
        return None

# ---------------- Обновление системных метрик ----------------
async def update_system_metrics(client: httpx.AsyncClient):
    cpu = await run_query('100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)', client)
    ram = await run_query('100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))', client)
    disk = await run_query('100 - ((node_filesystem_avail_bytes{mountpoint="/", fstype!="rootfs"} * 100) / node_filesystem_size_bytes{mountpoint="/", fstype!="rootfs"})', client)
    uptime_seconds = await run_query('time() - node_boot_time_seconds', client)
    uptime_days = round(uptime_seconds / 86400, 1) if uptime_seconds is not None else None
    load1 = await run_query('node_load1', client)
    ctx_switches = await run_query('rate(node_context_switches_total[5m])', client)
    tcp_connections = await run_query('node_netstat_Tcp_CurrEstab', client)
    fd_open = await run_query('node_filefd_allocated', client)
    swap = await run_query('(1 - (node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes)) * 100', client)

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

# ---------------- Обновление метрик Minecraft ----------------
async def update_minecraft_metrics(client: httpx.AsyncClient):
    players = await run_query('sum(mc_players_online_total)', client)
    chunks = await run_query('sum(mc_loaded_chunks_total)', client)
    entities = await run_query('sum(mc_entities_total)', client)
    world_bytes = await run_query('sum(mc_world_size)', client)
    tps = await run_query('mc_tps', client)

    world_gb = round(world_bytes / (1024**3), 2) if world_bytes is not None else None
    return {
        "players_online": int(players) if players is not None else 0,
        "loaded_chunks": int(chunks) if chunks is not None else 0,
        "entities": int(entities) if entities is not None else 0,
        "world_size_gb": world_gb,
        "tps": round(tps, 1) if tps is not None else 0
    }

# ---------------- Фоновая задача обновления кэша ----------------
async def background_updater():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                system = await update_system_metrics(client)
                minecraft = await update_minecraft_metrics(client)
                metrics_cache["system"] = system
                metrics_cache["minecraft"] = minecraft
                metrics_cache["last_update"] = datetime.now()
            except Exception as e:
                print(f"Ошибка обновления метрик: {e}")
            await asyncio.sleep(5)  # обновление каждые 5 секунд

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_updater())

# ---------------- Эндпоинты (мгновенный ответ из кэша) ----------------
@app.get("/api/metrics")
async def get_metrics():
    return metrics_cache["system"]

@app.get("/api/minecraft/metrics")
async def get_minecraft():
    return metrics_cache["minecraft"]

# ---------------- Range-запросы (оставляем синхронными, но можно переписать) ----------------
@app.get("/api/metrics/range")
def get_metrics_range(metric: str = "cpu", hours: float = 1.0, step: int = 15):
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
        import requests
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
        import requests
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