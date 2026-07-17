"""E2E with the REAL Claude Code CLI through the deepbox pipeline."""
import asyncio, json, httpx, websockets, os, sys

BASE = "http://localhost:8077"

async def main():
    ck = None
    async with httpx.AsyncClient(base_url=BASE) as c:
        u = f"ctester_{os.getpid()}"
        r = await c.post("/api/auth/register", json={"username":u,"password":"pw"})
        r.raise_for_status()
        cookie = r.cookies.get("deepbox_session")
        ck = {"deepbox_session": cookie}
        r = await c.post("/api/devboxes", json={"name":"box1"}, cookies=ck)
        d = r.json(); token = d["token"]; devbox_id = d["devbox"]["id"]
        r = await c.post(f"/api/devboxes/{devbox_id}/agents",
                         json={"handle":"claude","runtime":"claude-code",
                               "cwd": r"C:\Code\deepbox"}, cookies=ck)
        agent_id = r.json()["id"]
        print("agent:", agent_id)

    sys.path.insert(0, ".")
    from connector.client import Connector
    conn = Connector(BASE, token)
    asyncio.create_task(conn.run())
    await asyncio.sleep(2)

    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.post(f"/api/agents/{agent_id}/sessions", cookies=ck)
        sid = r.json()["id"]

    got = []
    async with websockets.connect("ws://localhost:8077/ws/term",
            additional_headers={"Cookie": f"deepbox_session={cookie}"}) as ws:
        await ws.send(json.dumps({"type":"open","session_id":sid}))
        await ws.send(json.dumps({"type":"resize","session_id":sid,"cols":120,"rows":30}))
        async def reader():
            async for raw in ws:
                f = json.loads(raw)
                if f.get("type")=="output":
                    got.append(f["data"])
        rt = asyncio.create_task(reader())
        await asyncio.sleep(10)  # let Claude TUI render
        rt.cancel()

    text = "".join(got)
    print("=== bytes:", len(text))
    ok = ("Claude" in text) and ("v2.1" in text or "Code" in text)
    print("=== REAL-CLAUDE E2E", "PASS" if ok else "FAIL", "===")
    # dump a slice for eyeballing
    sys.stdout.buffer.write(text[:1500].encode("utf-8","replace"))
    os._exit(0 if ok else 1)

asyncio.run(main())
