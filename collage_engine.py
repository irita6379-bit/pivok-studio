import copy
import functools
import gc
import json
import os
import tempfile
import threading
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

import yolo_local

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCES_DIR = os.path.join(BASE_DIR, "resources")

WORK_ROOT = os.environ.get("PIVOK_WORK_ROOT", os.path.join(BASE_DIR, "work_data"))
WORK_DIR = os.path.join(WORK_ROOT, "incoming")
SAVE_DIR = os.path.join(WORK_ROOT, "output")
for _d in (WORK_DIR, SAVE_DIR):
    os.makedirs(_d, exist_ok=True)

_asset_lock = threading.Lock()
face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)

SINGLE_CATEGORIES = ["ОДИНАРНЫЙ", "SINGLE"]


@functools.lru_cache(maxsize=64)
def get_cached_font(font_path: str, exact_size: int, weight: int = 560):
    font = ImageFont.truetype(font_path, exact_size)
    try:
        axes = font.get_variation_axes()
        if axes:
            vals = []
            for ax in axes:
                name = ax.get("name", b"")
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="ignore")
                if "Weight" in name:
                    vals.append(max(ax["minimum"], min(ax["maximum"], weight)))
                elif "Optical" in name:
                    vals.append(max(ax["minimum"], min(ax["maximum"], 28)))
                else:
                    vals.append(ax["default"])
            font.set_variation_by_axes(vals)
    except Exception:
        pass
    return font


@functools.lru_cache(maxsize=2)
def _load_template_config_cached(is_single: bool):
    config_name = "SINGLE_config.json" if is_single else "STANDART_config.json"
    bg_name = "SINGLE_bg.png" if is_single else "STANDART_bg.png"
    with open(os.path.join(RESOURCES_DIR, config_name), "r", encoding="utf-8") as f:
        config = json.load(f)
    with Image.open(os.path.join(RESOURCES_DIR, bg_name)) as _bg:
        bg = _bg.convert("RGBA")
        top_h = int(config["rects"].get("LEFT", config["rects"].get("MAIN"))[1])
        bot_y = int(config["rects"].get("LEFT", config["rects"].get("MAIN"))[3])
        top_rgb = bg.crop((0, 0, bg.width, top_h)).convert("RGB")
        bot_rgb = bg.crop((0, bot_y, bg.width, bg.height)).convert("RGB")
        canvas_size = bg.size
    return config, canvas_size, top_rgb, bot_rgb


def load_template_assets(is_single: bool):
    with _asset_lock:
        config, canvas_size, top_rgb, bot_rgb = _load_template_config_cached(bool(is_single))
    return copy.deepcopy(config), canvas_size, top_rgb, bot_rgb


def load_rgb_image(path: str) -> Image.Image:
    with Image.open(path) as _img:
        return ImageOps.exif_transpose(_img).convert("RGB")


