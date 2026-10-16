"""Trợ lý chuẩn bị LIVE: hàm thuần, chỉ đọc dict dashboard_state đã có.

build_readiness(dash, extra) -> {ok, steps:[...], platforms:{tiktok, shopee}, summary:{done,total,blocking}}
extra: {video_library: {count, ready} | None, whisper_ready: bool | None}
Mỗi bước có "platform": "tiktok" | "shopee" | None (dùng chung). Bước của sàn không dùng
(theo nhận diện tự động) thành "na"; trang có thể chọn lại sàn rồi tính lại (live-wizard-model.js).
Không I/O; dashboard_state đã gom sẵn dữ liệu.
"""

PLUGIN_SYNC = "dong-bo-video"
STATES = ("done", "todo", "blocked", "error", "optional", "na")
SHOPEE_MODES = ("obs",)


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


def _step(sid, group, title, why, state, detail="", required=True, action=None, platform=None):
    return {"id": sid, "group": group, "title": title, "why": why, "state": state,
            "detail": detail, "required": bool(required), "action": action, "platform": platform}


def platform_info(dash):
    """Sàn đang dùng: nhận diện bật hoặc tiện ích đã kết nối/đang LIVE."""
    dash = _d(dash)
    channels = _d(dash.get("live_channels"))
    tiktok, shopee = _d(channels.get("tiktok")), _d(channels.get("shopee"))
    tk_ext = {**_d(tiktok.get("extension")), **_d(dash.get("extension"))}
    sp_ext = _d(shopee.get("extension"))
    tk_conn, sp_conn = bool(tk_ext.get("connected")), bool(sp_ext.get("connected"))
    tk_live = tk_conn and bool(tk_ext.get("live_running"))
    sp_live = bool(sp_ext.get("live_running")) and sp_ext.get("connected") is not False
    tk_on = _d(tiktok.get("settings")).get("enabled") is True
    sp_on = _d(shopee.get("settings")).get("enabled") is True
    # Ưu tiên: sàn đang LIVE > sàn có tiện ích kết nối > sàn bật nhận diện > TikTok.
    for tk, sp in ((tk_live, sp_live), (tk_conn, sp_conn), (tk_on, sp_on), (True, False)):
        if tk or sp:
            break
    return {"tiktok": {"active": bool(tk), "connected": tk_conn, "live": tk_live, "enabled": tk_on},
            "shopee": {"active": bool(sp), "connected": sp_conn, "live": sp_live, "enabled": sp_on}}


def summarize(steps):
    counted = [s for s in steps if s["state"] != "na"]
    return {"done": sum(1 for s in counted if s["state"] == "done"), "total": len(counted),
            "blocking": sum(1 for s in counted if s["required"] and s["group"] != "live"
                            and s["state"] in ("todo", "blocked", "error"))}


