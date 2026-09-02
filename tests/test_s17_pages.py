"""S-17: the LAN web front door — routes, content types, traversal, and the
JSON snapshot endpoint that hardware testing curls."""
import os
from base.main import route_request, STATIC, lan_addresses
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator
from common.roster import demo_roster


def test_routes_and_types():
    for path, name in (("/", "index.html"), ("/rider", "rider.html"), ("/ops", "ops.html"),
                       ("/dashboard", "ops.html"), ("/rider?id=r2_rider", "rider.html")):
        status, body, ctype = route_request(path)
        assert status == "200 OK" and ctype.startswith("text/html"), path
        assert body == open(os.path.join(STATIC, name), "rb").read()
    assert route_request("/manifest.webmanifest")[2] == "application/manifest+json"
    assert route_request("/icon.svg")[2] == "image/svg+xml"
    assert route_request("/health")[1] == b"ok\n"


def test_traversal_and_missing():
    for p in ("/../Makefile", "/../../etc/passwd", "/static/../../base/main.py", "/nope.html"):
        assert route_request(p)[0].startswith("404"), p


def test_snapshot_json_endpoint():
    import json
    orc = Orchestrator(demo_roster(3), PyMixer(rtp_port=5474))
    status, body, ctype = route_request("/snapshot.json?x=1", orc)
    snap = json.loads(body)
    assert status == "200 OK" and ctype == "application/json"
    assert set(snap["riders"]) == {"r0_lead", "r1_chase", "r2_rider"}
    assert route_request("/snapshot.json")[0].startswith("503")


def test_pages_reference_only_local_assets():
    """The convoy LAN has no internet: pages must not load anything remote."""
    for name in ("index.html", "rider.html", "ops.html"):
        html = open(os.path.join(STATIC, name)).read()
        assert "https://" not in html and "http://cdn" not in html, name
        assert "ws://${location.hostname}:8800" in html, name


def test_lan_addresses_shape():
    for a in lan_addresses():
        assert a.count(".") == 3 and not a.startswith("127.")