def compute_bounds_from_pose(
    yolo_data: Optional[dict], small_bgr: Optional[np.ndarray] = None, is_pants: bool = False
):
    head_top_y_pct = 0.12
    foot_y_pct = 1.0
    body_cx_pct = 0.5
    body_w_pct = 0.5
    is_person = False
    face_cx_pct = 0.5
    face_w_pct = 0.0
    shoulder_w_pct = 0.0

    if yolo_data and yolo_data.get("boxes"):
        all_boxes = yolo_data["boxes"]
        shape = yolo_data.get("shape") or (
            list(small_bgr.shape[:2]) if small_bgr is not None else [640, 480]
        )
        h_small, w_small = max(1, int(shape[0])), max(1, int(shape[1]))
        person_indices = [idx for idx, b in enumerate(all_boxes) if b.get("cls") == 0]
        if not person_indices:
            person_indices = list(range(len(all_boxes)))

        def _score_candidate(idx):
            b = all_boxes[idx]
            x1, y1, x2, y2 = b["xyxy"]
            bw = max(1.0, float(x2 - x1))
            bh = max(1.0, float(y2 - y1))
            area = (bw * bh) / float(max(1, w_small * h_small))
            cx_pct = ((x1 + x2) / 2.0) / float(w_small)
            center_factor = max(0.5, 1.0 - abs(cx_pct - 0.5) * 1.1)
            face_factor = 1.0
            if yolo_data.get("keypoints") and idx < len(yolo_data["keypoints"]):
                kps_c = yolo_data["keypoints"][idx]
                if len(kps_c) > 4:
                    el, er = kps_c[1], kps_c[2]
                    earl, earr = kps_c[3], kps_c[4]
                    eye_sep = abs(el[0] - er[0]) if (el[0] > 0 and er[0] > 0) else 0.0
                    ear_sep = abs(earl[0] - earr[0]) if (earl[0] > 0 and earr[0] > 0) else 0.0
                    face_factor = 1.35 if (eye_sep >= bw * 0.035 or ear_sep >= bw * 0.07) else 0.75
            return area * center_factor * face_factor

        best_idx = max(person_indices, key=_score_candidate)
        best_box = all_boxes[best_idx]
        box = best_box["xyxy"]
        box_n = [box[0] / w_small, box[1] / h_small, box[2] / w_small, box[3] / h_small]
        is_person = best_box.get("cls") == 0

        head_top_y_pct = (
            float(box_n[1]) + (float(box_n[3]) - float(box_n[1])) * 0.45
            if is_pants
            else float(box_n[1])
        )
        foot_y_pct = float(box_n[3])
        has_valid_head = True

        if yolo_data.get("keypoints"):
            kps = yolo_data["keypoints"][best_idx] if best_idx < len(yolo_data["keypoints"]) else []
            if kps:
                def get_y(kp_idx):
                    if kp_idx < len(kps):
                        x, y = kps[kp_idx]
                        if y < h_small - 5 and 5 < x < w_small - 5:
                            return y / h_small
                    return 0.0

                max_ankle_y = max(get_y(15), get_y(16))
                l_hip_y, r_hip_y = get_y(11), get_y(12)
                min_hip_y = min(l_hip_y, r_hip_y) if (l_hip_y > 0 and r_hip_y > 0) else max(l_hip_y, r_hip_y)
                if is_pants and min_hip_y > 0:
                    head_top_y_pct = max(0.0, min_hip_y - 0.12)

                max_knee_y = max(get_y(13), get_y(14))
                has_valid_head = any(kp[0] > 15 and kp[1] > 15 for kp in kps[0:5]) if len(kps) >= 5 else False

                if not has_valid_head and not is_pants:
                    head_top_y_pct = -max(0.001, min_hip_y - 0.115) if min_hip_y > 0 else 0.0

                if max_ankle_y > 0.1 or foot_y_pct > 0.85:
                    if not has_valid_head:
                        foot_y_pct = min(1.0, max(foot_y_pct, max_ankle_y + 0.04))
                    else:
                        foot_y_pct = min(1.0, max(0.985, foot_y_pct + 0.04, max_ankle_y + 0.06))
                elif max_knee_y > 0:
                    foot_y_pct = max(foot_y_pct, max_knee_y)

        body_cx_pct = float(box_n[0] + box_n[2]) / 2.0
        body_w_pct = float(box_n[2] - box_n[0])
        face_cx_pct = body_cx_pct
        yolo_face_w = 0.0

        if yolo_data.get("keypoints") and has_valid_head:
            try:
                kps = yolo_data["keypoints"][best_idx]
                if len(kps) > 4:
                    ear_l, ear_r = kps[3], kps[4]
                    eye_l, eye_r = kps[1], kps[2]
                    if ear_l[0] > 15 and ear_r[0] > 15 and ear_l[1] > 15 and ear_r[1] > 15:
                        yolo_face_w = abs(ear_l[0] - ear_r[0])
                    elif eye_l[0] > 15 and eye_r[0] > 5 and eye_l[1] > 15 and eye_r[1] > 15:
                        yolo_face_w = abs(eye_l[0] - eye_r[0]) * 2.2
                    if yolo_face_w > 0:
                        face_w_pct = yolo_face_w / float(w_small)
                        face_pts = [p[0] for p in kps[0:5] if p[0] > 15 and p[1] > 15]
                        if face_pts:
                            face_cx_pct = sum(face_pts) / len(face_pts) / float(w_small)
            except Exception:
                pass

        if yolo_face_w == 0 and is_person and not is_pants and has_valid_head and small_bgr is not None:
            x1, y1, x2, y2 = map(int, box)
            y2_half = y1 + (y2 - y1) // 2
            person_roi = small_bgr[max(0, y1) : max(0, y2_half), max(0, x1) : max(0, x2)]
            if person_roi.size > 0 and face_cascade is not None:
                gray = cv2.cvtColor(person_roi, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(gray, 1.1, 4)
                if len(faces) > 0:
                    faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
                    fx, _, fw_face, _ = faces[0]
                    face_cx_pct = (x1 + fx + fw_face / 2.0) / float(w_small)
                    face_w_pct = fw_face / float(w_small)

        if yolo_data.get("keypoints"):
            try:
                kps = yolo_data["keypoints"][best_idx]
                if len(kps) > 6:
                    ls, rs = kps[5], kps[6]
                    if ls[0] > 0 and rs[0] > 0:
                        shoulder_w_pct = abs(ls[0] - rs[0]) / float(w_small)
                    face_pts = [p[0] for p in kps[0:5] if p[0] > 15 and p[1] > 15]
                    face_y_pts = [p[1] for p in kps[0:5] if p[0] > 15 and p[1] > 15]
                    if face_pts:
                        face_cx_pct = sum(face_pts) / len(face_pts) / float(w_small)
                    if face_y_pts and has_valid_head:
                        eye_level_pct = min(face_y_pts) / float(h_small)
                        if face_w_pct > 0:
                            expected_hair_top = eye_level_pct - (face_w_pct * 0.60)
                        elif shoulder_w_pct > 0:
                            expected_hair_top = eye_level_pct - (shoulder_w_pct * 0.25)
                        else:
                            expected_hair_top = eye_level_pct - 0.055
                        if not is_pants:
                            head_top_y_pct = max(0.0, expected_hair_top)
            except Exception:
                pass

    head_h_pct = (
        face_w_pct * 2.7 if face_w_pct > 0 else (shoulder_w_pct * 1.2 if shoulder_w_pct > 0 else 0.18)
    )
    return (
        head_top_y_pct,
        body_cx_pct,
        body_w_pct,
        foot_y_pct,
        is_person,
        face_w_pct,
        face_cx_pct,
        shoulder_w_pct,
        head_h_pct,
    )


def get_person_bounds_pct(filepath: str, is_pants: bool = False, precomputed_pose: Optional[dict] = None):
    try:
        if precomputed_pose is not None:
            return compute_bounds_from_pose(precomputed_pose, None, is_pants)

        pil_img = load_rgb_image(filepath)
        img = np.array(pil_img)[:, :, ::-1]
        del pil_img
        h, w = img.shape[:2]
        if max(h, w) > 640:
            sc = 640.0 / max(h, w)
            small_img = cv2.resize(img, (int(w * sc), int(h * sc)))
        else:
            small_img = img
        del img
        yolo_data = yolo_local.detect_yolo(small_img, "yolov8n-pose")
        return compute_bounds_from_pose(yolo_data, small_img, is_pants)
    except Exception:
        return 0.12, 0.5, 0.1, 1.0, False, 0.0, 0.5, 0.0, 0.10


def fit_image_to_rect(img: Image.Image, rect_bbox, head_y=None, head_x=None, foot_y=None, mode=None, is_person=None, head_h=None, category=None) -> Image.Image:
    rect_w = int(rect_bbox[2]) - int(rect_bbox[0])
    rect_h = int(rect_bbox[3]) - int(rect_bbox[1])

    scale_base = max(rect_w / img.width, rect_h / img.height)
    scale = scale_base
    head_top_pct = 0.15
    headless_waist_guard_pct = None

    if is_person and head_y is not None:
        if head_y < 0:
            headless_waist_guard_pct = abs(head_y)
            pct_y = 0.0
        else:
            pct_y = head_y
        if pct_y > 1.0:
            pct_y /= img.height
        head_top_pct = max(0.0, min(pct_y, 1.0))

        if foot_y is not None:
            if foot_y > 1.0:
                foot_y /= img.height
            person_h_pct = max(0.3, foot_y - head_top_pct)

            if mode == "RIGHT":
                target_headroom = rect_h * 0.015
                scale_no_gap = (
                    (rect_h - target_headroom) / (img.height * (1.0 - head_top_pct))
                    if head_top_pct < 0.99
                    else 0
                )
                scale = max(scale_base, scale_no_gap)
            else:
                target_headroom = rect_h * 0.015
                target_footroom = rect_h * 0.015
                target_person_px = rect_h - target_headroom - target_footroom
                scale_person = target_person_px / (person_h_pct * img.height)
                scale_no_gap = (
                    (rect_h - target_headroom) / (img.height * (1.0 - head_top_pct))
                    if head_top_pct < 0.99
                    else 0
                )
                scale = max(scale_base, scale_person, scale_no_gap)

    new_w, new_h = max(rect_w, int(img.width * scale)), max(rect_h, int(img.height * scale))
    img_resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)

    if is_person and head_x is not None:
        left = int(head_x * new_w - rect_w / 2)
    else:
        left = (new_w - rect_w) // 2
    left = max(0, min(left, new_w - rect_w))

    if is_person and head_y is not None:
        top = int(head_top_pct * new_h - rect_h * 0.015)
        if mode != "RIGHT" and foot_y is not None and head_top_pct < 0.03:
            foot_bottom_px = int(min(1.0, foot_y) * new_h + rect_h * 0.045)
            if foot_bottom_px > top + rect_h:
                desired_top = foot_bottom_px - rect_h
                top = (
                    min(desired_top, int(headless_waist_guard_pct * new_h))
                    if headless_waist_guard_pct is not None
                    else desired_top
                )
    else:
        top = int((new_h - rect_h) * 0.15)

    top = max(0, min(top, new_h - rect_h))
    cropped = img_resized.crop((left, top, left + rect_w, top + rect_h))
    del img_resized
    return cropped


