#!/usr/bin/env python3
"""Estrae N punti xy (origine al centro immagine) da una silhouette."""
#(point_map) PS C:\Users\Pc-Gaming\Documents\Repositories\force-graph\tools> python extract_points.py --image adriano.png --mode canny --canny-enhance clahe  --canny-low 60 --canny-high 100
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw


def load_mask(img: Image.Image, threshold: int, invert: bool, use_alpha: bool) -> np.ndarray:
    rgba = img.convert("RGBA")
    arr = np.asarray(rgba)
    if use_alpha and arr[:, :, 3].max() < 255:
        mask = arr[:, :, 3] > threshold
    else:
        gray = np.dot(arr[:, :, :3], [0.299, 0.587, 0.114])
        mask = gray < threshold if not invert else gray > threshold
    return mask


def drop_border_pixels(
    points: list[tuple[int, int]], width: int, height: int, border: int
) -> list[tuple[int, int]]:
    if border <= 0:
        return points
    x0, y0 = border, border
    x1, y1 = width - border, height - border
    return [(x, y) for x, y in points if x0 <= x < x1 and y0 <= y < y1]


def edge_pixels(mask: np.ndarray, border: int = 1) -> list[tuple[int, int]]:
    h, w = mask.shape
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    inner = padded[1:-1, 1:-1]
    up = padded[:-2, 1:-1]
    down = padded[2:, 1:-1]
    left = padded[1:-1, :-2]
    right = padded[1:-1, 2:]
    edges = inner & (~up | ~down | ~left | ~right)
    ys, xs = np.where(edges)
    points = list(zip(xs.tolist(), ys.tolist()))
    return drop_border_pixels(points, w, h, border)


def rgba_to_gray(img: Image.Image, bg: str = "black") -> np.ndarray:
    rgba = np.asarray(img.convert("RGBA"))
    rgb = rgba[:, :, :3].astype(np.float32)
    alpha = rgba[:, :, 3:4] / 255.0
    backdrop = 0.0 if bg == "black" else 255.0
    gray = rgb * alpha + backdrop * (1.0 - alpha)
    return np.dot(gray, [0.299, 0.587, 0.114]).astype(np.uint8)


def alpha_mask(img: Image.Image, threshold: int) -> np.ndarray:
    return np.asarray(img.convert("RGBA"))[:, :, 3] > threshold


def enhance_gray(gray: np.ndarray, mode: str) -> np.ndarray:
    if mode == "equalize":
        return cv2.equalizeHist(gray)
    if mode == "clahe":
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return clahe.apply(gray)
    return gray


