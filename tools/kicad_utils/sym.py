"""
kicad_sym.py
============

A small, dependency-free library for reading and writing KiCad symbol
library files (``.kicad_sym``) as editable Python objects.

Design goal (hard requirement)
-------------------------------
``dump(load(path)) == open(path).read()`` whenever nothing was edited.

How that's achieved
--------------------
KiCad's own S-expression pretty-printer has a lot of small, undocumented
formatting rules (tab indentation, which lists get collapsed onto one
line, how point lists are wrapped, float precision, etc.). Rather than
trying to reverse-engineer every rule, this parser keeps a pointer
(``start``/``end``) into the *original source text* for every node it
parses. When dumping:

  * A node that was never touched is re-emitted by slicing the original
    text verbatim -- byte for byte, including whatever whitespace KiCad
    used -- so unmodified files (or unmodified sub-trees) round-trip
    exactly.
  * A node that you created or edited no longer has a valid source span
    (editing it clears its own span *and* every ancestor's span, since
    the ancestors' cached text is now stale), so it gets freshly
    rendered using a formatter that mimics KiCad's observed style
    (tab indentation, one child per line for "compound" lists, a single
    line for "leaf" lists of atoms/strings). This is best-effort for
    new content, but it's exact for anything you didn't touch.

Because edits only invalidate the direct chain of ancestors, editing
one pin deep inside a symbol does not force the rest of the file to be
reformatted -- everything else is still sliced verbatim.

Quick start
-----------
    from kicad_sym import KicadSymbolLibrary

    lib = KicadSymbolLibrary.load("MXCN947VDFT.kicad_sym")
    sym = lib.get_symbol("MXCN947VDFT")

    pin = sym.get_pin(number="1")
    pin.add_alternate("PA0/ADC1_IN0", "input", "line")

    lib.save("MXCN947VDFT_edited.kicad_sym")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Optional, Union



__all__ = [
    "KicadSymbolLibrary"
]



# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------
class ParseError(Exception):
    pass


def _tokenize(text: str):
    """Yield (kind, value, start, end) tuples.

    kind is one of 'LP', 'RP', 'ATOM', 'STR'.
    """
    i, n = 0, len(text)
    tokens = []
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "(":
            tokens.append(("LP", "(", i, i + 1))
            i += 1
            continue
        if c == ")":
            tokens.append(("RP", ")", i, i + 1))
            i += 1
            continue
        if c == '"':
            start = i
            i += 1
            buf = []
            closed = False
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    if nxt == '"':
                        buf.append('"')
                        i += 2
                        continue
                    if nxt == "\\":
                        buf.append("\\")
                        i += 2
                        continue
                    if nxt == "n":
                        buf.append("\n")
                        i += 2
                        continue
                    if nxt == "t":
                        buf.append("\t")
                        i += 2
                        continue
                    # unknown escape - keep literally, don't consume meaning
                    buf.append(ch)
                    i += 1
                    continue
                if ch == '"':
                    i += 1
                    closed = True
                    break
                buf.append(ch)
                i += 1
            if not closed:
                raise ParseError(f"Unterminated string starting at offset {start}")
            tokens.append(("STR", "".join(buf), start, i))
            continue
        # bare atom: run until whitespace/paren/quote
        start = i
        while i < n and text[i] not in ' \t\r\n()"':
            i += 1
        if i == start:
            raise ParseError(f"Unexpected character {c!r} at offset {i}")
        tokens.append(("ATOM", text[start:i], start, i))
    return tokens


# ---------------------------------------------------------------------------
# AST node types
# ---------------------------------------------------------------------------

class Node:
    """Base class. ``start``/``end`` are byte offsets into the original
    source text this node was parsed from, or None if the node is new
    or has been edited (in which case it must be freshly rendered)."""

    __slots__ = ("parent", "start", "end")

    def __init__(self):
        self.parent: Optional["Sx"] = None
        self.start: Optional[int] = None
        self.end: Optional[int] = None

    def _invalidate(self):
        """Mark this node (and every ancestor) as no longer safe to
        slice verbatim from the original source, because its content
        changed."""
        node = self
        while node is not None:
            node.start = None
            node.end = None
            node = node.parent


class Atom(Node):
    """A bare, unquoted token: a symbol or number such as ``pin``,
    ``input``, ``1.27``, ``-3.81``, ``yes``."""

    __slots__ = ("_value",)

    def __init__(self, value: Union[str, int, float]):
        super().__init__()
        self._value = str(value)

    @property
    def value(self) -> str:
        return self._value

    @value.setter
    def value(self, new: Union[str, int, float]):
        self._value = str(new)
        self._invalidate()

    def as_float(self) -> float:
        return float(self._value)

    def as_int(self) -> int:
        return int(float(self._value))

    def __repr__(self):
        return f"Atom({self._value!r})"


class Str(Node):
    """A double-quoted string token."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        super().__init__()
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    @value.setter
    def value(self, new: str):
        self._value = new
        self._invalidate()

    def __repr__(self):
        return f"Str({self._value!r})"