def _format_price_str(val: str) -> str:
    val = str(val).strip()
    if not val:
        return ""
    return val if val.endswith("$") else f"{val}$"


def draw_pivok_bottom_bar(draw: ImageDraw.ImageDraw, config: dict, code: str, sizes: str, price: str):
    style = config.get("style", {})
    gold_rgb = tuple(style.get("gold_rgb", [214, 164, 116]))
    weight = int(style.get("font_weight", 560))
    label_size = int(style.get("label_size", 51))
    value_size = int(style.get("value_size", 52))
    price_size = int(style.get("price_size", 104))
    dual_text_sz = int(style.get("dual_text_size", 44))
    dual_price_sz = int(style.get("dual_price_size", 55))
    size_label = style.get("size_label", "SİZE:")

    font_path = os.path.join(RESOURCES_DIR, "SourceSerif.ttf")
    texts_cfg = config.get("texts", {})
    box_c = texts_cfg.get("CODE", [53, 1796, 493, 1920])
    box_p = texts_cfg.get("PRICE", [507, 1796, 933, 1920])
    box_s = texts_cfg.get("SIZE", [947, 1796, 1387, 1920])

    cx_c = (box_c[0] + box_c[2]) // 2
    cx_p = (box_p[0] + box_p[2]) // 2
    cx_s = (box_s[0] + box_s[2]) // 2
    bar_top, bar_bot = box_p[1], box_p[3]
    bar_h = bar_bot - bar_top

    y_line1 = bar_top + int(bar_h * 0.26)
    y_line2 = bar_top + int(bar_h * 0.76)
    y_center = bar_top + int(bar_h * 0.52)

    if ("|" in code) or ("|" in sizes) or ("|" in price):
        c1, c2 = code.split("|", 1) if "|" in code else (code, "")
        s1, s2 = sizes.split("|", 1) if "|" in sizes else (sizes, "")
        p1, p2 = price.split("|", 1) if "|" in price else (price, "")

        f_dual = get_cached_font(font_path, dual_text_sz, weight)
        f_dual_p = get_cached_font(font_path, dual_price_sz, weight)
        if c1:
            draw.text((cx_c, y_line1), f"CODE: {c1}", font=f_dual, fill=gold_rgb, anchor="mm")
        if c2:
            draw.text((cx_c, y_line2), f"CODE: {c2}", font=f_dual, fill=gold_rgb, anchor="mm")
        if p1 and p2:
            draw.text((cx_p, y_line1), _format_price_str(p1), font=f_dual_p, fill=gold_rgb, anchor="mm")
            draw.text((cx_p, y_line2), _format_price_str(p2), font=f_dual_p, fill=gold_rgb, anchor="mm")
        elif p1:
            f_single_p = get_cached_font(font_path, price_size, weight)
            draw.text((cx_p, y_center), _format_price_str(p1), font=f_single_p, fill=gold_rgb, anchor="mm")
        if s1:
            draw.text((cx_s, y_line1), f"{size_label} {s1}", font=f_dual, fill=gold_rgb, anchor="mm")
        if s2:
            draw.text((cx_s, y_line2), f"{size_label} {s2}", font=f_dual, fill=gold_rgb, anchor="mm")
        return

    has_price = bool(price and str(price).strip() and str(price).strip() != "-")
    has_size = bool(sizes and str(sizes).strip() and str(sizes).strip() != "-")

    if not has_price and not has_size:
        # Elegant centered CODE layout when price and sizes are not required
        cx_mid = canvas_size[0] // 2
        f_mid_label = get_cached_font(font_path, label_size + 4, weight)
        cur_code_size = value_size + 8
        f_code_val = get_cached_font(font_path, cur_code_size, weight)
        draw.text((cx_mid, y_line1), "CODE:", font=f_mid_label, fill=gold_rgb, anchor="mm")
        draw.text((cx_mid, y_line2), str(code).upper(), font=f_code_val, fill=gold_rgb, anchor="mm")
        return

    f_label = get_cached_font(font_path, label_size, weight)
    max_col_w = (box_c[2] - box_c[0]) - 16

    cur_code_size = value_size
    f_code_val = get_cached_font(font_path, cur_code_size, weight)
    while cur_code_size > 30 and f_code_val.getlength(code) > max_col_w:
        cur_code_size -= 3
        f_code_val = get_cached_font(font_path, cur_code_size, weight)

    cur_size_sz = value_size
    f_size_val = get_cached_font(font_path, cur_size_sz, weight)
    while cur_size_sz > 30 and f_size_val.getlength(str(sizes)) > max_col_w:
        cur_size_sz -= 3
        f_size_val = get_cached_font(font_path, cur_size_sz, weight)

    f_price = get_cached_font(font_path, price_size, weight)

    draw.text((cx_c, y_line1), "CODE:", font=f_label, fill=gold_rgb, anchor="mm")
    draw.text((cx_c, y_line2), str(code).upper(), font=f_code_val, fill=gold_rgb, anchor="mm")
    if has_price:
        draw.text((cx_p, y_center), _format_price_str(price), font=f_price, fill=gold_rgb, anchor="mm")
    if has_size:
        draw.text((cx_s, y_line1), size_label, font=f_label, fill=gold_rgb, anchor="mm")
        draw.text((cx_s, y_line2), str(sizes), font=f_size_val, fill=gold_rgb, anchor="mm")


