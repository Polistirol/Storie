"""
Stippling pesato di un volto: da immagine a N punti riconoscibili.

Librerie necessarie:
    pip install numpy pillow scipy matplotlib
    pip install controlnet-aux torch       # solo per LINE_ART_MODE

Uso:
    python extract_points.py -i volto.jpg -o points.csv
Output:
    <file -o>       -> coordinate x,y dei punti (origine al centro)
    preview.png     -> anteprima dello stipple (nella cwd)
"""

import argparse
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter, ImageOps
from scipy import ndimage
from scipy.spatial import cKDTree
import matplotlib.pyplot as plt

# ----------------- OPZIONI (le uniche da toccare) -----------------
N_POINTS   = 2500   # numero di punti
ITERATIONS = 40     # iterazioni di Lloyd
EDGE_BOOST = 1.5    # peso extra ai contorni (solo se LINE_ART_MODE = False)
MAX_SIZE   = 800    # lato massimo immagine di lavoro

# modalità line art: i punti finiscono SOLO sulle linee del disegno
LINE_ART_MODE  = True
LINEART_RES    = 1024   # risoluzione di analisi: 512 = <1s, 1024 = ~5s, più dettagli
LINEART_COARSE = False  # True = solo tratti principali, False = anche dettagli fini

# sistema di riferimento di output: origine al centro
OUT_HALF   = 2000   # area da -OUT_HALF a +OUT_HALF in x e y
FLIP_Y     = True   # True: y cresce verso l'alto (cartesiano)
# -------------------------------------------------------------------


def extract_lineart(rgb_img, input_path):
    """Foto -> line art (linee chiare su sfondo nero) via modello AI leggero."""
    from controlnet_aux import LineartDetector
    det = LineartDetector.from_pretrained("lllyasviel/Annotators")
    la = det(rgb_img, coarse=LINEART_COARSE,
             detect_resolution=LINEART_RES, image_resolution=LINEART_RES)
    inp = Path(input_path)
    lineart_path = inp.parent / f"{inp.stem}_lineart.png"
    la.save(lineart_path)
    return la.convert("L").resize(rgb_img.size)


def density_map(path):
    """Mappa di densità. I pixel trasparenti (PNG con alpha) sono esclusi.
    LINE_ART_MODE: densità solo sulle linee del disegno AI.
    Altrimenti: zone scure + contorni."""
    rgba = Image.open(path).convert("RGBA")
    rgba.thumbnail((MAX_SIZE, MAX_SIZE))

    alpha = np.asarray(rgba.split()[-1], dtype=np.float64) / 255.0
    mask = alpha > 0.5                                  # True = volto

    # compositing su bianco, così il trasparente non diventa nero
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    rgb = Image.alpha_composite(bg, rgba).convert("RGB")

    if LINE_ART_MODE:
        la = extract_lineart(rgb, path)
        rho = np.asarray(la, dtype=np.float64) / 255.0  # linee chiare = dense
        rho = rho ** 1.5                                # scarta il rumore debole
    else:
        img = ImageOps.autocontrast(rgb.convert("L"))
        gray = np.asarray(img, dtype=np.float64) / 255.0
        dark = 1.0 - gray                               # scuro = denso
        edges = np.asarray(img.filter(ImageFilter.FIND_EDGES), dtype=np.float64) / 255.0
        edges = ndimage.gaussian_filter(edges, 1.0)
        edges /= edges.max() + 1e-9
        rho = (dark + EDGE_BOOST * edges) ** 1.5

    rho[~mask] = 0.0                                    # niente punti fuori dal volto
    return rho / rho.sum()


def initial_points(rho, n, rng):
    """Campiona n punti proporzionalmente alla densità."""
    h, w = rho.shape
    idx = rng.choice(h * w, size=n, replace=False, p=rho.ravel())
    ys, xs = np.unravel_index(idx, (h, w))
    return np.column_stack([xs, ys]).astype(np.float64)


def lloyd(points, rho, iterations):
    """Lloyd discreto: ogni pixel va al punto più vicino,
    poi ogni punto si sposta nel centroide pesato dei suoi pixel."""
    h, w = rho.shape
    ys, xs = np.mgrid[0:h, 0:w]
    pix = np.column_stack([xs.ravel(), ys.ravel()])
    wgt = rho.ravel()

    for it in range(iterations):
        # assegna ogni pixel al punto più vicino tramite KD-tree (veloce)
        tree = cKDTree(points)
        _, labels = tree.query(pix, workers=-1)

        # centroidi pesati
        wsum = np.bincount(labels, weights=wgt, minlength=len(points))
        cx = np.bincount(labels, weights=wgt * pix[:, 0], minlength=len(points))
        cy = np.bincount(labels, weights=wgt * pix[:, 1], minlength=len(points))
        ok = wsum > 0
        points[ok, 0] = cx[ok] / wsum[ok]
        points[ok, 1] = cy[ok] / wsum[ok]
        print(f"iterazione {it + 1}/{iterations}")
    return points


def to_output_coords(pts, shape):
    """Rimappa dalle coordinate pixel al sistema centrato in 0,
    da -OUT_HALF a +OUT_HALF, mantenendo le proporzioni."""
    h, w = shape
    scale = 2 * OUT_HALF / max(w, h)        # il lato lungo riempie l'area
    out = pts.copy()
    out[:, 0] = (pts[:, 0] - w / 2) * scale
    out[:, 1] = (pts[:, 1] - h / 2) * -scale
    if FLIP_Y:
        out[:, 1] *= -1                      # y verso l'alto
    return out


def parse_args():
    p = argparse.ArgumentParser(description="Stippling pesato: immagine -> punti CSV")
    p.add_argument("-i", "--input", required=True, help="Percorso immagine di input")
    p.add_argument("-o", "--output", required=True, help="Percorso file CSV di output")
    return p.parse_args()


def main():
    args = parse_args()

    rng = np.random.default_rng(42)
    rho = density_map(args.input)
    pts = initial_points(rho, N_POINTS, rng)
    pts = lloyd(pts, rho, ITERATIONS)
    pts = to_output_coords(pts, rho.shape)

    np.savetxt(args.output, pts, delimiter=",", header="x,y", comments="")

    plt.figure(figsize=(8, 8))
    plt.scatter(pts[:, 0], pts[:, 1], s=2, c="black")
    if not FLIP_Y:
        plt.gca().invert_yaxis()
    plt.xlim(-OUT_HALF, OUT_HALF); plt.ylim(-OUT_HALF, OUT_HALF)
    plt.axis("equal"); plt.axis("off")
    plt.savefig("preview.png", dpi=150, bbox_inches="tight")
    print(f"Salvati {args.output} e preview.png")


if __name__ == "__main__":
    main()