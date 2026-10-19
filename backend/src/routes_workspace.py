"""Bộ xử lý route HTTP (workspace) — tách NGUYÊN VĂN từ Handler.do_GET/do_POST trong server.py.

Mỗi hàm nhận ``self`` là Handler hiện tại (send_json, local_tool_request, engine...).
Tên toàn cục (ROOT, WEB_DIR, clean, report_problem, ...) được tra trong module server:
http_routes.bind() gắn lại __globals__ bằng engine_mixins.rebind — không import server ở đây.
"""
from __future__ import annotations

# ruff: noqa: F821  (tên toàn cục lấy từ server khi bind)


def get_learning_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        if route == "/api/learning":
            self.send_json(200, self.engine.learning_state())
        elif route == "/api/learning/prompt-view":
            self.send_json(200, self.engine.learning_prompt_view(
                (parse_qs(parsed.query).get("persona_id") or [""])[0]))
        elif route == "/api/learning/runs":
            ref = (parse_qs(parsed.query).get("ref") or [""])[0]
            self.send_json(200, {"ok": True, "runs": [
                self.engine.learning_run(r["run_id"])["run"]
                for r in self.engine.learning.runs("distill", limit=5, ref_id=ref)] if ref else []})
        elif route.startswith("/api/learning/runs/"):
            self.send_json(200, self.engine.learning_run(unquote(route.rsplit("/", 1)[-1])))
        else:
            self.send_json(404, {"ok": False, "error": "not_found"})
    except ValueError as exc:
        self.send_json(400, {"ok": False, "error": str(exc)})


def get_workspace_sales_playbook(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.sales_playbook_state())


