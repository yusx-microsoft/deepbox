"""End-to-end smoke test for deepbox P0.
Registers a user, creates a devbox+mock agent, starts a connector in-process,
opens a human terminal WS, sends input, verifies mock CLI output flows back."""
import asyncio, json, httpx, websockets, os, sys

BASE = "http://localhost:8077"

async def main():
    async with httpx.AsyncClient(base_url=BASE) as c:
        u = f"tester_{os.getpid()}"
        r = await c.post("/api/auth/register", json={"username":u,"password":"pw"})
        r.raise_for_status()
        cookie = r.cookies.get("deepbox_session")
        ck = {"deepbox_session": cookie}
        r = await c.post("/api/devboxes", json={"name":"box1"}, cookies=ck)
        d = r.json(); token = d["token"]; devbox_id = d["devbox"]["id"]
        r = await c.post(f"/api/devboxes/{devbox_id}/agents",
                         json={"handle":"mock","runtime":"mock"}, cookies=ck)
        agent = r.json(); agent_id = agent["id"]
        print("created agent", agent_id)

    # start connector in-process
    sys.path.insert(0, ".")
    from connector.client import Connector
    conn = Connector(BASE, token)
    conn_task = asyncio.create_task(conn.run())
    await asyncio.sleep(2)  # let it connect

    # create session + open human term WS
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.post(f"/api/agents/{agent_id}/sessions", cookies=ck)
        sess = r.json(); sid = sess["id"]

    got = []
    async with websockets.connect(f"ws://localhost:8077/ws/term",
            additional_headers={"Cookie": f"deepbox_session={cookie}"}) as ws:
        await ws.send(json.dumps({"type":"open","session_id":sid}))
        # read for a bit, then send input
        async def reader():
            async for raw in ws:
                f = json.loads(raw)
                if f.get("type")=="output":
                    got.append(f["data"])
                    print("OUT:", repr(f["data"]))
        rt = asyncio.create_task(reader())
        await asyncio.sleep(4)
        await ws.send(json.dumps({"type":"input","session_id":sid,"data":"hello world\r"}))
        await asyncio.sleep(3)
        rt.cancel()

    text = "".join(got)
    print("=== collected ===", repr(text))
    ok = ("mock-agent ready" in text) and ("you said: hello world" in text)
    print("\n=== E2E", "PASS" if ok else "FAIL", "===")
    os._exit(0 if ok else 1)

asyncio.run(main())
