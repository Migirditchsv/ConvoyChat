"""S-06: self-eviction policy state machine (INV-9)."""
from bridge.net.supervisor import EvictionPolicy, LinkStats


def test_evicts_on_sustained_degradation():
    actions = []
    p = EvictionPolicy(lambda: actions.append("evict"),
                       lambda n: actions.append(f"earcon:{n}"))
    for _ in range(2):
        p.tick_1s(LinkStats(6.0, 40.0))
    assert not actions                       # not yet: needs 3 s sustained
    p.tick_1s(LinkStats(6.0, 40.0))
    assert actions == ["earcon:link_lost", "evict"]


def test_rate_limited():
    n = {"e": 0}
    p = EvictionPolicy(lambda: n.__setitem__("e", n["e"] + 1))
    for _ in range(40):                      # 40 s of continuous badness
        p.tick_1s(LinkStats(1.0, 90.0))
    assert n["e"] == 2                       # once, then once after 30 s cooldown


def test_recovers_counter_on_good_second():
    n = {"e": 0}
    p = EvictionPolicy(lambda: n.__setitem__("e", n["e"] + 1))
    for _ in range(10):
        p.tick_1s(LinkStats(6.0, 40.0)); p.tick_1s(LinkStats(50.0, 0.0))
    assert n["e"] == 0