def canny_edges(
    img: Image.Image,
    low: int,
    high: int,
    alpha_threshold: int,
    use_alpha: bool,
    border: int,
    enhance: str,
    blur: int,
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    gray = enhance_gray(rgba_to_gray(img), enhance)
    if blur > 0:
        k = blur * 2 + 1
        gray = cv2.GaussianBlur(gray, (k, k), 0)
    edges = cv2.Canny(gray, low, high)
    if use_alpha:
        edges &= alpha_mask(img, alpha_threshold).astype(np.uint8) * 255
    ys, xs = np.where(edges > 0)
    points = drop_border_pixels(list(zip(xs.tolist(), ys.tolist())), edges.shape[1], edges.shape[0], border)
    return edges, points


def floyd_steinberg(gray: np.ndarray, threshold: int) -> np.ndarray:
    work = gray.astype(np.float64).copy()
    h, w = work.shape
    out = np.zeros((h, w), dtype=bool)
    for y in range(h):
        for x in range(w):
            old = work[y, x]
            new = 0.0 if old < threshold else 255.0
            out[y, x] = new == 0.0
            err = old - new
            if x + 1 < w:
                work[y, x + 1] += err * 7 / 16
            if y + 1 < h:
                if x > 0:
                    work[y + 1, x - 1] += err * 3 / 16
                work[y + 1, x] += err * 5 / 16
                if x + 1 < w:
                    work[y + 1, x + 1] += err * 1 / 16
    return out


def sample_even(points: list[tuple[int, int]], count: int) -> list[tuple[int, int]]:
    if len(points) <= count:
        return points
    idx = np.linspace(0, len(points) - 1, count, dtype=int)
    return [points[i] for i in idx]


def order_nearest(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(points) <= 1:
        return points
    remaining = points[1:]
    ordered = [points[0]]
    while remaining:
        lx, ly = ordered[-1]
        best_i = min(
            range(len(remaining)),
            key=lambda i: (remaining[i][0] - lx) ** 2 + (remaining[i][1] - ly) ** 2,
        )
        ordered.append(remaining.pop(best_i))
    return ordered


def order_by_angle(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(points) <= 1:
        return points
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    return sorted(points, key=lambda p: np.arctan2(p[1] - cy, p[0] - cx))


def sample_from_mask(mask: np.ndarray, count: int, seed: int) -> list[tuple[int, int]]:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return []
    rng = np.random.default_rng(seed)
    if len(xs) <= count:
        return list(zip(xs.tolist(), ys.tolist()))
    pick = rng.choice(len(xs), size=count, replace=False)
    return [(int(xs[i]), int(ys[i])) for i in pick]


def to_center_coords(
    points: list[tuple[int, int]], width: int, height: int, scale: float
) -> list[dict[str, float]]:
    cx = (width - 1) / 2.0
    cy = (height - 1) / 2.0
    out = []
    for x, y in points:
        out.append({"x": round((x - cx) * scale, 2), "y": round((cy - y) * scale, 2)})
    return out


def scale_to_radius(points: list[dict[str, float]], radius: float) -> list[dict[str, float]]:
    if not points:
        return points
    max_r = max(max(abs(p["x"]), abs(p["y"])) for p in points) or 1.0
    s = radius / max_r
    return [{"x": round(p["x"] * s, 2), "y": round(p["y"] * s, 2)} for p in points]


def extract(
    image_path: Path,
    count: int,
    mode: str,
    threshold: int,
    invert: bool,
    use_alpha: bool,
    dither_threshold: int,
    order: str,
    scale: float,
    fit_radius_to: float | None,
    seed: int,
    border: int,
    canny_low: int,
    canny_high: int,
    canny_enhance: str,
    canny_blur: int,
) -> tuple[dict, np.ndarray | None]:
    img = Image.open(image_path)
    w, h = img.size

    canny_map = None

    if mode == "outline":
        mask = load_mask(img, threshold, invert, use_alpha)
        raw = edge_pixels(mask, border)
        picked = sample_even(raw, count)
        if order == "nearest":
            picked = order_nearest(picked)
        elif order == "angle":
            picked = order_by_angle(picked)
    elif mode == "canny":
        canny_map, raw = canny_edges(
            img, canny_low, canny_high, threshold, use_alpha, border, canny_enhance, canny_blur
        )
        picked = sample_even(raw, count)
        if order == "nearest":
            picked = order_nearest(picked)
        elif order == "angle":
            picked = order_by_angle(picked)
    elif mode == "dither":
        gray = rgba_to_gray(img).astype(np.float64)
        mask = floyd_steinberg(gray, dither_threshold)
        if use_alpha:
            mask &= alpha_mask(img, threshold)
        if invert:
            mask = ~mask
        picked = sample_from_mask(mask, count, seed)
        picked = drop_border_pixels(picked, w, h, border)
        if order == "nearest":
            picked = order_nearest(picked)
        elif order == "angle":
            picked = order_by_angle(picked)
    elif mode == "fill":
        mask = load_mask(img, threshold, invert, use_alpha)
        picked = sample_from_mask(mask, count, seed)
        picked = drop_border_pixels(picked, w, h, border)
        if order == "nearest":
            picked = order_nearest(picked)
        elif order == "angle":
            picked = order_by_angle(picked)
    else:
        raise ValueError(f"mode sconosciuta: {mode}")

    points = to_center_coords(picked, w, h, scale)
    if fit_radius_to is not None:
        points = scale_to_radius(points, fit_radius_to)

    return {
        "version": 1,
        "source": image_path.name,
        "width": w,
        "height": h,
        "count": len(points),
        "mode": mode,
        "threshold": threshold,
        "dither_threshold": dither_threshold,
        "canny_low": canny_low,
        "canny_high": canny_high,
        "canny_enhance": canny_enhance,
        "canny_blur": canny_blur,
        "border": border,
        "order": order,
        "scale": scale,
        "fit_radius": fit_radius_to,
        "pixels": [{"x": x, "y": y} for x, y in picked],
        "points": points,
    }, canny_map


def data_pixels(data: dict) -> list[tuple[int, int]]:
    if "pixels" in data:
        return [(p["x"], p["y"]) for p in data["pixels"]]
    if data.get("fit_radius"):
        raise ValueError("JSON senza 'pixels' e con fit_radius: riesegui extract_points.py")
    w, h = data["width"], data["height"]
    scale = data.get("scale", 1.0)
    cx = (w - 1) / 2.0
    cy = (h - 1) / 2.0
    out = []
    for p in data["points"]:
        px = int(round(p["x"] / scale + cx))
        py = int(round(cy - p["y"] / scale))
        out.append((px, py))
    return out


def render_preview(
    image_path: Path,
    data: dict,
    output_path: Path,
    dot_radius: int = 2,
    draw_lines: bool = True,
) -> None:
    img = Image.open(image_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    pixels = data_pixels(data)

    if draw_lines and len(pixels) > 1:
        draw.line(pixels, fill=(255, 60, 60, 200), width=1)
    r = dot_radius
    for x, y in pixels:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 220, 0, 230))

    out = Image.alpha_composite(img, overlay).convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(output_path)


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Estrae punti xy da silhouette immagine.")
    parser.add_argument("--image", type=Path, default=here / "adriano.png")
    parser.add_argument("--output", type=Path, default=here / "adriano_points.json")
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument(
        "--mode",
        choices=["outline", "canny", "dither", "fill"],
        default="outline",
        help="outline=bordo silhouette, canny=Canny edges, dither=Floyd-Steinberg, fill=area piena",
    )
    parser.add_argument("--threshold", type=int, default=128, help="soglia luminanza/alpha")
    parser.add_argument("--dither-threshold", type=int, default=128)
    parser.add_argument("--canny-low", type=int, default=50, help="soglia bassa Canny (più bassa = più linee)")
    parser.add_argument("--canny-high", type=int, default=150, help="soglia alta Canny (~2-3x low)")
    parser.add_argument(
        "--canny-enhance",
        choices=["none", "equalize", "clahe"],
        default="none",
        help="contrasto prima di Canny (clahe utile su marmo/viso)",
    )
    parser.add_argument(
        "--canny-blur",
        type=int,
        default=0,
        help="blur gaussiano prima di Canny (0=off, 1-3 per ridurre rumore)",
    )
    parser.add_argument("--border", type=int, default=2, help="esclude pixel sul bordo immagine")
    parser.add_argument(
        "--canny-output",
        type=Path,
        default=None,
        help="salva mappa edge Canny (solo con --mode canny)",
    )
    parser.add_argument("--invert", action="store_true", help="inverti maschera luminanza")
    parser.add_argument("--no-alpha", action="store_true", help="ignora canale alpha")
    parser.add_argument(
        "--order",
        choices=["none", "nearest", "angle"],
        default="nearest",
        help="ordine punti per polilinea (outline usa nearest di default)",
    )
    parser.add_argument("--scale", type=float, default=1.0, help="moltiplicatore coordinate")
    parser.add_argument(
        "--fit-radius",
        type=float,
        default=None,
        help="scala tutti i punti per rientrare in questo raggio max",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--preview",
        type=Path,
        default=None,
        help="salva PNG con punti sovrapposti all'immagine",
    )
    parser.add_argument("--preview-only", type=Path, default=None, help="preview da JSON esistente")
    parser.add_argument("--no-lines", action="store_true", help="preview: solo punti, senza polilinea")
    parser.add_argument("--dot-radius", type=int, default=2)
    args = parser.parse_args()

    if args.preview_only:
        data = json.loads(args.preview_only.read_text(encoding="utf-8"))
        image = args.image
        if args.preview is None:
            args.preview = args.preview_only.with_suffix(".preview.png")
        render_preview(image, data, args.preview, args.dot_radius, not args.no_lines)
        print(f"preview salvata in {args.preview}")
        return

    if args.mode in ("outline", "canny") and args.order == "none":
        args.order = "nearest"

    result, canny_map = extract(
        image_path=args.image,
        count=args.count,
        mode=args.mode,
        threshold=args.threshold,
        invert=args.invert,
        use_alpha=not args.no_alpha,
        dither_threshold=args.dither_threshold,
        order=args.order,
        scale=args.scale,
        fit_radius_to=args.fit_radius,
        seed=args.seed,
        border=args.border,
        canny_low=args.canny_low,
        canny_high=args.canny_high,
        canny_enhance=args.canny_enhance,
        canny_blur=args.canny_blur,
    )

    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"scritti {result['count']} punti in {args.output}")

    if canny_map is not None:
        canny_path = args.canny_output or args.output.with_name(args.output.stem + "_canny.png")
        Image.fromarray(canny_map).save(canny_path)
        print(f"mappa Canny salvata in {canny_path}")

    if args.preview is None:
        args.preview = args.output.with_suffix(".preview.png")
    render_preview(args.image, result, args.preview, args.dot_radius, not args.no_lines)
    print(f"preview salvata in {args.preview}")


if __name__ == "__main__":
    main()
