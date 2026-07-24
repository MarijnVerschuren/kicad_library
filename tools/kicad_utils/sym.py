from pathlib import Path
import re




__all__ = [
    "Library",
    "Symbol",
    "Pin",
    "dump"
]



# ============================================================
# S-expression parser
# ============================================================
TOKEN_RE = re.compile(
    r'''
    \s*
    (
        [()] |
        "(?:\\.|[^"])*" |
        [^\s()]+
    )
    ''',
    re.VERBOSE,
)



def tokenize(text):
    return [
        x.group(1)
        for x in TOKEN_RE.finditer(text)
    ]


def parse(tokens):
    stack = []
    current = []

    for token in tokens:
        if token == "(":
            stack.append(current)
            current = []

        elif token == ")":
            if not stack:
                raise SyntaxError("Unexpected ')'")
            completed = current
            current = stack.pop()
            current.append(completed)

        else:
            if token.startswith('"'):
                token = bytes(
                    token[1:-1],
                    "utf-8"
                ).decode("unicode_escape")
            current.append(token)

    if stack:
        raise SyntaxError("Missing ')'")
    if len(current) != 1:
        raise SyntaxError("Expected one root node")

    return current[0]



def dump(obj, depth=0):
    if isinstance(obj, Library): return dump(obj.node, depth)
    if isinstance(obj, Symbol):  return dump(obj.node, depth)
    if isinstance(obj, Pin):     return dump(obj.node, depth)

    if isinstance(obj, list):
        if not obj: return "()"

        # Simple lists stay on one line.
        if all(not isinstance(x, list) for x in obj):
            return "(" + " ".join(dump(x, depth) for x in obj) + ")"

        indent = "  " * (depth + 1)
        lines = ["(" + dump(obj[0], depth)]

        for child in obj[1:]:
            lines.append(indent + dump(child, depth + 1))

        lines.append("  " * depth + ")")
        return "\n".join(lines)

    if isinstance(obj, str):
        if (
            obj == ""
            or any(c in obj for c in ' ()"')
            or "\\" in obj
        ):
            obj = (
                obj.replace("\\", "\\\\").replace('"', '\\"')
            )
            return f'"{obj}"'
        return obj
    return str(obj)




# ============================================================
# Helpers
# ============================================================
def children(node, name):
    for x in node:
        if isinstance(x, list):
            if len(x) and x[0] == name:
                yield x



def child(node,name):
    return next(children(node, name), None)



# ============================================================
# KiCad objects
# ============================================================
class Pin:
    def __init__(self, node):
        self.node = node


    @property
    def name(self):
        n = child(self.node,"name")
        return n[1] if n else None


    @property
    def number(self):
        n = child(self.node,"number")
        return n[1] if n else None


    def alternates(self):
        for alt in children(self.node,"alternate"):
            yield {
                "name": alt[1],
                "type": alt[2]
            }


    def add_alternate(
        self,
        name,
        electrical_type="passive"
    ):
        self.node.append(
            [
                "alternate",
                name,
                electrical_type,
                "line"
            ]
        )


    def rename(self,name):
        n=child(self.node,"name")
        if n: n[1]=name




class Symbol:
    def __init__(self,node):
        self.node = node


    @property
    def name(self):
        return self.node[1]


    def sub_symbols(self):
        for s in children(
            self.node,
            "symbol"
        ):
            yield Symbol(s)


    def pins(self):
        for p in children(
            self.node,
            "pin"
        ):
            yield Pin(p)


    def pin(self,number):
        for p in self.pins():
            if p.number == str(number):
                return p
        raise KeyError(number)


    def add_property(
        self,
        name,
        value
    ):
        self.node.append(
            [
                "property",
                name,
                value
            ]
        )




class Library:
    def __init__(self, node):
        self.node = node


    @classmethod
    def load(cls, file):
        text = Path(file).read_text()
        return cls(
            parse(
                tokenize(text)
            )
        )


    def save(self,file):
        Path(file).write_text(
            dump(self.node)
        )


    def symbols(self):
        for s in children(
            self.node,
            "symbol"
        ):
            yield Symbol(s)


    def symbol(self, name):
        for s in self.symbols():
            if s.name == name:
                return s
        raise KeyError(name)
