"""
Installs the Python dependencies required for the conidia drawing extractor.

Usage:
    python instalar_dependencias.py
"""

import subprocess
import sys


PACKAGES = [
    "PyMuPDF",
    "opencv-python",
    "scikit-image",
    "pytesseract",
    "Pillow",
    "numpy",
    "tqdm",
    "click",
]


def main():
    print("Instalando dependencias para o extrator de conidios...\n")

    failed = []
    for pkg in PACKAGES:
        print(f"  -> {pkg} ... ", end="", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("OK")
        else:
            print("FALHOU")
            failed.append(pkg)

    print()
    if failed:
        print(f"Pacotes que falharam: {', '.join(failed)}")
        print("Tente instalar manualmente:")
        print(f"  pip install {' '.join(failed)}")
        return 1

    print("Todas as dependencias instaladas com sucesso!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
