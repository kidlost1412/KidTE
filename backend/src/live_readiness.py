"""Trợ lý chuẩn bị LIVE: hàm thuần, chỉ đọc dict dashboard_state đã có.

build_readiness(dash, extra) -> {ok, steps:[...], summary:{done,total,blocking}}
extra: {video_library: {count, ready} | None, whisper_ready: bool | None}
Không I/O; dashboard_state đã gom sẵn dữ liệu.
"""

PLUGIN_SYNC = "dong-bo-video"
STATES = ("done", "todo", "blocked", "error", "optional", "na")


def _d(value):
    return value if isinstance(value, dict) else {}


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _nav(page, sub="", arg=""):
    return {"kind": "navigate", "page": page, "sub": sub, "arg": arg}


def _post(endpoint, body=None, label=""):
    return {"kind": "post", "endpoint": endpoint, "body": body or {}, "label": label}


def _toggle(target, on, label=""):
    return {"kind": "toggle", "target": target, "on": bool(on), "label": label}


def _step(sid, group, title, why, state, detail="", required=True, action=None):
    return {"id": sid, "group": group, "title": title, "why": why, "state": state,
            "detail": detail, "required": bool(required), "action": action}


def build_readiness(dash, extra=None):
    dash, extra = _d(dash), _d(extra)
    ext = _d(dash.get("extension"))
    ai = _d(dash.get("ai"))
    catalog = _d(dash.get("catalog"))
    channels = _d(dash.get("live_channels"))
    tiktok, shopee = _d(channels.get("tiktok")), _d(channels.get("shopee"))
    shopee_on = bool(shopee) and _d(shopee.get("settings")).get("enabled") is not False
    captcha = _d(dash.get("captcha"))
    review = _d(dash.get("video_review"))
    library = _d(extra.get("video_library"))
    plugins = _d(dash.get("plugins")).get("plugins") or []
    sync = next((p for p in plugins if isinstance(p, dict) and p.get("id") == PLUGIN_SYNC), None)
    sync_running = bool(sync) and _d(sync.get("runtime")).get("state") == "running"
    connected = bool(ext.get("connected"))
    steps = []
    add = steps.append

    # A: chuẩn bị một lần
    add(_step("A1", "once", "Nối Kid TE với Chrome (TikTok)",
              "Tiện ích là tay chân của Kid TE trên trang LIVE: đọc bình luận, trả lời, ghim.",
              "done" if connected else "todo",
              "Đã kết nối" if connected else "Chưa thấy tiện ích TikTok gửi tín hiệu",
              action=_post("/api/extension/setup", {}, "Cài tiện ích")))
    sh_ext = bool(_d(shopee.get("extension")).get("connected"))
    add(_step("A2", "once", "Nối Chrome cho Shopee", "Chỉ cần nếu bạn bán trên Shopee LIVE.",
              "na" if not shopee_on else ("done" if sh_ext else "todo"),
              "Đang tắt Shopee" if not shopee_on else ("Đã kết nối" if sh_ext else "Chưa thấy tiện ích Shopee"),
              required=shopee_on, action=_post("/api/extension/setup", {"platform": "shopee"}, "Cài tiện ích Shopee")))
    ai_ok = bool(ai.get("deepseek_available"))
    add(_step("A3", "once", "Nhập khóa AI (DeepSeek)", "Không có khóa thì AI không trả lời được khách.",
              "done" if ai_ok else "todo",
              "AI sẵn sàng" if ai_ok else "Chưa có khóa hoặc khóa chưa dùng được",
              action=_nav("system", "setup")))
    approved = _int(catalog.get("approved_profiles"))
    add(_step("A4", "once", "Thêm sản phẩm và duyệt hồ sơ", "AI chỉ tư vấn sản phẩm đã có hồ sơ được duyệt.",
              "done" if approved else "todo", f"{approved} hồ sơ đã duyệt",
              action=_nav("catalog", "profiles")))
    total = _int(catalog.get("total"))
    add(_step("A5", "once", "Lấy giỏ hàng từ TikTok",
              "Mở LIVE Manager trên Chrome rồi bấm “Quét giỏ” trong tiện ích.",
              "done" if total else ("todo" if connected else "blocked"),
              f"{total} sản phẩm trong giỏ" if total else ("Chưa có giỏ hàng" if connected else "Cần nối tiện ích trước"),
              action=_nav("catalog", "products")))
    unmapped = _int(catalog.get("unmapped"))
    add(_step("A6", "once", "Gắn hồ sơ cho từng sản phẩm", "Sản phẩm chưa gắn hồ sơ thì AI không được tư vấn.",
              "blocked" if not total or not approved else ("done" if not unmapped else "todo"),
              (f"Còn {unmapped} sản phẩm chưa gắn" if unmapped else "Đã gắn đủ") if total and approved
              else "Cần giỏ hàng và hồ sơ trước",
              action=_post("/api/mapping/auto-match", {}, "Tự khớp")))
    whisper = extra.get("whisper_ready")
    add(_step("A7", "once", "Chuẩn bị nghe video (Whisper)", "Cần để AI nghe video và gợi ý lúc ghim sản phẩm.",
              "done" if whisper else ("todo" if whisper is False else "optional"),
              "Đã có model chép lời" if whisper else "Chưa tải model chép lời",
              required=whisper is not None, action=_nav("system", "whisper")))
    health = _d(captcha.get("health")).get("state")
    if captcha.get("key_rejected"):
        cstate, cdetail = "error", "Khóa giải CAPTCHA bị từ chối"
    elif captcha.get("configured") and health in ("ready", "solving"):
        cstate, cdetail = "done", "Đang chạy"
    else:
        cstate, cdetail = "optional", "Đang chạy" if captcha.get("running") else "Chưa bật"
    add(_step("A8", "once", "Chống CAPTCHA (khuyên dùng)", "Tự giải ô xác minh để LIVE không bị ngắt.",
              cstate, cdetail, required=False, action=_nav("captcha")))

    # B: trước mỗi phiên
    count, ready = _int(library.get("count")), _int(library.get("ready"))
    known = bool(library)
    add(_step("B1", "session", "Chọn và tải video phát lại", "Kid TE cần biết video bạn sẽ phát.",
              "done" if count else "todo", f"{count} video trong thư viện" if known else "Chưa đọc được thư viện",
              action=_nav("video", "library", "upload")))
    pending = _int(review.get("pending_jobs"))
    job = str(review.get("first_job_id") or "")
    add(_step("B2", "session", "Kiểm lịch ghim", "Duyệt các mốc ghim AI gợi ý rồi bấm Phát hành.",
              "blocked" if not count else ("done" if ready and not pending else "todo"),
              f"{pending} video chờ duyệt" if pending else f"{ready} video đã có lịch ghim",
              action=_nav("video", "review", job) if job else _nav("video", "library")))
    sources = []
    tset = _d(tiktok.get("settings"))
    if tset:
        sources.append(("TikTok", tset.get("enabled") is not False and bool(
            tset.get("device_id") if tset.get("mode") == "device" else tset.get("process_name"))))
    sset = _d(shopee.get("settings"))
    if shopee_on and sset:
        sources.append(("Shopee", bool(sset.get("input_name") or sset.get("bindings"))))
    src_ok = bool(sources) and all(ok for _, ok in sources)
    add(_step("B3", "session", "Chọn nguồn phát", "Để Kid TE biết video đang chạy tới đâu.",
              "done" if src_ok else "todo",
              " · ".join(f"{n}: {'đã chọn' if ok else 'chưa chọn'}" for n, ok in sources) or "Chưa đặt nguồn",
              action=_nav("system", "live")))
    add(_step("B4", "session", "Bật đồng bộ video", "Cần để biết đang tới sản phẩm nào mà ghim đúng.",
              "done" if sync_running else ("error" if not sync else "todo"),
              "Đang chạy" if sync_running else ("Không thấy dịch vụ đồng bộ video" if not sync else "Đang tắt"),
              action=_toggle("sync", not sync_running, "Tắt" if sync_running else "Bật") if sync else _nav("automation")))
    backup = _d(dash.get("backup"))
    fresh = bool(backup) and not backup.get("overdue")
    add(_step("B5", "session", "Sao lưu nhanh", "Giữ an toàn dữ liệu trước khi LIVE.",
              "done" if fresh else "optional",
              "Đã sao lưu gần đây" if fresh else "Đã lâu chưa sao lưu",
              required=False, action=None if fresh else _post("/api/backup", {}, "Sao lưu ngay")))

    # C: trong phiên (không tính vào số việc bắt buộc)
    live = connected and bool(ext.get("live_running"))
    add(_step("C1", "live", "Mở LIVE trên TikTok/Shopee", "Bắt đầu phát trên sàn, tiện ích sẽ tự nhận phòng.",
              "done" if live else "todo", "Đang LIVE" if live else "Chưa thấy phiên LIVE", required=False))
    positions = [(_d(ch.get("position")).get("available"), _d(ch.get("health")).get("reason"))
                 for ch in (tiktok, shopee if shopee_on else {}) if ch]
    found = any(p for p, _ in positions)
    reason = "" if found else next((r for p, r in positions if r), "")
    add(_step("C2", "live", "Video đã nhận diện", "Kid TE nghe/đọc nguồn phát để biết video đang ở đâu.",
              "done" if found else ("todo" if live else "blocked"), reason, required=False,
              action=None if found else _post("/api/live/reidentify", {}, "Nghe lại")))
    add(_step("C3", "live", "Tự ghim đang chạy", "Tiện ích tự ghim theo lịch khi phòng đang LIVE.",
              "done" if found and live else "optional",
              "Đang theo lịch ghim" if found and live else "Sẽ chạy khi đang LIVE và nhận được video", required=False))
    ai_on = bool(ext.get("ai_enabled")) and bool(ext.get("ai_ready"))
    add(_step("C4", "live", "Bật AI trả lời", "AI trả lời bình luận khách khi được bật.",
              "done" if ai_on else ("todo" if connected else "blocked"),
              "AI đang trả lời" if ai_on else "AI đang tắt", required=False))
    add(_step("C5", "live", "Theo dõi khách hỏi", "Xem câu chưa trả lời và duyệt câu AI đã gửi.",
              "optional", "", required=False, action=_nav("inbox", "todo")))

    counted = [s for s in steps if s["state"] != "na"]
    done = sum(1 for s in counted if s["state"] == "done")
    blocking = sum(1 for s in steps if s["required"] and s["group"] != "live"
                   and s["state"] in ("todo", "blocked", "error"))
    return {"ok": True, "steps": steps, "summary": {"done": done, "total": len(counted), "blocking": blocking}}