def parse_folder_metadata(item_name: str):
    parts = item_name.split("_")
    if len(parts) >= 4 and parts[-1].isdigit() and len(parts[-1]) >= 9:
        parts = parts[:-1]

    brand, category = "PIVOK", "ПЛАТЬЕ"
    if len(parts) >= 5:
        brand, category = parts[0].upper(), parts[1]
        code_raw, sizes_raw, price_raw = parts[2], parts[3], parts[4]
    elif len(parts) == 4:
        brand = parts[0].upper()
        code_raw, sizes_raw, price_raw = parts[1], parts[2], parts[3]
    elif len(parts) == 3:
        code_raw, sizes_raw, price_raw = parts[0], parts[1], parts[2]
    else:
        return "PIVOK", "ПЛАТЬЕ", "0000", "38-44", "0"

    code = code_raw.replace("~", "|")
    sizes_clean = sizes_raw.replace("~", "|")
    if "&" in sizes_clean:
        sizes = sizes_clean
    else:
        final_sizes = []
        for s in sizes_clean.split("|"):
            s_parts = s.split("-")
            final_sizes.append(f"{s_parts[0]}-{s_parts[-1]}" if len(s_parts) > 2 else s)
        sizes = "|".join(final_sizes)

    price = price_raw.split()[0].replace("~", "|")
    return brand, category, code, sizes, price


