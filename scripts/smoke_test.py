#!/usr/bin/env python3
"""针对运行中服务的端到端冒烟测试（仅用标准库）。

用法::

    python3 scripts/smoke_test.py [BASE_URL]

默认 http://127.0.0.1:8080。退出码非 0 表示冒烟失败。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
failures = []


def call(method: str, path: str, body=None, expect_status=200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        status = e.code
        payload = json.loads(e.read().decode())
    tag = f"{method} {path}"
    if status != expect_status:
        failures.append(f"{tag}: 期望 HTTP {expect_status}，实际 {status}，body={payload}")
    return status, payload


def main() -> int:
    status, health = call("GET", "/health")
    assert health.get("has_data"), health

    # 1. 正常查询（示例数据：E4 在 10:00-11:00 封闭）
    status, route = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T09:55:00Z",
        "avoid_stairs": False,
    })
    assert status == 200, route
    assert "stops" not in route, route  # 无停靠点时保持原有返回结构
    print("普通查询:", route["edge_sequence"], "->", route["arrival_time"])

    # 2. 避台阶：只能走 E1 E3 E5
    status, route_flat = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:00:00Z",
        "avoid_stairs": True,
    })
    assert route_flat["edge_sequence"] == ["E1", "E3", "E5"], route_flat
    print("避台阶查询:", route_flat["edge_sequence"])

    # 3. 未知节点 → 400
    status, err = call("POST", "/api/route", {
        "origin": "GHOST", "destination": "TOWER",
        "departure_time": "2026-10-05T08:00:00Z",
    }, expect_status=400)
    assert err["error"] == "invalid_payload", err

    # 4. 非法导入（引用未知节点）→ 400 且版本不变
    before = call("GET", "/version")[1]
    status, bad = call("POST", "/admin/graph", {
        "nodes": ["X"],
        "edges": [{"id": "X1", "from": "X", "to": "Y",
                   "travel_seconds": 10, "has_stairs": False}],
    }, expect_status=400)
    after = call("GET", "/version")[1]
    assert before["data_version"] == after["data_version"] == bad["active_version"], (before, after, bad)
    print(f"拒绝非法导入，保留版本 v{after['data_version']}")

    # 5. 非法秒数 → 400
    status, bad2 = call("POST", "/admin/graph", {
        "nodes": ["X", "Y"],
        "edges": [{"id": "X1", "from": "X", "to": "Y",
                   "travel_seconds": 0, "has_stairs": False}],
    }, expect_status=400)
    assert "正数" in bad2["detail"], bad2

    # 6. 无效时间区间 → 400
    status, bad3 = call("POST", "/admin/closures", {
        "closures": [{"edge_id": "E1",
                      "start": "2026-10-05T10:00:00Z",
                      "end": "2026-10-05T09:00:00Z"}],
    }, expect_status=400)

    # 7. 按顺序停靠：LAKE 窗口 [09:00,09:10] 停留 300s
    #    08:50 出发 → 08:58 抵 LAKE，等窗至 09:00，09:05 离开，09:09 到 TOWER
    status, route_stop = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:50:00Z",
        "stops": [{"node": "LAKE",
                   "earliest_start": "2026-10-05T09:00:00Z",
                   "latest_start": "2026-10-05T09:10:00Z",
                   "stay_seconds": 300}],
    })
    assert route_stop["edge_sequence"] == ["E1", "E3", "E5"], route_stop
    assert route_stop["arrival_time"] == "2026-10-05T09:09:00Z", route_stop
    (visit,) = route_stop["stops"]
    assert (visit["arrival_time"], visit["start_time"], visit["end_time"]) == (
        "2026-10-05T08:58:00Z", "2026-10-05T09:00:00Z", "2026-10-05T09:05:00Z"), visit
    assert route_stop["total_stay_seconds"] == 300, route_stop
    print("停靠查询:", visit["node"], visit["start_time"], "->", route_stop["arrival_time"])

    # 8. 停靠窗口不可行（最晚 08:55 早于抵达 08:58）→ 404 no_route
    status, nr = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:50:00Z",
        "stops": [{"node": "LAKE",
                   "earliest_start": "2026-10-05T08:00:00Z",
                   "latest_start": "2026-10-05T08:55:00Z",
                   "stay_seconds": 60}],
    }, expect_status=404)
    assert nr["error"] == "no_route", nr

    # 9. 停留秒数非法 / 停靠地点未知 → 400 invalid_payload
    status, bad4 = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:50:00Z",
        "stops": [{"node": "LAKE",
                   "earliest_start": "2026-10-05T09:00:00Z",
                   "latest_start": "2026-10-05T09:10:00Z",
                   "stay_seconds": -1}],
    }, expect_status=400)
    assert bad4["error"] == "invalid_payload", bad4
    status, bad5 = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:50:00Z",
        "stops": [{"node": "GHOST",
                   "earliest_start": "2026-10-05T09:00:00Z",
                   "latest_start": "2026-10-05T09:10:00Z",
                   "stay_seconds": 60}],
    }, expect_status=400)
    assert bad5["error"] == "invalid_payload", bad5

    # 10. 多人汇合：alice 09:55 从 GATE、bob 09:50 从 PLAZA；
    #     LAKE：alice 10:03 到、bob 09:55 到 → 汇合 10:03
    #     TOWER：alice 10:07 到、bob 09:59 到 → 汇合 10:07 → 选 LAKE
    status, meeting = call("POST", "/api/meeting", {
        "visitors": [
            {"id": "alice", "origin": "GATE",
             "departure_time": "2026-10-05T09:55:00Z", "avoid_stairs": False},
            {"id": "bob", "origin": "PLAZA",
             "departure_time": "2026-10-05T09:50:00Z", "avoid_stairs": False},
        ],
        "candidates": ["TOWER", "LAKE"],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    })
    assert meeting["meeting_node"] == "LAKE", meeting
    assert meeting["meeting_time"] == "2026-10-05T10:03:00Z", meeting
    alice, bob = meeting["visitors"]
    assert alice["edge_sequence"] == ["E1", "E3"], alice
    assert alice["wait_after_arrival_seconds"] == 0
    assert bob["edge_sequence"] == ["E3"]
    assert bob["wait_after_arrival_seconds"] == 480, bob
    print("汇合查询:", meeting["meeting_node"], meeting["meeting_time"])

    # 11. 最晚汇合时刻早于全员可达时刻 → 404 no_meeting_candidate
    status, nm = call("POST", "/api/meeting", {
        "visitors": [
            {"origin": "GATE", "departure_time": "2026-10-05T09:55:00Z"},
            {"origin": "PLAZA", "departure_time": "2026-10-05T09:50:00Z"},
        ],
        "candidates": ["LAKE"],
        "latest_meeting_time": "2026-10-05T10:00:00Z",
    }, expect_status=404)
    assert nm["error"] == "no_meeting_candidate", nm

    # 12. 汇合参数非法：访客不足两人 / 未知候选节点 / 候选为空 → 400
    status, mb1 = call("POST", "/api/meeting", {
        "visitors": [{"origin": "GATE", "departure_time": "2026-10-05T09:00:00Z"}],
        "candidates": ["LAKE"],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    }, expect_status=400)
    assert mb1["error"] == "invalid_payload", mb1
    status, mb2 = call("POST", "/api/meeting", {
        "visitors": [
            {"origin": "GATE", "departure_time": "2026-10-05T09:00:00Z"},
            {"origin": "PLAZA", "departure_time": "2026-10-05T09:00:00Z"},
        ],
        "candidates": ["GHOST"],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    }, expect_status=400)
    assert mb2["error"] == "invalid_payload", mb2
    status, mb3 = call("POST", "/api/meeting", {
        "visitors": [
            {"origin": "GATE", "departure_time": "2026-10-05T09:00:00Z"},
            {"origin": "PLAZA", "departure_time": "2026-10-05T09:00:00Z"},
        ],
        "candidates": [],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    }, expect_status=400)
    assert mb3["error"] == "invalid_payload", mb3

    # 13. 反向时间窗：给 E5(LAKE->TOWER,240s) 增加活动时段反向窗，
    #     其余路段不变（E4 的封闭引用因此保留）
    version_before = call("GET", "/version")[1]
    reverse_graph = {
        "nodes": ["GATE", "PLAZA", "HILL", "LAKE", "TOWER"],
        "edges": [
            {"id": "E1", "from": "GATE", "to": "PLAZA", "travel_seconds": 180, "has_stairs": False},
            {"id": "E2", "from": "PLAZA", "to": "HILL", "travel_seconds": 240, "has_stairs": True},
            {"id": "E3", "from": "PLAZA", "to": "LAKE", "travel_seconds": 300, "has_stairs": False},
            {"id": "E4", "from": "HILL", "to": "TOWER", "travel_seconds": 120, "has_stairs": True},
            {"id": "E5", "from": "LAKE", "to": "TOWER", "travel_seconds": 240, "has_stairs": False,
             "reverse_windows": [
                 {"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T13:00:00Z"},
                 {"start": "2026-10-05T14:00:00Z", "end": "2026-10-05T16:00:00Z"}]},
            {"id": "E6", "from": "PLAZA", "to": "TOWER", "travel_seconds": 600, "has_stairs": True},
        ],
    }
    status, published = call("POST", "/admin/graph", reverse_graph)
    assert status == 200, published
    ver = call("GET", "/version")[1]
    assert ver["reverse_windows"] == 2, ver
    print(f"已发布反向窗步道图 v{published['data_version']}")

    # 14. 窗内反向：TOWER 12:00 出发，等 0 秒即反向走 E5 -> 12:04 到 LAKE
    status, rev = call("POST", "/api/route", {
        "origin": "TOWER", "destination": "LAKE",
        "departure_time": "2026-10-05T12:00:00Z",
    })
    assert status == 200, rev
    (seg,) = rev["segments"]
    assert seg["edge_id"] == "E5", rev
    assert (seg["from_node"], seg["to_node"]) == ("TOWER", "LAKE"), seg
    assert seg["enter_time"] == "2026-10-05T12:00:00Z", seg
    assert seg["leave_time"] == "2026-10-05T12:04:00Z", seg
    print("反向窗内通行:", seg["from_node"], "->", seg["to_node"], seg["leave_time"])

    # 15. 窗外反向不可达：09:00 出发需等到 11:00 开窗（允许节点等待），
    #     11:00 进入、11:04 到达；再测窗结束后的时刻无后续窗可用 → 404
    status, wait_rev = call("POST", "/api/route", {
        "origin": "TOWER", "destination": "LAKE",
        "departure_time": "2026-10-05T09:00:00Z",
    })
    assert wait_rev["segments"][0]["enter_time"] == "2026-10-05T11:00:00Z", wait_rev
    assert wait_rev["segments"][0]["waiting_seconds"] == 7200, wait_rev
    status, no_rev = call("POST", "/api/route", {
        "origin": "TOWER", "destination": "LAKE",
        "departure_time": "2026-10-05T16:30:00Z",
    }, expect_status=404)
    assert no_rev["error"] == "no_route", no_rev
    print("窗外反向：等窗开放可走，之后无窗则 404")

    # 16. 窗内原方向禁行：LAKE 12:00 出发须等到 13:00 窗结束再走 E5
    status, fwd = call("POST", "/api/route", {
        "origin": "LAKE", "destination": "TOWER",
        "departure_time": "2026-10-05T12:00:00Z",
    })
    (fseg,) = fwd["segments"]
    assert (fseg["from_node"], fseg["to_node"]) == ("LAKE", "TOWER"), fseg
    assert fseg["enter_time"] == "2026-10-05T13:00:00Z", fseg
    assert fseg["waiting_seconds"] == 3600, fseg
    print("窗内原方向等待至窗结束:", fseg["enter_time"])

    # 17. 反向窗无效 / 重叠 → 拒绝整次导入，保留旧版本
    bad_rev = json.loads(json.dumps(reverse_graph))
    bad_rev["edges"][4]["reverse_windows"] = [
        {"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T11:00:00Z"}]
    status, rejected = call("POST", "/admin/graph", bad_rev, expect_status=400)
    assert rejected["error"] == "invalid_payload" and rejected["rejected"] is True, rejected
    overlap = json.loads(json.dumps(reverse_graph))
    overlap["edges"][4]["reverse_windows"] = [
        {"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T12:30:00Z"},
        {"start": "2026-10-05T12:00:00Z", "end": "2026-10-05T13:00:00Z"}]
    status, rejected2 = call("POST", "/admin/graph", overlap, expect_status=400)
    assert rejected2["active_version"] == published["data_version"], rejected2
    assert call("GET", "/version")[1]["reverse_windows"] == 2
    print("无效/重叠反向窗拒绝导入，旧版本保留")

    # 恢复不带反向窗的示例图，避免影响后续重复冒烟
    call("POST", "/admin/graph", {
        "nodes": reverse_graph["nodes"],
        "edges": [{k: v for k, v in e.items() if k != "reverse_windows"}
                  for e in reverse_graph["edges"]],
    })
    assert call("GET", "/version")[1]["reverse_windows"] == 0
    print("已恢复无反向窗示例图")

    # 18. 联合预检：候选图 + 封闭记录一次校验，只读不改变当前版本
    version_before = call("GET", "/version")[1]
    ok_precheck = {
        "graph": {
            "nodes": ["N1", "N2", "N3"],
            "edges": [
                {"id": "A1", "from": "N1", "to": "N2", "travel_seconds": 60, "has_stairs": False},
                {"id": "A2", "from": "N2", "to": "N3", "travel_seconds": 90, "has_stairs": True,
                 "reverse_windows": [
                     {"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T12:00:00Z"},
                     {"start": "2026-10-05T13:00:00Z", "end": "2026-10-05T14:00:00Z"}]},
            ],
        },
        "closures": {"closures": [
            {"edge_id": "A1", "start": "2026-10-05T08:00:00Z", "end": "2026-10-05T09:00:00Z"}]},
    }
    status, pre = call("POST", "/admin/precheck", ok_precheck)
    assert status == 200 and pre["ok"] is True and pre["errors"] == [], pre
    assert pre["counts"] == {"nodes": 3, "edges": 2, "closures": 1, "reverse_windows": 2}, pre
    assert pre["data_version"] == version_before["data_version"], pre
    print("联合预检通过:", pre["counts"])

    # 候选图有效时，封闭记录按候选图的路段编号校验（与库中版本无关）
    status, pre_ref = call("POST", "/admin/precheck", {
        "graph": ok_precheck["graph"],
        "closures": {"closures": [
            {"edge_id": "ZZ", "start": "2026-10-05T08:00:00Z", "end": "2026-10-05T09:00:00Z"}]},
    })
    assert pre_ref["ok"] is False, pre_ref
    assert [(e["source"], e["path"], e["index"]) for e in pre_ref["errors"]] == [
        ("closures", "closures[0].edge_id", 0)], pre_ref

    # 候选图无效：封闭记录的路段引用不误报，自身错误仍报告；版本不变
    status, pre_bad = call("POST", "/admin/precheck", {
        "graph": {
            "nodes": ["N1", "N2"],
            "edges": [
                {"id": "A1", "from": "N1", "to": "GHOST",
                 "travel_seconds": 0, "has_stairs": False},
            ],
        },
        "closures": {"closures": [
            {"edge_id": "A1", "start": "2026-10-05T09:00:00Z", "end": "2026-10-05T08:00:00Z"},
            {"edge_id": "UNKNOWN", "start": "2026-10-05T08:00:00Z", "end": "2026-10-05T09:00:00Z"},
        ]},
    })
    assert status == 200 and pre_bad["ok"] is False, pre_bad
    paths = {(e["source"], e["path"]) for e in pre_bad["errors"]}
    assert ("graph", "edges[0].to") in paths, pre_bad
    assert ("graph", "edges[0].travel_seconds") in paths, pre_bad
    assert ("closures", "closures[0]") in paths, pre_bad  # 自身时间区间错误
    assert ("closures", "closures[1].edge_id") not in paths, pre_bad  # 图无效→不误报引用
    assert call("GET", "/version")[1]["data_version"] == version_before["data_version"]
    print("联合预检失败: 收集全部错误且不改动版本 ✔")

    # 预检包络非法（缺 closures）→ 400
    status, pre_env = call("POST", "/admin/precheck",
                           {"graph": ok_precheck["graph"]}, expect_status=400)
    assert pre_env["error"] == "invalid_payload", pre_env

    # 19. 版本恢复：把历史版本 v1 整体恢复为新的生效版本（版本号递增，非回拨）
    ver_now = call("GET", "/version")[1]
    status, restored = call("POST", "/admin/restore", {"version": 1})
    assert restored["status"] == "restored", restored
    assert restored["source_version"] == 1, restored
    assert restored["previous_version"] == ver_now["data_version"], restored
    assert restored["data_version"] == ver_now["data_version"] + 1, restored
    v_after = call("GET", "/version")[1]
    assert v_after["data_version"] == restored["data_version"], v_after
    # v1 是纯步道图版本：封闭记录与反向窗计数随内容整体恢复
    assert v_after["closures"] == 0 and v_after["reverse_windows"] == 0, v_after
    print(f"版本恢复: v{ver_now['data_version']} -> v{restored['data_version']}"
          f"（内容=v1，历史版本仍可追溯）")

    # 恢复回冒烟前的版本，保证服务数据不受影响
    status, back = call("POST", "/admin/restore",
                        {"version": ver_now["data_version"]})
    assert back["source_version"] == ver_now["data_version"], back
    v_back = call("GET", "/version")[1]
    assert v_back["closures"] == ver_now["closures"], v_back
    assert v_back["reverse_windows"] == ver_now["reverse_windows"], v_back

    # 目标版本不存在 → 404 version_not_found，生效版本不变
    status, nf = call("POST", "/admin/restore", {"version": 999999},
                      expect_status=404)
    assert nf["error"] == "version_not_found" and nf["rejected"] is True, nf
    assert nf["active_version"] == v_back["data_version"], nf
    # 参数非法 → 400 invalid_payload，生效版本不变
    for bad_payload in ({"version": 0}, {"version": "1"}, {"version": True}, {}):
        status, bad = call("POST", "/admin/restore", bad_payload,
                           expect_status=400)
        assert bad["error"] == "invalid_payload", bad
    assert call("GET", "/version")[1]["data_version"] == v_back["data_version"]
    print("恢复拒绝：不存在版本 404、非法参数 400，生效版本均不变 ✔")

    # 20. 容量确认（高峰行程）：示例图 E1/E3/E5 容量为 2，其余不限。
    #     发布一张确定的容量图（新世代），用往返的半开占用验证串行确认。
    cap_graph = {
        "nodes": ["P", "Q", "R"],
        "edges": [
            {"id": "C1", "from": "P", "to": "Q", "travel_seconds": 120,
             "has_stairs": False, "capacity": 2},
            {"id": "C2", "from": "Q", "to": "R", "travel_seconds": 180,
             "has_stairs": False, "capacity": 2},
        ],
    }
    status, pub = call("POST", "/admin/graph", cap_graph)
    assert status == 200, pub
    gen_after_graph = call("GET", "/version")[1]["data_version"]

    book = {
        "origin": "P", "destination": "R",
        "departure_time": "2026-10-06T08:00:00Z",
        "avoid_stairs": False, "people": 2,
    }
    status, b1 = call("POST", "/api/trips", book, expect_status=201)
    assert [s["edge_id"] for s in b1["segments"]] == ["C1", "C2"], b1
    assert (b1["segments"][0]["enter_time"], b1["segments"][0]["capacity"]) == (
        "2026-10-06T08:00:00Z", 2), b1
    assert b1["generation_id"] == gen_after_graph, b1
    print("容量确认 #1:", b1["trip_id"],
          [s["enter_time"][11:19] for s in b1["segments"]])

    # 同发第二单（各 2 人、容量 2）：半开占用串行，C1 须等到 08:02，C2 等到 08:05
    status, b2 = call("POST", "/api/trips", book, expect_status=201)
    assert [s["enter_time"][11:19] for s in b2["segments"]] == ["08:02:00", "08:05:00"], b2
    assert b2["arrival_time"] == "2026-10-06T08:08:00Z", b2
    print("容量确认 #2（容量不足节点等待）:",
          [s["enter_time"][11:19] for s in b2["segments"]], "->", b2["arrival_time"][11:19])

    # 人数超过容量（people=3 > 2，唯一路径）→ 404 no_route，且不留占用
    status, full = call("POST", "/api/trips", {**book, "people": 3}, expect_status=404)
    assert full["error"] == "no_route", full

    # 负载非法 → 400 invalid_payload
    for bad_book in ({**book, "people": 0}, {**book, "people": "2"},
                     {**book, "departure_time": "nope"},
                     {**book, "origin": "GHOST"}, {**book, "stops": []}):
        status, bx = call("POST", "/api/trips", bad_book, expect_status=400)
        assert bx["error"] == "invalid_payload", bx

    # 数据版本过期 → 409 version_conflict，不写入
    status, conflict = call("POST", "/api/trips",
                            {**book, "data_version": b1["data_version"] - 1 or 1},
                            expect_status=409)
    assert conflict["error"] == "version_conflict", conflict
    assert conflict["current_version"] == gen_after_graph, conflict
    print("过期版本确认 → 409 version_conflict（不写入过时方案）")

    # 携带当前版本可正常确认
    status, b3 = call("POST", "/api/trips",
                      {**book, "data_version": gen_after_graph}, expect_status=201)
    assert b3["data_version"] == gen_after_graph, b3

    # 按编号查询
    status, got = call("GET", f"/api/trips/{b1['trip_id']}")
    assert got["trip_id"] == b1["trip_id"] and got["status"] == "confirmed", got
    assert got["people"] == 2 and len(got["segments"]) == 2, got
    status, missing = call("GET", "/api/trips/999999", expect_status=404)
    assert missing["error"] == "trip_not_found", missing
    status, badid = call("GET", "/api/trips/xyz", expect_status=400)
    assert badid["error"] == "invalid_payload", badid

    # 取消：首次释放、重复取消幂等不重复释放
    status, cancel1 = call("POST", f"/api/trips/{b1['trip_id']}/cancel")
    assert cancel1["status"] == "cancelled" and cancel1["released"] is True, cancel1
    status, cancel2 = call("POST", f"/api/trips/{b1['trip_id']}/cancel")
    assert cancel2["released"] is False, cancel2
    status, gotc = call("GET", f"/api/trips/{b1['trip_id']}")
    assert gotc["status"] == "cancelled" and gotc["cancelled_at"], gotc
    print("取消幂等：首次 released=true，重复取消 released=false，行程仍可查询")

    # 容量已释放：同发新单可立即进入 C1（08:00 出发）
    status, b4 = call("POST", "/api/trips", book, expect_status=201)
    assert b4["segments"][0]["enter_time"] == "2026-10-06T08:00:00Z", b4

    # 封闭记录更新：同世代继续计量（b2/b3/b4 仍占容量）
    status, cp = call("POST", "/admin/closures", {"closures": []})
    assert status == 200, cp
    ver_same = call("GET", "/version")[1]["data_version"]
    status, b5 = call("POST", "/api/trips",
                      {**book, "data_version": ver_same}, expect_status=201)
    # b2 占 C1[08:02,08:04)、b3 占 C1[08:04,08:06)、b4 占 C1[08:00,08:02)：
    # 08:00 出发的新单须排到 08:06 进入 C1
    assert b5["segments"][0]["enter_time"] == "2026-10-06T08:06:00Z", b5
    print("封闭更新同世代：已确认行程继续计入容量，新单排队",
          b5["segments"][0]["enter_time"][11:19])

    # 重新发布图 → 新世代独立计量；旧行程仍可查询/取消
    status, rep = call("POST", "/admin/graph", cap_graph)
    assert status == 200, rep
    assert rep["data_version"] != b2["generation_id"], rep
    status, b6 = call("POST", "/api/trips", book, expect_status=201)
    assert b6["segments"][0]["enter_time"] == "2026-10-06T08:00:00Z", b6
    assert b6["generation_id"] != b2["generation_id"], b6
    status, old_got = call("GET", f"/api/trips/{b2['trip_id']}")
    assert old_got["status"] == "confirmed", old_got
    status, old_cancel = call("POST", f"/api/trips/{b2['trip_id']}/cancel")
    assert old_cancel["released"] is True, old_cancel
    print("重新发布：新世代独立计量，旧行程仍可查询和取消 ✔")

    # 普通路线查询语义不变：容量字段不出现在 /api/route 响应
    status, plain = call("POST", "/api/route", {
        "origin": "P", "destination": "R",
        "departure_time": "2026-10-06T08:00:00Z"})
    assert plain["edge_sequence"] == ["C1", "C2"], plain
    assert "capacity" not in plain["segments"][0], plain

    if failures:
        print("\n".join("FAIL: " + f for f in failures), file=sys.stderr)
        return 1
    print("全部冒烟检查通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
