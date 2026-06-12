"""
Stippling pesato di un volto: da immagine a N punti riconoscibili.

Librerie necessarie:
    pip install numpy pillow scipy matplotlib

Uso:
    python stippling.py volto.jpg
Output:
    points.csv      -> coordinate x,y dei punti (origine in alto a sinistra)
    preview.png     -> anteprima dello stipple
"""

import sys
import numpy as np
from PIL import Image, ImageFilter, ImageOps
from scipy import ndimage
from scipy.spatial import cKDTree
import matplotlib.pyplot as plt

# ----------------- OPZIONI (le uniche da toccare) -----------------
N_POINTS   = 2500   # numero di punti
ITERATIONS = 40     # iterazioni di Lloyd
EDGE_BOOST = 1.5    # peso extra ai contorni (0 = solo luminosità)
MAX_SIZE   = 800    # lato massimo immagine di lavoro

# preprocessing immagine
REMOVE_WHITE = True # escludi pixel quasi bianchi (sfondo)
WHITE_MIN    = 250  # soglia 0-255: pixel >= WHITE_MIN vengono ignorati

# sistema di riferimento di output: origine al centro
OUT_HALF   = 2000   # area da -OUT_HALF a +OUT_HALF in x e y
FLIP_Y     = True   # True: y cresce verso l'alto (cartesiano)
# -------------------------------------------------------------------


def density_map(path):
    """Mappa di densità: zone scure + contorni -> più punti.
    I pixel trasparenti (PNG con alpha) e quelli quasi bianchi (se REMOVE_WHITE)
    vengono esclusi del tutto."""
    rgba = Image.open(path).convert("RGBA")
    rgba.thumbnail((MAX_SIZE, MAX_SIZE))

    alpha = np.asarray(rgba.split()[-1], dtype=np.float64) / 255.0

    # compositing su bianco, così il trasparente non diventa nero
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    img = Image.alpha_composite(bg, rgba).convert("L")

    gray_u8 = np.asarray(img, dtype=np.uint8)
    mask = alpha > 0.5                                  # True = volto
    if REMOVE_WHITE:
        mask &= gray_u8 < WHITE_MIN                     # escludi sfondo bianco

    img = ImageOps.autocontrast(img)

    gray = np.asarray(img, dtype=np.float64) / 255.0
    dark = 1.0 - gray                                   # scuro = denso

    edges = np.asarray(img.filter(ImageFilter.FIND_EDGES), dtype=np.float64) / 255.0
    edges = ndimage.gaussian_filter(edges, 1.0)         # ammorbidisce i bordi
    edges /= edges.max() + 1e-9

    rho = dark + EDGE_BOOST * edges
    rho = rho ** 1.5                                    # accentua il contrasto
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
    out[:, 1] = (pts[:, 1] - h / 2) * scale
    if FLIP_Y:
        out[:, 1] *= -1                      # y verso l'alto (cartesiano)
    out *= -1                                # inverti x e y per l'output
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python stippling.py volto.jpg")

    rng = np.random.default_rng(42)
    rho = density_map(sys.argv[1])
    pts = initial_points(rho, N_POINTS, rng)
    pts = lloyd(pts, rho, ITERATIONS)
    pts = to_output_coords(pts, rho.shape)

    np.savetxt("points.csv", pts, delimiter=",", header="x,y", comments="")

    plt.figure(figsize=(8, 8))
    plt.scatter(pts[:, 0], pts[:, 1], s=2, c="black")
    if not FLIP_Y:
        plt.gca().invert_yaxis()
    plt.xlim(-OUT_HALF, OUT_HALF); plt.ylim(-OUT_HALF, OUT_HALF)
    plt.axis("equal"); plt.axis("off")
    plt.savefig("preview.png", dpi=150, bbox_inches="tight")
    print("Salvati points.csv e preview.png")


if __name__ == "__main__":
    main()