def _render_one_collage(
    i: int,
    is_single: bool,
    photos: list[str],
    canvas_size: tuple[int, int],
    top_rgb: Image.Image,
    bot_rgb: Image.Image,
    config: dict,
    code: str,
    sizes: str,
    price: str,
    out_folder_path: str,
    category: str,
    pose_cache: dict,
    used_names: set,
):
    is_pants = any(k in category.upper() for k in ("ШТАНЫ", "БРЮКИ", "ШОРТЫ"))
    canvas = Image.new("RGB", canvas_size, (255, 255, 255))

    manual_crop = None
    manual_crop_file = os.path.join(os.path.dirname(photos[0]), "manual_crop.json") if photos else ""
    if manual_crop_file and os.path.exists(manual_crop_file):
        try:
            with open(manual_crop_file, "r", encoding="utf-8") as f:
                mc_list = json.load(f)
                idx = i if is_single else i // 2
                if idx < len(mc_list):
                    manual_crop = mc_list[idx]
        except Exception:
            pass

    def apply_manual_crop(img: Image.Image, rect_name: str):
        if not manual_crop or rect_name not in manual_crop:
            return None
        mc = manual_crop[rect_name]
        new_w, new_h = max(1, int(img.width * mc["scaleX"])), max(1, int(img.height * mc["scaleY"]))
        resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
        paste_x = int(mc["left"] - new_w / 2)
        paste_y = int(mc["top"] - new_h / 2)
        clip_rect = config["rects"][rect_name]
        res = Image.new("RGB", (clip_rect[2] - clip_rect[0], clip_rect[3] - clip_rect[1]), (255, 255, 255))
        res.paste(resized, (paste_x - clip_rect[0], paste_y - clip_rect[1]))
        del resized
        return res

    if is_single:
        path = photos[i]
        img = load_rgb_image(path)
        pose_d = pose_cache.get(os.path.basename(path))
        hy, cx, fw, fy, is_p, face_w, face_cx, _, head_h = get_person_bounds_pct(path, is_pants, pose_d)
        if face_w == 0:
            face_cx = cx
        rect_bbox = config["rects"]["MAIN"]
        mc_img = apply_manual_crop(img, "MAIN")
        fitted = mc_img if mc_img else fit_image_to_rect(img, rect_bbox, hy, face_cx, fy, "MAIN", is_p, head_h, category=category)
        del img
        canvas.paste(fitted, (int(rect_bbox[0]), int(rect_bbox[1])))
        del fitted
        basename = os.path.basename(path)
        color_name = basename.split("-")[0].upper() if "-" in basename else f"COLOR_{i + 1}"
    else:
        p_left = photos[i]
        p_right = photos[i + 1] if i + 1 < len(photos) else photos[i]
        hy_l, cx_l, fw_l, fy_l, isp_l, face_l_w, face_l_cx, shoulder_l_w, head_h_l = get_person_bounds_pct(
            p_left, is_pants, pose_cache.get(os.path.basename(p_left))
        )
        hy_r, cx_r, fw_r, fy_r, isp_r, face_r_w, face_r_cx, shoulder_r_w, head_h_r = get_person_bounds_pct(
            p_right, is_pants, pose_cache.get(os.path.basename(p_right))
        )

        zoom_l = face_l_w if face_l_w > 0 else (shoulder_l_w if shoulder_l_w > 0 else fw_l)
        zoom_r = face_r_w if face_r_w > 0 else (shoulder_r_w if shoulder_r_w > 0 else fw_r)
        should_swap = zoom_l > zoom_r

        if manual_crop and "LEFT" in manual_crop and "filename" in manual_crop["LEFT"]:
            should_swap = os.path.basename(p_left) != manual_crop["LEFT"]["filename"]

        try:
            inc_dir = os.path.dirname(photos[0])
            swap_file = os.path.join(inc_dir, "swap_state.json")
            swap_data = {}
            if os.path.exists(swap_file):
                with open(swap_file, "r", encoding="utf-8") as sf:
                    swap_data = json.load(sf)
            swap_data[os.path.basename(photos[i])] = should_swap
            with open(swap_file, "w", encoding="utf-8") as sf:
                json.dump(swap_data, sf)
        except Exception:
            pass

        if should_swap:
            p_left, p_right = p_right, p_left
            hy_l, hy_r = hy_r, hy_l
            cx_l, cx_r = cx_r, cx_l
            fy_l, fy_r = fy_r, fy_l
            fw_l, fw_r = fw_r, fw_l
            isp_l, isp_r = isp_r, isp_l
            face_l_w, face_r_w = face_r_w, face_l_w
            face_l_cx, face_r_cx = face_r_cx, face_l_cx
            shoulder_l_w, shoulder_r_w = shoulder_r_w, shoulder_l_w
            head_h_l, head_h_r = head_h_r, head_h_l

        if face_l_w == 0:
            face_l_cx = cx_l
        if face_r_w == 0:
            face_r_cx = cx_r

        rect_l = config["rects"]["LEFT"]
        rect_r = config["rects"]["RIGHT"]

        img_l = load_rgb_image(p_left)
        mc_l = apply_manual_crop(img_l, "LEFT")
        fit_l = mc_l if mc_l else fit_image_to_rect(img_l, rect_l, hy_l, face_l_cx, fy_l, "LEFT", isp_l, head_h_l, category=category)
        del img_l
        canvas.paste(fit_l, (int(rect_l[0]), int(rect_l[1])))
        del fit_l

        img_r = load_rgb_image(p_right)
        mc_r = apply_manual_crop(img_r, "RIGHT")
        fit_r = mc_r if mc_r else fit_image_to_rect(img_r, rect_r, hy_r, face_r_cx, fy_r, "RIGHT", isp_r, head_h_r, category=category)
        del img_r
        canvas.paste(fit_r, (int(rect_r[0]), int(rect_r[1])))
        del fit_r

        basename = os.path.basename(photos[i])
        color_name = basename.split("-")[0].upper() if "-" in basename else f"COLOR_{i // 2 + 1}"

    bot_y = int(config["rects"].get("LEFT", config["rects"].get("MAIN"))[3])
    canvas.paste(top_rgb, (0, 0))
    canvas.paste(bot_rgb, (0, bot_y))

    draw = ImageDraw.Draw(canvas)
    draw_pivok_bottom_bar(draw, config, code=code, sizes=sizes, price=price)

    save_path = os.path.join(out_folder_path, f"{color_name}.jpg")
    counter = 1
    while save_path in used_names or os.path.exists(save_path):
        counter += 1
        save_path = os.path.join(out_folder_path, f"{color_name}_{counter}.jpg")
    used_names.add(save_path)

    canvas.save(save_path, "JPEG", quality=89)
    del canvas
    gc.collect()


