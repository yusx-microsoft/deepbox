"""Provision a demo account + devbox + claude agent, print the token."""
import httpx, os
BASE="http://localhost:8077"
c=httpx.Client(base_url=BASE)
user="demo"; pw="demo"
r=c.post("/api/auth/register",json={"username":user,"password":pw})
if r.status_code!=200:
    r=c.post("/api/auth/login",json={"username":user,"password":pw})
ck={"deepbox_session":r.cookies.get("deepbox_session")}
boxes=c.get("/api/devboxes",cookies=ck).json()
if boxes:
    print("TOKEN=(existing devbox, rotating)")
    did=boxes[0]["id"]
    tok=c.post(f"/api/devboxes/{did}/tokens",cookies=ck).json()["token"]
else:
    d=c.post("/api/devboxes",json={"name":"My Laptop"},cookies=ck).json()
    did=d["devbox"]["id"]; tok=d["token"]
    c.post(f"/api/devboxes/{did}/agents",
           json={"handle":"claude","display_name":"Claude Code","runtime":"claude-code","cwd":r"C:\Code\deepbox"},cookies=ck)
print("LOGIN: demo / demo")
print("TOKEN:",tok)
