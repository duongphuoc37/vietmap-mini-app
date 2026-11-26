import json
import os
import re
from datetime import datetime, timedelta, timezone
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

VN_TZ = timezone(timedelta(hours=7))
NOW_VN = datetime.now(VN_TZ)
TODAY_STR = NOW_VN.strftime("%Y%m%d")
TODAY_VN = NOW_VN.strftime("%d/%m/%Y")
NOW_ISO = datetime.now(timezone.utc).isoformat()

# Khung tọa độ tỉnh Nghệ An (lọc tọa độ rác)
LAT_MIN, LAT_MAX = 18.50, 19.95
LNG_MIN, LNG_MAX = 103.85, 105.90


def is_valid_nghe_an_coord(lat, lng):
    try:
        lat_f, lng_f = float(lat), float(lng)
        return LAT_MIN <= lat_f <= LAT_MAX and LNG_MIN <= lng_f <= LNG_MAX
    except (TypeError, ValueError):
        return False


def fetch_overpass_points():
    """Quét camera tốc độ, trạm thu phí và giao cắt đường sắt tại Nghệ An từ OpenStreetMap."""
    query = """
    [out:json][timeout:25];
    (
      node["highway"="speed_camera"](18.55,105.10,19.45,105.85);
      node["barrier"="toll_booth"](18.55,105.10,19.45,105.85);
      node["railway"="level_crossing"](18.60,105.45,19.35,105.80);
    );
    out body 120;
    """
    points = []
    try:
        res = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=30,
        )
        res.raise_for_status()
        elements = res.json().get("elements", [])

        for el in elements:
            lat, lng = el.get("lat"), el.get("lon")
            if not is_valid_nghe_an_coord(lat, lng):
                continue

            tags = el.get("tags", {})
            raw_speed = str(tags.get("maxspeed", "50"))
            speed_match = re.search(r"\d+", raw_speed)
            max_spd = int(speed_match.group()) if speed_match else 50

            if tags.get("barrier") == "toll_booth":
                source, ptype, spd = "VIETMAP", "toll", 30
                title = tags.get("name") or "Trạm Thu Phí VETC / ePass"
            elif tags.get("railway") == "level_crossing":
                source, ptype, spd = "GTVT", "gtvt_work", 40
                title = f"Sở GTVT: Giao Cắt Đường Sắt ({tags.get('name', 'Chú ý tàu hỏa')})"
            else:
                source, ptype, spd = "VIETMAP", "camera", max_spd
                title = tags.get("name") or f"Camera Giám Sát Tốc Độ ({max_spd} km/h)"

            points.append({
                "id": f"osm-{el['id']}",
                "source": source,
                "lat": round(float(lat), 5),
                "lng": round(float(lng), 5),
                "type": ptype,
                "speed_limit": spd,
                "title": title[:160],
                "flow": "Thông thoáng",
                "avg_speed": max(25, spd - 8),
                "updated_at": NOW_ISO,
            })
        print(f"[OSM] Thu thập được {len(points)} điểm.")
    except Exception as exc:
        print(f"[OSM] Bỏ qua do lỗi kết nối: {exc}")
    return points