def generate_folder_collages(folder_name: str, folder_path: str) -> tuple[int, list[str]]:
    if not os.path.isdir(folder_path):
        return 0, []

    brand, category, code, sizes, price = parse_folder_metadata(folder_name)
    photos = sorted(
        os.path.join(folder_path, f)
        for f in os.listdir(folder_path)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    )
    if not photos:
        return 0, []

    pose_cache = {}
    pose_cache_path = os.path.join(folder_path, "pose_cache.json")
    if os.path.exists(pose_cache_path):
        try:
            with open(pose_cache_path, "r", encoding="utf-8") as pf:
                pose_cache = json.load(pf)
        except Exception:
            pass

    color_groups = {}
    for p in photos:
        basename = os.path.basename(p)
        prefix = basename.split("-")[0] if "-" in basename else "DEFAULT"
        color_groups.setdefault(prefix, []).append(p)

    is_single = (category.upper() in SINGLE_CATEGORIES) or all(
        len(p_list) == 1 for p_list in color_groups.values()
    )

    valid_photos = []
    for prefix, p_list in color_groups.items():
        if is_single:
            valid_photos.extend(p_list)
        elif prefix == "DEFAULT":
            valid_photos.extend(p_list[: (len(p_list) // 2) * 2])
        elif len(p_list) >= 2:
            valid_photos.extend(p_list[:2])

    if not valid_photos or (not is_single and len(valid_photos) < 2):
        return 0, []

    config, canvas_size, top_rgb, bot_rgb = load_template_assets(is_single)
    iterations = range(len(valid_photos)) if is_single else range(0, len(valid_photos), 2)
    out_folder_path = os.path.join(SAVE_DIR, folder_name)

    os.makedirs(out_folder_path, exist_ok=True)
    for _fn in os.listdir(out_folder_path):
        try:
            os.remove(os.path.join(out_folder_path, _fn))
        except Exception:
            pass

    used_names = set()
    count = 0
    for i in iterations:
        _render_one_collage(
            i,
            is_single,
            valid_photos,
            canvas_size,
            top_rgb,
            bot_rgb,
            config,
            code,
            sizes,
            price,
            out_folder_path,
            category,
            pose_cache,
            used_names,
        )
        count += 1

    return count, [folder_name]