class Sx(Node):
    """A parenthesized list: ``(tag child child ...)``."""

    __slots__ = ("items",)

    def __init__(self, items: Optional[Iterable[Node]] = None):
        super().__init__()
        self.items: List[Node] = []
        if items:
            for it in items:
                self.append(it)

    # -- structural mutation -------------------------------------------------
    def append(self, node: Node) -> Node:
        node.parent = self
        self.items.append(node)
        self._invalidate()
        return node

    def insert(self, index: int, node: Node) -> Node:
        node.parent = self
        self.items.insert(index, node)
        self._invalidate()
        return node

    def remove(self, node: Node) -> None:
        self.items.remove(node)
        node.parent = None
        self._invalidate()

    # -- lookup helpers -------------------------------------------------------
    @property
    def tag(self) -> Optional[str]:
        if self.items and isinstance(self.items[0], Atom):
            return self.items[0].value
        return None

    def find(self, tag: str) -> Optional["Sx"]:
        for it in self.items:
            if isinstance(it, Sx) and it.tag == tag:
                return it
        return None

    def find_all(self, tag: str) -> List["Sx"]:
        return [it for it in self.items if isinstance(it, Sx) and it.tag == tag]

    def walk(self, tag: str) -> Iterator["Sx"]:
        """Recursively yield every descendant Sx node with the given tag."""
        for it in self.items:
            if isinstance(it, Sx):
                if it.tag == tag:
                    yield it
                yield from it.walk(tag)

    def text_of(self, tag: str) -> Optional[str]:
        """For a child shaped like ``(tag "value" ...)`` or ``(tag value ...)``,
        return the value of its first argument (works for both Str and Atom)."""
        child = self.find(tag)
        if child is None or len(child.items) < 2:
            return None
        return child.items[1].value

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        return self.items[i]

    def __repr__(self):
        return f"Sx({self.tag!r}, {len(self.items)} items)"


def mklist(*items: Node) -> Sx:
    """Build a brand-new Sx node from already-constructed child nodes,
    e.g. ``mklist(Atom('at'), Atom(0), Atom(0), Atom(0))``."""
    return Sx(items)


def fmt_num(v: Union[int, float]) -> str:
    """Render a number the way KiCad typically does: integers with no
    decimal point, floats trimmed of trailing zeros."""
    if isinstance(v, int):
        return str(v)
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    if s in ("", "-", "-0"):
        s = "0"
    return s


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_sexpr(text: str) -> Sx:
    """Parse the *entire* text of a .kicad_sym file into a tree rooted at
    the single top-level list, e.g. ``(kicad_symbol_lib ...)``."""
    tokens = _tokenize(text)
    if not tokens:
        raise ParseError("empty document")
    if tokens[0][0] != "LP":
        raise ParseError("expected a top-level '(' expression")

    pos = 0

    def parse_node() -> Node:
        nonlocal pos
        kind, val, start, end = tokens[pos]
        if kind == "LP":
            node = Sx()
            node.start = start
            pos += 1
            while True:
                if pos >= len(tokens):
                    raise ParseError("unexpected end of file inside a list")
                if tokens[pos][0] == "RP":
                    node.end = tokens[pos][3]
                    pos += 1
                    break
                child = parse_node()
                child.parent = node
                node.items.append(child)
            return node
        elif kind == "STR":
            s = Str(val)
            s.start, s.end = start, end
            pos += 1
            return s
        elif kind == "ATOM":
            a = Atom(val)
            a.start, a.end = start, end
            pos += 1
            return a
        else:
            raise ParseError(f"unexpected token {kind!r} at offset {start}")

    root = parse_node()
    if pos != len(tokens):
        _, _, off, _ = tokens[pos]
        raise ParseError(f"trailing content after the top-level expression at offset {off}")
    return root


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _escape_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(node: Node, source: str, depth: int = 0) -> str:
    """Render a node to text. Unmodified nodes are sliced verbatim from
    ``source``; new/edited nodes are freshly formatted."""
    if node.start is not None and node.end is not None:
        return source[node.start:node.end]

    if isinstance(node, Atom):
        return node.value
    if isinstance(node, Str):
        return _escape_str(node.value)
    if isinstance(node, Sx):
        return _render_sx_fresh(node, source, depth)
    raise TypeError(f"unknown node type {type(node)!r}")