def get_workspace_dose_rules(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    import dose_rules
    with self.engine.workspace._db() as db:
        self.send_json(200, dose_rules.list_rules(
            db, (parse_qs(parsed.query).get("sku") or [""])[0] or None))


def get_workspace_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    try:
        if len(parts) == 1:
            result = self.engine.workspace_list(parts[0], {
                key: values[0] for key, values in parse_qs(parsed.query).items()
            })
        elif len(parts) == 2:
            result = self.engine.workspace_detail(parts[0], parts[1])
        else:
            raise ValueError("workspace_route_not_found")
        self.send_json(200, result)
    except ValueError as exc:
        self.send_json(404 if "not_found" in str(exc) else 400,
                       {"ok": False, "error": str(exc)})


def get_profiles_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    tail = [unquote(p) for p in route[len("/api/profiles/"):].split("/") if p]
    if tail == ["enrich-batch"]:
        self.send_json(200, self.engine.enrich.batch_status())
    elif len(tail) == 2 and tail[1] == "enrich":
        self.send_json(200, self.engine.enrich.status(tail[0]))
    else:
        self.send_json(404, {"ok": False, "error": "not_found"})


def get_ingest_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        tail = [unquote(p) for p in route[len("/api/ingest/"):].split("/") if p]
        if tail == ["status"]:
            self.send_json(200, {"ok": True, **self.engine.ingest.status()})
        elif tail == ["defaults-status"]:
            self.send_json(200, self.engine.product_defaults.status())
        elif tail == ["removed-products"]:
            self.send_json(200, {"ok": True, "products": list(product_blocklist.removed_products().values())})
        elif len(tail) == 2 and tail[0] == "suggest":
            self.send_json(200, {"ok": True, "suggestions": self.engine.ingest.suggest_skus(tail[1])})
        elif len(tail) == 2 and tail[0] == "image":
            self.send_local_file(self.engine.ingest.staged_image_path(tail[1]), "image/webp")
        else:
            self.send_json(404, {"ok": False, "error": "not_found"})
    except ValueError as exc:
        self.send_json(404, {"ok": False, "error": str(exc)})


def get_product_image_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        product_id = unquote(route.rsplit("/", 1)[-1])
        path = self.engine.workspace.product_image_path(product_id)
        mime = "image/png" if path.suffix.lower() == ".png" else "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
        self.send_local_file(path, mime)
    except ValueError as exc:
        self.send_json(404, {"ok": False, "error": str(exc)})


def get_profile_thumb_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        path = self.engine.profile_thumb_path(unquote(route.rsplit("/", 1)[-1]))
        self.send_cacheable_file(path, "image/jpeg")
    except ValueError as exc:
        self.send_json(404, {"ok": False, "error": str(exc)})


def get_product_thumb_prefix(self, route, parsed):
    from urllib.parse import parse_qs, unquote, urlsplit
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    try:
        path = self.engine.product_thumb_path(unquote(route.rsplit("/", 1)[-1]))
        suffix = path.suffix.lower()
        mime = ("image/webp" if suffix == ".webp" else
                "image/png" if suffix == ".png" else "image/jpeg")
        self.send_cacheable_file(path, mime)
    except ValueError as exc:
        self.send_json(404, {"ok": False, "error": str(exc)})


def get_mapping(self, route, parsed):
    if not self.local_tool_request():
        self.send_json(403, {"ok": False, "error": "local_only"})
        return
    self.send_json(200, self.engine.mapping_state())


def get_shopee_mappings(self, route, parsed):
    if not self.local_tool_request() and not self.headers.get("Origin", "").startswith("chrome-extension://"):
        self.send_json(403, {"ok": False, "error": "extension_origin_required"})
        return
    if not self.extension_paired("shopee"):
        self.send_json(403, {"ok": False, "error": "extension_not_paired"})
        return
    self.send_json(200, self.engine.shopee_mapping_state())


def post_ingest_upload(self, route):
    from urllib.parse import parse_qs, unquote, urlsplit
    # Tệp thô (không base64) cho "Thêm sản phẩm bằng AI": danh mục PDF có thể nặng hàng chục MB.
    if not self.local_tool_request():
        self.reject(403, "local_only")
        return
    try:
        from product_ingest import MAX_FILE_BYTES
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0:
            raise ValueError("Tệp rỗng, hãy chọn lại tệp.")
        if size > MAX_FILE_BYTES:
            self._drain_request_body(size)
            self.send_json(413, {"ok": False, "error": f"Tệp quá lớn (tối đa {MAX_FILE_BYTES // 1048576} MB)."})
            return
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        meta = self.engine.ingest.add_upload(unquote(query.get("name", "")), self.rfile.read(size))
        self.send_json(200, {"ok": True, **meta})
    except ValueError as exc:
        self.send_json(400, {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"POST {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})


def post_profiles_prefix(self, route):
    from urllib.parse import parse_qs, unquote, urlsplit
    # "Bổ sung bằng Google": chỉ trang công cụ trên máy được gọi.
    if not self.local_tool_request():
        self.reject(403, "local_only")
        return
    try:
        size = int(self.headers.get("Content-Length", "0") or 0)
        if size > 1024 * 1024:
            self._drain_request_body(size)
            raise ValueError("Yêu cầu quá lớn.")
        payload = request_json_object(self.rfile.read(size)) if size > 0 else {}
        tail = [unquote(p) for p in route[len("/api/profiles/"):].split("/") if p]
        enrich = self.engine.enrich
        if tail == ["enrich-batch"]:
            ids = payload.get("ids")
            result = enrich.start_batch([clean(i, 80) for i in ids][:500] if isinstance(ids, list) else None)
        elif tail == ["enrich-batch", "cancel"]:
            result = enrich.cancel_batch()
        elif len(tail) == 2 and tail[1] == "enrich":
            result = {"ok": True, "job": enrich.start(clean(tail[0], 80))}
        elif len(tail) == 3 and tail[1:] == ["enrich", "apply"]:
            fields = payload.get("fields")
            result = enrich.apply(clean(tail[0], 80),
                                  [clean(f, 60) for f in fields][:20] if isinstance(fields, list) else [])
        else:
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        self.send_json(200, result)
    except (ValueError, json.JSONDecodeError) as exc:
        self.send_json(400, {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"POST {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})


def post_ingest_extract(self, route):
    if not self.local_tool_request():
        self.reject(403, "local_only")
        return
    try:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0:
            raise ValueError("Yêu cầu rỗng.")
        if size > 40 * 1024 * 1024:
            # Phải đọc bỏ phần thân trước khi trả lời, nếu không trình duyệt thấy kết nối bị cắt
            # ("Mất kết nối") thay vì nhận được lý do thật.
            self._drain_request_body(size)
            self.send_json(413, {"ok": False, "error": "Dữ liệu gửi lên quá lớn. Hãy chọn lại tệp (tệp được tải lên riêng)."})
            return
        payload = request_json_object(self.rfile.read(size))
        ingest = self.engine.ingest
        if route == "/api/ingest/extract":
            result = {"ok": True, **ingest.extract(payload)}
        elif route == "/api/ingest/draft":
            result = ingest.create_draft(payload)
        elif route == "/api/ingest/combo/propose":
            result = self.engine.combo_profiles.propose(payload)
        elif route == "/api/ingest/combo/propose-batch":
            result = self.engine.combo_profiles.propose_batch(payload)
        elif route == "/api/ingest/combo/draft":
            result = self.engine.combo_profiles.create_draft(payload)
        elif route == "/api/ingest/defaults/save":
            result = self.engine.product_defaults.save(payload)
        elif route == "/api/ingest/defaults/choose":
            result = self.engine.product_defaults.choose(payload)
        elif route == "/api/ingest/product/remove":
            result = self.engine.remove_listing(payload)
        elif route == "/api/ingest/product/restore":
            result = self.engine.remove_listing(payload, restore=True)
        else:
            result = ingest.link_draft(clean(payload.get("id"), 80), clean(payload.get("product_id"), 160),
                                       use_image=payload.get("use_image") is not False)
        self.send_json(200, result)
    except (ValueError, json.JSONDecodeError) as exc:
        self.send_json(400, {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"POST {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})


def post_learning_prefix(self, route):
    if not self.local_tool_request():
        self.reject(403, "local_only")
        return
    try:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 2_000_000:
            raise ValueError("invalid_body_size")
        payload = request_json_object(self.rfile.read(size)) if size else {}
        self.send_json(200, self.engine.learning_post(route[len("/api/learning/"):], payload))
    except (ValueError, json.JSONDecodeError) as exc:
        self.send_json(400, {"ok": False, "error": str(exc)})
    except Exception as exc:
        report_problem("api_error", f"POST {route}", exc)
        self.send_json(500, {"ok": False, "error": "internal_error"})


def post_workspace_corrections(self, route, payload, flags):
    result = self.engine.workspace_correct(payload)
    return result


def post_correction_undo(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = [unquote(part) for part in route.split("/") if part]
    if len(parts) != 5 or parts[:3] != ["api", "workspace", "corrections"]:
        raise ValueError("workspace_route_invalid")
    result = self.engine.workspace_undo_correction(parts[3])
    return result


def post_dose_test_route(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    if len(parts) != 3 or parts[2] != "dose-test":
        raise ValueError("workspace_route_invalid")
    result = self.engine.workspace_dose_test(parts[1], payload)
    return result


def post_dose_rules_route(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    from http_routes import RESPONSE_SENT
    import dose_rules
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    try:
        with self.engine.workspace._db() as db:
            if parts == ["dose-rules", "import"]:
                result = dose_rules.import_rules(db, payload.get("rules"))
            elif parts == ["dose-rules", "import-label"]:
                import label_parser
                sku = clean(payload.get("sku"), 160)
                text = payload.get("text")
                if not sku or not isinstance(text, str) or not text.strip() or len(text) > 20000:
                    raise ValueError("dose_label_invalid")
                result = label_parser.import_label(
                    db, text, sku, source_doc=clean(payload.get("source_doc"), 200))
            elif len(parts) == 3 and parts[2] == "review":
                result = dose_rules.review_rule(
                    db, parts[1], payload.get("decision"))
            else:
                raise ValueError("workspace_route_invalid")
    except dose_rules.DoseRuleConflict as exc:
        self.send_json(409, {"ok": False, "error": str(exc)})
        return RESPONSE_SENT
    return result


def post_conversation_example_route(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    if len(parts) != 3:
        raise ValueError("workspace_route_invalid")
    result = (self.engine.workspace_approve_reply_example(parts[1], payload)
              if parts[2] == "approve-example" else
              self.engine.workspace_disable_reply_example(parts[1], payload))
    return result


def post_product_image_route(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    if len(parts) != 3:
        raise ValueError("workspace_route_invalid")
    result = self.engine.workspace_save_product_image(parts[1], payload)
    return result


def post_workspace_route(self, route, payload, flags):
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = [unquote(part) for part in route.split("/")[3:] if part]
    if len(parts) != 3 or parts[2] not in {"rollback", "restore"}:
        raise ValueError("workspace_route_invalid")
    result = (self.engine.workspace_rollback(parts[0], parts[1])
              if parts[2] == "rollback" else
              self.engine.workspace_restore(parts[0], parts[1]))
    return result


def post_mapping(self, route, payload, flags):
    result = self.engine.save_mapping(payload)
    return result


def post_mapping_undo(self, route, payload, flags):
    result = self.engine.undo_mapping()
    return result


def post_mapping_auto_match(self, route, payload, flags):
    result = self.engine.auto_match_mapping(payload)
    return result


def post_profiles_bulk_draft(self, route, payload, flags):
    result = self.engine.bulk_draft_profiles(payload)
    return result


def post_profiles_approve(self, route, payload, flags):
    result = self.engine.approve_profiles(payload)
    return result


def post_knowledge_update(self, route, payload, flags):
    result = self.engine.update_knowledge_package(payload)
    return result


def post_knowledge_undo(self, route, payload, flags):
    result = self.engine.undo_knowledge_package(payload)
    return result


def post_knowledge_export(self, route, payload, flags):
    result = self.engine.export_knowledge_package(payload)
    return result


def post_shopee_mappings(self, route, payload, flags):
    result = self.engine.save_shopee_mappings(payload)
    return result
