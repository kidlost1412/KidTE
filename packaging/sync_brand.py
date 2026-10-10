"""Export the approved master logo to Windows and Chrome icon formats."""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def sync():
    with Image.open(ROOT / "assets/kid-te-master.png") as source:
        master = source.convert("RGBA")
        master.resize((256,256),Image.Resampling.LANCZOS).save(ROOT / "assets/kid-te-icon.png")
        master.resize((256,256),Image.Resampling.LANCZOS).save(ROOT / "assets/kid-te.ico",sizes=[(n,n) for n in (16,24,32,48,64,128,256)])
        master.resize((256,256),Image.Resampling.LANCZOS).save(ROOT / "backend/src/web/kid-te-icon.png")
        for folder in ("extension","extension-shopee"):
            target = ROOT / folder / "icons"
            target.mkdir(exist_ok=True)
            for size in (16,32,48,128):
                master.resize((size,size),Image.Resampling.LANCZOS).save(target / f"kid-te-{size}.png")


if __name__ == "__main__":
    sync()
