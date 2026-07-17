"""Connect as the browser would, capture Claude's PTY stream, and render it
through a real terminal emulator (pyte) to a text snapshot — i.e. exactly what
the user sees in xterm.js."""
import asyncio, json, websockets, httpx, pyte, sys

BASE="http://localhost:8077"

async def main():
    c=httpx.Client(base_url=BASE)
    r=c.post("/api/auth/login",json={"username":"demo","password":"demo"})
    cookie=r.cookies.get("deepbox_session")
    ck={"deepbox_session":cookie}
    aid="a76a32164fe84de59345ab6aa4bafcec"
    sid=c.post(f"/api/agents/{aid}/sessions",cookies=ck).json()["id"]

    cols,rows=120,30
    screen=pyte.Screen(cols,rows); stream=pyte.ByteStream(screen)

    async with websockets.connect("ws://localhost:8077/ws/term",
            additional_headers={"Cookie":f"deepbox_session={cookie}"}) as ws:
        await ws.send(json.dumps({"type":"open","session_id":sid}))
        await ws.send(json.dumps({"type":"resize","session_id":sid,"cols":cols,"rows":rows}))
        async def reader():
            async for raw in ws:
                f=json.loads(raw)
                if f.get("type")=="output":
                    stream.feed(f["data"].encode("utf-8","replace"))
        rt=asyncio.create_task(reader())
        await asyncio.sleep(9)
        # type a prompt to Claude
        await ws.send(json.dumps({"type":"input","session_id":sid,"data":"say hello in one short sentence\r"}))
        await asyncio.sleep(12)
        rt.cancel()

    with open("snapshot.txt","w",encoding="utf-8") as fp:
        fp.write("="*cols+"\n")
        for line in screen.display:
            fp.write(line.rstrip()+"\n")
        fp.write("="*cols+"\n")
    import os; os._exit(0)

asyncio.run(main())