def _render_sx_fresh(node: Sx, source: str, depth: int) -> str:
    items = node.items
    first_sx_idx = next((i for i, it in enumerate(items) if isinstance(it, Sx)), None)

    if first_sx_idx is None:
        # "Leaf" list -- only atoms/strings -- stays on one line.
        return "(" + " ".join(render(it, source, depth) for it in items) + ")"

    head = items[:first_sx_idx]
    tail = items[first_sx_idx:]
    indent = "\t" * depth
    child_indent = "\t" * (depth + 1)

    opening = "(" + " ".join(render(it, source, depth) for it in head)
    lines = [opening]

    if node.tag == "pts" and all(isinstance(it, Sx) for it in tail):
        # KiCad keeps a point list's (xy ..) entries on one continuation line.
        joined = " ".join(render(it, source, depth + 1) for it in tail)
        lines.append(child_indent + joined)
    else:
        for it in tail:
            lines.append(child_indent + render(it, source, depth + 1))

    lines.append(indent + ")")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# High level: library / symbol / pin / property wrappers
# ---------------------------------------------------------------------------

class Alternate:
    """Wraps ``(alternate "NAME" electrical_type graphic_style)``."""

    def __init__(self, node: Sx):
        self.node = node

    @property
    def name(self) -> str:
        return self.node.items[1].value

    @name.setter
    def name(self, value: str):
        self.node.items[1].value = value

    @property
    def electrical_type(self) -> str:
        return self.node.items[2].value

    @electrical_type.setter
    def electrical_type(self, value: str):
        self.node.items[2].value = value

    @property
    def graphic_style(self) -> str:
        return self.node.items[3].value

    @graphic_style.setter
    def graphic_style(self, value: str):
        self.node.items[3].value = value

    def __str__(self):
        return f"Alternate({self.name!r}, {self.electrical_type!r}, {self.graphic_style!r})"

    def __repr__(self):
        return str(self)