def fetch_gemini_daily_alerts():
    """Dùng Gemini API + Google Search quét tin sự cố, phân luồng, camera mới tại Nghệ An 24h qua."""
    if not GEMINI_API_KEY:
        print("[Gemini] Chưa cấu hình GEMINI_API_KEY, bỏ qua bước quét tin tức.")
        return []

    prompt = f"""
Hôm nay là ngày {TODAY_VN}. Hãy tìm kiếm tin tức giao thông thực tế trong 24 giờ qua tại tỉnh Nghệ An và TP. Vinh (sự cố tai nạn, thi công đường, phân luồng của Sở GTVT, cảnh báo thời tiết/ngập úng, điểm phạt nguội/camera AI mới).
Trả về DUY NHẤT một mảng JSON hợp lệ (tối đa 5 phần tử, không thêm giải thích hay chữ ngoài JSON) theo cấu trúc sau:
[
  {{
    "id": "ai-{TODAY_STR}-1",
    "source": "GTVT",
    "lat": 18.67495,
    "lng": 105.68085,
    "type": "gtvt_work",
    "speed_limit": 50,
    "title": "Cảnh báo ({TODAY_VN}): Mô tả ngắn gọn vị trí và nội dung cảnh báo",
    "flow": "Đông phương tiện",
    "avg_speed": 35
  }}
]
Quy định bắt buộc:
- "source" chỉ nhận: "GTVT", "VIETMAP", hoặc "CCTV".
- "type" chỉ nhận: "gtvt_work", "camera", "speed", "cctv", hoặc "no_park".
- "lat" phải nằm trong khoảng 18.55 đến 19.50 và "lng" trong khoảng 105.00 đến 105.80 (tọa độ chính xác tại Nghệ An).
- "speed_limit" là số nguyên từ 30 đến 90.
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    }

    valid_points = []
    try:
        res = requests.post(url, json=payload, timeout=45)
        res.raise_for_status()
        data = res.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]

        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            print("[Gemini] Không tìm thấy mảng JSON hợp lệ trong phản hồi.")
            return []

        items = json.loads(match.group(0))
        allowed_sources = {"GTVT", "VIETMAP", "CCTV"}
        allowed_types = {"gtvt_work", "camera", "speed", "cctv", "no_park", "toll"}

        for idx, item in enumerate(items[:5], start=1):
            lat, lng = item.get("lat"), item.get("lng")
            if not is_valid_nghe_an_coord(lat, lng):
                continue

            src = item.get("source", "GTVT")
            ptype = item.get("type", "gtvt_work")
            spd = int(item.get("speed_limit", 50))
            spd = max(30, min(120, spd))

            valid_points.append({
                "id": f"ai-{TODAY_STR}-{idx}",
                "source": src if src in allowed_sources else "GTVT",
                "lat": round(float(lat), 5),
                "lng": round(float(lng), 5),
                "type": ptype if ptype in allowed_types else "gtvt_work",
                "speed_limit": spd,
                "title": str(item.get("title", f"Cảnh báo giao thông ({TODAY_VN})"))[:180],
                "flow": str(item.get("flow", "Lưu thông ổn định"))[:50],
                "avg_speed": int(item.get("avg_speed", 38)),
                "updated_at": NOW_ISO,
            })
        print(f"[Gemini] Tạo được {len(valid_points)} cảnh báo tin tức mới.")
    except Exception as exc:
        print(f"[Gemini] Lỗi khi gọi API: {exc}")
    return valid_points


def cleanup_old_ai_alerts(headers):
    """Dọn dẹp các tin cảnh báo sự cố tạm thời (ai-*) cũ hơn 3 ngày để bản đồ luôn gọn."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    url = f"{SUPABASE_URL}/rest/v1/traffic_points"
    params = {
        "id": "like.ai-*",
        "updated_at": f"lt.{cutoff}",
    }
    try:
        res = requests.delete(url, headers=headers, params=params, timeout=20)
        if res.status_code in (200, 204):
            print("[Supabase] Đã dọn dẹp các tin cảnh báo cũ hơn 3 ngày.")
    except Exception as exc:
        print(f"[Supabase] Lỗi dọn dẹp dữ liệu cũ: {exc}")


def upsert_to_supabase(points):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Thiếu biến môi trường SUPABASE_URL hoặc SUPABASE_KEY.")
    if not points:
        print("[Supabase] Không có điểm mới để đẩy lên.")
        return

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    cleanup_old_ai_alerts(headers)

    url = f"{SUPABASE_URL}/rest/v1/traffic_points"
    # Chia từng lô 50 điểm để gửi ổn định
    batch_size = 50
    for i in range(0, len(points), batch_size):
        batch = points[i : i + batch_size]
        res = requests.post(url, headers=headers, json=batch, timeout=30)
        res.raise_for_status()

    print(f"[Supabase] Đồng bộ thành công tổng cộng {len(points)} điểm lên bảng traffic_points.")


if __name__ == "__main__":
    all_points = fetch_overpass_points() + fetch_gemini_daily_alerts()
    upsert_to_supabase(all_points)
