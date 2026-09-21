"""Regression checks for exact scan overrides and a non-destructive import."""
import ast
import gc
import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import import_clean_card_images as importer


class CleanImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temp.name)
        self.db = self.root / "test.sqlite"
        self.assets = self.root / "assets"
        self.assets.mkdir()
        tree = ast.parse(Path("app_optcg.py").read_text(encoding="utf-8-sig"))
        functions = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {
                "make_card_key", "image_columns", "resolve_image", "variant_number",
                "is_special_card", "build_image_map",
            }:
                node.decorator_list = []
                functions.append(node)
        self.ns = dict(pd=pd, Path=Path, hashlib=hashlib, json=json, re=re,
                       DB_PATH=str(self.db), ASSET_DIR=self.assets,
                       connect=lambda: sqlite3.connect(self.db))
        exec(compile(ast.Module(body=functions, type_ignores=[]), "app_optcg.py", "exec"), self.ns)
        self.rows = [dict(terminal_id=i, product_set="OP01", card_number="OP01-001",
                          name=f"Variant {i}", rarity="L", variant="ALT", drop_class="Alt-art")
                     for i in (1, 2)]
        self.ns["load_set_data"] = lambda _: (pd.DataFrame(self.rows), pd.DataFrame())
        with sqlite3.connect(self.db) as conn:
            conn.execute("""CREATE TABLE cards (card_uid TEXT, print_id TEXT, base_id TEXT,
                variant_suffix TEXT, variant_family TEXT, image_url TEXT)""")
            for index in (1, 2):
                conn.execute("INSERT INTO cards VALUES (?,?,?,?,?,?)",
                             (f"uid{index}", f"OP01-001_p{index}", "OP01-001", f"p{index}",
                              "parallel", f"https://example.org/{index}.png"))

    def add_override(self):
        path = self.assets / "scan.png"
        path.write_bytes(b"test fixture")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE card_image_overrides (card_key TEXT, local_image_path TEXT)")
            conn.execute("INSERT INTO card_image_overrides VALUES (?,?)",
                         (self.ns["make_card_key"](self.rows[0]), "assets/scan.png"))
        return path

    def test_original_mapping_without_import(self):
        self.assertEqual(self.ns["build_image_map"]("OP01"),
                         {1: "https://example.org/1.png", 2: "https://example.org/2.png"})

    def test_exact_override_preserves_other_variant_position(self):
        path = self.add_override()
        mapping = self.ns["build_image_map"]("OP01")
        self.assertEqual(mapping[1], str(path))
        self.assertEqual(mapping[2], "https://example.org/2.png")
        self.rows[0]["terminal_id"] = 100
        self.assertEqual(self.ns["build_image_map"]("OP01")[100], str(path))

    def test_missing_scan_uses_original_image(self):
        self.add_override().unlink()
        self.assertEqual(self.ns["build_image_map"]("OP01")[1], "https://example.org/1.png")

    def test_apply_preserves_user_data_and_makes_backup(self):
        output = self.assets / "clean_cards"
        output.mkdir()
        (output / "scan.png").write_bytes(b"test fixture")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE user_collection (card_key TEXT, quantity INTEGER)")
            conn.execute("INSERT INTO user_collection VALUES ('owned', 7)")
        plan = dict(card_updates=[dict(card_uid="uid1", local_path="assets/clean_cards/scan.png")],
                    terminal_updates=[dict(card_key="exact", local_path="assets/clean_cards/scan.png",
                                           source_url="https://cdn.poneglyph.one/scan.png", tcgplayer_id="123")])
        plan_path = output / "import_plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        original = {name: getattr(importer, name) for name in ("ROOT", "DB", "OUTPUT", "PLAN")}
        try:
            importer.ROOT, importer.DB, importer.OUTPUT, importer.PLAN = self.root, self.db, output, plan_path
            importer.apply()
        finally:
            for name, value in original.items():
                setattr(importer, name, value)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT * FROM user_collection").fetchall(), [("owned", 7)])
            self.assertEqual(conn.execute("SELECT count(*) FROM cards").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT clean_image_path FROM cards WHERE card_uid='uid1'").fetchone()[0],
                             "assets/clean_cards/scan.png")
        backup = next((self.root / "backups").glob("*.sqlite"))
        with sqlite3.connect(backup) as conn:
            self.assertNotIn("clean_image_path", {row[1] for row in conn.execute("PRAGMA table_info(cards)")})


if __name__ == "__main__":
    unittest.main()
