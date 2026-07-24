#!/usr/bin/env python
from os.path import dirname as dn 

from kicad_utils import *



def print_pin(pin: Pin) -> None:
    name = pin.name
    pad = pin.number

    if name.startswith("P"):
        port, num = [int(i) for i in name.replace("P", "").split("_")]
        name = f"{chr(port + 65)}{num}"

    print(f"{pad}\t{name}")
    for alt in pin.alternates():
        print(alt)
    print()




ROOT_DIR = dn(dn(__file__))
SYM_FILE = f"{ROOT_DIR}/symbol/Marijn.kicad_sym"
if __name__ == "__main__":
    lib = Library.load(SYM_FILE)

    s = lib.symbol(
        "MCXN947VDFT"
    )
    
    for ss in s.sub_symbols():
        for pin in ss.pins():
            print_pin(pin)
    
    print(dump(lib))
