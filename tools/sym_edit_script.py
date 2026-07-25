#!/usr/bin/env python
from os.path import dirname as dn 
from kicad_utils import *

from math import isnan
from tabula import read_pdf as read_pdf_tables



ROOT_DIR = dn(dn(__file__))
SYM_DIR = f"{ROOT_DIR}/symbol"
UNPACK_DIR = f"{ROOT_DIR}/unpack"

SYM_FILE = f"{SYM_DIR}/Marijn.kicad_sym"
SYM_ED_FILE = f"{SYM_DIR}/Marijn_edit.kicad_sym"

PRINT = False

def MCXN947VDFT_edit(lib: KicadSymbolLibrary):
    NAN = float("nan") 
    item_fmt = lambda x: x if type(x) != float else (None if isnan(x) else x) 

    sym = lib.get_symbol("MCXN947VDFT")
    
    if PRINT:
        print(f"selected: {sym}")
        for pin in sym.pins():
            print(f"\t{pin}")
            for alt in pin.alternates:
                print(f"\t\t{alt}")
            print()
    
    tables = read_pdf_tables(f"{UNPACK_DIR}/MCXN94.pdf", pages=list(range(100, 133)))
 
    columns = list(tables[1].columns)   # 0     1       2       3           4          5           6         7
    if PRINT: print(columns)            # name, 184BGA, 172QFP, 100QFP N94, 100QFP N*, mux (alts), settings, analogue alts 
    rows = []

    for tab in tables[1:]:
        if tab.shape[1] != 8: continue
        for row in tab.itertuples():
            row = list(row)[1:]

            rows.append([
                item_fmt(row[0]), item_fmt(row[1]),
                item_fmt(row[5]), item_fmt(row[7])
            ])
    
    if PRINT: print(*rows, sep="\n")

    current_pin = None
    def update_current_pin(name, pad) -> None:
        nonlocal current_pin
        if not pad or not name:      return # skip
        current_pin = sym.get_pin(pad)
        if not current_pin:          return # error
        if len(current_pin.alternates):
            current_pin = None
            return                          # skip
        if current_pin.name == name: return # success
        current_pin = None                  # error

    def process_alt_string(alt, aliases: list = []) -> str or None:
        if not alt:         return None
        if "_" not in alt:  return None
        alt = alt[alt.find("-") + 1:].strip()

        for alias in aliases:
            alt = alt.replace(*alias)

        if "/" in alt:
            alt = [a.strip() for a in alt.split("/") if a]
        return alt


    aliases = [
        ("FLEXIO", "FIO"),
        ("SMARTDMA", "SDMA"),
        ("FLEXSPI", "FSPI"),
        ("INP", "IN"),
        ("DATA", "D")
    ]

    for name, pad, alt, analog in rows:
        update_current_pin(name, pad)
        if not current_pin: continue

        alt =       process_alt_string(alt, aliases)
        if analog and "ISP" in analog: analog = None
        analog =    process_alt_string(analog)

        if alt:
            if type(alt) != list: alt = [alt]
            for fn in alt:
                if fn == current_pin.name: continue
                current_pin.add_alternate(fn, "passive", "line")
        if analog:
            if type(analog) != list: analog = [analog]
            for fn in analog:
                current_pin.add_alternate(fn, "passive", "line")
        
            


    # pin = sym.get_pin(number="C3")
    # pin.add_alternate("TRACE_DATA2", "passive", "line")





if __name__ == "__main__":
    with open(SYM_FILE, "r", encoding="utf-8", newline="") as f:
        original = f.read()

    lib = KicadSymbolLibrary.loads(original)
    if lib.dumps() != original:
        exit(1)

    print(*lib.symbols, sep="\n", end="\n\n")

    #MCXN947VDFT_edit(lib)
    lib.dump(SYM_ED_FILE)