"""The faster 'running alongside' check must give exactly the answers of the plain pair-by-pair haversine test."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))

import bt_paths_map as m  # noqa: E402


def plain(sa, sb, th):
    if not sa:
        return 0.0
    return sum(1 for pa in sa if any(m._haversine_m(pa[0], pa[1], pb[0], pb[1]) <= th for pb in sb)) / len(sa)


def test_overlap_fraction_matches_the_plain_distance_test_including_points_just_inside_and_outside():
    rnd = random.Random(7)
    for _ in range(300):
        base = (35.0 + rnd.random() * 0.2, 33.0 + rnd.random() * 0.2)
        a = [(base[0] + rnd.uniform(-0.002, 0.002), base[1] + rnd.uniform(-0.002, 0.002)) for _ in range(16)]
        b = [(base[0] + rnd.uniform(-0.002, 0.002), base[1] + rnd.uniform(-0.002, 0.002)) for _ in range(16)]
        for th in (m.OVERLAP_M, m.DUPLICATE_OVERLAP_M):
            assert m._overlap_fraction(a, b, th) == plain(a, b, th)
    # points exactly on the threshold, north-south and east-west
    p = (35.1, 33.1)
    for dlat, dlon in ((120 / 111195.0, 0), (0, 120 / (111195.0 * 0.8168)), (119 / 111195.0, 0), (121 / 111195.0, 0)):
        q = [(p[0] + dlat, p[1] + dlon)]
        assert m._overlap_fraction([p], q, 120) == plain([p], q, 120)
    assert m._overlap_fraction([], [p], 120) == 0.0 and m._overlap_fraction([p], [], 120) == 0.0