class Pin:
    """Wraps ``(pin electrical_type graphic_style (at ..) (length ..)
    (name "..") (number "..") (alternate ..)*)``."""

    def __init__(self, node: Sx):
        self.node = node

    @property
    def electrical_type(self) -> str:
        return self.node.items[1].value

    @electrical_type.setter
    def electrical_type(self, value: str):
        self.node.items[1].value = value

    @property
    def graphic_style(self) -> str:
        return self.node.items[2].value

    @graphic_style.setter
    def graphic_style(self, value: str):
        self.node.items[2].value = value

    @property
    def name(self) -> Optional[str]:
        n = self.node.find("name")
        return n.items[1].value if n else None

    @name.setter
    def name(self, value: str):
        n = self.node.find("name")
        if n is None:
            raise ValueError("pin has no (name ...) node")
        n.items[1].value = value

    @property
    def number(self) -> Optional[str]:
        n = self.node.find("number")
        return n.items[1].value if n else None

    @number.setter
    def number(self, value: str):
        n = self.node.find("number")
        if n is None:
            raise ValueError("pin has no (number ...) node")
        n.items[1].value = value

    @property
    def at(self):
        """(x, y, rotation) as floats."""
        n = self.node.find("at")
        return tuple(float(it.value) for it in n.items[1:4])

    @at.setter
    def at(self, xy_rot):
        x, y, rot = xy_rot
        n = self.node.find("at")
        n.items[1].value = fmt_num(x)
        n.items[2].value = fmt_num(y)
        n.items[3].value = fmt_num(rot)

    @property
    def length(self) -> Optional[float]:
        n = self.node.find("length")
        return float(n.items[1].value) if n else None

    @length.setter
    def length(self, value: float):
        n = self.node.find("length")
        if n is None:
            raise ValueError("pin has no (length ...) node")
        n.items[1].value = fmt_num(value)

    @property
    def alternates(self) -> List[Alternate]:
        return [Alternate(n) for n in self.node.find_all("alternate")]

    def get_alternate(self, name: str) -> Optional[Alternate]:
        for alt in self.alternates:
            if alt.name == name:
                return alt
        return None

    def add_alternate(self, name: str, electrical_type: str, graphic_style: str) -> Alternate:
        """Add an alternate pin function (KiCad's "Alternate Pin Assignment"
        feature -- exactly what you need for MCU pins with multiple mux'd
        functions)."""
        existing = self.get_alternate(name)
        if existing is not None:
            existing.electrical_type = electrical_type
            existing.graphic_style = graphic_style
            return existing
        node = mklist(Atom("alternate"), Str(name), Atom(electrical_type), Atom(graphic_style))
        self.node.append(node)
        return Alternate(node)

    def remove_alternate(self, name: str) -> bool:
        alt = self.get_alternate(name)
        if alt is None:
            return False
        self.node.remove(alt.node)
        return True

    def __str__(self):
        return f"Pin(number={self.number!r}, name={self.name!r}, alternates={len(self.alternates)})"

    def __repr__(self):
        return str(self)


class Property:
    """Wraps ``(property "Key" "Value" (at ..) (effects ..))``."""

    def __init__(self, node: Sx):
        self.node = node

    @property
    def key(self) -> str:
        return self.node.items[1].value

    @property
    def value(self) -> str:
        return self.node.items[2].value

    @value.setter
    def value(self, new: str):
        self.node.items[2].value = new

    def __str__(self):
        return f"Property({self.key!r}={self.value!r})"

    def __repr__(self):
        return str(self)


def _default_effects(size: float = 1.27) -> Sx:
    return mklist(
        Atom("effects"),
        mklist(Atom("font"), mklist(Atom("size"), Atom(fmt_num(size)), Atom(fmt_num(size)))),
    )


