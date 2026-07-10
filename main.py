"""
Pipeline completo: descubrimiento → análisis → scoring.

Uso:
    export HELIUS_API_KEY="tu_clave"
    python main.py            # un ciclo completo
    python main.py --loop     # corre cada 6 horas indefinidamente
"""

import sys
import time

from discovery import run_discovery
from wallet_analyzer import run_analysis

CYCLE_HOURS = 6


def one_cycle():
    print("═" * 60)
    print("FASE 1 · Descubrimiento de tokens ganadores")
    print("═" * 60)
    run_discovery()

    print("\n" + "═" * 60)
    print("FASE 2 · Análisis de compradores tempranos y scoring")
    print("═" * 60)
    run_analysis()


if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            one_cycle()
            print(f"\n⏳ Próximo ciclo en {CYCLE_HOURS} horas…")
            time.sleep(CYCLE_HOURS * 3600)
    else:
        one_cycle()