def build_readiness(dash, extra=None):
    dash, extra = _d(dash), _d(extra)
    ext = _d(dash.get("extension"))
    ai = _d(dash.get("ai"))
    catalog = _d(dash.get("catalog"))
    channels = _d(dash.get("live_channels"))
    tiktok, shopee = _d(channels.get("tiktok")), _d(channels.get("shopee"))
    plats = platform_info(dash)
    tk_use, sp_use = plats["tiktok"]["active"], plats["shopee"]["active"]
    captcha = _d(dash.get("captcha"))
    review = _d(dash.get("video_review"))
    library = _d(extra.get("video_library"))
    plugins = _d(dash.get("plugins")).get("plugins") or []
    sync = next((p for p in plugins if isinstance(p, dict) and p.get("id") == PLUGIN_SYNC), None)
    sync_running = bool(sync) and _d(sync.get("runtime")).get("state") == "running"
    connected = plats["tiktok"]["connected"]
    sh_beat = _d(shopee.get("extension"))
    sh_ext = plats["shopee"]["connected"]
    tk_live, sp_live = plats["tiktok"]["live"], plats["shopee"]["live"]
    steps = []
    add = steps.append

    # A: chuẩn bị một lần
    add(_step("A1", "once", "Nối Kid TE với Chrome (TikTok)",
              "Tiện ích là tay chân của Kid TE trên trang LIVE: đọc bình luận, trả lời, ghim.",
              "done" if connected else "todo",
              "Đã kết nối" if connected else "Chưa thấy tiện ích TikTok gửi tín hiệu",
              action=_post("/api/extension/setup", {}, "Cài tiện ích"), platform="tiktok"))
    add(_step("A2", "once", "Nối Chrome cho Shopee", "Tiện ích Shopee đọc bình luận và trả lời trên Shopee LIVE.",
              "done" if sh_ext else "todo", "Đã kết nối" if sh_ext else "Chưa thấy tiện ích Shopee",
              action=_post("/api/extension/setup", {"platform": "shopee"}, "Cài tiện ích Shopee"), platform="shopee"))
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
              action=_nav("catalog", "products"), platform="tiktok"))
    sp_total = _int(_d(shopee.get("catalog")).get("products_count"))
    add(_step("A5S", "once", "Lấy giỏ hàng từ Shopee", "Tiện ích Shopee đọc giỏ của phòng LIVE.",
              "done" if sp_total else ("todo" if sh_ext else "blocked"),
              f"{sp_total} sản phẩm trong giỏ" if sp_total else ("Chưa có giỏ hàng" if sh_ext else "Cần nối tiện ích Shopee trước"),
              action=_nav("catalog", "products"), platform="shopee"))
    unmapped = _int(catalog.get("unmapped"))
    any_cart = total or sp_total
    add(_step("A6", "once", "Gắn hồ sơ cho từng sản phẩm", "Sản phẩm chưa gắn hồ sơ thì AI không được tư vấn.",
              "blocked" if not any_cart or not approved else ("done" if not unmapped else "todo"),
              (f"Còn {unmapped} sản phẩm chưa gắn" if unmapped else "Đã gắn đủ") if any_cart and approved
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
    add(_step("A8", "once", "Chống CAPTCHA (khuyên dùng)", "Tự giải ô xác minh để TikTok LIVE không bị ngắt.",
              cstate, cdetail, required=False, action=_nav("captcha"), platform="tiktok"))

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
              (f"{pending} video mới chờ duyệt" if ready else f"{pending} video chờ duyệt") if pending
              else f"{ready} video đã có lịch ghim",
              required=not ready,
              action=_nav("video", "review", job) if job else _nav("video", "library")))
    tset = _d(tiktok.get("settings"))
    tk_src = tset.get("enabled") is not False and bool(
        (tset.get("device_id") if tset.get("mode") == "device" else tset.get("process_name"))
        or _d(tiktok.get("position")).get("available"))
    add(_step("B3", "session", "Chọn nguồn phát TikTok", "Để Kid TE biết video TikTok đang chạy tới đâu.",
              "done" if tk_src else "todo", "Đã chọn" if tk_src else "Chưa chọn",
              action=_nav("system", "live"), platform="tiktok"))
    sset = _d(shopee.get("settings"))
    sp_src = sset.get("enabled") is True and str(sset.get("mode") or "obs") in SHOPEE_MODES and bool(
        _d(shopee.get("position")).get("available") or sset.get("input_name") or sset.get("bindings")
        or sset.get("password_configured"))
    add(_step("B3S", "session", "Chọn nguồn phát Shopee (OBS)", "Để Kid TE biết video Shopee đang chạy tới đâu.",
              "done" if sp_src else "todo",
              "Đã chọn" if sp_src else ("Nhận diện Shopee đang tắt" if sset.get("enabled") is not True else "Chưa chọn"),
              action=_nav("system", "live"), platform="shopee"))
    add(_step("B4", "session", "Bật đồng bộ video TikTok", "Cần để biết đang tới sản phẩm nào mà ghim đúng.",
              "done" if sync_running else ("error" if not sync else "todo"),
              "Đang chạy" if sync_running else ("Không thấy dịch vụ đồng bộ video" if not sync else "Đang tắt"),
              action=_toggle("sync", not sync_running, "Tắt" if sync_running else "Bật") if sync else _nav("automation"),
              platform="tiktok"))
    sp_pos = _d(shopee.get("position"))
    sp_sync = bool(sp_pos.get("available")) or sp_pos.get("state") == "locked"
    add(_step("B4S", "session", "Đồng bộ video Shopee (OBS)", "Shopee tự đọc vị trí video từ OBS, chạy riêng.",
              "done" if sp_sync or sp_src else "todo",
              "Đang khớp video" if sp_sync else ("Nguồn OBS đã xác nhận" if sp_src else "Chưa đọc được OBS"),
              action=_nav("system", "live"), platform="shopee"))
    backup = _d(dash.get("backup"))
    fresh = bool(backup) and not backup.get("overdue")
    add(_step("B5", "session", "Sao lưu nhanh", "Giữ an toàn dữ liệu trước khi LIVE.",
              "done" if fresh else "optional",
              "Đã sao lưu gần đây" if fresh else "Đã lâu chưa sao lưu",
              required=False, action=None if fresh else _post("/api/backup", {}, "Sao lưu ngay")))

    # C: trong phiên (không tính vào số việc bắt buộc). Xét theo từng sàn đang dùng.
    use = [("TikTok", tiktok, tk_live, tk_use), ("Shopee", shopee, sp_live, sp_use)]
    live_names = [n for n, _, lv, _ in use if lv]
    live = bool(live_names)
    add(_step("C1", "live", "Mở LIVE trên sàn", "Bắt đầu phát trên sàn, tiện ích sẽ tự nhận phòng.",
              "done" if live else "todo",
              ("Đang LIVE: " + ", ".join(live_names)) if live else "Chưa thấy phiên LIVE", required=False))
    watch = [(n, ch) for n, ch, lv, u in use if lv or (u and not live)]
    pos = [(n, bool(_d(ch.get("position")).get("available")), _d(ch.get("health")).get("reason")) for n, ch in watch]
    found_names = [n for n, ok, _ in pos if ok]
    found = bool(found_names)
    reason = "" if found else next((r for _, _, r in pos if r), "")
    add(_step("C2", "live", "Video đã nhận diện", "Kid TE nghe/đọc nguồn phát để biết video đang ở đâu.",
              "done" if found else ("todo" if live else "blocked"),
              ("Đã khớp: " + ", ".join(found_names)) if found else reason, required=False,
              action=None if found else _post("/api/live/reidentify", {}, "Nghe lại")))
    # Tự ghim: TikTok theo đồng hồ ghim của tiện ích (pin_running); tiện ích cũ thì suy từ vị trí video.
    # Shopee: pin_running có thể null -> suy từ đang LIVE + có vị trí video.
    tk_found = bool(_d(tiktok.get("position")).get("available"))
    sp_found = bool(_d(shopee.get("position")).get("available"))
    pin_flag = ext.get("pin_running") if connected else None
    tk_pin = (pin_flag is True and tk_found) if isinstance(pin_flag, bool) else (tk_found and tk_live)
    sp_flag = sh_beat.get("pin_running")
    sp_pin = (sp_flag is True and sp_found) if isinstance(sp_flag, bool) else (sp_found and sp_live)
    pin_names = [n for n, ok in (("TikTok", tk_pin), ("Shopee", sp_pin)) if ok]
    add(_step("C3", "live", "Tự ghim đang chạy", "Tiện ích tự ghim theo lịch khi phòng đang LIVE.",
              "done" if pin_names else "optional",
              ("Đang theo lịch ghim: " + ", ".join(pin_names)) if pin_names else
              ("Tự ghim đang tạm dừng" if pin_flag is False or sp_flag is False else "Sẽ chạy khi đang LIVE và nhận được video"),
              required=False))
    ai_tk = bool(ext.get("ai_enabled")) and bool(ext.get("ai_ready"))
    ai_sp = sh_beat.get("connected") is not False and bool(sh_beat.get("ai_enabled")) and bool(sh_beat.get("ai_ready"))
    ai_names = [n for n, ok in (("TikTok", ai_tk), ("Shopee", ai_sp)) if ok]
    add(_step("C4", "live", "Bật AI trả lời", "AI trả lời bình luận khách khi được bật.",
              "done" if ai_names else ("todo" if connected or sh_ext else "blocked"),
              f"AI đang trả lời ({', '.join(ai_names)})" if ai_names else "AI đang tắt", required=False))
    add(_step("C5", "live", "Theo dõi khách hỏi", "Xem câu chưa trả lời và duyệt câu AI đã gửi.",
              "optional", "", required=False, action=_nav("inbox", "todo")))

    for s in steps:
        pf = s["platform"]
        if pf and not plats[pf]["active"]:
            # Giữ trạng thái thật để trang đổi lựa chọn sàn mà không cần hỏi lại máy chủ.
            s["real"] = {"state": s["state"], "required": s["required"], "detail": s["detail"]}
            s["state"], s["required"], s["detail"] = "na", False, "Không dùng sàn này"
    return {"ok": True, "steps": steps, "platforms": plats, "summary": summarize(steps)}
