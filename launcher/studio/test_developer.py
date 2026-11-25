import threading, tempfile, hashlib, json, time
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
import studio_server as srv

# Exercise the real verifier with a disposable test credential.
test_password="local-test-fixture"
with tempfile.TemporaryDirectory() as tmp:
    salt=b"0123456789abcdef"
    digest=hashlib.pbkdf2_hmac("sha256",test_password.encode(),salt,600000)
    Path(tmp,"developer-auth.json").write_text(json.dumps({"salt":salt.hex(),"hash":digest.hex()}))
    with patch.object(srv,"AUTH_FILE",Path(tmp,"developer-auth.json")):
        assert srv.verify_password(test_password)
        assert not srv.verify_password("wrong")
        assert not srv.verify_password(None)
# Browser integration uses a fixture credential; production verifier stays intact.
server=srv.ThreadingHTTPServer(("127.0.0.1",0),srv.Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
url=f"http://127.0.0.1:{server.server_port}/?key={srv.TOKEN}"
headers={"X-Studio-Key":srv.TOKEN}
try:
    with patch.object(srv,"verify_password",side_effect=lambda s:s==test_password),sync_playwright() as w:
        b=w.chromium.launch(channel="chrome",headless=True)
        page=b.new_page(viewport={"width":1280,"height":800},accept_downloads=True)
        errors=[];page.on("pageerror",lambda e:errors.append(str(e)))
        page.goto(url)
        page.wait_for_function("document.getElementById('connection').textContent.includes('connecté')")
        assert page.locator("#play").is_visible()
        assert not page.locator("[data-page=logs]").is_visible()
        assert not page.locator("#debug").is_visible()
        assert not page.locator("#build-select").is_visible()
        assert not page.locator("#play-frame").is_visible()
        d=page.request.get(url.split('/?')[0]+"/api/state",headers=headers).json()
        assert d["developer"] is False and not d["current"]["lines"] and not d["source"]
        assert len(d["versions"])<=1
        cfg=dict(version="victory.exe",console="series-x",debug=True,surface=False,warp=False)
        assert page.request.post(url.split('/?')[0]+"/api/open-launcher",headers=headers,data=cfg).status==403
        page.screenshot(path="preview-player.png",animations="disabled")
        page.locator("[data-page=settings]").click()
        page.locator("#dev-toggle").click()
        page.locator("#dev-password").fill("incorrect")
        page.locator("#dev-form button[type=submit]").click()
        page.wait_for_function("document.getElementById('dev-status').textContent.includes('incorrect')")
        assert not page.locator("[data-page=logs]").is_visible()
        time.sleep(1.1)
        page.locator("#dev-password").fill(test_password)
        page.locator("#dev-form button[type=submit]").click()
        page.wait_for_function("document.body.classList.contains('developer')")
        assert page.locator("[data-page=logs]").is_visible()
        page.screenshot(path="preview-dev-settings.png",animations="disabled")
        page.locator("[data-page=play]").click()
        assert page.locator("#debug").is_visible()
        page.locator("#debug").check()
        page.screenshot(path="preview-developer.png",animations="disabled")
        page.locator("[data-page=logs]").click()
        page.locator("#query").fill("D3D12")
        assert page.locator("#result-count").inner_text()
        page.locator("[data-page=overview]").click()
        assert page.locator("#diagnostics .data-row").count()==5
        page.locator("[data-page=versions]").click()
        assert page.locator("#versionbody tr").count()>0
        page.locator("[data-page=settings]").click()
        page.locator("#dev-toggle").click()
        page.wait_for_function("!document.body.classList.contains('developer')")
        assert not page.locator("[data-page=logs]").is_visible()
        page.locator("[data-page=play]").click()
        assert not page.locator("#debug").is_checked()
        d=page.request.get(url.split('/?')[0]+"/api/state",headers=headers).json()
        assert not d["developer"] and not d["current"]["lines"]
        assert not errors,errors
        b.close()
    print("PASS: verifier, normal mode, denied API, wrong password, unlock, advanced tabs, relock, cleared options; no JS errors")
finally:
    server.shutdown();server.server_close()
