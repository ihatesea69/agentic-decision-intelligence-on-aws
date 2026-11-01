"""The Glue Python shell job runs Python 3.9. Parse every module it imports with the 3.9 grammar and
reject runtime uses of 3.10+ features (match statements, PEP 604 unions outside annotations)."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = [ROOT / "glue" / "adi_glue" / "build_snapshot.py", ROOT / "engines" / "adi_engines" / "__init__.py",
         ROOT / "engines" / "adi_engines" / "dq.py", ROOT / "engines" / "adi_engines" / "states.py"]
bad = 0
for f in FILES:
    src = f.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(f), feature_version=(3, 9))
    except SyntaxError as e:
        print(f"FAIL {f.name}: {e}")
        bad += 1
        continue
    future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and
                 any(a.name == "annotations" for a in n.names) for n in tree.body)
    ann_nodes = set()
    for n in ast.walk(tree):
        for a in ("annotation", "returns"):
            if getattr(n, a, None) is not None:
                ann_nodes.update(id(x) for x in ast.walk(getattr(n, a)))
    unions = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
              and id(n) in ann_nodes and not future]
    print(("FAIL " if unions else "PASS ") + f"{f.name} (3.9 grammar, future annotations={future})"
          + (f" unions at {unions}" if unions else ""))
    bad += bool(unions)
sys.exit(1 if bad else 0)
