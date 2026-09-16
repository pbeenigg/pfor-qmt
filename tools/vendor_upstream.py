"""Reproducible extraction from the pinned commit, never from its worktree."""
import hashlib
import ast
import json
import re
import subprocess
from pathlib import Path

COMMIT = "5baa4daf8dab01fb415afdd45a72cd254cea042f"
ROOT = Path(__file__).resolve().parents[1]
FILES = ["protocol.py", "pipe_transport.py", "pipe_client.py", "pipe_hub.py", "logging_i18n.py", "qmt_strategy_package.py"]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    manifest = []
    for source, target in [("LICENSE", "LICENSE")] + [("cfquant/" + name, "pfor_qmt/" + name) for name in FILES]:
        raw = subprocess.check_output(["git", "-C", str(args.source), "show", COMMIT + ":" + source])
        text = raw.decode("utf-8")
        changes = {}
        for old in sorted(set(re.findall(r"\bCFQUANT_[A-Z_]+\b", text))):
            changes[old] = "PFOR_QMT_" + old[len("CFQUANT_"):]
        changes.update({"cfquant_pipe_hub": "pfor_qmt_pipe_hub", "cfquant.normal.request": "pfor_qmt.market.request"})
        if source.endswith("qmt_strategy_package.py"):
            changes.update({"CFQ_[A-Z0-9_]{1,59}": "PFOR_[A-Z0-9_]{1,58}", "cfquant account bridge": "pfor-qmt market bridge"})
        for old, new in changes.items():
            text = text.replace(old, new)
        (ROOT / target).write_text(text, encoding="utf-8", newline="\n")
        manifest.append({"source": source, "target": target, "sha256": hashlib.sha256(raw).hexdigest(), "replacements": changes})
    source = "cfquant/tx_trade_bridge.py"
    raw = subprocess.check_output(["git", "-C", str(args.source), "show", COMMIT + ":" + source])
    text = raw.decode("utf-8")
    methods = {"_get_market_data_ex", "_get_local_data", "_call_local_data", "_get_instrument_detail", "_get_stock_list_in_sector", "_get_sector_list", "_first_param", "_list_param", "_call_variants", "_get_callable", "_require_qmt_callable"}
    cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == "TxTradeBridge")
    lines = text.splitlines(keepends=True)
    body = "\n".join("".join(lines[n.lineno-1:n.end_lineno]) for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods)
    target = "pfor_qmt/qmt_methods.py"
    (ROOT / target).write_text('"""Selected market-only methods from pinned cfquant; see UPSTREAM.md."""\nimport inspect\n\nL2_PERIODS = frozenset()\n\n\nclass QmtMethods(object):\n' + body + "\n", encoding="utf-8", newline="\n")
    manifest.append({"source": source, "target": target, "sha256": hashlib.sha256(raw).hexdigest(), "methods": sorted(methods)})
    source = "cfquant/level2.py"
    raw = subprocess.check_output(["git", "-C", str(args.source), "show", COMMIT + ":" + source])
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    names = {"quote_plain", "quote_records", "quote_callback_data"}
    body = "\n\n".join("".join(lines[n.lineno-1:n.end_lineno]) for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name in names)
    target = "pfor_qmt/quote.py"
    (ROOT / target).write_text(body + "\n", encoding="utf-8", newline="\n")
    manifest.append({"source": source, "target": target, "sha256": hashlib.sha256(raw).hexdigest(), "methods": sorted(names)})
    (ROOT / "docs/upstream-manifest.json").write_text(json.dumps({"commit": COMMIT, "files": manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