class Symbol:
    """Wraps a top-level ``(symbol "NAME" ...)`` node, including any nested
    per-unit sub-symbols such as ``NAME_1_1``."""

    def __init__(self, node: Sx):
        self.node = node

    @property
    def name(self) -> str:
        return self.node.items[1].value

    def units(self) -> List["Symbol"]:
        """Nested per-unit/body-style sub-symbols (e.g. ``NAME_1_1``)."""
        return [Symbol(n) for n in self.node.find_all("symbol")]

    def pins(self) -> List[Pin]:
        return [Pin(n) for n in self.node.walk("pin")]

    def get_pin(self, number: Optional[str] = None, name: Optional[str] = None) -> Optional[Pin]:
        for pin in self.pins():
            if number is not None and pin.number == number:
                return pin
            if name is not None and pin.name == name:
                return pin
        return None

    def properties(self) -> List[Property]:
        return [Property(n) for n in self.node.find_all("property")]

    def get_property(self, key: str) -> Optional[Property]:
        for prop in self.properties():
            if prop.key == key:
                return prop
        return None

    def set_property(self, key: str, value: str, at=(0.0, 0.0, 0.0)) -> Property:
        prop = self.get_property(key)
        if prop is not None:
            prop.value = value
            return prop
        x, y, rot = at
        node = mklist(
            Atom("property"), Str(key), Str(value),
            mklist(Atom("at"), Atom(fmt_num(x)), Atom(fmt_num(y)), Atom(fmt_num(rot))),
            _default_effects(),
        )
        # Insert before the first nested unit sub-symbol, if any, else append.
        first_unit_idx = next(
            (i for i, it in enumerate(self.node.items)
             if isinstance(it, Sx) and it.tag == "symbol"),
            None,
        )
        if first_unit_idx is None:
            self.node.append(node)
        else:
            self.node.insert(first_unit_idx, node)
        return Property(node)

    def add_unit(self, unit: int = 1, body_style: int = 1) -> "Symbol":
        """Create a new nested per-unit sub-symbol, e.g. NAME_<unit>_<body_style>,
        the container KiCad expects pins/graphics to live in."""
        sub_name = f"{self.name}_{unit}_{body_style}"
        node = mklist(Atom("symbol"), Str(sub_name))
        self.node.append(node)
        return Symbol(node)

    def add_pin(
        self,
        unit_node: Optional["Symbol"],
        electrical_type: str,
        graphic_style: str,
        at,
        length: float,
        name: str,
        number: str,
        name_size: float = 1.27,
        number_size: float = 1.27,
    ) -> Pin:
        """Add a brand-new pin. Pass the Symbol returned by add_unit()/units()
        as ``unit_node`` for a multi-unit part, or None to add directly under
        this symbol (single body-style parts that skip the unit wrapper)."""
        x, y, rot = at
        pin_node = mklist(
            Atom("pin"), Atom(electrical_type), Atom(graphic_style),
            mklist(Atom("at"), Atom(fmt_num(x)), Atom(fmt_num(y)), Atom(fmt_num(rot))),
            mklist(Atom("length"), Atom(fmt_num(length))),
            mklist(Atom("name"), Str(name), _default_effects(name_size)),
            mklist(Atom("number"), Str(number), _default_effects(number_size)),
        )
        target = unit_node.node if unit_node is not None else self.node
        target.append(pin_node)
        return Pin(pin_node)

    def __str__(self):
        return f"Symbol({self.name!r}, {len(self.pins())} pins, {len(self.units())} units)"

    def __repr__(self):
        return str(self)




class KicadSymbolLibrary:
    """Wraps a whole ``.kicad_sym`` file: ``(kicad_symbol_lib ...)``."""

    def __init__(self, source: str, root: Sx):
        self.source = source
        self.root = root
        self.prefix = source[:root.start] if root.start is not None else ""
        self.suffix = source[root.end:] if root.end is not None else ""

    @classmethod
    def load(cls, path: str, encoding: str = "utf-8") -> "KicadSymbolLibrary":
        with open(path, "r", encoding=encoding, newline="") as f:
            text = f.read()
        return cls.loads(text)

    @classmethod
    def loads(cls, text: str) -> "KicadSymbolLibrary":
        root = parse_sexpr(text)
        return cls(text, root)

    @classmethod
    def new(cls, generator: str = "python", generator_version: str = "1.0",
            version: int = 20231120) -> "KicadSymbolLibrary":
        root = mklist(
            Atom("kicad_symbol_lib"),
            mklist(Atom("version"), Atom(version)),
            mklist(Atom("generator"), Str(generator)),
            mklist(Atom("generator_version"), Str(generator_version)),
        )
        return cls("", root)

    @property
    def symbols(self) -> List[Symbol]:
        return [Symbol(n) for n in self.root.find_all("symbol")]

    def get_symbol(self, name: str) -> Optional[Symbol]:
        for sym in self.symbols:
            if sym.name == name:
                return sym
        return None

    def new_symbol(self, name: str) -> Symbol:
        node = mklist(
            Atom("symbol"), Str(name),
            mklist(Atom("pin_names"), mklist(Atom("offset"), Atom(fmt_num(1.016)))),
            mklist(Atom("exclude_from_sim"), Atom("no")),
            mklist(Atom("in_bom"), Atom("yes")),
            mklist(Atom("on_board"), Atom("yes")),
        )
        self.root.append(node)
        return Symbol(node)

    def dumps(self) -> str:
        return self.prefix + render(self.root, self.source, 0) + self.suffix

    def dump(self, path: str, encoding: str = "utf-8") -> None:
        with open(path, "w", encoding=encoding, newline="") as f:
            f.write(self.dumps())

    def __str__(self):
        return f"KicadSymbolLibrary({len(self.symbols())} symbols)"

    def __repr__(self):
        return str(self)