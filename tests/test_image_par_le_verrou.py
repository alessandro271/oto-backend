"""L'image (`Dockerfile`) s'installe PAR LE VERROU, comme la production et la CI.

Elle installait la lib connecteurs depuis sa branche `main`, hors verrou : une image
construite à la main embarquait une autre lib que celle qui sert (21/10/2026)."""
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
DOCKERFILE = (RACINE / "Dockerfile").read_text()
ACTION = (RACINE / ".github/actions/installer-par-le-verrou/action.yml").read_text()


def test_l_image_installe_par_le_verrou_et_rien_d_autre():
    assert "uv sync --frozen" in DOCKERFILE
    assert "pip install" not in DOCKERFILE, "une installation hors verrou"
    assert "@main" not in DOCKERFILE, "une branche flottante"


def test_l_image_lit_le_verrou_avec_l_uv_de_la_ci():
    (ci,) = re.findall(r'version: "([0-9.]+)"', ACTION)
    (image,) = re.findall(r"ghcr\.io/astral-sh/uv:([0-9.]+)", DOCKERFILE)
    assert image == ci, f"uv {image} dans l'image, {ci} en CI et au déploiement"
